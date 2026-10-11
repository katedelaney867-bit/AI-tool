# -*- coding: utf-8 -*-
"""Remail（https://remail.aishop6.com）邮箱来源。

Remail 的开放 API 与「随机生成邮箱」类服务不同：

1. 用 API Key 按项目下一个 ``purchase``（长效，可重复收件）或 ``code``（短效接码）订单；
2. 订单返回交付邮箱和**只属于该订单**的 service token；
3. 取码走 ``/v1/pickup``，不再携带 API Key，只带邮箱 + service token。

所以 service token 必须随 Inbox 一路带到取码环节。

提炼自 turb-gpt-free-register/core/remail_client.py，去掉了邮箱池、SQLite 落库、
以及跨账号恢复取件上下文的逻辑 —— 新项目一个进程跑一批任务，凭证留在内存。
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .base import Inbox, MailboxError, MailboxProvider
from .otp import extract_otp

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://remail.aishop6.com"
DEFAULT_TIMEOUT = 20
_CODE_RE = re.compile(r"\b(\d{6})\b")
_FINAL_ORDER_STATUSES = {"failed", "refunded", "closed"}

# 项目详情缓存：上游项目结构变动不频繁，60 秒内重复点「加载邮箱目标」直接复用。
_DETAIL_CACHE: dict[int, tuple[float, dict]] = {}
_DETAIL_TTL = 60


def _split_filter(text: str) -> list[str]:
    """把 ``"chatgpt, OpenAI"`` 拆成 ``["chatgpt", "openai"]``。"""
    raw = str(text or "")
    return [part.strip().lower().lstrip("@") for part in raw.split(",") if part.strip()]


def _pretty_price(value) -> str:
    """``"15.000000"`` → ``"15"``，``"0.500000"`` → ``"0.5"``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if number == int(number):
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


class RemailProvider(MailboxProvider):
    name = "remail"
    label = "Remail 邮箱"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_API_BASE,
        service_mode: str = "purchase",
        supply_policy: str = "public_only",
        request_timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise MailboxError("Remail API Key 未填写")
        self.base_url = _normalize_base(base_url)
        mode = str(service_mode or "purchase").strip().lower()
        if mode not in {"purchase", "code"}:
            raise MailboxError("Remail 服务模式无效，只支持 purchase 或 code")
        policy = str(supply_policy or "public_only").strip().lower()
        if policy not in {"private_first", "public_only"}:
            raise MailboxError("Remail 库存策略无效，只支持 private_first 或 public_only")
        self.service_mode = mode
        self.supply_policy = policy
        self.request_timeout = max(1, min(120, int(request_timeout)))

    # ------------------------------------------------------------------ HTTP

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        authenticated: bool = True,
    ):
        request_headers = {"Accept": "application/json"}
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self.api_key}"
        if headers:
            request_headers.update(headers)

        url = self.base_url + (path if path.startswith("/") else f"/{path}")
        try:
            response = requests.request(
                method.upper(), url,
                params=params, json=json_body, headers=request_headers,
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise MailboxError(f"Remail 请求失败 ({method.upper()} {path}): {type(exc).__name__}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code >= 400:
                raise MailboxError(
                    f"Remail 请求失败 ({method.upper()} {path}): HTTP {response.status_code}; "
                    f"{str(getattr(response, 'text', '') or '')[:240]}"
                ) from exc
            raise MailboxError(f"Remail 响应不是 JSON ({method.upper()} {path})") from exc

        if response.status_code >= 400:
            if response.status_code == 401 and authenticated:
                raise MailboxError("Remail API Key 无效或已失效")
            raise MailboxError(
                f"Remail 请求失败 ({method.upper()} {path}): HTTP {response.status_code}; "
                f"{_error_message(payload, response)}"
            )
        return payload

    # -------------------------------------------------------------- 账号状态

    def status(self) -> dict:
        payload = self._request("GET", "/v1/open/apikey/profile")
        if not isinstance(payload, dict):
            raise MailboxError("Remail API Key 资料响应不是对象")
        profile = payload.get("apiKey") if isinstance(payload.get("apiKey"), dict) else payload.get("data")
        if isinstance(profile, dict) and isinstance(profile.get("apiKey"), dict):
            profile = profile["apiKey"]
        if not isinstance(profile, dict):
            raise MailboxError("Remail API Key 资料响应缺少 apiKey")
        allowed = (
            "id", "name", "keyPrefix", "enabled", "concurrencyLimit", "quotaLimit", "quotaUsed",
            "remainingQuota", "balance", "requestCount", "activeRequests", "expireAt",
        )
        # 白名单过滤：上游以后新增敏感字段也不会被透传到 GUI。
        return {key: value for key, value in profile.items() if key in allowed}

    # ---------------------------------------------------------------- 目标

    def targets(self, *, project_filter: str = "", suffix_filter: str = "") -> list[dict]:
        """把「项目 × 后缀」拍平成 GUI 的一个下拉列表。

        ``target_id`` 形如 ``"42:outlook.com"``。

        性能上的关键点：Remail 有 60+ 个项目，每个项目的后缀都要单独请求一次
        详情接口。全量拉一遍要 40 秒以上，所以这里做两件事：

        1. ``project_filter`` 命中的项目才去拉详情（默认只拉 chatgpt，1 次请求）；
        2. 没给过滤条件时用线程池并发拉，而不是串行。
        """
        wanted_projects = _split_filter(project_filter)
        wanted_suffixes = _split_filter(suffix_filter)

        projects = self._list_projects()
        if wanted_projects:
            projects = [
                item for item in projects
                if any(key in str(item.get("name") or "").lower() for key in wanted_projects)
            ]
        if not projects:
            return []

        out: list[dict] = []
        for project, detail in self._project_details(projects):
            project_id = int(project.get("id") or 0)
            listed = str(project.get("status") or "").lower() == "listed"
            name = str(project.get("name") or f"项目 {project_id}")
            for product in detail.get("products") or []:
                if not product.get("purchaseEnabled"):
                    continue
                if str(product.get("status") or "enabled").lower() != "enabled":
                    continue
                price = _pretty_price(product.get("purchasePrice"))
                suffixes = [item for item in (product.get("suffixes") or []) if isinstance(item, dict)]
                if suffixes:
                    rows = []
                    for row in suffixes:
                        suffix = str(
                            row.get("suffix") or row.get("emailSuffix") or row.get("domain") or ""
                        ).strip().lower().lstrip("@")
                        if not suffix:
                            continue
                        if wanted_suffixes and suffix not in wanted_suffixes:
                            continue
                        available = _int(row.get("purchasePublicAvailable") or row.get("purchaseAvailable")
                                         or row.get("publicAvailable") or row.get("totalAvailable"))
                        rows.append({
                            "id": f"{project_id}:{suffix}",
                            "project_id": project_id,
                            "project_name": name,
                            "suffix": suffix,
                            "product_type": str(product.get("type") or ""),
                            "available": available,
                            "price": price,
                            "listed": listed,
                            "label": f"{suffix}  ·  库存 {available}  ·  {_points_label(price)}",
                        })
                    out.extend(rows)
                elif not wanted_suffixes:
                    # 部分产品只给总库存，不展开后缀。
                    available = _int(product.get("purchasePublicAvailable") or product.get("purchaseAvailable")
                                     or product.get("publicAvailable") or product.get("totalAvailable"))
                    out.append({
                        "id": f"{project_id}:",
                        "project_id": project_id,
                        "project_name": name,
                        "suffix": "",
                        "product_type": str(product.get("type") or ""),
                        "available": available,
                        "price": price,
                        "listed": listed,
                        "label": f"{name}  ·  库存 {available}  ·  {_points_label(price)}",
                    })

        # 有货的排前面；项目只有一个时再按后缀排序，读起来更顺。
        out.sort(key=lambda item: (-int(item["available"] or 0), item["id"]))

        # 跨多个项目时把项目名带上，否则一堆 outlook.com 分不清是哪个项目的。
        if len({item["project_id"] for item in out}) > 1:
            for item in out:
                item["label"] = f"{item['project_name']} · {item['label']}"
        return out

    def _project_details(self, projects: list[dict]) -> list[tuple[dict, dict]]:
        """并发拉取项目详情。带 60 秒缓存，重复点「加载」不会重复打上游。"""
        now = time.time()
        results: list[tuple[dict, dict]] = []
        pending: list[dict] = []

        for project in projects:
            project_id = int(project.get("id") or 0)
            if project_id <= 0:
                continue
            cached = _DETAIL_CACHE.get(project_id)
            if cached and now - cached[0] < _DETAIL_TTL:
                results.append((project, cached[1]))
            else:
                pending.append(project)

        if not pending:
            return results

        # 只有 1 个也不用开线程池，省掉调度开销。
        if len(pending) == 1:
            project = pending[0]
            try:
                detail = self.project_detail(int(project["id"]))
            except MailboxError as exc:
                logger.warning("[Remail] 读取项目 %s 详情失败: %s", project.get("id"), exc)
                return results
            _DETAIL_CACHE[int(project["id"])] = (time.time(), detail)
            results.append((project, detail))
            return results

        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
            futures = {
                pool.submit(self.project_detail, int(item["id"])): item for item in pending
            }
            for future, project in futures.items():
                try:
                    detail = future.result()
                except Exception as exc:
                    logger.warning("[Remail] 读取项目 %s 详情失败: %s", project.get("id"), exc)
                    continue
                _DETAIL_CACHE[int(project["id"])] = (time.time(), detail)
                results.append((project, detail))
        return results

    def _list_projects(self, *, search: str | None = None) -> list[dict]:
        params: dict = {"offset": 0, "limit": 100}
        if search:
            params["search"] = str(search).strip()
        payload = self._request("GET", "/v1/open/projects", params=params)
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            items = data.get("items") if isinstance(data, dict) else None
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def project_detail(self, project_id: int) -> dict:
        pid = int(project_id)
        if pid <= 0:
            raise MailboxError("Remail 项目 ID 无效")
        payload = self._request("GET", f"/v1/open/projects/{pid}")
        if not isinstance(payload, dict):
            raise MailboxError("Remail 项目详情响应不是对象")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data.get("project"), dict):
            raise MailboxError("Remail 项目详情响应缺少 project")
        return {
            "project": dict(data["project"]),
            "products": [dict(item) for item in (data.get("products") or []) if isinstance(item, dict)],
        }

    # ---------------------------------------------------------------- 下单

    def allocate(self, *, target_id: str, idempotency_key: str) -> Inbox:
        project_text, _, suffix = str(target_id or "").partition(":")
        try:
            project_id = int(project_text)
        except (TypeError, ValueError) as exc:
            raise MailboxError("邮箱目标无效，请重新选择") from exc
        if project_id <= 0:
            raise MailboxError("邮箱目标无效，请重新选择")
        suffix = suffix.strip().lower().lstrip("@")
        idem = str(idempotency_key or "").strip()
        if not idem or len(idem) > 128:
            raise MailboxError("内部幂等键无效")

        body: dict = {"projectId": project_id}
        if suffix:
            body["emailSuffix"] = suffix
        order = _unwrap_order(self._request(
            "POST", "/v1/open/orders",
            params={"serviceMode": self.service_mode, "supply": self.supply_policy},
            json_body=body,
            headers={"Idempotency-Key": idem},
        ))

        email, token, order_no = self._wait_for_credentials(order)
        if not email or not token:
            raise MailboxError("Remail 订单未返回可用取件凭证")
        logger.info("[Remail] 下单成功 order=%s email=%s", order_no or "?", email)
        return Inbox(
            email=email, service_token=token, order_no=order_no,
            project_id=project_id, suffix=suffix, provider=self.name,
            extra={"status": order.get("status")},
        )

    def _wait_for_credentials(self, order: dict, *, timeout: int = 30) -> tuple[str, str, str]:
        """下单后 deliveryEmail/serviceToken 可能延迟返回，轮询补齐。"""
        credentials = _order_credentials(order)
        if credentials:
            return credentials
        order_no = str(_first_value(order, "orderNo", "order_no") or "").strip()
        if not order_no:
            return "", "", ""
        deadline = time.monotonic() + max(0, timeout)
        latest = order
        while time.monotonic() <= deadline:
            status = str(_first_value(latest, "status", "remoteStatus") or "").lower()
            if status in _FINAL_ORDER_STATUSES:
                raise MailboxError(
                    f"Remail 订单未能交付邮箱（状态 {status}）："
                    f"{_first_value(latest, 'failureCode', 'failureReason') or '无详情'}"
                )
            time.sleep(2)
            try:
                latest = _unwrap_order(self._request("GET", f"/v1/open/orders/{quote(order_no, safe='')}"))
            except MailboxError:
                continue
            credentials = _order_credentials(latest)
            if credentials:
                return credentials
        return "", "", order_no

    # ---------------------------------------------------------------- 取码

    def wait_for_otp(self, inbox: Inbox, *, after_ts: float, timeout: int = 180) -> str:
        """轮询收件箱取最新验证码。

        前一半时间只认 OpenAI/ChatGPT 邮件（同一个临时邮箱可能收到别的信）；
        后一半时间放宽，只按「6 位数字 + 验证码上下文」抽取，避免因为发件人
        被重写而漏码。
        """
        if not inbox.email or not inbox.service_token:
            raise MailboxError("Remail 取件凭证不完整")
        deadline = time.monotonic() + max(1, int(timeout))
        strict_until = time.monotonic() + max(1, int(timeout)) * 0.5
        interval = 3.0
        last_error = "收件箱还没有新邮件"

        logger.info("[Remail] 等待验证码 email=%s 最长 %ss", inbox.email, timeout)
        while True:
            strict = time.monotonic() < strict_until
            try:
                otp = self._pickup_once(inbox, after_ts=after_ts, require_openai=strict)
                if otp:
                    logger.info("[Remail] 已取到验证码 email=%s", inbox.email)
                    return otp
            except MailboxError as exc:
                last_error = str(exc)
                logger.debug("[Remail] 取件失败，继续重试: %s", last_error)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise MailboxError(f"等待邮箱验证码超时（{timeout}s）：{last_error}")

    def _pickup_once(self, inbox: Inbox, *, after_ts: float, require_openai: bool) -> str | None:
        payload = self._request(
            "GET", "/v1/pickup",
            params={"email": inbox.email, "token": inbox.service_token},
            authenticated=False,
        )
        candidates: list[tuple[float, str]] = []
        for message in _pickup_items(payload):
            received_ts = _parse_timestamp(
                _first_value(message, "receivedAt", "received_at", "timestamp", "createdAt", "created_at")
            )
            # 允许 30 秒时钟偏差。
            if after_ts and received_ts is not None and received_ts < after_ts - 30:
                continue
            if require_openai and not _looks_like_openai(message):
                continue
            code = _message_code(message)
            if code:
                candidates.append((received_ts if received_ts is not None else float("-inf"), code))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]


# ---------------------------------------------------------------- 工具函数


def _normalize_base(value: str | None) -> str:
    raw = str(value or "").strip() or DEFAULT_API_BASE
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urlsplit(raw.rstrip("/"))
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise MailboxError("Remail 服务地址无效")
    path = parsed.path.rstrip("/")
    # 用户常直接粘贴文档链接 .../docs，API 在同一域名根路径。
    if path.lower() == "/docs":
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _error_message(payload, response) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("detail")
        if message:
            request_id = payload.get("requestId") or payload.get("request_id")
            return f"{message} (requestId={request_id})" if request_id else str(message)
    text = str(getattr(response, "text", "") or "").strip()
    return text[:240] if text else "服务端未返回错误信息"


def _first_value(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# Remail 的价目字段（purchasePrice / codePrice）单位是**积分**，不是元。1000 积分 = 1 元。
REMAIL_POINTS_PER_YUAN = 1000.0


def _points_label(price: str) -> str:
    """``"15"`` -> ``"15 积分 ≈ ¥0.015"``。

    以前这里直接印 ``¥15``，把积分当成了元，看着像比号池贵一千倍。
    """
    text = str(price or "").strip()
    if not text:
        return "价格未知"
    try:
        points = float(text)
    except ValueError:
        return text
    return f"{text} 积分 ≈ ¥{points / REMAIL_POINTS_PER_YUAN:.6f}".rstrip("0").rstrip(".")


def _unwrap_order(payload) -> dict:
    if not isinstance(payload, dict):
        raise MailboxError("Remail 下单响应不是对象")
    if any(key in payload for key in (
        "orderNo", "order_no", "deliveryEmail", "delivery_email", "serviceToken", "service_token"
    )):
        return payload
    for key in ("data", "order"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    raise MailboxError("Remail 下单响应缺少订单数据")


def _order_credentials(order: dict) -> tuple[str, str, str]:
    email = str(_first_value(order, "deliveryEmail", "delivery_email") or "").strip()
    token = str(_first_value(order, "serviceToken", "service_token") or "").strip()
    order_no = str(_first_value(order, "orderNo", "order_no") or "").strip()
    if email and "@" in email and token:
        return email, token, order_no
    return "", "", order_no


def _parse_timestamp(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        # 毫秒时间戳。
        return value / 1000.0 if value > 1e11 else value
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        value = float(text)
        return value / 1000.0 if value > 1e11 else value
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _pickup_items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        payload = data
    elif isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    items = payload.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _looks_like_openai(message: dict) -> bool:
    marker = " ".join((
        str(_first_value(message, "sender", "from", "fromEmail", "from.emailAddress.address") or ""),
        str(message.get("subject") or ""),
        str(_first_value(message, "bodyPreview", "body_preview") or ""),
    )).casefold()
    return "openai" in marker or "chatgpt" in marker


def _message_code(message: dict) -> str | None:
    direct = _first_value(message, "verificationCode", "verification_code", "code", "otp")
    if direct is not None:
        match = _CODE_RE.search(str(direct))
        if match:
            return match.group(1)
    preview = str(_first_value(message, "bodyPreview", "body_preview", "body", "text", "content") or "")
    return extract_otp({
        "from": str(_first_value(message, "sender", "from", "fromEmail") or ""),
        "subject": str(message.get("subject") or ""),
        "text": preview,
        "content": preview,
    })
