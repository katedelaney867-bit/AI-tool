# -*- coding: utf-8 -*-
"""ChatGPT 桌面端激活（Codex OAuth 授权）。

流程：

    生成 PKCE + state
      → 清掉浏览器里注册留下的登录态
      → 打开 auth.openai.com 授权页
      → 邮箱登录 + 邮箱 OTP（复用本单的邮箱订单取件）
      → 手机号验证（接码平台，页面要求时才做）
      → consent / workspace → 捕获 localhost:1455 callback
      → authorization code 换 token（本地 PKCE，不需要 CPA 服务）
      → 保存 Codex 凭证

与原项目 roxy_codex_oauth 的差异：原实现把 callback 提交给外部 CPA/sub2 管理服务，
凭证由那边落库；这里固定走本地 PKCE，自己换 token 并落盘，用户不需要额外部署。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import netproxy, oauth_flow, sitepool as sitepool_mod
from .mailbox.base import Inbox
from .miaoic import MiaoicSmsClient
from .paths import data_dir
from .settings import Config
from .shortcode import ShortCodeSmsClient
from .sitepool import SitePoolSmsClient
from .sms import LongTermSmsClient, FiveSimClient, SmsClient, SmsError
from .stopflag import StopRequested, check_stop
from .store import AccountRecord

logger = logging.getLogger(__name__)

# 与 CLIProxyAPI 对齐的 Codex OAuth 客户端参数（公开固定值）。
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPE = "openid email profile offline_access"

CREDENTIAL_DIR = data_dir() / "codex_credentials"

EventFn = Callable[[str, str], None]


def _noop(stage: str, detail: str = "") -> None:
    return None


# ------------------------------------------------------------------ PKCE


def generate_pkce() -> tuple[str, str]:
    """PKCE 对：verifier = base64url(96 随机字节)，challenge = base64url(sha256(verifier))。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(state: str, code_challenge: str, prompt: str = "") -> str:
    """拼授权地址。

    ``prompt`` 留空 = 不强制重登：浏览器里已经有会话就直接进授权/同意页。
    只有确实需要「换个账号重登」时才传 ``"login"``。
    """
    params = {
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": CODEX_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    if str(prompt or "").strip():
        params["prompt"] = prompt
    return f"{CODEX_AUTH_URL}?{urlencode(params)}"


def extract_code(location: str, state: str) -> str:
    """从 callback URL 里取出并校验 authorization code。"""
    query = parse_qs(urlparse(location).query)
    error = (query.get("error") or [""])[0]
    if error:
        description = (query.get("error_description") or [""])[0]
        raise RuntimeError(f"授权服务器返回错误: error={error}, desc={description}")
    code = (query.get("code") or [""])[0]
    if not code:
        raise RuntimeError(f"callback 缺少 code 参数: {location[:200]}")
    returned_state = (query.get("state") or [""])[0]
    if returned_state and returned_state != state:
        raise RuntimeError("OAuth state 不匹配（疑似 CSRF），已中止")
    return code


# ------------------------------------------------------------------ token


def _decode_jwt_segment(segment: str) -> dict:
    try:
        padding = "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment + padding))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def parse_id_token(id_token: str) -> dict:
    """解析 id_token payload（不验签，仅用于取展示字段）。"""
    parts = str(id_token or "").split(".")
    if len(parts) < 2:
        return {}
    claims = _decode_jwt_segment(parts[1])
    auth_claim = claims.get("https://api.openai.com/auth") or {}
    profile_claim = claims.get("https://api.openai.com/profile") or {}
    return {
        "email": claims.get("email") or profile_claim.get("email", ""),
        "account_id": auth_claim.get("chatgpt_account_id", ""),
        "plan_type": auth_claim.get("chatgpt_plan_type", ""),
    }


def exchange_token(code: str, code_verifier: str, *, proxy: str = "", timeout: int = 30) -> dict:
    """用 authorization code 换 token。标准 OAuth 2.0 PKCE，不需要 CPA 服务。"""
    proxies = netproxy.proxy_dict(proxy)
    try:
        response = requests.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CODEX_CLIENT_ID,
                "code": code,
                "redirect_uri": CODEX_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            proxies=proxies,
            timeout=max(10, int(timeout)),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"换 token 请求失败: {type(exc).__name__}") from exc

    if response.status_code != 200:
        raise RuntimeError(f"换 token 失败 HTTP {response.status_code}: {str(response.text or '')[:240]}")

    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("换 token 响应缺少 access_token")
    return payload


def build_credential(token_payload: dict, id_claims: dict) -> dict:
    """组装 CLIProxyAPI CodexTokenStorage 结构，便于被其它工具直接读。"""
    expires_in = int(token_payload.get("expires_in") or 0)
    now = datetime.now(timezone.utc)
    return {
        "id_token": token_payload.get("id_token", ""),
        "access_token": token_payload.get("access_token", ""),
        "refresh_token": token_payload.get("refresh_token", ""),
        "account_id": id_claims.get("account_id", ""),
        "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": id_claims.get("email", ""),
        "type": "codex",
        "expired": (now + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def save_credential(credential: dict, email: str) -> Path:
    """凭证落盘到 data/codex_credentials/<email>.json。"""
    CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", str(email or "unknown"))
    path = CREDENTIAL_DIR / f"{safe}.json"
    path.write_text(json.dumps(credential, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 编排


def run_desktop_activation(
    *,
    driver,
    record: AccountRecord,
    config: Config,
    inbox: Inbox,
    mailbox,
    on_event: EventFn | None = None,
) -> tuple[bool, str]:
    """用注册时的同一个浏览器窗口跑完整 Codex OAuth 授权。

    返回 ``(ok, note)``。异常一律转成 ``(False, 原因)``，不打断注册主流程。
    """
    emit = on_event or _noop
    sms_client = None

    try:
        sitepool_mode = str(config.sms_provider or "").strip().lower() == "sitepool"
        miaoic_mode = str(config.sms_provider or "").strip().lower() == "miaoic"
        shortcode_mode = str(config.sms_provider or "").strip().lower() == "shortcode"
        fivesim_mode = str(config.sms_provider or "").strip().lower() == "fivesim"
        longterm_mode = str(config.sms_provider or "").strip().lower() == "longterm"
        if sitepool_mode:
            # 站内只有一把「工具密钥」，邮箱和号码共用 —— /v1/pool/* 整组是同一个
            # 中间件、同一套身份解析（handler.identity() 都从 API Key 取用户），
            # 所以不存在「取邮箱的密钥」和「取号码的密钥」之分。
            #
            # ★ 这里以前写的是 `config.sitepool_sms_key or config.sitepool_mail_key`：
            #   号码那侧**优先**用号码密钥。旧配置里若留着一把没清掉的号码密钥，
            #   就会出现「邮箱拿新密钥全通、只有取号 401」的静默错位，
            #   base_url 已经换到生产时尤其难查。合并成一把后这个面彻底消失。
            sms_key = str(config.sitepool_tool_key or "").strip()
        else:
            sms_key = str(config.sms_api_key or "").strip()

        if config.enable_activation and (sms_key or shortcode_mode):
            try:
                if shortcode_mode:
                    # 「短效接码」token 页：凭证是那条链接本身，没有 API Key
                    # （一个 token 一个号、换号次数有限 ⇒ 默认复用它已有的号码）
                    sms_client = ShortCodeSmsClient(
                        str(config.shortcode_url or "").strip(),
                    )
                elif sitepool_mode:
                    base = str(config.sitepool_base or sitepool_mod.DEFAULT_BASE_URL or "").strip()
                    if not base:
                        raise RuntimeError(
                            "本站号池没配接口地址（config.json 里的 sitepool_base），"
                            "取不到号码"
                        )
                    sms_client = SitePoolSmsClient(
                        base,
                        sms_key,
                        sku=config.sitepool_phone_sku,
                    )
                else:
                    # miaoic 不是 SMS-Activate handler API：它没有 service/country/maxPrice，
                    # 也没有 activation_id（一个 Token 同时只持有一个号码）。
                    # 所以不能塞给 SmsClient，走它自己的客户端。
                    if miaoic_mode:
                        sms_client = MiaoicSmsClient(
                            sms_key,
                            base_url=str(config.miaoic_base or "").strip(),
                        )
                    elif fivesim_mode:
                        # 5SIM 是另一套 API（Bearer + REST），不能用 SmsClient
                        sms_client = FiveSimClient(
                            sms_key,
                            country=str(config.fivesim_country or "indonesia"),
                            operator=str(config.fivesim_operator or "any"),
                            product=str(config.fivesim_product or "openai"),
                        )
                    elif longterm_mode:
                        # 长效号源：裸 token + REST；**不可退**（cancel 只记日志），
                        # 所以号段（prefixes）是唯一的质量手段。
                        sms_client = LongTermSmsClient(
                            sms_key,
                            app_id=int(config.longterm_app_id or 0),
                            prefixes=str(config.longterm_prefixes or ""),
                            expiry=int(config.longterm_expiry or 0),
                            # 空 = 正常买新号；填了号 = 复用实验（不花钱）
                            pin_numbers=str(getattr(config, "longterm_pin_numbers", "") or ""),
                        )
                    else:
                        sms_client = SmsClient(
                            config.sms_provider,
                            sms_key,
                            service=config.sms_service,
                            country=config.sms_country,
                            max_price=config.sms_max_price,
                        )
            except SmsError as exc:
                logger.warning("[激活] 接码平台初始化失败：%s", exc)
                sms_client = None

        def otp_provider(
            email: str,
            after_ts: float | None = None,
            timeout: int | None = None,
            **_kwargs,
        ) -> str:
            # 授权页要的是同一张邮箱订单的最新验证码。
            # ★ timeout 必须允许调用方覆盖（2026-10-04）：写死 config.otp_timeout(180s)
            #   时，`_wait_for_fresh_email_otp(timeout=90)` 的 90s 完全不起作用，
            #   日志里表现成「外层以为等 90s、实际干等 180s」。
            return mailbox.wait_for_otp(
                inbox,
                after_ts=float(after_ts or 0),
                timeout=int(timeout or config.otp_timeout),
            )

        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(32)
        auth_url = build_authorize_url(state, challenge)

        # ★ 不再清 Cookie、也不再强制 prompt=login。
        # 注册刚结束时浏览器里就是这个账号的会话，直接开授权页即可；
        # 之前「清登录态 + prompt=login」双重强制重登，而重登分支在
        # auth.openai.com/log-in 上提交邮箱后并不前进到 OTP 页，
        # 于是验证码邮件压根不发，卡死 3 轮。真没会话时
        # _fill_email_and_otp 自己会检测到邮箱输入框并走完整登录。
        # （要恢复旧行为：build_authorize_url(state, challenge, prompt="login")
        #   并在下面调用 oauth_flow.clear_browser_auth_state(driver)。）

        # 授权页的密码分支需要注册时设置的密码。
        oauth_flow.ACCOUNT_PASSWORD = str(record.password or "")
        oauth_flow.CALLBACK_TIMEOUT = max(60, int(config.oauth_timeout))

        emit("activation", "正在打开 Codex 授权页")
        oauth_flow._fill_email_and_otp(driver, inbox.email, otp_provider, auth_url)
        check_stop()

        emit("activation", "正在处理手机号验证")

        # ★ 半自动模式：把「请人手动过验证」做成一个回调传进去。
        #   这样 oauth_flow 不用知道 config / 半自动开关的存在，默认行为一字不变。
        def _human_waiter(reason: str) -> None:
            from .registration import _wait_for_human  # 局部导入，避开循环依赖

            _wait_for_human(
                driver,
                emit=emit,
                config=config,
                reason=reason,
                # 这一阶段的「就绪」= 能认出手机号表单或验证码页，**不是**登录页
                probe=lambda d: "ok" if (
                    oauth_flow._has_strict_add_phone_form(d)
                    or oauth_flow._is_phone_code_page(d)
                ) else "challenge",
                want=("ok",),
            )

        oauth_flow._do_phone_verification_if_present(
            driver, sms_client,
            max_retries=max(1, int(config.sms_max_retries)),
            code_wait=max(30, int(config.sms_code_wait)),
            human_waiter=_human_waiter if config.semi_auto else None,
        )
        check_stop()

        # ★ 2026-10-17：把「本单用掉的那个手机号」落进账号记录。
        #   原因：长效号源的长效号不可退、有效期 ~22 天，若能复用就近乎零边际成本；
        #   要验证「同一个号能不能吃第二单」就必须先有「号 ↔ 账号」的对应关系。
        phone_used = str(getattr(driver, "_grl_phone_accepted", "") or "")
        if phone_used and not record.phone:
            record.phone = phone_used
            logger.info("[激活] 本单手机号已记录进账号记录：%s", phone_used)

        emit("activation", "正在等待授权确认")
        callback_url = oauth_flow._finish_consent_workspace(driver)
        code = extract_code(callback_url, state)

        emit("activation", "正在换取 Codex 凭证")
        token_payload = exchange_token(code, verifier, proxy=config.proxy)
        id_claims = parse_id_token(str(token_payload.get("id_token") or ""))
        credential = build_credential(token_payload, id_claims)
        path = save_credential(credential, id_claims.get("email") or inbox.email)

        # 回写到账号记录，导出时一起带走。
        record.refresh_token = str(credential.get("refresh_token") or "")
        record.id_token = str(credential.get("id_token") or "")
        if credential.get("account_id"):
            record.account_id = str(credential["account_id"])

        plan = id_claims.get("plan_type") or "unknown"
        logger.info("[激活] 成功，凭证已保存: %s", path)
        return True, f"已激活 · 套餐 {plan} · 凭证 {path.name}"

    except StopRequested:
        return False, "用户停止"
    except Exception as exc:
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.warning("[激活] 失败：%s", message)
        return False, message
