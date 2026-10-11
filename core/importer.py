# -*- coding: utf-8 -*-
"""把外部账号数据解析成统一的账号记录。

小白手上能拿到的东西五花八门，这里全部吃下：

* ChatGPT 网页版的完整 session JSON（``accessToken`` + ``user`` + ``account``）
* 一堆 token 组成的 JSON 数组
* 光秃秃的一条 ``access_token``（JWT 字符串）
* 本工具自己导出的 ``accounts_export.json``
* 从别处复制来的、带各种前后缀的混合文本

解析只做本地字符串/JSON 处理，**不联网**。能不能用由后续的查额度决定。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from .quota import account_brief

logger = logging.getLogger(__name__)

# 一条 JWT：三段 base64url。宽松点，避免误伤缺少 padding 的情况。
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

ACCESS_KEYS = ("accesstoken", "access_token", "access-token", "token")
REFRESH_KEYS = ("refreshtoken", "refresh_token", "refresh-token")
ID_TOKEN_KEYS = ("idtoken", "id_token", "id-token")
SESSION_TOKEN_KEYS = ("sessiontoken", "session_token", "session-token")


def _lower_map(payload: dict) -> dict:
    """把字典的键统一成小写去分隔符形式，兼容 accessToken / access_token。"""
    out: dict = {}
    for key, value in payload.items():
        out[str(key).lower().replace("_", "").replace("-", "")] = value
    return out


def _first(payload: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _to_iso(value) -> str:
    """把各种可能的时间表示统一成 ISO 字符串。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        # 秒 / 毫秒都可能出现
        stamp = float(value)
        if stamp > 1e11:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if not text:
        return ""
    # 纯数字字符串
    if text.isdigit():
        return _to_iso(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _record_from_token(token: str, *, refresh_token: str = "", id_token: str = "",
                       session_token: str = "", expires_at: str = "",
                       source: str = "粘贴") -> dict:
    """拿 token 里的 JWT claims 补齐所有能补的字段。"""
    brief = account_brief(token)
    exp_iso = _to_iso(brief.get("exp"))
    return {
        "email": brief.get("email") or "",
        "access_token": token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "account_id": brief.get("account_id") or "",
        "plan": brief.get("plan") or "",
        "expires_at": expires_at or exp_iso,
        "source": source,
    }


def _record_from_object(payload: dict, *, source: str) -> dict | None:
    """一个 JSON 对象 → 一条账号。认不出就返回 None。"""
    flat = _lower_map(payload)

    token = _first(flat, ACCESS_KEYS)
    refresh = _first(flat, REFRESH_KEYS)
    id_token = _first(flat, ID_TOKEN_KEYS)
    session_token = _first(flat, SESSION_TOKEN_KEYS)

    # ChatGPT 的 session JSON 里，token 有时塞在 user / tokens 子对象里。
    for sub_key in ("tokens", "auth", "session"):
        sub = flat.get(sub_key)
        if isinstance(sub, dict):
            sub_flat = _lower_map(sub)
            token = token or _first(sub_flat, ACCESS_KEYS)
            refresh = refresh or _first(sub_flat, REFRESH_KEYS)
            id_token = id_token or _first(sub_flat, ID_TOKEN_KEYS)
            session_token = session_token or _first(sub_flat, SESSION_TOKEN_KEYS)

    # 有些导出把 access_token 直接放顶层，但值是一个 JSON 字符串。
    if token.startswith("{"):
        try:
            inner = json.loads(token)
        except json.JSONDecodeError:
            inner = None
        if isinstance(inner, dict):
            nested = _record_from_object(inner, source=source)
            if nested:
                return nested

    if not token or "." not in token:
        return None

    expires = _to_iso(flat.get("expires") or flat.get("expiry") or flat.get("expired"))

    # 对象里可能直接带了邮箱 / 套餐 / 账号 ID，优先信它（token 里没有时）。
    record = _record_from_token(
        token,
        refresh_token=refresh,
        id_token=id_token,
        session_token=session_token,
        expires_at=expires,
        source=source,
    )
    user = flat.get("user")
    if isinstance(user, dict):
        user_flat = _lower_map(user)
        record["email"] = record["email"] or str(user_flat.get("email") or "").strip()
        record["account_id"] = record["account_id"] or str(user_flat.get("id") or "").strip()
    account = flat.get("account")
    if isinstance(account, dict):
        account_flat = _lower_map(account)
        record["account_id"] = record["account_id"] or str(account_flat.get("id") or "").strip()
        record["plan"] = record["plan"] or str(account_flat.get("plantype") or "").strip()
    for key in ("email", "plan", "accountid"):
        if not record.get({"email": "email", "plan": "plan", "accountid": "account_id"}[key]):
            value = flat.get(key)
            if isinstance(value, str) and value.strip():
                record[{"email": "email", "plan": "plan", "accountid": "account_id"}[key]] = value.strip()
    return record


def _collect_objects(payload, out: list[dict], *, source: str, depth: int = 0) -> None:
    """递归翻找 JSON 里的账号对象。"""
    if depth > 4:
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_objects(item, out, source=source, depth=depth + 1)
        return
    if not isinstance(payload, dict):
        return
    record = _record_from_object(payload, source=source)
    if record:
        out.append(record)
        return
    # 不是账号对象，就往里再翻一层（比如 {"data": [...]}）。
    for value in payload.values():
        if isinstance(value, (dict, list)):
            _collect_objects(value, out, source=source, depth=depth + 1)


def parse(text: str, *, source: str = "粘贴") -> tuple[list[dict], list[str]]:
    """解析任意粘贴内容。

    返回 ``(账号列表, 提示信息列表)``。解析不出东西时列表为空，提示里会说明原因。
    """
    raw = str(text or "").strip()
    if not raw:
        return [], ["内容为空"]

    notes: list[str] = []
    found: list[dict] = []

    # 1) 整体是 JSON
    if raw[:1] in "[{":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            notes.append(f"JSON 解析失败（{exc.msg}），已按纯文本继续提取")
        else:
            _collect_objects(payload, found, source=source)
            if not found:
                notes.append("JSON 里没找到 accessToken 字段")

    # 2) 纯文本 / 混合文本：直接刮 JWT
    if not found:
        tokens = JWT_RE.findall(raw)
        # 去重但保持顺序
        seen: set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            record = _record_from_object({"accessToken": token}, source=source)
            if record:
                found.append(record)
        if found:
            notes.append(f"按文本提取到 {len(found)} 个 token")
        elif not notes:
            notes.append("没找到 access_token，请检查粘贴内容")

    # 去重（同一条 token 只留一份）
    unique: list[dict] = []
    seen_tokens: set[str] = set()
    for record in found:
        token = record.get("access_token") or ""
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        unique.append(record)

    if found and len(unique) < len(found):
        notes.append(f"去重后剩 {len(unique)} 个账号")

    return unique, notes


def looks_like_session_json(text: str) -> bool:
    """粗判是不是 ChatGPT 的 session JSON，用于给小白更准确的提示。"""
    raw = str(text or "")
    return '"accessToken"' in raw or '"access_token"' in raw
