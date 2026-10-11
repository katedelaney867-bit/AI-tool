# -*- coding: utf-8 -*-
"""账号查活。

原项目的「查活」是**重新登录一遍**（完整 web 登录 + 邮箱 OTP + OAuth callback），
依赖 curl_cffi 指纹会话和 `core/session.py` 那套 35KB 的设备画像，还要再消耗
一次邮箱收件。对一个注册工具来说太重了。

这里改用只读校验：拿注册时已经拿到的 access_token 调
``/backend-api/accounts/check/v4-2023-04-27``：

    200  → token 有效，顺带把套餐类型读出来
    401  → token 已失效 / 账号被封（这是权威判定）
    403  → 被 Cloudflare 挑战，**不能判死**，返回 unknown
    其它 → 网络或服务端问题，返回 unknown

判活的关键是「不能把风控误判成封号」，所以 401 之外的失败一律是 unknown。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from . import netproxy

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
CHECK_URL = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min=-480"

# 401 才代表 token 真的失效；403/429 是风控，不能据此判死。
DEAD_STATUSES = {401}


def _headers(token: str, account_id: str, device_id: str, session_id: str) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {token}",
        "referer": "https://chatgpt.com/",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "priority": "u=1, i",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_PATH,
        "oai-device-id": device_id,
        "oai-session-id": session_id,
        "oai-language": "en-US",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


def parse_plan(data: Any, account_id: str = "") -> dict:
    """从 accounts/check 响应里提取套餐信息。字段名按线上实际响应。"""
    if not isinstance(data, dict):
        return {}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return {}

    item = None
    if account_id and isinstance(accounts.get(account_id), dict):
        item = accounts[account_id]
    elif isinstance(accounts.get("default"), dict):
        item = accounts["default"]
    else:
        for key, value in accounts.items():
            if key != "default" and isinstance(value, dict):
                item = value
                break
    if not isinstance(item, dict):
        return {}

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    plan_type = str(account.get("plan_type") or "")
    subscription_plan = str(entitlement.get("subscription_plan") or "")
    is_free = plan_type.lower() == "free" or subscription_plan.lower() == "chatgptfreeplan"
    return {
        "plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": bool(entitlement.get("has_active_subscription")),
        "is_free": is_free,
        "expires_at": entitlement.get("expires_at"),
    }


def _http_get(url: str, headers: dict, proxy: str, timeout: int):
    """发一次 GET，返回 ``(response, error)``。

    优先 curl_cffi：它的 TLS 指纹更接近真实浏览器，撞 Cloudflare 的概率低。
    但 curl_cffi 走的是自带 libcurl，在部分环境（代理/DNS 配置特殊）会直接
    连接超时 —— 那种情况不是账号问题，所以回退到 requests 再试一次。
    连接层的失败不该被误判成「账号失效」。
    """
    proxies = netproxy.proxy_dict(proxy)
    errors: list[str] = []

    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        curl_requests = None

    if curl_requests is not None:
        try:
            response = curl_requests.get(
                url,
                headers=headers,
                proxies=proxies,
                # 连接层问题要快速失败，不能把整个查活拖到 timeout。
                timeout=min(10, max(5, int(timeout))),
                impersonate="chrome",
            )
            return response, ""
        except Exception as exc:
            logger.debug("[查活] curl_cffi 失败，回退 requests: %s: %s", type(exc).__name__, exc)
            errors.append(f"curl_cffi: {type(exc).__name__}")

    try:
        import requests

        response = requests.get(url, headers=headers, proxies=proxies, timeout=max(5, int(timeout)))
        return response, ""
    except Exception as exc:
        errors.append(f"requests: {type(exc).__name__}")

    return None, "；".join(errors)


def check_liveness(
    *,
    access_token: str,
    account_id: str = "",
    proxy: str = "",
    timeout: int = 30,
    device_id: str = "",
    session_id: str = "",
) -> tuple[str, str, dict]:
    """校验一个 access_token 是否仍然有效。

    返回 ``(status, note, detail)``，status ∈ ``alive`` / ``dead`` / ``unknown``。
    """
    token = str(access_token or "").strip()
    if not token:
        return "unknown", "缺少 access_token", {}

    headers = _headers(
        token, account_id,
        device_id or str(uuid.uuid4()),
        session_id or str(uuid.uuid4()),
    )

    response, error = _http_get(CHECK_URL, headers, proxy, timeout)
    if response is None:
        # 网络/代理问题不能判死。
        return "unknown", f"请求失败: {error}", {}

    status = int(getattr(response, "status_code", 0) or 0)

    if status in DEAD_STATUSES:
        return "dead", f"HTTP {status} token 已失效", {"http_status": status}

    if status == 403:
        return "unknown", "HTTP 403 被风控拦截，无法判定", {"http_status": status}
    if status == 429:
        return "unknown", "HTTP 429 触发限流，无法判定", {"http_status": status}
    if not 200 <= status < 300:
        return "unknown", f"HTTP {status}", {"http_status": status}

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        text = str(getattr(response, "text", "") or "")
        if text.strip().startswith(("{", "[")):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
        else:
            payload = None
    if not isinstance(payload, (dict, list)):
        return "alive", "HTTP 200，响应非 JSON", {"http_status": status}

    detail = parse_plan(payload, account_id)
    detail["http_status"] = status
    if detail:
        plan = detail.get("plan_type") or detail.get("subscription_plan") or "未知"
        return "alive", f"有效 · 套餐 {plan}", detail
    return "alive", "有效", detail
