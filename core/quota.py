# -*- coding: utf-8 -*-
"""查询 ChatGPT 账号的生图额度。

调用 ``POST /backend-api/conversation/init`` 读取账号的功能额度面板。
**只读**：不生成图片、不消耗额度、不修改账号状态。

判定逻辑与 ChatGPT2API 一致：在 ``limits_progress`` 里找
``feature_name == "image_gen"``，找不到记为"未知"而不是 0（未知不等于不能用）。
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from . import netproxy

logger = logging.getLogger(__name__)

BASE = "https://chatgpt.com"
ME_PATH = "/backend-api/me"
INIT_PATH = "/backend-api/conversation/init"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


def jwt_claims(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        segment = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def account_brief(access_token: str) -> dict:
    """从 token 里读出展示信息（不联网）。"""
    claims = jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    return {
        "email": claims.get("email") or profile.get("email") or "",
        "plan": auth.get("chatgpt_plan_type") or "",
        "account_id": auth.get("chatgpt_account_id") or "",
        "exp": claims.get("exp"),
    }


def _headers(token: str, path: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "oai-language": "zh-CN",
        "oai-device-id": str(uuid.uuid4()),
        "oai-session-id": str(uuid.uuid4()),
        "origin": BASE,
        "referer": BASE + "/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": USER_AGENT,
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }


def http_request(method: str, url: str, headers: dict, body: dict | None, proxy: str, timeout: int):
    """优先 curl_cffi（TLS 指纹更像浏览器），失败回落 requests。

    返回 ``(response, error)``。连接层失败不该被误判成账号失效。
    """
    proxies = netproxy.proxy_dict(proxy)
    errors: list[str] = []
    payload = json.dumps(body).encode() if body is not None else None

    try:
        from curl_cffi import requests as curl_requests

        response = curl_requests.request(
            method, url, headers=headers, data=payload,
            proxies=proxies, timeout=min(15, max(5, timeout)), impersonate="chrome",
        )
        return response, ""
    except ImportError:
        pass
    except Exception as exc:
        errors.append(f"curl_cffi: {type(exc).__name__}")

    try:
        import requests

        response = requests.request(
            method, url, headers=headers, data=payload,
            proxies=proxies, timeout=max(5, timeout),
        )
        return response, ""
    except Exception as exc:
        errors.append(f"requests: {type(exc).__name__}")

    return None, "；".join(errors)


def extract_image_quota(limits: list) -> tuple[int, str, bool]:
    """返回 ``(剩余额度, 重置时间, 是否未知)``。"""
    for item in limits or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("feature_name") or "") != "image_gen":
            continue
        try:
            remaining = int(item.get("remaining"))
        except (TypeError, ValueError):
            remaining = 0
        return remaining, str(item.get("reset_after") or ""), False
    return 0, "", True


def fetch(access_token: str, *, proxy: str = "", timeout: int = 30) -> dict:
    """查询一个账号的额度与套餐信息。

    返回 ``{"ok": bool, "quota": int, "quota_unknown": bool, ...}``。
    任何失败都返回 ``ok=False`` 并带上原因，不抛异常。
    """
    token = str(access_token or "").strip()
    if not token:
        return {"ok": False, "error": "缺少 access_token"}

    brief = account_brief(token)

    response, error = http_request("GET", BASE + ME_PATH, _headers(token, ME_PATH), None, proxy, timeout)
    if response is None:
        return {"ok": False, "error": f"连接失败: {error}", **brief}
    status = int(getattr(response, "status_code", 0) or 0)
    if status in (401, 403):
        return {"ok": False, "error": f"token 已失效（HTTP {status}）", "dead": True, **brief}
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}", **brief}

    body = {"gizmo_id": None, "requested_default_model": None,
            "conversation_id": None, "timezone_offset_min": -480}
    response, error = http_request("POST", BASE + INIT_PATH, _headers(token, INIT_PATH), body, proxy, timeout)
    if response is None:
        return {"ok": False, "error": f"连接失败: {error}", **brief}
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        return {"ok": False, "error": f"额度查询 HTTP {status}", **brief}

    try:
        payload = response.json()
    except Exception:
        return {"ok": False, "error": "额度响应不是 JSON", **brief}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "额度响应格式异常", **brief}

    limits = payload.get("limits_progress") or []
    remaining, restore_at, unknown = extract_image_quota(limits)
    blocked = [
        item for item in (payload.get("blocked_features") or [])
        if isinstance(item, dict) and str(item.get("name") or "") == "image_gen"
    ]

    if blocked:
        reason = str(blocked[0].get("description") or blocked[0].get("block_reason") or "被上游限制")
        usable = False
        note = f"生图被禁：{reason}"
    elif unknown:
        usable = True          # 未知不等于不能用
        note = "额度未知"
    elif remaining <= 0:
        usable = False
        note = "额度已用尽"
    else:
        usable = True
        note = f"可生图 {remaining} 次"

    return {
        "ok": True,
        "quota": remaining,
        "quota_unknown": unknown,
        "restore_at": restore_at,
        "usable": usable,
        "note": note,
        "default_model": payload.get("default_model_slug") or "",
        "limits": limits,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **brief,
    }


def token_expired(access_token: str) -> bool:
    """只看本地 JWT，判断是否已经过期。"""
    exp = account_brief(access_token).get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return int(exp) < int(time.time())
