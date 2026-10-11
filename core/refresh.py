# -*- coding: utf-8 -*-
"""Codex 凭证刷新：用 refresh_token 换新的 access_token。

只有走过「ChatGPT 桌面端激活」的账号才有 refresh_token —— 注册流程从
``/api/auth/session`` 拿到的只是 ChatGPT session token，没有 refresh 能力。

刷新请求（与 fulfillment-center/app/oauth_reauth.py 的线上实现一致）：

    POST https://auth.openai.com/oauth/token
    grant_type=refresh_token & refresh_token=... & client_id=...

服务端可能轮换 refresh_token，所以新旧都接受：响应里给了就用新的，
没给就继续用旧的。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import netproxy
from .oauth import CODEX_CLIENT_ID, CODEX_TOKEN_URL, _decode_jwt_segment
from .paths import data_dir

logger = logging.getLogger(__name__)

CREDENTIAL_DIR = data_dir() / "codex_credentials"


class RefreshError(RuntimeError):
    """刷新失败。``permanent=True`` 表示 refresh_token 本身已失效，重试无用。"""

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def resolve_client_id(credential: dict) -> str:
    """从凭证里的 JWT claims 取 client_id；取不到就用 Codex 默认值。"""
    for token_key in ("access_token", "id_token"):
        token = str(credential.get(token_key) or "")
        parts = token.split(".")
        if len(parts) < 2:
            continue
        claims = _decode_jwt_segment(parts[1])
        candidate = str(
            claims.get("client_id")
            or (claims.get("https://api.openai.com/auth") or {}).get("client_id")
            or ""
        ).strip()
        if candidate:
            return candidate
    return CODEX_CLIENT_ID


def _error_detail(payload, response) -> str:
    """从错误响应里取一句人能看懂的话。

    OpenAI 的错误体有两种形状：``{"error": "invalid_grant"}`` 和
    ``{"error": {"message": "...", "type": "..."}}``，都要处理。
    """
    if not isinstance(payload, dict):
        return f"HTTP {getattr(response, 'status_code', 0)}"
    error_value = payload.get("error")
    if isinstance(error_value, dict):
        text = str(error_value.get("message") or error_value.get("code") or "")
    else:
        text = str(error_value or "")
    if not text:
        text = str(payload.get("error_description") or payload.get("message") or "")
    if not text:
        text = f"HTTP {getattr(response, 'status_code', 0)}"
        try:
            body = str(getattr(response, "text", "") or "").strip()
        except Exception:
            body = ""
        if body:
            text = f"{text}: {body[:160]}"
    return text


def refresh_codex_token(
    refresh_token: str,
    *,
    client_id: str = CODEX_CLIENT_ID,
    proxy: str = "",
    timeout: int = 45,
) -> dict:
    """执行一次刷新，返回服务端响应（已校验过 access_token 存在）。"""
    token = str(refresh_token or "").strip()
    if not token:
        raise RefreshError("凭证没有 refresh_token，无法刷新", permanent=True)

    proxies = netproxy.proxy_dict(proxy)
    try:
        response = requests.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": client_id or CODEX_CLIENT_ID,
            },
            headers={"Accept": "application/json"},
            proxies=proxies,
            timeout=max(10, int(timeout)),
        )
    except requests.RequestException as exc:
        # 网络问题不是凭证问题，允许重试。
        raise RefreshError(f"刷新请求失败: {type(exc).__name__}") from exc

    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}

    if response.status_code >= 400 or not isinstance(payload, dict):
        detail = _error_detail(payload, response)
        # 400/401/403 在这个端点都意味着凭证或参数本身有问题，重试无用。
        # （网络类失败在上面已经单独抛出了，那种才是可重试的。）
        permanent = response.status_code in (400, 401, 403)
        raise RefreshError(f"refresh_token 被拒绝: {detail[:200]}", permanent=permanent)

    if not payload.get("access_token"):
        raise RefreshError("刷新响应缺少 access_token")

    logger.info("[刷新] 成功，expires_in=%s", payload.get("expires_in"))
    return payload


def apply_refresh(credential: dict, payload: dict) -> dict:
    """把刷新结果合并进凭证结构，保持 CLIProxyAPI 的字段布局。"""
    now = datetime.now(timezone.utc)
    expires_in = int(payload.get("expires_in") or 0)
    updated = dict(credential)
    updated["access_token"] = str(payload.get("access_token") or "")
    # 服务端轮换 refresh_token 时用新的，否则保留原来的。
    updated["refresh_token"] = str(payload.get("refresh_token") or credential.get("refresh_token") or "")
    if payload.get("id_token"):
        updated["id_token"] = str(payload["id_token"])
    updated["last_refresh"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if expires_in:
        updated["expired"] = (now + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return updated


def iter_credential_files() -> list[Path]:
    if not CREDENTIAL_DIR.exists():
        return []
    return sorted(CREDENTIAL_DIR.glob("*.json"))


def refresh_credential_file(path: Path, *, proxy: str = "", timeout: int = 45) -> tuple[bool, str]:
    """刷新单个凭证文件并原地写回。返回 ``(ok, 说明)``。"""
    try:
        credential = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"凭证文件读取失败: {type(exc).__name__}"
    if not isinstance(credential, dict):
        return False, "凭证文件格式不是对象"

    email = str(credential.get("email") or Path(path).stem)
    try:
        payload = refresh_codex_token(
            str(credential.get("refresh_token") or ""),
            client_id=resolve_client_id(credential),
            proxy=proxy,
            timeout=timeout,
        )
    except RefreshError as exc:
        return False, f"{email}: {exc}"

    updated = apply_refresh(credential, payload)
    try:
        Path(path).write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return False, f"{email}: 写回失败 {type(exc).__name__}"

    return True, f"{email}: 已刷新（有效期至 {updated.get('expired') or '未知'}）"
