# -*- coding: utf-8 -*-
"""接码平台：SMSBower / GrizzlySMS / 5SIM。

SMSBower 和 GrizzlySMS 都用 SMS-Activate 兼容的 handler API，一套代码通吃，
只是 base 和 key 不同。

★ 2026-10-16 加 5SIM：它的 API 是另外一套（Bearer token + REST 路径），
所以单独一个 `FiveSimClient`。加它的原因：OpenAI 接码在 5SIM 上有**真正的大库存**
（印尼 virtual34 22 万、菲律宾 virtual34 27 万、哥伦比亚 virtual34 29 万），
而 SMSBower 的便宜档往往只有几百到几千个号（英国那个 $0.046 档只有 798 个，
我们因此翻来覆去抽到同一个被风控的号段）。

提炼自 turb-gpt-free-register/core/sms_provider.py，砍掉本地 L/H 取号服务
（需要用户自建 localhost:8788 后台，对小白不现实），只保留两家公开平台。
"""
from __future__ import annotations

import json
import os
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, dict] = {
    "smsbower": {
        "label": "SMSBower",
        "api_base": "https://smsbower.online/stubs/handler_api.php",
    },
    "grizzly": {
        "label": "GrizzlySMS",
        "api_base": "https://api.grizzlysms.com/stubs/handler_api.php",
    },
}

# setStatus 取值
STATUS_READY = 1       # 号码已就绪，短信已发出
STATUS_RESEND = 3      # 请求重发短信
STATUS_COMPLETE = 6    # 激活完成
STATUS_CANCEL = 8      # 取消激活


class SmsError(RuntimeError):
    """接码平台通用错误。"""


class SmsNoNumbers(SmsError):
    """平台暂无可用号码。"""


class SmsNoBalance(SmsError):
    """平台余额不足。"""


class SmsCodeTimeout(SmsError):
    """等短信超时 —— 上层应当换号重试。"""


class SmsClient:
    """一家接码平台的会话。"""

    def __init__(
        self,
        provider: str,
        api_key: str,
        *,
        service: str = "dr",
        country: str = "10",
        max_price: str = "",
        timeout: int = 30,
    ) -> None:
        key = str(provider or "").strip().lower()
        if key not in PROVIDERS:
            raise SmsError(f"不支持的接码平台：{provider}")
        self.provider = key
        self.label = PROVIDERS[key]["label"]
        self.api_base = PROVIDERS[key]["api_base"]
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise SmsError(f"{self.label} API Key 未填写")
        self.service = str(service or "dr").strip()
        self.country = str(country or "10").strip()
        self.max_price = str(max_price or "").strip()
        self.timeout = max(5, int(timeout))

    # ------------------------------------------------------------- HTTP

    def _call(self, params: dict) -> str:
        query = {"api_key": self.api_key, **params}
        try:
            response = requests.get(self.api_base, params=query, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SmsError(f"{self.label} 请求失败: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise SmsError(f"{self.label} API Key 无效或无权访问（HTTP {response.status_code}）")
        if response.status_code != 200:
            raise SmsError(f"{self.label} HTTP {response.status_code}")

        text = str(response.text or "").strip()
        if text == "BAD_KEY":
            raise SmsError(f"{self.label} API Key 无效（BAD_KEY）")
        if text == "NO_BALANCE":
            raise SmsNoBalance(f"{self.label} 余额不足（NO_BALANCE），请先充值")
        if text == "NO_NUMBERS":
            raise SmsNoNumbers(f"{self.label} 暂无可用号码（NO_NUMBERS）")
        if text == "SERVICE_UNAVAILABLE_REGION":
            raise SmsError(f"{self.label} 该地区受限（SERVICE_UNAVAILABLE_REGION），请换代理国家")
        if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
            raise SmsError(f"{self.label} 请求参数错误：{text}")
        if text == "NO_ACTIVATION":
            raise SmsError(f"{self.label} 激活 ID 不存在（NO_ACTIVATION）")
        if text.startswith("The service is prohibited"):
            raise SmsError(f"{self.label} 该服务被平台禁售：{text[:120]}")
        return text

    # ---------------------------------------------------------- 取号/收码

    def acquire(self) -> tuple[str, str]:
        """取一个号，返回 (activation_id, 手机号)。手机号不带 + 前缀。"""
        params = {"action": "getNumber", "service": self.service, "country": self.country}
        if self.max_price:
            params["maxPrice"] = self.max_price
        text = self._call(params)
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsError(f"{self.label} getNumber 响应异常：{text[:160]}")
        parts = text.split(":")
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            raise SmsError(f"{self.label} getNumber 格式异常：{text[:160]}")
        activation_id, phone = parts[1].strip(), parts[2].strip()
        # 号码记进日志：不然「哪个号接到的码」事后完全查不到（租来的号已经释放，
        # 上游那边也翻不回来）。这是用完即弃的临时号，记下来只用于排查。
        logger.info("[SMS:%s] 取号成功 id=%s 号码=%s", self.label, activation_id, phone)
        return activation_id, phone

    def wait_code(
        self,
        activation_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 5,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询 getStatus 直到拿到短信验证码。"""
        deadline = time.time() + max(10, int(max_wait))
        interval = max(2, int(poll_interval))
        rounds = 0
        logger.info("[SMS:%s] 等待短信验证码，最长 %ss", self.label, max_wait)

        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise SmsError("用户已请求停止")
            rounds += 1
            text = self._call({"action": "getStatus", "id": activation_id})

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info("[SMS:%s] 第 %s 轮收到验证码", self.label, rounds)
                return code
            if text == "STATUS_CANCEL":
                raise SmsError(f"{self.label} 激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY / STATUS_WAIT_RESEND → 继续等

            remaining = int(deadline - time.time())
            if remaining <= 0:
                break
            logger.debug("[SMS:%s] 第 %s 轮未收到（%s），剩 %ss", self.label, rounds, text[:40], remaining)
            time.sleep(min(interval, remaining))

        raise SmsCodeTimeout(f"{self.label} 等待短信超时（>{max_wait}s）")

    # ------------------------------------------------------------- 状态

    def set_status(self, activation_id: str, status: int) -> str:
        try:
            return self._call({"action": "setStatus", "status": str(int(status)), "id": activation_id})
        except SmsError as exc:
            logger.debug("[SMS:%s] setStatus(%s) 失败: %s", self.label, status, exc)
            return ""

    def complete(self, activation_id: str) -> None:
        """标记激活完成（编号 6）。失败不影响主流程。"""
        self.set_status(activation_id, STATUS_COMPLETE)

    def cancel(self, activation_id: str) -> None:
        """取消并释放号码（编号 8）。失败不影响主流程。"""
        self.set_status(activation_id, STATUS_CANCEL)


# ---------------------------------------------------------------------------
# 5SIM：不是 handler_api 那套，得单独一个客户端（2026-10-16 实测）
# ---------------------------------------------------------------------------

FIVESIM_BASE = "https://5sim.net/v1"


class FiveSimClient:
    """5SIM 会话：Bearer token + REST 路径。

    与 `SmsClient`（SMSBower/Grizzly 那套 handler_api）的区别：

        取号  GET /v1/user/buy/activation/{country}/{operator}/{product}
        查码  GET /v1/user/check/{id}      → JSON，`sms` 列表非空即有码
        完成  GET /v1/user/finish/{id}
        退号  GET /v1/user/cancel/{id}     → 未收码**全额退款**（实测余额精确回补）

    ★★ 账户级可买的货 ≠ 公开价位表（实测踩到的坑）：
      公开 `/v1/guest/prices?product=openai` 显示 colombia/virtual34 有 29 万库存、
      单价 $0.0769，但**这个账户**买 virtual34 报 `no free phones`，
      实际只能买 virtual66（$0.17）/ philippines virtual58（$0.1077）/
      usa virtual51（$0.20）。所以「看板说便宜」不等于「我们买得到」——
      报价要在**账号维度**实测一次。
    """

    label = "5SIM"

    def __init__(
        self,
        api_key: str,
        *,
        country: str = "indonesia",
        operator: str = "any",
        product: str = "openai",
        timeout: int = 30,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise SmsError(f"{self.label} API Key 未填写")
        self.country = str(country or "indonesia").strip().lower()
        self.operator = str(operator or "any").strip().lower()
        self.product = str(product or "openai").strip().lower()
        self.timeout = max(5, int(timeout))

    # ------------------------------------------------------------- HTTP

    def _call(self, path: str) -> str:
        try:
            response = requests.get(
                FIVESIM_BASE + path,
                headers={"Authorization": "Bearer " + self.api_key, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SmsError(f"{self.label} 请求失败: {type(exc).__name__}") from exc

        text = str(response.text or "").strip()
        if response.status_code in (401, 403):
            raise SmsError(f"{self.label} API Key 无效或无权访问（HTTP {response.status_code}）")
        if response.status_code == 402:
            raise SmsNoBalance(f"{self.label} 余额不足（HTTP 402）")
        if text.lower() == "no free phones":
            raise SmsNoNumbers(f"{self.label} 暂无可用号码（no free phones）")
        if text.lower() == "no balance":
            raise SmsNoBalance(f"{self.label} 余额不足（no balance）")
        if text.lower() in ("bad token", "unauthorized"):
            raise SmsError(f"{self.label} API Key 无效（{text}）")
        if response.status_code != 200:
            raise SmsError(f"{self.label} HTTP {response.status_code}: {text[:120]}")
        return text

    # ---------------------------------------------------------- 取号/收码

    def acquire(self) -> tuple[str, str]:
        """取一个号，返回 (order_id, 手机号)。手机号**不带 + 前缀**（与 SmsClient 一致）。"""
        raw = self._call(f"/user/buy/activation/{self.country}/{self.operator}/{self.product}")
        try:
            order = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise SmsError(f"{self.label} 取号响应不是 JSON：{raw[:160]}") from exc
        if not isinstance(order, dict) or not order.get("id"):
            raise SmsError(f"{self.label} 取号响应异常：{raw[:160]}")
        order_id = str(order.get("id"))
        digits = "".join(ch for ch in str(order.get("phone") or "") if ch.isdigit())
        if not digits:
            raise SmsError(f"{self.label} 取号没拿到号码：{raw[:160]}")
        logger.info(
            "[SMS:%s] 取号成功 id=%s 号码=%s 运营商=%s 价格=$%s",
            self.label, order_id, digits, order.get("operator"), order.get("price"),
        )
        return order_id, digits

    def wait_code(
        self,
        activation_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 5,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询 check 直到 `sms` 列表里出现验证码。"""
        deadline = time.time() + max(10, int(max_wait))
        interval = max(2, int(poll_interval))
        rounds = 0
        logger.info("[SMS:%s] 等待短信验证码，最长 %ss", self.label, max_wait)

        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise SmsError("用户已请求停止")
            rounds += 1
            raw = self._call(f"/user/check/{activation_id}")
            try:
                order = json.loads(raw)
            except Exception:  # noqa: BLE001
                order = {}
            sms = order.get("sms") if isinstance(order, dict) else None
            if isinstance(sms, list) and sms:
                first = sms[0] if isinstance(sms[0], dict) else {}
                code = str(first.get("code") or "").strip()
                if code:
                    logger.info("[SMS:%s] 第 %s 轮收到验证码", self.label, rounds)
                    return code
            status = str((order or {}).get("status") or "").upper()
            if status in ("CANCELED", "CANCELLED", "TIMEOUT", "FINISHED"):
                raise SmsError(f"{self.label} 订单已结束（{status}），换号")

            remaining = int(deadline - time.time())
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise SmsCodeTimeout(f"{self.label} 等待短信超时（>{max_wait}s）")

    # ------------------------------------------------------------- 状态

    def set_status(self, activation_id: str, status: int) -> str:
        """5SIM 没有 SMS-Activate 那套 setStatus —— 这里只是占位、什么都不做。

        ★★ 2026-10-17 补（真事故）：流程里会调 `sms_client.set_status(id, 1)`
        （"号码已就绪"），这是照 SMS-Activate 写的。缺了它，整轮手机验证会在
        **刚买完号**之后当场抛 `AttributeError` —— 5SIM 的号还能退，
        长效号源的长效号就直接作废（不可退），一次白扔 3 个号。
        """
        logger.debug("[SMS:%s] set_status(%s) 无对应动作，忽略", self.label, status)
        return ""

    def complete(self, activation_id: str) -> None:
        """标记完成（收码后释放）。失败不影响主流程。"""
        try:
            self._call(f"/user/finish/{activation_id}")
        except SmsError as exc:
            logger.debug("[SMS:%s] finish 失败: %s", self.label, exc)

    def cancel(self, activation_id: str) -> None:
        """退号（未收码全额退款）。失败不影响主流程。"""
        try:
            self._call(f"/user/cancel/{activation_id}")
        except SmsError as exc:
            logger.debug("[SMS:%s] cancel 失败: %s", self.label, exc)


# ---------------------------------------------------------------------------
# 长效号源：长效卡号（北美），**买了不能退不能换**，所以只按号段买最少数量
# ---------------------------------------------------------------------------

# 长效号源接口地址：**不在源码里硬编码**，用环境变量 GRL_LONGTERM_BASE 配置；
# 没配就让这个号源不可用（报明确错误），而不是把钱烧在一个写死的地址上。
LONGTERM_BASE = os.environ.get("GRL_LONGTERM_BASE", "").strip()
_LONGTERM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 号段轮换的持久化游标（跨进程、跨单）。见 LongTermSmsClient._pick_prefix 的注释。
_LONGTERM_ROTATE_FILE = Path(__file__).resolve().parent.parent / "data" / "longterm_prefix_rotate.json"


def _longterm_next_index(count: int) -> int:
    """读出「下一个该用第几个号段」并推进一格；任何异常都退回 0。

    写失败不影响取号 —— 轮换只是**采样手段**，不该因为它挂掉整个激活流程。
    """
    total = max(1, int(count))
    path = _LONGTERM_ROTATE_FILE
    index = 0
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            index = int(payload.get("next") or 0)
    except Exception:  # noqa: BLE001
        index = 0
    index %= total
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"next": (index + 1) % total}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.debug("[SMS:长效] 号段轮换游标写不进去（不影响取号）")
    return index


# 本地号表：号码 → 接码链接（买的号只在下单响应里给一次链接，进程重启就丢）。
# 有了它才能「复用旧号」以及事后按号段算真实成本。
_LONGTERM_NUMBERS_FILE = Path(__file__).resolve().parent.parent / "data" / "longterm_numbers.json"


def _longterm_load_numbers() -> dict:
    try:
        if _LONGTERM_NUMBERS_FILE.exists():
            # ★ 用 utf-8-sig：PowerShell 5.1 的 `Set-Content -Encoding utf8` 会带 BOM，
            #   而 json.loads 遇到 BOM 直接抛 ⇒ 整张号表被当成空表，
            #   复用号永远找不到接码链接（2026-10-17 实测踩了一次）。
            raw = json.loads(_LONGTERM_NUMBERS_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                return raw
    except Exception:  # noqa: BLE001
        logger.debug("[SMS:长效] 本地号表读不出来，按空表处理")
    return {}


def _longterm_save_numbers(book: dict) -> None:
    try:
        _LONGTERM_NUMBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LONGTERM_NUMBERS_FILE.write_text(
            json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        logger.debug("[SMS:长效] 本地号表写不进去（不影响取号）")


class LongTermSmsClient:
    """长效号码（项目 ID 需在设置里自己填，默认 0 = 未配置）。

    2026-10-17 拿老板的 token 实测通过的协议：

        GET  /api/v1/profile/info        余额
        POST /api/v1/buy/prefix          {app_id,type,expiry} → 号段库存
        POST /api/v1/buy/create          {app_id,num,prefix,expiry} → 号码 + 接码链接
        GET  {api}/api/record?token=...&format=json3 → {data:{sms,...}}
        POST /api/v1/order/list          {page,limit,type} → 订单
        POST /api/v1/order/api           {ordernum} → 该订单的号码 + 接码链接

    ★ 认证是**裸 token**（`Authorization: <token>`），不是 Bearer。
    ★★ 与 SMSBower 最大的区别：**不可退、不可换**。所以：
      - `cancel()` 只记日志、不退款（钱已经花了）；
      - 号码有效期很长（实测 ~22 天），**同一号码可以复用**（见 `pin_numbers`）；
      - 选号段（prefix）是唯一的质量手段 ⇒ 按 `prefixes` 顺序轮着买，
        日志里打 `号段=`，事后就能按号段统计成功率。
    ★ 限速：buy/create 每用户 10 次/60s；超限 429 且封 120 秒。
    """

    label = "长效号码"

    def __init__(
        self,
        api_key: str,
        *,
        app_id: int = 0,
        prefixes: str = "",
        expiry: int = 0,
        pin_numbers: str = "",
        timeout: int = 40,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise SmsError(f"{self.label} API Token 未填写")
        self.app_id = int(app_id or 0)
        self.prefixes = [p.strip() for p in str(prefixes or "").replace(" ", "").split(",") if p.strip()]
        self.expiry = int(expiry or 0)
        self.timeout = max(10, int(timeout))
        # 指定复用号码（只在手工做「长效号能不能吃第二单」实验时填）
        self.pin_numbers = [
            "".join(ch for ch in str(n) if ch.isdigit())
            for n in str(pin_numbers or "").replace(" ", "").split(",")
            if str(n).strip()
        ]
        self._record_urls: dict[str, str] = {}   # order_id → 接码链接
        self._prefix_of: dict[str, str] = {}     # order_id → 买的号段（统计用）
        self._baseline: dict[str, tuple[str, str]] = {}  # 复用号：复用前的 (sms, sms_time)

    # ------------------------------------------------------------- HTTP

    def _call(self, path: str, body: dict | None = None) -> dict:
        if not LONGTERM_BASE:
            raise SmsError(
                f"{self.label} 接口地址未配置：请设置环境变量 GRL_LONGTERM_BASE（本程序不内置上游地址）"
            )
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            LONGTERM_BASE + path,
            data=data,
            method="POST" if body is not None else "GET",
            headers={
                "Authorization": self.api_key,   # ★ 裸 token，不加 Bearer
                "Content-Type": "application/json",
                "User-Agent": _LONGTERM_UA,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:160] if exc.fp else ""
            if exc.code == 429:
                raise SmsError(f"{self.label} 请求过于频繁（429，会封 120~900 秒）：{detail}") from exc
            raise SmsError(f"{self.label} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SmsError(f"{self.label} 连不上：{exc.reason}") from exc
        try:
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise SmsError(f"{self.label} 响应不是 JSON：{raw[:160]}") from exc

    def _pick_prefix(self) -> str:
        """按配置顺序轮着用号段（每买一个换下一个）。空 = 让平台自己挑。

        ★★ 2026-10-17 修：轮换状态必须**跨进程**。
          `LongTermSmsClient` 是每次激活新建一个实例，实例里的 `_rotate` 每单都从 0
          开始 ⇒ 配了 3 个号段也永远只用第一个（实测连开两单全是 1236，另外两个
          一个样本都拿不到）。号段是长效号**唯一**的质量手段，必须让它轮起来。
        """
        if not self.prefixes:
            return ""
        prefix = self.prefixes[_longterm_next_index(len(self.prefixes))]
        return prefix

    # ---------------------------------------------------------- 取号/收码

    def _fetch_record_once(self, record_url: str) -> dict:
        """读一次接码链接，失败返回 {}（复用号取基线、以及 wait_code 都用它）。"""
        url = record_url + ("&" if "?" in record_url else "?") + "format=json3"
        req = urllib.request.Request(url, headers={"User-Agent": _LONGTERM_UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return {}
        return payload.get("data") or {}

    def _pick_pinned(self) -> tuple[str, str] | None:
        """复用旧号：不花钱、直接用上次买过、本地号表里还有接码链接的号码。

        只在 `pin_numbers` 显式配置时才走这条 —— 默认永不触发（老板的正常产线
        是「每次都买新号」）。

        ★ 轮转：挑 `pinned_attempts` 最少的那个，测两个号时自动交替。
        ★★ 旧码陷阱：长效号的接码页会**留着上一单的验证码**（实测 12368618112
          在 10:17 收的码，10:30 还挂在上面）。所以这里先读一次基线，
          `wait_code` 看到与基线一模一样的码就继续等，绝不当成新码填进页面。
        """
        if not self.pin_numbers:
            return None
        book = _longterm_load_numbers()
        usable = [n for n in self.pin_numbers if str((book.get(n) or {}).get("api") or "")]
        if not usable:
            logger.warning(
                "[SMS:长效] 指定复用号在本地号表里没有接码链接（%s），改为现买",
                ",".join(self.pin_numbers),
            )
            return None
        usable.sort(key=lambda n: int((book[n].get("pinned_attempts") or 0)))
        tel = usable[0]
        entry = book[tel]
        entry["pinned_attempts"] = int(entry.get("pinned_attempts") or 0) + 1
        entry["last_pinned_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _longterm_save_numbers(book)
        record_url = str(entry.get("api") or "")
        baseline = self._fetch_record_once(record_url)
        order_id = f"pin-{tel}"
        self._record_urls[order_id] = record_url
        self._prefix_of[order_id] = tel[:4]
        self._baseline[order_id] = (
            str(baseline.get("sms") or ""), str(baseline.get("sms_time") or "")
        )
        logger.warning(
            "[SMS:长效] 复用旧号（本次不买号、不花钱）：%s 号段=%s 第 %s 次复用；"
            "已记录旧码基线=%r（等同旧码会继续等）",
            tel, tel[:4], entry["pinned_attempts"], self._baseline[order_id][0][:40],
        )
        return order_id, tel

    def _remember(self, phone: str, record_url: str, item: dict) -> None:
        """把买到的号落进本地号表（复用、算成本都靠它）。"""
        book = _longterm_load_numbers()
        book[phone] = {
            "api": record_url,
            "prefix": phone[:4],
            "end_time": str(item.get("end_time") or ""),
            "bought_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bought_price_usd": 0.027 if self.expiry == 1 else 0.030,
            "pinned_attempts": int((book.get(phone) or {}).get("pinned_attempts") or 0),
        }
        _longterm_save_numbers(book)

    def _recover_lost_purchase(self) -> tuple[str, str] | None:
        """下单响应在网络上丢了（SSL EOF 之类），去订单列表把那个号找回来。

        ★★ 2026-10-17 真事故：`buy/create` 连续三次
          `SSL: UNEXPECTED_EOF_WHILE_READING`，换号循环把它当成 3 个号都失败，
          整单报废 —— 而实际上**上游可能已经把号卖给我们了**（钱照扣、
          号码躺在我们名下没人知道）。所以先查最近 3 分钟的订单，
          有就把它捡回来用，没有才重新下单。
        """
        try:
            payload = self._call("/api/v1/order/list", {"page": 1, "limit": 5, "type": 1})
        except Exception:  # noqa: BLE001
            return None
        orders = (payload.get("data") or {}).get("list") or []
        cutoff = time.time() - 180
        for order in orders:
            created = str(order.get("create_time") or "")
            try:
                made_at = time.mktime(time.strptime(created, "%Y-%m-%d %H:%M:%S"))
            except Exception:  # noqa: BLE001
                continue
            if made_at < cutoff:
                continue
            ordnum = str(order.get("ordernum") or "")
            if not ordnum:
                continue
            try:
                detail = self._call("/api/v1/order/api", {"ordernum": ordnum})
            except Exception:  # noqa: BLE001
                continue
            items = (detail.get("data") or {}).get("list") or []
            for item in items:
                tel = "".join(ch for ch in str(item.get("tel") or "") if ch.isdigit())
                url = str(item.get("api") or "").strip()
                if not tel or not url:
                    continue
                if tel in _longterm_load_numbers():
                    continue      # 已经在我们表里 = 早就处理过
                self._remember(tel, url, item)
                order_id = str(item.get("token") or "") or tel
                self._record_urls[order_id] = url
                self._prefix_of[order_id] = tel[:4]
                logger.warning(
                    "[SMS:长效] 下单响应丢了，但从订单 %s 里把号捡回来了：%s 号段=%s",
                    ordnum, tel, tel[:4],
                )
                return order_id, tel
        return None

    def acquire(self) -> tuple[str, str]:
        """买一个号，返回 (order_id, 手机号纯数字)。**不可退**，所以一次只买 1 个。"""
        pinned = self._pick_pinned()
        if pinned is not None:
            return pinned
        prefix = self._pick_prefix()
        body: dict = {"app_id": self.app_id, "type": 1, "num": 1, "expiry": self.expiry}
        if prefix:
            body["prefix"] = prefix
        payload: dict = {}
        for attempt in (1, 2, 3):
            try:
                payload = self._call("/api/v1/buy/create", body)
                break
            except SmsError as exc:
                text = str(exc)
                transient = any(key in text for key in (
                    "连不上", "EOF", "SSL", "timed out", "超时", "Connection",
                ))
                if not transient or attempt == 3:
                    raise
                logger.warning(
                    "[SMS:%s] 下单请求中断（第 %s/3 次）：%s —— 先去订单列表确认有没有被扣号",
                    self.label, attempt, text[:160],
                )
                recovered = self._recover_lost_purchase()
                if recovered is not None:
                    return recovered
                time.sleep(2 * attempt)
        if payload.get("code") != 1:
            msg = str(payload.get("msg") or "")
            if "库存不足" in msg or "人数过多" in msg:
                raise SmsNoNumbers(f"{self.label} {msg}")
            raise SmsError(f"{self.label} 下单失败：{msg or payload}")
        data = payload.get("data") or {}
        items = data.get("list") or []
        if not items:
            raise SmsError(f"{self.label} 下单成功但没返回号码：{str(data)[:200]}")
        item = items[0]
        phone = "".join(ch for ch in str(item.get("tel") or "") if ch.isdigit())
        record_url = str(item.get("api") or "").strip()
        order_id = str(item.get("token") or "") or phone or str(data.get("ordernum") or "")
        if not phone or not record_url:
            raise SmsError(f"{self.label} 返回字段不全：{str(item)[:200]}")
        self._record_urls[order_id] = record_url
        self._prefix_of[order_id] = prefix or phone[:4]
        self._remember(phone, record_url, item)
        logger.info(
            "[SMS:%s] 取号成功 号段=%s 号码=%s 到期=%s（不可退）",
            self.label, self._prefix_of[order_id], phone, item.get("end_time"),
        )
        return order_id, phone

    def wait_code(
        self,
        activation_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 5,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询接码链接直到出现验证码。"""
        record_url = self._record_urls.get(activation_id)
        if not record_url:
            raise SmsError(f"{self.label} 找不到接码链接（{activation_id}）")
        deadline = time.time() + max(10, int(max_wait))
        interval = max(3, int(poll_interval))
        rounds = 0
        baseline = self._baseline.get(activation_id)
        logger.info("[SMS:%s] 等待短信验证码，最长 %ss（号段=%s%s）",
                    self.label, max_wait, self._prefix_of.get(activation_id, "?"),
                    "，复用号·忽略旧码" if baseline else "")
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise SmsError("用户已请求停止")
            rounds += 1
            url = record_url + ("&" if "?" in record_url else "?") + "format=json3"
            req = urllib.request.Request(url, headers={"User-Agent": _LONGTERM_UA})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SMS:%s] 第 %s 轮读取失败：%s", self.label, rounds, exc)
                payload = {}
            data = payload.get("data") or {}
            sms = str(data.get("sms") or "").strip()
            if sms and baseline:
                # 复用号上挂着的还是上一单那一条 ⇒ 不是这一单的码，继续等。
                if sms == baseline[0] and str(data.get("sms_time") or "") == baseline[1]:
                    sms = ""
            if sms:
                digits = "".join(ch for ch in sms if ch.isdigit())
                code = digits if 4 <= len(digits) <= 8 else sms
                logger.info("[SMS:%s] 第 %s 轮收到验证码（号段=%s）",
                            self.label, rounds, self._prefix_of.get(activation_id, "?"))
                return code
            remaining = int(deadline - time.time())
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        raise SmsCodeTimeout(f"{self.label} 等待短信超时（>{max_wait}s）")

    # ------------------------------------------------------------- 状态

    def set_status(self, activation_id: str, status: int) -> str:
        """长效号源没有 setStatus 那套接口 —— 占位、什么都不做。

        ★★ 2026-10-17 真事故：第一版漏了这个方法，流程在"号码已就绪"那步
        当场抛 `AttributeError`，而长效号**不可退** ⇒ 一次白扔 3 个号（$0.09）。
        """
        logger.debug("[SMS:%s] set_status(%s) 无对应动作，忽略", self.label, status)
        return ""

    def complete(self, activation_id: str) -> None:
        """长效号完成不需要通知上游（号码还有效，留着可能复用）。"""
        logger.debug("[SMS:%s] complete：长效号无需释放", self.label)

    def cancel(self, activation_id: str) -> None:
        """★ 长效号**不可退**：只记日志，不退款。

        老板口径（2026-10-17）：「拿号之后不能再换号或退款」—— 换号等于白扔 $0.03。
        这条日志让「扔掉了几个号、扔的是哪个号段」事后可见。
        """
        if activation_id.startswith("pin-"):
            logger.warning(
                "[SMS:%s] 复用号没吃下这一单（不涉及钱）：%s",
                self.label, activation_id[4:],
            )
            return
        logger.warning(
            "[SMS:%s] 换号：长效号不可退，这个号已作废（号段=%s）",
            self.label, self._prefix_of.get(activation_id, "?"),
        )
