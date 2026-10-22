# -*- coding: utf-8 -*-
"""Codex OAuth 授权页面的浏览器动作（提炼自 turb-gpt-free-register/core/roxy_codex_oauth.py）。

只负责「在授权页上点、填、等」，不负责：浏览器启动、取码、换 token、凭证落库。
流程编排见 core/oauth.py。

被剥离/注入的外部依赖：
    config.roxybrowser                  -> 本地常量 CALLBACK_TIMEOUT
    core.codex_oauth._account_registration_password -> 本地 ACCOUNT_PASSWORD 变量
    core.codex_oauth._account_totp_code -> 置空（本项目注册流程不设置 2FA）
    core.sms_provider (L/H 自建取号)     -> core.sms.SmsClient，由调用方注入
    core.roxy_registration              -> core.browser_flow

★ 2026-10-06：**本文件现在是唯一源，可以直接改。**

  原来这里写着「请勿手工编辑本文件；改动请改 tools/extract_codex_oauth.py 后重新生成」——
  **那句话已经作废**。本文件后来被手工改得比生成器新得多：生成器只能产出 1373 行，
  比这里少 900 多行，跑一次会抹掉 `_TOTP_REGISTRY`（2FA 密钥注册表）、`check_stop()`
  （界面「停止」按钮）、通道/号码收敛循环、`fraud_guard` 止损等一整批修复。

  生成器已改名为 `tools/extract_codex_oauth.py.retired`，并加了写盘闸门。
  来龙去脉见 `docs/20261006-维护记录.md`。
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import traceback
from urllib.parse import urlparse

# ★ 只为了「按类型」识别「上游没有号码」这个条件（见 _do_phone_verification_if_present
#   里的止损判断）。单靠关键字匹配会被号池的文案改动骗过 —— 2026-10-06 实测踩过：
#   ACCOUNT_POOL_UPSTREAM_BUSY 的人话是「上游暂时没有可用资源，稍后重试」，
#   关键字表里一个词都不命中，于是 12 次重试全打在空气上。
from .sms import SmsNoNumbers

from .browser_flow import (
    _auth_error_page_reason,
    _click_any,
    _click_continue,
    _click_email_entry_option,
    _click_passwordless_signup_if_present,
    _clear_otp_inputs,
    _dump_clickables,
    _email_otp_page_state,
    _find_any,
    _human_click,
    _human_type_text,
    _is_email_verification_page,
    _is_login_password_page,
    _maybe_accept,
    _submit_email_step,
    _set_otp_value_via_js,
    _type_any,
    _type_email_address,
    _type_otp,
)
from .humanize import delay as human_delay

logger = logging.getLogger(__name__)

# 原 config.roxybrowser.ROXY_CODEX_CALLBACK_TIMEOUT
CALLBACK_TIMEOUT: int = 90

# 手机验证码提交后页面「没前进也没报错」时，强制同步 + 重试提交几次再判失败。
# 2 次是实测经验：真没被接受的号重试多少次都一样，而 React 抢跑那类问题一次就够。
PHONE_OTP_SUBMIT_RETRIES: int = 2

# 注册流程中设置的 OpenAI 密码；注册后立刻激活时由调用方注入。
ACCOUNT_PASSWORD: str = ""

# OpenAI 判定账号已废的错误码。
ACCOUNT_DEAD_CODES = frozenset({"account_deactivated", "account_deleted", "account_banned"})

# add-phone 页写号码：回读不一致时最多重写几次。
# 页面会在 input 事件里同步重排号码（偶尔吃掉最后一位），一次不一致就换号太浪费。
PHONE_SET_ATTEMPTS = 3


class AccountUnusableError(Exception):
    """邮箱对应的 OpenAI 账号已废（删除/停用/封禁），重试也是同样结果。"""

    def __init__(self, message: str, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


def detect_account_unusable_response_body(body: str) -> str:
    """从接口响应 JSON 的 error.code 识别账号已废（不是页面文字识别）。"""
    try:
        payload = json.loads(body or "")
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    code = ""
    if isinstance(err, dict):
        code = str(err.get("code") or "")
    else:
        code = str(payload.get("code") or payload.get("error_code") or "")
    return code if code in ACCOUNT_DEAD_CODES else ""


def _account_password(email: str) -> str:
    """本次注册设置的 OpenAI 密码，用于授权页的密码登录分支。"""
    return str(ACCOUNT_PASSWORD or "")


# --------------------------------------------------------- 本次注册的 TOTP 密钥注册表
#
# 为什么需要：
#   `mfa/enroll` 要求「认证后 240 秒内」才允许 —— 前端原码里的 `VVe = 240`，
#   判定依据是 accessToken 里的 `pwd_auth_time`（见 core/account_api.py 的长注释）。
#   而我们的流程「注册认证 → 手机激活」要 5~10 分钟，
#   **2FA 放在激活之后必然超窗**（实测相隔 562 秒 ⇒ 401 recent_auth_required）。
#
#   所以 2FA 必须**提前到「拿到 accessToken 之后、激活之前」**。
#   代价是激活那套 OAuth 登录会遇到 MFA 挑战 ——
#   而 `_is_mfa_challenge_page` / `_fill_mfa_challenge_if_present` 早就写好了
#   检测+填码+提交，**缺的只是「密钥从哪来」**，也就是下面这个注册表。
#
# ★ 必须 threading.local：小工具支持并发（concurrency>1），每个任务一个线程。
#   模块级全局会被别的线程覆盖 —— `REGISTER_PASSWORD` 就踩过这个坑
#   （记下来的密码 ≠ 实际设进去的密码，而且完全不会报错）。
_TOTP_REGISTRY = threading.local()


def register_account_totp(email: str, secret: str) -> None:
    """把「本轮刚绑好的 TOTP 密钥」登记进来，供后续激活流程填 MFA 挑战用。"""
    key = str(email or "").strip().lower()
    val = str(secret or "").strip().upper().replace(" ", "")
    if not key or not val:
        return
    by_email = getattr(_TOTP_REGISTRY, "by_email", None)
    if by_email is None:
        by_email = {}
        _TOTP_REGISTRY.by_email = by_email
    by_email[key] = val


def _registered_totp_secret(email: str) -> str:
    by_email = getattr(_TOTP_REGISTRY, "by_email", None) or {}
    return by_email.get(str(email or "").strip().lower(), "")


def _account_totp_code_for_email(email: str) -> str:
    """按邮箱算出当前 6 位 TOTP 码；没有密钥就返回空串。

    ★ 原来这里是 `return ""` 的死桩（注释写着「本项目注册流程不设置 2FA」）——
      那是**不绑 2FA 的时代**留下的。现在要绑了，就得真算。
      RFC 6238 实现复用 `security_setup.totp_now`（已对过官方测试向量）。
    """
    secret = _registered_totp_secret(email)
    if not secret:
        return ""
    try:
        from .security_setup import totp_now

        return str(totp_now(secret))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[激活] 算 TOTP 失败：%s", exc)
        return ""


def _is_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.hostname in ("localhost", "127.0.0.1")
        and parsed.port == 1455
        and parsed.path == "/auth/callback"
    )


def _extract_callback_url_from_page(driver) -> str:
    """从当前页面提取 OAuth callback URL。

    浏览器跳转到 http://localhost:1455/auth/callback?... 时，本地没有服务监听会显示
    chrome-error://chromewebdata/。地址栏可能变成 chrome-error，但 Chromium 的
    performance navigation entry 仍保留原始 callback URL，可直接提取后提交 CPA。
    """
    try:
        current = str(driver.current_url or "")
        if _is_callback_url(current):
            return current
    except Exception:
        pass
    try:
        urls = driver.execute_script(r"""
        const out = [];
        const push = v => { if (v && typeof v === 'string') out.push(v); };
        try { push(location.href); } catch (e) {}
        try { push(document.URL); } catch (e) {}
        try { push(document.documentURI); } catch (e) {}
        try { for (const e of performance.getEntriesByType('navigation')) push(e.name); } catch (e) {}
        try { for (const e of performance.getEntries()) push(e.name); } catch (e) {}
        return [...new Set(out)];
        """) or []
        for url in urls:
            if _is_callback_url(str(url)):
                logger.info("[激活] 已从浏览器性能记录提取 callback URL：%s", str(url)[:160])
                return str(url)
    except Exception as exc:
        logger.debug("[激活] 从页面提取 callback URL 失败：%s", exc)
    return ""


def _extract_callback_url_from_any_window(driver) -> str:
    found = _extract_callback_url_from_page(driver)
    if found:
        return found
    try:
        for handle in list(getattr(driver, "window_handles", []) or []):
            try:
                driver.switch_to.window(handle)
                found = _extract_callback_url_from_page(driver)
                if found:
                    return found
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _wait_for_callback(driver, timeout: int | None = None) -> str:
    end = time.time() + (timeout or int(CALLBACK_TIMEOUT))
    last_url = ""
    while time.time() < end:
        try:
            current = str(driver.current_url or "")
            if current != last_url:
                logger.debug("[激活] 当前 URL: %s", current)
                last_url = current
            callback = _extract_callback_url_from_any_window(driver)
            if callback:
                return callback
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"等待 Codex callback 超时，最后 URL={last_url}")


def _click_if_present(driver, selectors: list[str], timeout: int = 3) -> bool:
    try:
        _click_any(driver, selectors, timeout=timeout)
        return True
    except Exception:
        return False


def _maybe_click_passwordless_after_email(driver, email: str, timeout: int = 18) -> str:
    """在密码页点「用一次性验证码登录」入口。返回 'otp_page' / 'clicked' / 'left_flow' / 'timeout'。

    ★ 2026-10-04 改：老版本认不到入口时**什么都不说**，调用方接着就去等
      一封「从没被请求过的」验证码邮件，白烧 180s×3。现在认不到就 dump 页面属性。
    """
    end = time.time() + timeout
    last_url = ""
    clicked = False
    while time.time() < end:
        try:
            if _is_email_verification_page(driver):
                if clicked:
                    logger.info("[激活] 一次性验证码入口已进入邮箱验证码页")
                return "otp_page"
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[激活] 页面跳转：url=%s", url or "-")
                last_url = url
            lower = url.lower()
            # ★ 这里以前写的是 `"authorize"`（子串），而 OAuth 授权地址本身就含
            #   `/authorize` —— 只要当前 URL 还停在授权地址上就会立刻 return，
            #   等于整段逻辑空转。收窄成 `/authorize` 路径才算「已离开登录流程」。
            if any(x in lower for x in ("phone", "workspace", "consent", "localhost:1455")):
                logger.info("[激活] 已离开登录流程：%s（不需要邮箱码）", url)
                return "left_flow"
            if "/password" in lower or "auth.openai.com" in lower:
                result = _click_passwordless_signup_if_present(driver)
                if result.get("ok"):
                    clicked = True
                    logger.info("[激活] 已点击一次性验证码入口：email=%s detail=%s", email, result)
                    human_delay("form")
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("[激活] 密码页一次性验证码入口探测失败：%s", str(exc)[:140])
        time.sleep(0.5)
    if clicked:
        logger.info("[激活] 已点击一次性验证码入口，但未立即进入验证码页")
        return "clicked"
    logger.warning("[激活] 在密码页找不到「一次性验证码登录」入口，dump 页面属性供定位")
    _dump_clickables(driver, "激活-密码页找不到一次性验证码入口")
    return "timeout"


def _wait_for_otp_input(driver, timeout: int = 30) -> None:
    """验证码已收到但 OTP 输入框可能尚未出现（点完一次性验证码后常有中间页/延迟渲染）。

    等待期间若仍停留在登录密码页，则补点一次性验证码入口（最多 2 次、间隔 6s）；
    超时仍未出现时打印页面状态便于定位。
    """
    end = time.time() + timeout
    passwordless_retries = 0
    while time.time() < end:
        if _is_email_verification_page(driver):
            return
        if _is_login_password_page(driver) and passwordless_retries < 2:
            passwordless_retries += 1
            result = _click_passwordless_signup_if_present(driver)
            if result.get("ok"):
                logger.info("[激活] 仍停留登录密码页，补点一次性验证码入口：%s", result.get("reason"))
                human_delay("form")
            time.sleep(6)
            continue
        time.sleep(0.8)
    state = _email_otp_page_state(driver)
    logger.warning(
        "[激活] 等待 OTP 输入框超时，页面 url=%s inputs=%s buttons=%s 文本前300字=%s",
        str(state.get("url") or ""),
        len(state.get("inputs") or []),
        [(b.get("text") or "")[:24] for b in (state.get("buttons") or [])][:8],
        str(state.get("text") or "")[:300],
    )
    raise RuntimeError("等待 OTP 输入框超时，页面未出现验证码输入框")


def _account_password_for_email(email: str) -> str:
    try:
        return _account_password(email)
    except Exception:
        return ""


# ★ 这里原本还有**第二个** `_account_totp_code_for_email`，函数体是 `return ""`。
#   它定义在后面，把上面那个实现**静默覆盖**掉了 —— 典型的「先定义后覆盖」坑，
#   也正是「以为有 TOTP 支持、实际永远填不进去」的根因。已删除。


def _is_mfa_challenge_page(driver) -> bool:
    try:
        url = str(driver.current_url or "").lower()
        if "/mfa-challenge/" in url or "/mfa-challenge" in url:
            return True
        state = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const form = [...document.querySelectorAll('form')].find(f => /\/mfa-challenge/i.test(f.getAttribute('action') || ''));
        const input = form ? [...form.querySelectorAll('input[name="code"], input[autocomplete="one-time-code"], input[maxlength="6"]')].find(visible) : null;
        return {ok: !!(form && input), url: location.href};
        """) or {}
        return bool(state.get("ok"))
    except Exception:
        return False


def _fill_mfa_challenge_if_present(driver, email: str, timeout: int = 15) -> bool:
    """如果当前进入 MFA challenge 页面，自动填入账号 TOTP 并提交。"""
    code = _account_totp_code_for_email(email)
    if not code:
        return False
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not _is_mfa_challenge_page(driver):
                time.sleep(0.4)
                continue
            # ★ 和密码分支同一个坑：只在 JS 里**打标记**，不把 DOM 元素返回给 Python。
            #   execute_script 返回值里的 DOM 元素会被序列化成普通对象，拿回来再调
            #   scrollIntoView()/click() 会报 "el.scrollIntoView is not a function"。
            #   这里原来就是 `return {ok:true, input, button}`，而外层 except 是
            #   **debug 级**，异常被彻底淹没 —— 于是 MFA 永远填不进去、激活一直卡在
            #   挑战页直到超时，且日志里什么都看不到（2026-10-11 一并修掉）。
            marked = driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
              && !el.disabled && !el.readOnly;
            document.querySelectorAll('[data-grl-mfa-target]').forEach(el => el.removeAttribute('data-grl-mfa-target'));
            const form = [...document.querySelectorAll('form')].find(f => /\/mfa-challenge/i.test(f.getAttribute('action') || ''));
            if (!form) return {ok:false, reason:'missing_form'};
            const input = [...form.querySelectorAll('input[name="code"], input[autocomplete="one-time-code"], input[maxlength="6"]')].find(visible);
            if (!input) return {ok:false, reason:'missing_code_input'};
            const button = [...form.querySelectorAll('button[type="submit"], button[data-dd-action-name="Continue"], button')].find(visible);
            if (!button) return {ok:false, reason:'missing_submit'};
            input.setAttribute('data-grl-mfa-target', 'input');
            button.setAttribute('data-grl-mfa-target', 'submit');
            return {ok:true};
            """) or {}
            if not marked.get("ok"):
                time.sleep(0.4)
                continue
            try:
                from selenium.webdriver.common.by import By
                code_input = driver.find_element(By.CSS_SELECTOR, '[data-grl-mfa-target="input"]')
                code_submit = driver.find_element(By.CSS_SELECTOR, '[data-grl-mfa-target="submit"]')
            except Exception as exc:  # noqa: BLE001
                logger.warning("[激活] 按标记取 MFA 输入/提交元素失败：%s", str(exc)[:140])
                time.sleep(0.5)
                continue
            if not _set_value_verified(driver, code_input, code, label="MFA 验证码"):
                logger.warning("[激活] MFA 验证码没能可靠填入，本轮不提交（避免白烧一次尝试）")
                time.sleep(0.5)
                continue
            human_delay("otp_input")
            _human_click(driver, code_submit, label="codex_mfa_submit")
            logger.info("[激活] 已填写并提交 MFA 验证码：%s", email)
            wait_end = time.time() + 12
            while time.time() < wait_end:
                if not _is_mfa_challenge_page(driver):
                    return True
                time.sleep(0.4)
            return True
        except Exception as exc:
            # ★ 原来是 debug 级：代码级错误会被彻底淹没（2026-10-11 教训：
            #   DOM 元素跨边界那类 TypeError 就是这么藏了 5 天的）。
            logger.warning("[激活] MFA challenge 处理失败：%s", str(exc)[:160])
            time.sleep(0.5)
    return False


def _set_value_verified(driver, el, value: str, *, label: str = "输入值") -> bool:
    """把 ``value`` 写进输入框并**回读校验**；键盘打字对不上就用 JS 原生 setter 重写一次。

    ★ 为什么必须回读：密码和验证码都会被 React 受控输入抢跑
      （验证码实测被抢跑成 ``169479`` → ``169497``，见 ``_human_type_text`` 的注释）。
      密码打错的后果更重：登录失败，还可能把账号锁掉。
      所以这里不信任「打过了」——一律回读；不一致就用 nativeInputValueSetter
      直接写值并派发 input/change（和 ``_set_otp_value_via_js`` 同一套路）。
    """
    def _read() -> str:
        try:
            return str(el.get_attribute("value") or "")
        except Exception:  # noqa: BLE001
            return ""

    try:
        # chunk=False：一律逐字符，绝不允许「两个字符一组」。
        _human_type_text(driver, el, value, clear=True, chunk=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[激活] %s键盘输入失败，改走 JS 写入：%s", label, str(exc)[:140])

    current = _read()
    if current == value:
        return True

    logger.warning("[激活] %s回读不一致（期望 %d 位，实际 %d 位），改用 JS 原生 setter 重写",
                   label, len(value), len(current))
    try:
        driver.execute_script(r"""
        const el = arguments[0], v = arguments[1];
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(el, v); else el.value = v;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        """, el, value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[激活] JS 写入%s失败：%s", label, str(exc)[:140])
        return False
    return _read() == value


def _fill_login_password_if_present(driver, email: str, timeout: int = 18) -> str | None:
    """Codex OAuth 若账号有密码，优先在登录密码页输入密码。返回 next_step / email_otp / None。"""
    password = _account_password_for_email(email)
    if not password:
        return None
    end = time.time() + timeout
    while time.time() < end:
        if _is_email_verification_page(driver):
            return "email_otp"
        if not _is_login_password_page(driver):
            time.sleep(0.4)
            continue
        # ★ 只在 JS 里**打标记**，绝不把 DOM 元素返回给 Python。
        #   execute_script 的返回值里 DOM 元素会被序列化成普通对象，
        #   拿回来再调 scrollIntoView()/click() 只会报
        #   "el.scrollIntoView is not a function"。
        #   2026-10-11 实测：密码分支就是这么把整个激活搞挂的（异常还被外层
        #   过宽的 except 吞成「未检测到邮箱输入框」，静默放弃激活）。
        marked = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        document.querySelectorAll('[data-grl-pw-target]').forEach(el => el.removeAttribute('data-grl-pw-target'));
        const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="current-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
          .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_submit'};
        input.setAttribute('data-grl-pw-target', 'input');
        buttons[0].el.setAttribute('data-grl-pw-target', 'submit');
        buttons[0].el.scrollIntoView({block:'center'});
        return {ok:true, reason:'password_targets'};
        """) or {}
        if not marked.get("ok"):
            logger.info("[激活] 登录密码页未找到输入/提交按钮：%s", marked)
            time.sleep(0.5)
            continue
        # 用 Selenium 按标记取**真正的 WebElement**（不是 JS 返回的序列化对象）。
        # 标记会被下一轮 JS 开头的清理语句去掉，这里不用管残留。
        try:
            from selenium.webdriver.common.by import By
            password_input = driver.find_element(By.CSS_SELECTOR, '[data-grl-pw-target="input"]')
            password_submit = driver.find_element(By.CSS_SELECTOR, '[data-grl-pw-target="submit"]')
        except Exception as exc:  # noqa: BLE001
            logger.info("[激活] 按标记取密码输入/提交元素失败：%s", str(exc)[:140])
            time.sleep(0.5)
            continue
        if not _set_value_verified(driver, password_input, password, label="登录密码"):
            # ★ 填不对就**不提交**：打错密码只会白烧一次尝试，还可能把账号锁掉。
            logger.warning("[激活] 密码没能可靠填入登录框，本轮不提交，等页面变化后重试")
            time.sleep(1.0)
            continue
        human_delay("form", minimum=2.0, maximum=3.6)
        _human_click(driver, password_submit, label="codex_password_submit")
        logger.info("[激活] 已填写并提交登录密码：%s", email)
        wait_end = time.time() + 12
        while time.time() < wait_end:
            if _is_mfa_challenge_page(driver):
                _fill_mfa_challenge_if_present(driver, email, timeout=15)
                return "next_step"
            if _is_email_verification_page(driver):
                return "email_otp"
            if not _is_login_password_page(driver):
                return "next_step"
            time.sleep(0.5)
        return "next_step"
    return None


def _on_choose_account_page(driver) -> bool:
    try:
        return "choose-an-account" in str(getattr(driver, "current_url", "") or "")
    except Exception:  # noqa: BLE001
        return False


def _click_account_chooser_if_present(driver, email: str, timeout: int = 25) -> bool:
    """授权页出现「选择账号」（choose-an-account）时，点我们要的那个账号。

    复用注册会话时不再走邮箱登录，授权页会直接给出「欢迎回来 + 选账号」。
    以前这里没人点，流程就停在那一页等到 callback 超时。

    ★ 点完必须确认页面真的走了。这个页面是 React 渲染的，JS 的 `.click()`
      有时不触发它的跳转，而老代码点完就 `return True`，后面一路等到回调超时
      （2026-10-03 实测：手机步骤因为「页面还停在 choose-an-account」被整个跳过）。
      现在：点完轮询确认；没走就换真实点击再试，最多几轮。
    """
    from selenium.webdriver.common.by import By

    want = str(email or "").strip().lower()
    if not want:
        return False
    deadline = time.time() + max(6, int(timeout))
    clicked_text = ""
    round_no = 0

    while time.time() < deadline:
        if not _on_choose_account_page(driver):
            # 已经不在选账号页了：如果之前点过，说明成功了
            return bool(clicked_text)
        round_no += 1

        # 第 1 轮用 JS 点（快）；之后用真实点击（React 更认）
        if round_no == 1:
            try:
                clicked_text = str(driver.execute_script(r"""
                const want = (arguments[0] || '').toLowerCase();
                const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const cands = [...document.querySelectorAll('a,button,[role=button],[role=link],[tabindex]')]
                  .filter(visible)
                  .filter(el => (el.innerText || el.textContent || '').toLowerCase().includes(want));
                if (!cands.length) return '';
                const target = cands[cands.length - 1];
                target.click();
                return (target.innerText || target.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120);
                """, want) or "")
                if clicked_text:
                    logger.info("[激活] 在选择账号页点了账号：%s", clicked_text)
            except Exception as exc:  # noqa: BLE001
                logger.info("[激活] 选择账号 JS 点击失败：%s", str(exc)[:120])
        else:
            try:
                # 找「文字里带这个邮箱」的元素，从最内层往外试真实点击
                xp = ("//*[contains(translate(text(), "
                      "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                      f"'{want}')]")
                els = driver.find_elements(By.XPATH, xp)
                clicked_real = False
                for el in reversed(els):
                    try:
                        el.click()
                        clicked_real = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
                if clicked_real:
                    logger.info("[激活] 用真实点击又点了一次账号（第 %s 轮）", round_no)
            except Exception as exc:  # noqa: BLE001
                logger.info("[激活] 选择账号真实点击失败：%s", str(exc)[:120])

        # 等页面反应，最多 5 秒
        for _ in range(10):
            time.sleep(0.5)
            if not _on_choose_account_page(driver):
                logger.info("[激活] 已离开选择账号页（第 %s 轮点击后）", round_no)
                return True

    logger.warning("[激活] 选了账号但页面一直停在 choose-an-account（点了 %s 轮）", round_no)
    return bool(clicked_text)


def _wait_for_post_email_page(driver, timeout: int = 45) -> str:
    """提交邮箱后等页面**真正落位**，返回 'otp' / 'password' / 'next' / 'timeout'。

    ★★ 为什么必须这么等（2026-10-04 实测，真实 DOM）：

      在 `/log-in` 提交邮箱后，页面要 **12 秒以上**才跳到 `/log-in/password`：

        +4s  → https://auth.openai.com/log-in
        +8s  → https://auth.openai.com/log-in
        +12s → https://auth.openai.com/log-in/password

      而「用一次性验证码登录」那个入口**只在 password 页上**：

        <BUTTON name='intent' value='passwordless_login_send_otp'>

      老代码提交完只等 ~3 秒就认定「已到验证码页」，接着去等一封
      **从没被请求过的**邮件，白烧 180s×3（`avaallen886138` 就是这么死的）。
      所以这里按**页面落位**判断，不按时间猜。
    """
    end = time.time() + max(1, int(timeout))
    last_url = ""
    while time.time() < end:
        # ★ 先判错误页：这类页面 **URL 不变**，只看 URL 会误判成「还在密码页」。
        try:
            err = _auth_error_page_reason(driver)
        except Exception:  # noqa: BLE001
            err = ""
        if err:
            logger.warning("[激活] 页面是 OpenAI 的错误页：%s", err)
            return "error"
        try:
            url = str(driver.current_url or "")
        except Exception:  # noqa: BLE001
            url = ""
        if url != last_url:
            logger.info("[激活] 页面落位中：url=%s", url or "-")
            last_url = url
        lower = url.lower()
        try:
            if _is_email_verification_page(driver):
                return "otp"
        except Exception:  # noqa: BLE001
            pass
        try:
            if _is_login_password_page(driver):
                return "password"
        except Exception:  # noqa: BLE001
            pass
        # 已经走到授权/同意/选账号/手机号 = 不需要邮箱码了
        if any(x in lower for x in ("consent", "workspace", "phone", "localhost:1455")):
            return "next"
        if "/create-account/password" in lower or "/log-in/password" in lower:
            return "password"
        time.sleep(0.5)
    return "timeout"


def _fill_email_and_otp(driver, email: str, otp_provider, auth_url: str) -> None:
    otp_after_ts = time.time()
    logger.info("[激活] 打开授权地址")
    logger.info("[激活] 完整授权地址: %s", auth_url)
    driver.get(auth_url)
    human_delay("navigate")
    logger.info("[激活] 授权页加载完成，检查是否需要邮箱登录")
    _maybe_accept(driver)

    # ★ 复用注册会话时，授权页会直接给「选择账号」而不是邮箱登录页。
    #   这里必须点一下账号，否则页面不会走到 consent，callback 永远不来。
    if _click_account_chooser_if_present(driver, email):
        logger.info("[激活] 已选择账号，跳过邮箱登录")
        return

    # ★ 2026-10-16 老板实测：「碰到人机验证我点一下刷新就正常了」。
    #   选账号页点不动时也先刷新再点一轮 —— 以前直接往下走，接着就是
    #   「未检测到手机号验证页 → 跳过手机步骤」→ 干等回调超时，
    #   而页面后来其实自己走到了 add-phone（白白废掉一单）。
    if _on_choose_account_page(driver):
        from .browser_flow import reload_and_settle as _reload_and_settle
        _reload_and_settle(driver, reason="选择账号页点了 5 轮仍没离开")
        if _click_account_chooser_if_present(driver, email):
            logger.info("[激活] 刷新后已选择账号，跳过邮箱登录")
            return

    # 可能已经处于账号选择/授权页；如果有邮箱输入框则完整登录。
    # 非日本出口时按钮文案/顺序会变，不能按可见文字点“继续”，否则可能误点 Google。
    try:
        _type_email_address(driver, email, timeout=12)
        logger.info("[激活] 已填写邮箱：%s", email)
        human_delay("form")
        _submit_email_step(driver)
        logger.info("[激活] 已提交邮箱，等待邮箱 OTP 页面")
        pw_result = _fill_login_password_if_present(driver, email, timeout=18)
        if pw_result == "next_step":
            if _is_mfa_challenge_page(driver):
                _fill_mfa_challenge_if_present(driver, email, timeout=15)
            logger.info("[激活] 账号已用密码完成登录，直接进入后续步骤")
            return
        if pw_result == "email_otp":
            logger.info("[激活] 密码登录后仍进入邮箱 OTP 页面")
        else:
            # ★★ 先等**页面落位**，再去找入口。
            #   实测在 /log-in 提交邮箱后要 12s+ 才跳到 /log-in/password，
            #   而「一次性验证码登录」入口只在 password 页上。
            #   老代码提交完就凭 3 秒的判断去等邮件 → 等一封从没被请求过的邮件，
            #   180s×3 全白烧。
            landed = _wait_for_post_email_page(driver, timeout=45)
            logger.info("[激活] 提交邮箱后页面落位：%s", landed)
            if landed == "password":
                status = _maybe_click_passwordless_after_email(driver, email, timeout=20)
                logger.info("[激活] 一次性验证码入口处理结果：%s", status)
                landed = _wait_for_post_email_page(driver, timeout=25)
                logger.info("[激活] 点完入口后页面落位：%s", landed)
            if landed == "timeout":
                logger.warning("[激活] 提交邮箱后页面既没到密码页也没到验证码页")
                _dump_clickables(driver, "激活-提交邮箱后未落位")
    except Exception as exc:
        # ★ 必须区分两种情况，绝不能一律当成「页面上没有邮箱输入框」：
        #   - 预期情况：页面已经不是邮箱登录页（已登录 / 已在下一步）→ 跳过邮箱登录
        #   - 代码级错误（TypeError / AttributeError / ...）→ 真 bug，必须抛出去
        #   2026-10-11 实测踩过：密码分支把 JS 返回的 DOM 元素当普通对象用，抛
        #   TypeError，被这里吞成「未检测到邮箱输入框，可能已登录或进入下一步」并
        #   return —— 激活静默放弃，而界面显示「正在等待授权确认」，
        #   看起来像卡住，实际什么都没在跑，极难排查。
        if isinstance(exc, (TypeError, AttributeError, NameError, KeyError, IndexError)):
            logger.error("[激活] 邮箱/密码登录步骤出现代码级错误，不再静默放弃：\n%s",
                         traceback.format_exc())
            raise
        logger.info("[激活] 未检测到邮箱输入框，可能已登录或进入下一步：%s", str(exc)[:120])
        return

    # 提交邮箱后不再执行任何全局“继续/授权/分支”兜底点击；后续只等待验证码页。
    # 避免页面已进入 OAuth consent 时误点授权按钮。

    used_codes: set[str] = set()
    max_otp_attempts = 3

    def _restart_email_otp_flow(reason: str) -> None:
        """Codex Auth 上直接点 resend 可能触发服务端 500；这里改为重新打开授权地址并提交邮箱。"""
        nonlocal otp_after_ts
        logger.info("[激活] 重新触发邮箱 OTP：%s", reason)
        otp_after_ts = time.time()
        driver.get(auth_url)
        human_delay("navigate")
        _maybe_accept(driver)
        try:
            _type_email_address(driver, email, timeout=12)
            human_delay("form")
            _submit_email_step(driver)
            logger.info("[激活] 已重新提交邮箱触发 OTP")
            pw_result = _fill_login_password_if_present(driver, email, timeout=12)
            if pw_result == "next_step":
                if _is_mfa_challenge_page(driver):
                    _fill_mfa_challenge_if_present(driver, email, timeout=15)
                logger.info("[激活] 重新提交邮箱后已用密码完成登录，进入后续步骤")
                return
            if pw_result != "email_otp":
                _maybe_click_passwordless_after_email(driver, email, timeout=12)
        except Exception as exc:
            # 如果重进授权地址后已经停在验证码/下一步页面，就不要再强行提交。
            if not _is_email_verification_page(driver):
                logger.warning("[激活] 重新提交邮箱失败，继续按当前页面轮询：%s", str(exc)[:180])
            else:
                logger.info("[激活] 重开授权后已在邮箱 OTP 页面")
        human_delay("api")

    saw_otp_page = False
    for otp_attempt in range(1, max_otp_attempts + 1):
        # ★★ 关键闸门（2026-10-04）：不在「会发验证码」的页面上，就**绝不去等邮件**。
        #   等一封从没被请求过的邮件 = 纯空等，而且现象和「邮件没来」一模一样，
        #   事后完全查不出真因（avaallen886138 白烧了 180s×3）。
        if _is_email_verification_page(driver):
            saw_otp_page = True
        elif not saw_otp_page:
            kind = _wait_for_post_email_page(driver, timeout=30)
            logger.info("[激活] 第 %s 次等码前页面落位：%s", otp_attempt, kind)
            if kind == "otp":
                saw_otp_page = True
            elif kind == "error":
                # 服务端明确给了错误页 —— 这不是「等一等就好」，重试同一个入口没意义。
                reason = _auth_error_page_reason(driver) or "未知错误页"
                logger.warning("[激活] OpenAI 拒绝了这次登录请求：%s", reason)
                _dump_clickables(driver, "激活-服务端错误页")
                raise RuntimeError(
                    f"激活时 OpenAI 返回错误页（{reason}），邮箱验证码不会被发出，不再空等"
                )
            else:
                logger.warning(
                    "[激活] 当前不在邮箱验证码页（落位=%s），拒绝空等验证码 —— 邮件根本没被请求过",
                    kind,
                )
                _dump_clickables(driver, "激活-拒绝空等验证码")
                raise RuntimeError(
                    f"激活未能进入邮箱验证码页（页面落位={kind}），拒绝空等验证码"
                )
        logger.info("[激活] 等待邮箱 OTP：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
        try:
            code = _wait_for_fresh_email_otp(
                otp_provider,
                email,
                after_ts=otp_after_ts,
                used_codes=used_codes,
                timeout=90,
            )
        except Exception as exc:
            if otp_attempt >= max_otp_attempts:
                raise
            logger.warning(
                "[激活] 一直未收到邮箱 OTP，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                otp_attempt + 1,
                max_otp_attempts,
                type(exc).__name__,
                str(exc)[:180],
            )
            _restart_email_otp_flow("等待验证码超时，避免点击 resend 导致 500")
            continue
        used_codes.add(str(code))
        logger.info("[激活] 邮箱 OTP 已收到（已隐藏）")
        _wait_for_otp_input(driver, timeout=30)
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        logger.info("[激活] 已填写邮箱 OTP")
        human_delay("otp_input")
        _install_email_otp_validate_hook(driver)
        clicked = _click_if_present(driver, [
            "button[type='submit']",
            "//button[contains(., 'Continue')]",
            "//button[contains(., '继续')]",
            "//button[contains(., 'Verify')]",
            "//button[contains(., '验证')]",
        ], timeout=8)
        if clicked:
            logger.info("[激活] 已提交邮箱 OTP，等待后续授权/手机号页面")
        else:
            logger.info("[激活] 未找到显式提交按钮，继续等待页面状态")

        outcome = _wait_after_email_otp_submit(driver, timeout=45)
        logger.info("[激活] 邮箱 OTP 提交后状态：%s", outcome)
        if _is_mfa_challenge_page(driver):
            _fill_mfa_challenge_if_present(driver, email, timeout=15)
            return
        if outcome == "accepted":
            return
        if str(outcome).startswith("deactivated:"):
            error_code = str(outcome).split(":", 1)[1] or "account_deactivated"
            raise AccountUnusableError(f"账号已废（{error_code}）", error_code=error_code)

        if otp_attempt >= max_otp_attempts:
            raise RuntimeError("Codex 邮箱验证码连续错误/过期，已达到最大重试次数")

        logger.warning(
            "[激活] 邮箱验证码错误/过期或页面未跳转，准备重新发送并重新获取最新验证码（%s/%s）",
            otp_attempt + 1,
            max_otp_attempts,
        )
        _restart_email_otp_flow("验证码错误/过期或页面未跳转，避免点击 resend 导致 500")



def _wait_for_fresh_email_otp(otp_provider, email: str, after_ts: float, used_codes: set[str] | None = None, timeout: int = 90) -> str:
    """获取一个未提交过的邮箱 OTP。

    通用 API 邮箱的取码接口有时会先返回缓存旧码；验证码错误后重发时，
    这里会拒绝复用已失败的 code，持续轮询直到出现新 code 或超时。
    """
    used_codes = {str(x) for x in (used_codes or set()) if x}
    end = time.time() + timeout
    last_code = ""
    while True:
        # ★ 必须把剩余时间传下去（2026-10-04）：`otp_provider` 内部会调
        #   `mailbox.wait_for_otp(timeout=config.otp_timeout)`，写死 180s。
        #   不传的话这里的 `timeout=90` 完全无效 —— 实测日志里就是干等 180s，
        #   外层以为只等 90s，两边对不上。
        remaining_before = int(end - time.time())
        code = str(
            otp_provider(email, after_ts=after_ts, timeout=max(5, remaining_before)) or ""
        ).strip()
        if code and code not in used_codes:
            return code
        last_code = code or last_code
        remaining = int(end - time.time())
        if remaining <= 0:
            raise RuntimeError("等待新的邮箱验证码超时，取码接口仍返回已失败验证码（已隐藏）")
        logger.warning(
            "[激活] 取码接口仍返回已提交过的旧 OTP=%s，继续等待最新验证码（剩余 %ss）",
            last_code or "-",
            remaining,
        )
        time.sleep(min(5, max(1, remaining)))


def _install_email_otp_validate_hook(driver) -> None:
    """
    在页面内 hook fetch/XHR，捕获 email-otp/validate 的接口响应体。

    指纹浏览器不能像纯协议模式一样直接拿 requests.Response，因此在提交邮箱 OTP 前
    注入此 hook，后续只读取接口 JSON error.code，不靠页面文字判断废号。
    """
    script = r"""
    (() => {
      window.__codexEmailOtpValidateResponses = [];
      if (window.__codexEmailOtpValidateHooked) return true;
      window.__codexEmailOtpValidateHooked = true;
      const hit = (url) => String(url || '').includes('/api/accounts/email-otp/validate');
      const save = (url, status, body) => {
        try {
          if (!hit(url)) return;
          window.__codexEmailOtpValidateResponses.push({
            url: String(url || ''),
            status: Number(status || 0),
            body: String(body || '').slice(0, 2000),
            ts: Date.now(),
          });
        } catch (e) {}
      };
      const origFetch = window.fetch;
      if (origFetch) {
        window.fetch = async function(input, init) {
          const resp = await origFetch.apply(this, arguments);
          try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            if (hit(url)) {
              resp.clone().text().then(t => save(url, resp.status, t)).catch(() => {});
            }
          } catch (e) {}
          return resp;
        };
      }
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        this.__codexOtpValidateUrl = url;
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        try {
          this.addEventListener('loadend', function() {
            try {
              if (hit(this.__codexOtpValidateUrl)) save(this.__codexOtpValidateUrl, this.status, this.responseText);
            } catch (e) {}
          });
        } catch (e) {}
        return origSend.apply(this, arguments);
      };
      return true;
    })();
    """
    try:
        driver.execute_script(script)
    except Exception as exc:
        logger.debug("[激活] 注入 email-otp/validate 响应 hook 失败：%s", exc)


def _read_email_otp_validate_dead_code(driver) -> str:
    try:
        rows = driver.execute_script("return window.__codexEmailOtpValidateResponses || [];") or []
    except Exception:
        return ""
    if not isinstance(rows, list):
        return ""
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        code = detect_account_unusable_response_body(str(row.get("body") or ""))
        if code:
            logger.warning(
                "[激活] email-otp/validate 响应识别账号已废：code=%s status=%s",
                code,
                row.get("status"),
            )
            return code
    return ""


# 邮箱验证码页判断复用 roxy_registration 的强版本（URL + 输入框属性识别，
# 且明确排除 /log-in/password），不使用本地弱化版，避免点完一次性验证码后
# 页面已渲染 OTP 输入框却因 URL 不含 email-verification 而识别失败。

def _wait_after_email_otp_submit(driver, timeout: int = 45) -> str:
    """
    提交邮箱 OTP 后等待页面离开 /email-verification。

    返回：
      - accepted：已离开邮箱验证码页 / 进入手机号页 / 进入 callback；
      - invalid：页面明确报错、输入框标红，或长时间停留验证码页。
    """
    end = time.time() + timeout
    last_url = ""
    last_log = 0.0
    while time.time() < end:
        try:
            dead_code = _read_email_otp_validate_dead_code(driver)
            if dead_code:
                return f"deactivated:{dead_code}"
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[激活] 邮箱 OTP 后等待跳转：url=%s", url)
                last_url = url
            if _is_callback_url(url):
                return "accepted"
            if _has_strict_add_phone_form(driver) or _is_phone_code_page(driver):
                return "accepted"
            # 已经离开 email-verification，交给后续授权/手机号/consent 流程处理。
            if "email-verification" not in url.lower():
                return "accepted"

            state = _email_otp_page_state(driver)
            invalid = any(str(i.get("ariaInvalid") or "").lower() == "true" for i in (state.get("inputs") or []))
            errors = [str(x) for x in (state.get("errors") or []) if str(x).strip()]
            body_text = str(state.get("text") or "").lower()
            error_hit = any(x in body_text for x in (
                "invalid code", "incorrect code", "wrong code", "expired",
                "验证码错误", "验证码无效", "验证码已过期", "コードが正しく", "無効", "期限",
            ))
            if invalid or errors or error_hit:
                logger.warning(
                    "[激活] 邮箱 OTP 提交后检测到错误/仍需验证码：errors=%s invalid=%s url=%s",
                    errors[:3],
                    invalid,
                    url,
                )
                return "invalid"

            if time.time() - last_log > 6:
                logger.info("[激活] 邮箱 OTP 后仍在 email-verification，继续等待页面自动跳转")
                last_log = time.time()
        except Exception:
            pass
        time.sleep(0.5)
    logger.warning("[激活] 邮箱 OTP 后等待跳转超时，当前 url=%s，按验证码无效/过期处理", getattr(driver, "current_url", ""))
    return "invalid"


def _phone_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible).map(el => ({
          name: el.name || '', value: el.value || '', checked: !!el.checked, id: el.id || ''
        }));
        const inputs = [...document.querySelectorAll('input,select,textarea')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', placeholder: el.getAttribute('placeholder') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const bodyText = (document.body?.innerText || '').slice(0, 1200);
        return {url: location.href, radios, inputs, forms, bodyText};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _click_sms_radio_for_real(driver) -> bool:
    """用真实点击选中 SMS 通道，并确认真的选中了。

    ★ 为什么不能只用 JS 的 `radio.click()`：那改的是 DOM，React 的受控 state 没变。
      提交时 React 按自己的 state 重渲染，通道又翻回默认的 WhatsApp ——
      OpenAI 不会给 WhatsApp 通道发短信，页面就一直停在 add-phone
      （实测表现：提交前确认是 SMS，提交后日志里变成 whatsapp_channel）。
    """
    from selenium.webdriver.common.by import By

    strategies = [
        (By.CSS_SELECTOR, "input[type=radio][value='sms']"),
        (By.CSS_SELECTOR, "input[type=radio][value='text']"),
        (By.XPATH, "//label[.//input[@value='sms']]"),
        (By.XPATH, "//label[.//input[@value='text']]"),
    ]
    for by, selector in strategies:
        try:
            els = driver.find_elements(by, selector)
        except Exception:  # noqa: BLE001
            continue
        for el in els:
            try:
                el.click()
            except Exception:  # noqa: BLE001
                continue
            time.sleep(0.4)
            if _sms_channel_checked(driver):
                return True
    return False


def _select_sms_channel_or_raise(driver) -> None:
    state = _phone_page_state(driver)
    radios = state.get('radios') or []
    # 如果存在 WhatsApp 且没有 SMS/text 可选，当前接码平台无法读取 WhatsApp，直接换号。
    has_whatsapp = any('whatsapp' in str(r.get('value','')).lower().replace(' ', '') for r in radios)
    has_sms = any(str(r.get('value','')).lower() in ('sms', 'text', 'text_message', 'text-message') for r in radios)
    if has_whatsapp and not has_sms:
        raise RuntimeError(f"whatsapp_channel: 页面仅提供 WhatsApp 通道 state={state}")

    # 先试真实点击（React 只认这个），失败再退到 JS 点击。
    if _click_sms_radio_for_real(driver):
        logger.info("[激活] 已选择 SMS 短信通道（真实点击）")
        return

    selected = driver.execute_script(r"""
    const radios = [...document.querySelectorAll('input[type=radio]')];
    const sms = radios.find(el => /^(sms|text|text_message|text-message)$/i.test(el.value || ''));
    if (!sms) return false;
    sms.click();
    sms.dispatchEvent(new Event('input', {bubbles:true}));
    sms.dispatchEvent(new Event('change', {bubbles:true}));
    return true;
    """)
    if selected:
        logger.info("[激活] 已选择 SMS 短信通道（JS 兜底）")


def _sms_channel_checked(driver) -> bool:
    """当前是不是选着 SMS 通道（没有通道选择器时视为 True）。"""
    try:
        return bool(driver.execute_script(r"""
        const rs = [...document.querySelectorAll('input[type=radio]')];
        const sms = rs.find(el => /^(sms|text|text_message|text-message)$/i.test(el.value || ''));
        if (!sms) return true;
        return !!sms.checked;
        """))
    except Exception:  # noqa: BLE001
        return True


def _is_phone_code_state(state: dict) -> bool:
    url = str(state.get('url') or '').lower()
    if 'email-verification' in url:
        # 邮箱 OTP 页面也会出现 autocomplete=one-time-code，不能误判成手机验证码页。
        return False
    if 'phone-verification' in url:
        return True
    forms = state.get('forms') or []
    form_actions = ' '.join(str(f.get('action') or '') for f in forms).lower()
    if 'phone-verification' in form_actions:
        return True
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','placeholder')) for i in inputs).lower()
    body = str(state.get('bodyText') or '').lower()
    has_code_input = 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs
    phone_hint = (
        'phone' in url or 'phone' in form_actions
        or 'check your phone' in body
        or 'verification code we just sent' in body
        or 'enter the verification code' in body and ('text message' in body or 'phone' in body)
        or 'resend text message' in body
        or 'sent to +' in body
    )
    return bool(phone_hint and has_code_input)


def _is_phone_code_page(driver) -> bool:
    return _is_phone_code_state(_phone_page_state(driver))


def _is_add_phone_page(driver) -> bool:
    state = _phone_page_state(driver)
    url = str(state.get('url') or '').lower()
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete')) for i in inputs).lower()
    return 'add-phone' in url or 'type tel' in attrs or 'phone' in attrs or 'tel' in attrs


_PHONE_INPUT_SELECTORS = [
    "input[type='tel']",
    "input[name='phone']",
    "input[name='phone_number']",
    "input[autocomplete='tel']",
    "input[id*='phone']",
    "input[placeholder*='Phone']",
    "input[placeholder*='phone']",
]


def _has_strict_add_phone_form(driver) -> bool:
    try:
        return bool(driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return false;
        return !![...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
        """))
    except Exception:
        return False


def _auth_origin(driver) -> str:
    try:
        parsed = urlparse(str(driver.current_url or ""))
        if parsed.scheme and parsed.netloc and parsed.hostname and parsed.hostname.endswith("openai.com"):
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return "https://auth.openai.com"


def _ensure_add_phone_input(driver, *, reason: str = ""):
    """确保当前页面回到 add-phone，并返回手机号输入框。

    换号时如果还停留在 phone-verification/OTP 页，必须先回到手机号页，
    再把新号码重新写入页面并重新提交。
    """
    if _has_strict_add_phone_form(driver):
        return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)

    current = str(getattr(driver, "current_url", "") or "")
    if "email-verification" in current.lower():
        logger.info("[激活] 当前仍在 email-verification，先等待授权流程自动跳转，避免 invalid_auth_step")
        _wait_after_email_otp_submit(driver, timeout=45)
        if _has_strict_add_phone_form(driver):
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
        current = str(getattr(driver, "current_url", "") or "")

    target = _auth_origin(driver).rstrip("/") + "/add-phone"
    logger.info(
        "[激活] 当前不在手机号输入页，准备重新打开 add-phone 后换号：reason=%s url=%s target=%s",
        reason or "retry", current, target,
    )
    try:
        driver.get(target)
        human_delay("navigate")
        return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=10)
    except Exception as first_exc:
        # 某些流程不允许直接打开 /add-phone，尝试浏览器返回到上一页。
        logger.info("[激活] 直接打开 add-phone 未拿到输入框，尝试 history back：%s", str(first_exc)[:160])
        try:
            driver.back()
            human_delay("navigate")
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
        except Exception as back_exc:
            raise RuntimeError(
                f"无法回到手机号输入页以重新换号: direct={type(first_exc).__name__}: {first_exc}; "
                f"back={type(back_exc).__name__}: {back_exc}; state={_phone_page_state(driver)}"
            )


# 区号 → ISO 国家码。用来在填号之前先把国家选择器切对。
# ★ 顺序很重要：按区号长度从长到短匹配，否则 "1" 会把 "1x" 开头的号码抢走。
#
# ★★ **凡是号池可能发给我们的国家，都必须在这张表里**（2026-10-15 补教训）。
#   这张表不只是「显示用」：`_type_phone_with_keyboard` 用它算 `want`，
#   拿 `want` 跟页面选中的区号比对，**不一致就拒绝用键盘打字**
#   （因为号码框带格式化面具，按错误国家打会把好号打坏）。
#   表里没有的国家 → `want` 是空串 → 那道守卫被绕过 → 页面没切对国家时
#   它会照打完整 E.164，把号码打花。所以漏一个国家 = 那个国家的号全废。
#   （2026-10-15 号池切「沙特优先」时发现 966 不在表里，就是这个洞。）
_DIAL_TO_ISO = (
    ("1", "US"), ("7", "RU"), ("20", "EG"), ("27", "ZA"), ("30", "GR"), ("31", "NL"),
    ("33", "FR"), ("34", "ES"), ("39", "IT"), ("44", "GB"), ("48", "PL"), ("49", "DE"),
    ("52", "MX"), ("55", "BR"), ("57", "CO"), ("60", "MY"), ("62", "ID"), ("63", "PH"),
    ("65", "SG"), ("66", "TH"), ("81", "JP"), ("82", "KR"), ("84", "VN"), ("86", "CN"),
    ("90", "TR"), ("91", "IN"), ("92", "PK"), ("94", "LK"), ("95", "MM"), ("98", "IR"),
    # 号池在用的国家（2026-10-15 起 upstream_ref = dr:53|33）
    ("966", "SA"), ("971", "AE"), ("234", "NG"), ("254", "KE"), ("880", "BD"),
    ("380", "UA"),
    # 2026-10-16 号池换成「看板推荐国家」（sms.fur.li 的 OpenAI 推荐列表）：
    #   尼日利亚 234 / 巴西 55 / 喀麦隆 237 / 加纳 233 / 泰国 66 / 波兰 48 / 安哥拉 244
    # 55/66/48 上面已有，这里补 237/233/244 —— 漏一个就是那个国家的号全废：
    # _type_phone_with_keyboard 的守卫是 `if want and dial != want: 跳过`，
    # 表里没有 → want 是空串 → 守卫被绕过 → 页面没切对国家时照打完整 E.164，把号码打花。
    ("237", "CM"), ("233", "GH"), ("244", "AO"),
)


def _target_country_iso(phone: str) -> str:
    """按号码前缀猜国家 ISO 码，用于切国家选择器。"""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    for dial, iso in sorted(_DIAL_TO_ISO, key=lambda kv: len(kv[0]), reverse=True):
        if digits.startswith(dial):
            return iso
    return ""


def _read_phone_country(driver) -> dict:
    """读 add-phone 国家控件的真实状态。

    这个控件是「原生 <select> + 一个盖在它上面的 <button>」：
    select 有 234 个 option（value 就是 ISO 码，CO=Colombia），但被 button 盖住，
    Playwright 点不动（element is covered by <BUTTON>，实测三种策略全失败）。
    按钮文案是 React 渲染出来的，所以「React 到底切没切」以它为准 ——
    select.value 我们自己随时能用 JS 改，改完 DOM 变了 React 未必认。
    """
    try:
        return driver.execute_script(r"""
        const form = document.querySelector("form[action*='/add-phone' i]");
        const sel = form ? form.querySelector('select') : null;
        const btn = form ? [...form.querySelectorAll('button')]
          .find(b => /\(\+\d/.test(b.innerText || '')) : null;
        return {
          selectValue: sel ? String(sel.value || '') : '',
          buttonText: btn ? String(btn.innerText || '').replace(/\s+/g, ' ').trim() : '',
        };
        """) or {}
    except Exception:  # noqa: BLE001
        return {}


def _dial_of(phone: str) -> str:
    """号码的国际区号（不含 +）。"""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    for dial, _iso in sorted(_DIAL_TO_ISO, key=lambda kv: len(kv[0]), reverse=True):
        if digits.startswith(dial):
            return dial
    return ""


# 换号失败原因的中文标签（只用于日志与最终报错，让一轮跑完能一眼看懂）。
#
# ★ 为什么不在这里做任何止损（2026-10-15 改，老板问「怎么还是跑 3 个号就停」之后）：
#   2026-10-03 的老规则是「连续 3 个号 fraud_guard → 整轮放弃」，理由是
#   「继续换只是白占款（每个号 ¥0.40 要等 20 分钟才退）」。而 2026-10-15 起
#   换号**立刻退号退款**（实测三个号一分钱没花），前提没了；
#   2026-10-15 晚些改成的「同一个号段 3 次就停」也删掉了，因为它**挡在号池前面**：
#   号池的号段黑名单门槛是 **5 次失败**（AccountPoolPhoneSectionMinSamples），
#   而它还会在下单时主动避开已拉黑的号段（命中就退号重抽）。
#   工具在第 3 个就停 ⇒ 号池永远攒不到 5 次 ⇒ 那个坏号段永远拉不黑 ⇒
#   **每轮都重复烧同一个号段的 3 个号**，正是"看起来怎么都没用"的成因。
#
#   现在的分工：
#     工具 = 把 max_retries（默认 12）个号试完 + 把每次失败原因写清楚；
#     号池 = 按号段记账、够数拉黑、下单时换号段、按成功率/探索/轮转换国家。
#   一轮全失败的成本只有时间（每个号下单即退，钱不花）。
# ★★ 账号/会话级失效的页面文案（**换号无用**，必须立刻停）
#
# 2026-10-16 老板实测踩到：那个账号已经封了，页面上明明白白写着
#
#     Session ended  Your sign-in session is no longer valid.
#     Please start over to continue.  Try again ...
#
# 而手机 OTP 提交那步把它归成 `phone_otp_not_accepted`（"提交后页面没动"）⇒
# 判定成**号码**问题 ⇒ 接着又换了 3 个号；那 3 个号其实**都收到了码**
# （日志里 `已收到手机验证码`），纯粹白买，还把号段的战绩统计污染成"失败"。
#
# 会话/账号失效换多少号都没用 —— 这是**账号级**结论，必须直接中止本轮激活。
# 只匹配明确的账号/会话文案，不用 "suspicious"（那是号码风控那条，别混）。
_ACCOUNT_DEAD_MARKERS = (
    "session ended",
    "session is no longer valid",
    "session has expired",
    "session expired",
    "no longer valid",
    "start over to continue",
    "account has been deactivated",
    "account was deactivated",
    "account is deactivated",
    "account has been suspended",
    "account was suspended",
    "account is suspended",
    "account is temporarily suspended",
    "temporarily suspended",
    "account banned",
    "permanently banned",
    "violating our policies",
    # ★★ 2026-10-17 00:49 生产实测补的（上一版漏了这条，白烧了一个号）：
    #   `Authentication Error  You do not have an account because it has been
    #    deleted or deactivated. If you bel...`
    "deleted or deactivated",
    "do not have an account",
    "account has been deleted",
    "account was deleted",
    "account no longer exists",
    # 兜底：`deactivated` 单独出现也是账号级（这版 UI 里它就是账号被停用的措辞）。
    # 放最后 —— 前面的具体文案先命中，返回的 reason 更准、便于排查。
    "deactivated",
    "账号已被封",
    "账号已停用",
    "账号已被删除",
    "会话已失效",
    "登录状态已失效",
)


def _account_dead_reason(text: str) -> str:
    """页面/错误文本里有没有「账号或会话已失效」的文案；命中的原文片段原样返回。"""
    body = str(text or "").lower()
    for marker in _ACCOUNT_DEAD_MARKERS:
        if marker in body:
            return marker
    return ""


def _phone_failure_label(err_text: str) -> str:
    """把一次换号的失败文本归成一个人话标签。"""
    text = str(err_text or "")
    if _account_dead_reason(text):
        # 放最前面：账号失效的原文里也可能出现别的关键词（比如 body 里带 code），
        # 归错类就会让人以为"还是号码问题"。
        return "账号已失效"
    if "fraud_guard" in text or "suspicious behavior" in text:
        return "风控拒号"
    if "phone_number_in_use" in text or "已被使用" in text:
        return "号码已被占用"
    if "invalid_phone_code" in text:
        return "验证码错误"
    if "invalid_phone" in text:
        return "号码无效"
    if "phone_otp_not_accepted" in text or "still_code_page" in text:
        return "提交后页面没动"
    if "等待短信验证码超时" in text or "还没收到短信" in text:
        return "号码收不到码"
    if "add_phone_rejected" in text:
        return "上游拒绝该号码"
    if "whatsapp" in text.lower():
        return "页面只给 WhatsApp 通道"
    if "超时" in text or "timeout" in text.lower():
        return "超时"
    return "其他"


def _phone_section_key(phone: str) -> str:
    """号码的号段 = 去掉国际区号后的前 3 位（和号池服务端同一把尺子）。

    只用于**日志与报错**（让一轮跑完能看出是哪几个号段在废）。
    认不出区号时返回空串，调用方显示成「未知」。
    """
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    dial = _dial_of(digits)
    if not dial:
        return ""
    national = digits[len(dial):]
    return national[:3] if len(national) >= 3 else ""


def _tally_attempts(notes: list[tuple[str, str]]) -> list[tuple[str, str, int]]:
    """把 [(号段, 原因)] 汇总成 [(号段, 原因, 次数)]，次数多的在前。

    纯函数，好单测 —— 最后那句报错就是靠它把「12 个号到底怎么废的」说清楚的。
    """
    counts: dict[tuple[str, str], int] = {}
    for section, label in notes:
        key = (section or "未知", label or "其他")
        counts[key] = counts.get(key, 0) + 1
    return [(section, label, n) for (section, label), n in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _phone_required_error_visible(driver) -> bool:
    """add-phone 页是否出现了**真正的**「号码必填」报错。

    ★ 2026-10-21 教训（我先踩了一次）：这个页面的**标题和副标题**本身就是
      「Phone number required / Add your phone number to continue…」，
      拿 `body.innerText` 匹配会**永远为真**（一上线就把激活直接判死）。
      只能看**报错容器**：role=alert / aria-live / React-Aria 的 field error 节点。
    """
    try:
        return bool(driver.execute_script(r"""
        const nodes = [
          ...document.querySelectorAll('[role="alert"], [aria-live="assertive"], [aria-live="polite"]'),
        ];
        const form = document.querySelector('form[action*="/add-phone" i]');
        if (form) {
          for (const el of form.querySelectorAll('[class*="error" i], [id*="error" i], [data-invalid="true"]')) {
            nodes.push(el);
          }
        }
        const norm = t => String(t || '').replace(/\s+/g, ' ').trim().toLowerCase();
        for (const el of nodes) {
          if (!el) continue;
          const t = norm(el.innerText || el.textContent);
          if (!t) continue;
          if (t.includes('phone number required') || t.includes('需要电话号码')) return true;
          if (t.includes('required') && t.includes('phone')) return true;
        }
        return false;
        """))
    except Exception:  # noqa: BLE001
        return False


def _page_selected_dial(driver) -> str:
    """读 add-phone 国家选择器当前选中项的区号（从 option 文本的 (+NN) 里取）。"""
    try:
        return str(driver.execute_script(r"""
        const form = document.querySelector("form[action*='/add-phone' i]");
        const sel = form ? form.querySelector('select') : null;
        if (!sel || sel.selectedIndex < 0) return '';
        const t = String(sel.options[sel.selectedIndex].textContent || '');
        const m = t.match(/\+(\d{1,4})/);
        return m ? m[1] : '';
        """) or "")
    except Exception:  # noqa: BLE001
        return ""


def _type_phone_with_keyboard(driver, info: dict) -> dict:
    """用真实键盘把号码重新打进可见框（兜底 React 不认 JS 写值的情况）。

    为什么需要这条路：`_set_phone_value` 是 JS 直接写值 + 派发 input 事件，
    React 有时不认 —— DOM 里每个字段都对，React 的受控状态却是空的，
    提交时页面回一句「需要电话号码」，一个号就这么废了（2026-10-03 实测）。
    真实键盘事件 React 一定会走 onChange，所以拿它当兜底。

    ★ 关键：必须**只打本国号码**，不能打完整 E.164。
      号码框带格式化面具，打进去的位数会被按当前国家重排；多打国家码就会被吃掉重排
      （早先国家还停在美国时，打 573012731422 被打成 (572) 217-2403）。
      调用前 `_set_phone_value` 已经把完整 E.164 写进去、页面据此把国家识别对了，
      所以这里按页面选中的区号把国家码去掉再打。
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    digits = "".join(ch for ch in str(info.get("e164") or "") if ch.isdigit())
    want = _dial_of(str(info.get("e164") or ""))
    dial = _page_selected_dial(driver)
    if not dial or (want and dial != want):
        # 页面选中的国家和号码对不上时**绝不要打**：这时候输入会被按错误国家重排，
        # 反而把本来写对的号码搞坏。跳过比打坏强。
        return {"mode": "keyboard", "skipped": True, "pageDial": dial, "wantDial": want}
    if digits.startswith(dial) and len(digits) > len(dial) + 4:
        digits = digits[len(dial):]
    if not digits:
        raise RuntimeError(f"键盘填写拿不到该输的数字：info={info}")

    box = driver.find_element(
        By.CSS_SELECTOR,
        "form[action*='/add-phone' i] input[type='tel'], "
        "form[action*='/add-phone' i] input[name='__reservedForPhoneNumberInput_tel']",
    )
    box.click()
    time.sleep(0.2)
    # ★ 2026-10-21：这个表单里 React 真正读的是**当前获焦的那个输入框**
    #   （点下去之后焦点可能落在可见的 `<input type="text">` 上，而不是 tel 面具框）。
    #   所以先按元素清空，再往「当前焦点元素」逐位打 —— 往写死的元素打，页面可能收不到。
    box.send_keys(Keys.CONTROL, "a")
    box.send_keys(Keys.DELETE)
    time.sleep(0.2)
    try:
        target = driver.switch_to.active_element or box
    except Exception:  # noqa: BLE001
        target = box
    # 一位一位打，中间留一点间隔：React-Aria 的格式化逻辑会在每次 input 后重排，
    # 一次性 send_keys 太快反而容易被它吃掉字符。
    for ch in digits:
        target.send_keys(ch)
        time.sleep(random.uniform(0.03, 0.09))
    time.sleep(0.4)
    actual = str(box.get_attribute("value") or "")
    try:
        typed_into = target.get_attribute("outerHTML") or ""
    except Exception:  # noqa: BLE001
        typed_into = ""
    box.send_keys(Keys.TAB)  # 移开焦点，让页面完成校验
    return {
        "mode": "keyboard",
        "expected": digits,
        "actual": actual,
        "target": typed_into[:160],
    }


def _set_phone_value(driver, phone: str, *, timeout: int = 10, keyboard: bool = False) -> dict:
    """按 FlowPilot 第 9 步逻辑填写 add-phone 表单。

    要点：
    - 所有元素 scoped 到 form[action*="/add-phone"]；
    - 先把国家选择器切到号码对应的国家（否则页面按错误区号重排号码，会吃掉一位）；
    - 可见 tel 输入框写入“页面期望显示的号码”；
    - 如果页面存在隐藏 input[name="phoneNumber"]，同步写入完整 E.164 号码；
    - 触发 input/change 并 blur，让 React/React-Aria 完成校验；
    - 写值后回读，不一致就重写（最多 PHONE_SET_ATTEMPTS 次），不再一次失败就放弃。
    """
    if not _has_strict_add_phone_form(driver):
        raise RuntimeError(f"当前不是 add-phone 手机号输入页，不能填写手机号: state={_phone_page_state(driver)}")
    script = r"""
    const rawPhone = String(arguments[0] || '').trim();
    const e164 = rawPhone.startsWith('+') ? rawPhone : ('+' + rawPhone.replace(/\D+/g, ''));
    const digits = e164.replace(/\D+/g, '');
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
    if (!form) {
      return {ok:false, error:'missing_add_phone_form', url: location.href};
    }
    const phoneInput = [...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')]
      .find(visible);
    if (!phoneInput) {
      return {ok:false, error:'missing_phone_input', url: location.href};
    }

    const hiddenPhoneNumberInput = form.querySelector('input[name="phoneNumber"]');
    const select = form.querySelector('select');
    let dialCode = '';
    let selectedText = '';
    let selectedAfter = '';
    let selectedChanged = false;
    // ★ 目标国家怎么认：页面本地化后 option 文本里常常只有国名、没有 +区号
    //   （实测日文界面显示「アメリカ合衆国」，于是老代码的 /\+(\d+)/ 一个都匹配不上，
    //    国家选择器永远不切换，号码被按错误国家格式化后截短一位）。
    //   所以按三条线索匹配：option 的 value（ISO 码）→ 文本里的 +区号 → 国名。
    const DIAL_HINTS = {
      '55': ['br', 'brazil'],
      '57': ['co', 'colombia'],
      '84': ['vn', 'vietnam'],
      '81': ['jp', 'japan'],
      '1':  ['us', 'united states'],
      '44': ['gb', 'united kingdom'],
      '91': ['in', 'india'],
      '62': ['id', 'indonesia'],
      '7':  ['ru', 'russia'],
      // ★ 与 Python 侧 _DIAL_TO_ISO 保持同步：号池在用的国家一个都不能少，
      //   少了这一项，dialCode 反查不出来（只影响回读与日志，不改国家）。
      '966': ['sa', 'saudi arabia'],
      '971': ['ae', 'united arab emirates'],
      '63': ['ph', 'philippines'],
      // ★ 2026-10-16 号池换国家（看板推荐列表）后补齐，与 Python 侧 _DIAL_TO_ISO 同步：
      '234': ['ng', 'nigeria'],
      '237': ['cm', 'cameroon'],
      '233': ['gh', 'ghana'],
      '66': ['th', 'thailand'],
      '48': ['pl', 'poland'],
      '244': ['ao', 'angola'],
    };
    const optionDialCode = (opt) => {
      const text = String(opt?.textContent || opt?.label || opt?.value || '').replace(/\s+/g, ' ').trim();
      const m = text.match(/\+(\d{1,4})\b/);
      return m ? m[1] : '';
    };
    // ★ 这个页面（英文）的 option 文本是纯国名「Colombia」，里面根本没有 "+57"，
    //   所以 optionDialCode 永远返回空 → dialCode 空 → 只好把完整 E.164 塞进可见框
    //   → 页面按默认国家（美国）格式化，号码被吃掉重排成 (572) 217-2403。
    //   改成：option 文本推不出来时，用 option 的 value（ISO 码，如 "CO"）反查区号。
    const dialFromIso = (iso) => {
      const v = String(iso || '').trim().toLowerCase();
      if (!v) return '';
      for (const code of Object.keys(DIAL_HINTS)) {
        if (DIAL_HINTS[code].indexOf(v) >= 0) return code;
      }
      return '';
    };
    const pickOption = (options) => {
      // 1) 文本里带 +区号（最可靠）
      const byCode = options
        .map(opt => ({opt, code: optionDialCode(opt)}))
        .filter(x => x.code && digits.startsWith(x.code))
        .sort((a, b) => b.code.length - a.code.length)[0];
      if (byCode) return byCode;
      // 2) value 就是 ISO 码 —— ★ 必须先把 value 精确匹配跑完，再考虑国名文本！
      //    老写法把 `val === hint` 和 `text.includes(hint)` 塞进同一个 find，
      //    而 option 是按国名字母序排的：查 'co' 时 "Cocos (Keeling) Islands"
      //    （国名里含 "co"，value 是 CC）排在 "Colombia" 前面，于是被选中。
      //    结果号码按科科斯群岛/默认国家格式化，整轮报废（2026-10-03 实测）。
      for (const code of Object.keys(DIAL_HINTS)) {
        if (!digits.startsWith(code)) continue;
        for (const hint of DIAL_HINTS[code]) {
          const exact = options.find(opt => String(opt.value || '').trim().toLowerCase() === hint);
          if (exact) return {opt: exact, code};
        }
      }
      // 3) 只有实在没有 ISO 值的页面，才退到国名文本匹配；
      //    而且要求按词边界匹配，不能用 includes 让 "Cocos" 抢走 "Colombia"。
      for (const code of Object.keys(DIAL_HINTS)) {
        if (!digits.startsWith(code)) continue;
        for (const hint of DIAL_HINTS[code]) {
          const loose = options.find(opt => {
            const text = String(opt.textContent || opt.label || '').trim().toLowerCase();
            return text === hint || text.startsWith(hint + ' ') || text.startsWith(hint + '(');
          });
          if (loose) return {opt: loose, code};
        }
      }
      return null;
    };
    if (select) {
      // ★★ 不再自己去切这个国家选择器。
      //   它是 React 受控的 combobox：原生 <select> 被一个 <button> 盖住，
      //   我们用 JS 写 select.value（哪怕走原生 setter + 派发 change），
      //   React 也会在下一次重渲染时按自己的 state 改回默认国家 —— 实测
      //   连续 5 次「写进去又被改回 United States」，号码于是被按 +1 重排。
      //   改成：**不碰选择器，直接把完整 E.164 写进输入框，让页面自己按号码识别国家**。
      //   页面本身就会做这件事（实测可见框会被它格式化成 +57 301 2894089）。
      const options = [...select.options];
      if (select.selectedIndex >= 0 && select.options[select.selectedIndex]) {
        const opt = select.options[select.selectedIndex];
        selectedText = String(opt.textContent || opt.label || opt.value || '').replace(/\s+/g, ' ').trim();
        dialCode = optionDialCode(opt) || dialFromIso(opt.value);
      }
      selectedAfter = String(select.value || '');
    }

    // ★ 一律填**完整 E.164**，让页面按号码自己识别国家（哥伦比亚 +57 实测稳定生效）。
    //   曾经试过「第 2 次改填本国号码」来绕开巴西号的截位问题 —— 那是负收益：
    //   页面没能识别出巴西（国家停在 Estados Unidos）时，写成 12981634760 会被
    //   当成 +1 号码重排成 1 (298) 163-4760，比截位还糟。实测后撤掉（2026-10-03）。
    const visibleValue = e164;

    const setNativeValue = (el, value) => {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      el.focus();
      if (setter) setter.call(el, ''); else el.value = '';
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      if (setter) setter.call(el, value); else el.value = value;
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
    };

    phoneInput.scrollIntoView({block:'center'});
    setNativeValue(phoneInput, visibleValue);
    // ★ 隐藏字段必须也走原生 setter。
    //   React 的受控输入用自己的 value tracker 判断「值变没变」：
    //   直接 el.value = x 再派发 input，它会认为没变化而跳过 onChange，
    //   于是 React state 里这个号码始终是空的，提交后页面报
    //   "Phone number required"（实测踩过：12 次换号全卡在这）。
    if (hiddenPhoneNumberInput) {
      setNativeValue(hiddenPhoneNumberInput, e164);
    }
    // ★ 2026-10-21 现场实证（phone_debug.json）：这个表单里还有**可见的受控
    //   `<input type="text">`（无 name）**，React 真正读的是它；只写
    //   `__reservedForPhoneNumberInput_tel` + hidden `phoneNumber` 时，页面一直停在
    //   「Phone number required」，Continue 点了等于没点 —— 也就是老板看到的
    //   「号码页不填号却一直点继续」。把空着的可见 text 框一并写上。
    const textInputs = [...form.querySelectorAll('input[type="text"]')].filter(visible);
    for (const el of textInputs) {
      const cur = String(el.value || '').trim();
      if (!cur || /^[+\d\s()-]+$/.test(cur)) {
        try { setNativeValue(el, e164); } catch (e) { /* 某个框不让写就算了 */ }
      }
    }
    phoneInput.blur();
    document.body?.focus?.();
    return {
      ok: true,
      e164,
      visibleValue,
      actualVisible: phoneInput.value || '',
      hiddenValue: hiddenPhoneNumberInput ? (hiddenPhoneNumberInput.value || '') : '',
      textValues: textInputs.map(el => String(el.value || '')),
      dialCode,
      selectedText,
      selectedAfter,
      selectedChanged,
      inputName: phoneInput.getAttribute('name') || '',
      inputId: phoneInput.id || '',
      url: location.href,
    };
    """
    # ★ 写值 → 回读 → 不一致就重写，而不是立刻 raise。
    # 页面（React-Aria）会在 input 事件里同步重排号码，偶尔把最后一位吃掉；
    # 以前这里一次不一致就放弃，整轮换号重来，8 次全卡在同一个地方。
    last_reason = "未执行"
    last_result = None
    for attempt in range(1, PHONE_SET_ATTEMPTS + 1):
        result = driver.execute_script(script, phone)
        if not result or not result.get("ok"):
            last_reason = f"写入脚本未成功：{result}"
            last_result = result
            logger.warning("[激活] 手机号写入失败（第 %s/%s 次）：%s",
                           attempt, PHONE_SET_ATTEMPTS, last_reason)
            time.sleep(0.8)
            continue

        actual = str(result.get("actualVisible") or "").strip()
        visible_value = str(result.get("visibleValue") or "").strip()
        hidden_value = str(result.get("hiddenValue") or "").strip()
        e164 = str(result.get("e164") or "").strip()
        # OpenAI/React-Aria 电话框会自动格式化，例如 +84925154291 -> +84 925 154 291。
        # 不能按界面字符串精确比较，只比较数字归一化后的值。
        actual_digits = ''.join(ch for ch in actual if ch.isdigit())
        visible_digits = ''.join(ch for ch in visible_value if ch.isdigit())
        e164_digits = ''.join(ch for ch in e164 if ch.isdigit())
        hidden_digits = ''.join(ch for ch in hidden_value if ch.isdigit())

        if not actual_digits or (actual_digits != visible_digits and actual_digits != e164_digits):
            last_reason = (f"可见输入框校验失败 expected_digits={visible_digits or e164_digits} "
                           f"actual={actual}")
            last_result = result
            logger.warning("[激活] 手机号可见框不一致（第 %s/%s 次）selected=%r changed=%s：%s",
                           attempt, PHONE_SET_ATTEMPTS, result.get("selectedText"),
                           result.get("selectedChanged"), last_reason)
            time.sleep(0.9)
            continue

        if hidden_value and hidden_digits != e164_digits:
            last_reason = f"隐藏字段校验失败 expected={e164} actual={hidden_value}"
            last_result = result
            logger.warning("[激活] 手机号隐藏字段不一致（第 %s/%s 次）：%s",
                           attempt, PHONE_SET_ATTEMPTS, last_reason)
            time.sleep(0.9)
            continue

        if attempt > 1:
            logger.info("[激活] 手机号第 %s 次写入后校验通过（国家选择器=%r 实际选中=%r 已切换=%s）",
                        attempt, result.get("selectedText"), result.get("selectedAfter"),
                        result.get("selectedChanged"))

        # ★ React 不认 JS 写值时，DOM 看着是对的、提交却说号码为空。
        #   所以每条连接都再用真实键盘打一遍 —— 键盘事件 React 一定走 onChange。
        if keyboard:
            try:
                kb = _type_phone_with_keyboard(driver, result)
                result["keyboard"] = kb
                if kb.get("skipped"):
                    # ★ 2026-10-05 P1：这条路径在当前流程里**每次都被主动跳过**
                    #   （`_page_selected_dial()` 拿不到页面选中的区号 → 空串，
                    #   于是按设计 `return {"skipped": True}` 不填）。
                    #   而旧代码在下面**无条件**拿空串跟号码比一次、必然不等 ——
                    #   结果每次尝试都打一条 WARNING「键盘填写的号码回读不一致」。
                    #   看着像出错，其实什么都没发生，白白误导排查（我自己就被它带偏过）。
                    logger.debug("[激活] 键盘复核被跳过（页面没给出可打的号码框）：%s", kb)
                else:
                    kb_digits = "".join(ch for ch in str(kb.get("actual") or "") if ch.isdigit())
                    if kb_digits != ''.join(ch for ch in str(result.get("visibleValue") or "") if ch.isdigit()):
                        logger.warning("[激活] 键盘填写的号码回读不一致：%s", kb)
                    else:
                        logger.info("[激活] 已用真实键盘复核号码：%s", kb.get("actual"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[激活] 键盘复核号码失败（不致命，继续用 JS 填的值）：%s: %s",
                               type(exc).__name__, exc)

        # 留给失败现场用：写盘时能看到当时页面实际认到的值
        try:
            driver._grl_last_phone_set = result
        except Exception:  # noqa: BLE001
            pass
        return result

    # ★ 2026-10-04 结论：印尼（+62）的 13 位号码**填不进这个表单**，已证伪 6 种写法。
    #   react-aria 的电话格式化器会吃掉最后一位（13→12），无论：
    #     1) JS 原生 setter 一次写完整 E.164
    #     2) 键盘打本国号码（清空后国家会重置回美国 → 被按 +1 重排）
    #     3) 键盘 Keys.END 后补打（光标落在前段，数字插到区号后面）
    #     4) JS 追加缺失位（无效果）
    #     5) JS 写本国号码（当时写进去了，2 秒后 React 重渲染又覆盖回 12 位）
    #     6) 清空后连 `+` 和区号逐位键盘输入（渐进识别跑飞：打成 +64 新西兰 / +86 中国）
    #   根因是页面侧格式化，不是我们的填写时机。详见
    #   artifacts/account-pool-experiment-20261004/ 下的各国日志。
    #   换句话说：**这个国家的号码在这里根本测不出来**，不是「印尼被 OpenAI 拒」。
    raise RuntimeError(f"手机号写入校验失败：{last_reason} result={last_result} state={_phone_page_state(driver)}")


def _blur_active_input_and_wait(driver, *, label: str = "输入完成") -> None:
    """输入手机号后移开焦点，并给前端校验/格式化留处理时间。"""
    try:
        driver.execute_script(r"""
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') active.blur();
        document.body?.focus?.();
        document.dispatchEvent(new Event('change', {bubbles:true}));
        """)
    except Exception:
        pass
    seconds = random.uniform(1.8, 3.2)
    logger.info("[激活] %s，已移开焦点，等待页面处理 %.1f 秒", label, seconds)
    time.sleep(seconds)


def _verify_add_phone_value_before_submit(driver, expected_e164: str) -> dict:
    result = driver.execute_script(r"""
    const expected = String(arguments[0] || '').trim();
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
    if (!form) return {ok:false, error:'missing_add_phone_form', url: location.href};
    const input = [...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
    const hidden = form.querySelector('input[name="phoneNumber"]');
    const visibleValue = String(input?.value || '').trim();
    const hiddenValue = String(hidden?.value || '').trim();
    const digits = value => String(value || '').replace(/\D+/g, '');
    const visibleDigits = digits(visibleValue);
    const hiddenDigits = digits(hiddenValue);
    const expectedDigits = digits(expected);
    // 国家选择器切对了以后，可见框里是**本国号码**（如哥伦比亚 +57 只填 3012894089），
    // 不再是完整 E.164。以前这里只认完整 E.164，于是「选对了国家」反倒被判校验失败，
    // 逼着代码留着那个切不动国家的写法（2026-10-03 实测）。两种都接受。
    const select = form.querySelector('select');
    let dial = '';
    if (select && select.selectedIndex >= 0) {
      const optText = String(select.options[select.selectedIndex]?.textContent || '');
      const m = optText.match(/\+(\d{1,4})/);
      if (m) dial = m[1];
    }
    const nationalDigits = (dial && expectedDigits.startsWith(dial))
      ? expectedDigits.slice(dial.length) : '';
    // ★ 不能只靠 dial 判断：这个页面的 option 文本是纯国名，推不出区号，
    //   于是 nationalDigits 恒为空，可见框里合法的本国号码（304 4579944）
    //   会被判成校验失败，白白换号（2026-10-03 实测）。
    //   再加一条通用规则：可见框是完整号码的后缀（本国号码），且长度够长即可。
    const suffixOk = visibleDigits.length >= 7 && expectedDigits.endsWith(visibleDigits);
    const visibleOk = visibleDigits === expectedDigits
      || (!!nationalDigits && visibleDigits === nationalDigits)
      || suffixOk;
    // 隐藏字段如果存在，必须等于完整 E.164。
    const ok = !!visibleDigits && visibleOk && (!hidden || hiddenDigits === expectedDigits);
    return {ok, visibleValue, hiddenValue, expected, visibleDigits, hiddenDigits, expectedDigits, dial, url: location.href};
    """, expected_e164)
    if not result or not result.get("ok"):
        raise RuntimeError(f"手机号提交前校验失败 result={result} state={_phone_page_state(driver)}")
    return result


def _wait_page_settle_after_submit() -> None:
    """点击提交后先等待页面处理，再检查发送状态。"""
    seconds = random.uniform(2.0, 4.0)
    logger.info("[激活] 已点击提交，等待页面发送/跳转处理 %.1f 秒后检查状态", seconds)
    time.sleep(seconds)


def _refresh_add_phone_for_retry(driver, *, reason: str = "") -> None:
    """发送失败/换号前刷新手机号页，避免旧错误状态和旧号码残留。"""
    try:
        logger.info("[激活] 发送失败/准备换号，刷新手机号页面：%s", reason or "retry")
        driver.refresh()
        human_delay("navigate")
        try:
            _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
            return
        except Exception:
            pass
        # 如果刷新后仍不在输入页，强制回 add-phone。
        target = _auth_origin(driver).rstrip("/") + "/add-phone"
        logger.info("[激活] 刷新后未找到手机号输入框，重新打开：%s", target)
        driver.get(target)
        human_delay("navigate")
        _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
    except Exception as exc:
        logger.info("[激活] 刷新手机号页失败，下一轮会再次尝试回到 add-phone：%s", str(exc)[:180])


def _click_add_phone_continue_button(driver, *, timeout: int = 10) -> dict:
    """点击 add-phone 表单里的 Continue/続行 按钮。

    参考 FlowPilot 的 getAddPhoneSubmitButton + simulateClick：优先在 add-phone form 内找
    enabled submit，点击失败时用 form.requestSubmit(button) 兜底。
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => {
              if (!el) return false;
              if (el.disabled) return false;
              if (String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
              return true;
            };
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            if (!form) return null;
            const buttons = [...form.querySelectorAll('button[type="submit"], input[type="submit"]')];
            return buttons.find(b => visible(b) && enabled(b) && (b.getAttribute('data-dd-action-name') || '').toLowerCase() === 'continue')
              || buttons.find(b => visible(b) && enabled(b))
              || buttons.find(b => visible(b))
              || null;
            """)
            if btn:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(random.uniform(0.3, 0.8))
                try:
                    text = str(getattr(btn, 'text', '') or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                except Exception:
                    text = ''
                try:
                    btn.click()
                    _wait_page_settle_after_submit()
                    return {"ok": True, "method": "click", "text": text}
                except Exception as click_exc:
                    last = click_exc
                    submitted = driver.execute_script(r"""
                    const btn = arguments[0];
                    const form = btn?.form || btn?.closest?.('form');
                    if (form && typeof form.requestSubmit === 'function') {
                      form.requestSubmit(btn);
                      return true;
                    }
                    if (btn && typeof btn.click === 'function') {
                      btn.click();
                      return true;
                    }
                    return false;
                    """, btn)
                    if submitted:
                        _wait_page_settle_after_submit()
                        return {"ok": True, "method": "requestSubmit", "text": text, "click_error": str(click_exc)[:160]}
        except Exception as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"submit_missing: add-phone Continue/続行 submit button not found last={last} state={_phone_page_state(driver)}")


def _force_submit_add_phone_form(driver) -> dict:
    """add-phone 页面点击按钮没生效时，直接 requestSubmit 当前 form。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return {ok:false, reason:'missing_form', url: location.href};
        const btn = [...form.querySelectorAll('button[type="submit"],input[type="submit"]')]
          .find(el => visible(el) && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true')
          || form.querySelector('button[type="submit"],input[type="submit"]');
        if (btn) btn.scrollIntoView({block:'center'});
        if (typeof form.requestSubmit === 'function') form.requestSubmit(btn || undefined);
        else if (btn && typeof btn.click === 'function') btn.click();
        else form.submit();
        return {ok:true, method: btn ? 'requestSubmit(button)' : 'requestSubmit(form)', url: location.href};
        """) or {}
    except Exception as exc:
        return {ok:false, reason:f'{type(exc).__name__}: {exc}', url:getattr(driver, 'current_url', '')}


def _dump_phone_debug(driver, state: dict, note: str) -> None:
    """把 add-phone 的完整现场写盘。

    这个页面失败时日志里的 state 只截了 240 字符，光看日志根本判断不出
    「号码没进 React state」还是「号码被拒」。留下完整 DOM 快照，下次一条命令就能看全。
    """
    import json as _json
    from pathlib import Path as _Path

    try:
        root = _Path(__file__).resolve().parent.parent
        out = root / "logs" / "phone_debug.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            dom = driver.execute_script(r"""
            const form = document.querySelector('form[action*="/add-phone" i]');
            const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const inputs = form ? [...form.querySelectorAll('input,select')].map(el => ({
              tag: el.tagName, type: el.type || '', name: el.name || '', id: el.id || '',
              value: String(el.value || '').slice(0, 40), visible: vis(el),
              ariaInvalid: el.getAttribute('aria-invalid') || '',
              required: !!el.required,
            })) : [];
            const radios = form ? [...form.querySelectorAll('input[type=radio]')].map(el => ({
              name: el.name || '', value: el.value || '', checked: !!el.checked,
            })) : [];
            return {url: location.href, title: document.title || '',
                    bodyText: (document.body ? (document.body.innerText || '') : '').slice(0, 1500),
                    inputs, radios};
            """) or {}
        except Exception as exc:  # noqa: BLE001
            dom = {"error": f"{type(exc).__name__}: {exc}"}
        payload = {
            "note": note,
            "ts": time.time(),
            "last_set_result": getattr(driver, "_grl_last_phone_set", None),
            # ★ 关键：提交到底有没有发出去请求。
            #   DOM 全对但页面不动，只有两种可能：点击根本没触发提交，或者提交了被上游拒。
            #   光看页面分不出来，得看网络记录（2026-10-03 加）。
            "network": list(getattr(driver, "_auth_network_events", []) or [])[-25:],
            "state": state,
            "dom": dom,
        }
        out.write_text(_json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        logger.warning("[激活] 已把 add-phone 现场写到 %s", out)
    except Exception:  # noqa: BLE001
        pass


def _dump_phone_dom(driver, tag: str) -> None:
    """把 add-phone 页面的真实结构（含按钮/combobox）落盘一次。

    国家选择器是个 React-Aria combobox：原生 <select> 被一个 <BUTTON> 盖住，
    Playwright 点不动它（element is covered by <BUTTON>）。日志里看不出那个按钮长什么样，
    所以这里直接把表单 HTML、所有按钮、还有一张截图存下来，省得靠猜。
    """
    import json as _json
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    out_dir = root / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        info = driver.execute_script(r"""
        const form = document.querySelector('form[action*="/add-phone" i]');
        if (!form) return {error: 'missing_form', url: location.href};
        const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const buttons = [...form.querySelectorAll('button,[role="button"],[role="combobox"]')].map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', role: el.getAttribute('role') || '',
          ariaHaspopup: el.getAttribute('aria-haspopup') || '',
          ariaExpanded: el.getAttribute('aria-expanded') || '',
          ariaLabel: el.getAttribute('aria-label') || '',
          name: el.getAttribute('name') || '', id: el.id || '',
          text: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60),
          visible: vis(el),
        }));
        const selects = [...form.querySelectorAll('select')].map(el => ({
          name: el.getAttribute('name') || '', id: el.id || '', value: el.value || '',
          options: el.options.length, visible: vis(el),
          selectedText: el.selectedIndex >= 0 ? String(el.options[el.selectedIndex].textContent || '') : '',
          firstValues: [...el.options].slice(0, 5).map(o => (o.value || '') + '/' + String(o.textContent || '').trim()),
          colombiaValue: ([...el.options].find(o => /colombia/i.test(o.textContent || '')) || {}).value || '',
        }));
        return {url: location.href, title: document.title || '',
                formHtml: String(form.outerHTML || '').slice(0, 6000),
                buttons, selects};
        """) or {}
    except Exception as exc:  # noqa: BLE001
        info = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        (out_dir / "phone_dom.json").write_text(
            _json.dumps({"tag": tag, "ts": time.time(), "info": info},
                        ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        page = getattr(driver, "page", None)
        if page is not None:
            page.screenshot(path=str(out_dir / "phone_dom.png"))
    except Exception:  # noqa: BLE001
        pass
    logger.warning("[激活] 已把 add-phone 页面结构写到 %s", out_dir / "phone_dom.json")


def _phone_value_looks_empty(driver) -> str:
    """看 add-phone 表单当前号码还在不在。返回 ""(还在) 或一句说明(没了/缺表单)。"""
    try:
        return str(driver.execute_script(r"""
        const form = document.querySelector('form[action*="/add-phone" i]');
        if (!form) return 'missing_form';
        const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const tel = [...form.querySelectorAll('input[type="tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(vis);
        const hid = form.querySelector('input[name="phoneNumber"]');
        const t = tel ? String(tel.value || '').trim() : '';
        const h = hid ? String(hid.value || '').trim() : '';
        if (!t && !h) return 'both_empty';
        if (!t) return 'visible_empty(hidden=' + h + ')';
        return '';
        """) or "")
    except Exception:  # noqa: BLE001
        return ""


def _last_add_phone_error(driver) -> str:
    """从网络记录里取 add-phone/send 的真实失败原因。

    ★ 为什么要这个：页面本身不给错误文案（提交后就是原样重渲染），
      所以老分类器只能靠猜，把「号码被风控拒了」误报成 whatsapp_channel，
      排查被带偏了两个小时。真正的答案在响应体里 —— OpenAI 会回
      {"error":{"code":"fraud_guard","message":"...phone numbers similar to yours..."}}。
      现在按它来报错，用户看到的才是真原因。
    """
    import re as _re

    try:
        events = list(getattr(driver, "_auth_network_events", []) or [])
    except Exception:  # noqa: BLE001
        return ""
    for e in reversed(events):
        if "add-phone/send" not in str(e.get("url") or ""):
            continue
        status = int(e.get("status") or 0)
        if status < 400:
            return ""
        body = str(e.get("body") or "")
        m = _re.search(r'"code"\s*:\s*"([^"]+)"', body)
        return m.group(1) if m else f"HTTP_{status}"
    return ""


def _wait_after_phone_send(driver, timeout: int = 12) -> str:
    end = time.time() + timeout
    last = {}
    force_submitted = False
    while time.time() < end:
        time.sleep(1)
        last = _phone_page_state(driver)
        # 必须优先判断验证码页：页面文案里可能包含 send/limit/check 等词，不能把
        # “Check your phone / Enter the verification code...” 误判成发送失败。
        if _is_phone_code_state(last):
            return 'code_page'
        body = str(last.get('bodyText') or '')
        # ★★ 账号/会话失效优先于一切号码级判断（2026-10-16）：
        #   号还没填/刚提交就先失效时，上游错误可能是别的词，别把账号问题当号码问题。
        dead = _account_dead_reason(body)
        if dead:
            _dump_phone_debug(driver, last, 'account_dead')
            raise RuntimeError(
                f"account_dead: 账号或登录会话已失效（页面出现 {dead!r}），换号无用；"
                f"页面原文={body[:240]}"
            )
        # ★ 先看网络里 OpenAI 到底怎么回的 —— 这比猜页面状态准得多。
        upstream = _last_add_phone_error(driver)
        if upstream:
            _dump_phone_debug(driver, last, upstream)
            if upstream == "fraud_guard":
                raise RuntimeError(
                    "fraud_guard: 这个号被 OpenAI 风控拒了（原文：We've detected suspicious "
                    "behavior from phone numbers similar to yours）。这是号码/号段问题，不是页面问题"
                )
            raise RuntimeError(f"add_phone_rejected:{upstream} 上游拒绝该号码 body={body[:160]}")
        reason = _classify_phone_page_failure(last)
        if reason:
            _dump_phone_debug(driver, last, reason)
            raise RuntimeError(f"{reason}: {body[:240]}")
        # 仍在 add-phone 且字段有 aria-invalid，认为号码被拒。
        if _is_add_phone_page(driver):
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
            if invalid:
                raise RuntimeError(f"invalid_phone: add-phone input aria-invalid state={last}")
            # Cloak/React-Aria 场景下 btn.click 可能只聚焦没触发表单提交；补一次 requestSubmit。
            if not force_submitted and time.time() > end - timeout + 3:
                info = _force_submit_add_phone_form(driver)
                logger.info("[激活] add-phone 点击后仍停留本页，补执行 form.requestSubmit：%s", info)
                force_submitted = True
                time.sleep(2)
    if _is_phone_code_state(last) or _is_phone_code_page(driver):
        return 'code_page'
    if _is_add_phone_page(driver):
        _dump_phone_debug(driver, last, "send_not_accepted")
        raise RuntimeError(f"send_not_accepted: 提交后仍停留在 add-phone state={last}")
    return 'unknown'


def _wait_after_phone_otp_submit(driver, timeout: int = 20) -> str:
    """手机验证码提交后等待结果。

    成功时通常会跳出 phone-verification，进入 consent/workspace/callback；不能在提交后
    3 秒立刻读取旧页面文案并按 send_limited 判失败。只有明确仍在手机号流程且出现错误时
    才返回失败。
    """
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(1)
        current = str(getattr(driver, "current_url", "") or "")
        if _is_callback_url(current):
            return "callback"
        last = _phone_page_state(driver)
        # 已离开手机验证码/加手机号页面，说明验证码被接受，后续交给 consent/callback 流程。
        if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
            return "left_phone_flow"
        # 仍在验证码页时，只把明确错误当失败；普通 Check your phone 页面继续等。
        if _is_phone_code_state(last):
            inputs = last.get('inputs') or []
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in inputs)
            body = str(last.get('bodyText') or '').lower()
            # ★★ 先判「账号/会话已失效」：这是账号级结论，换号无用，
            #   必须在判「验证码错/号无效」之前拦下来（2026-10-16 实测见函数上方注释）。
            dead = _account_dead_reason(body)
            if dead:
                raise RuntimeError(
                    f"account_dead: 账号或登录会话已失效（页面出现 {dead!r}），换号无用；"
                    f"页面原文={str(last.get('bodyText') or '')[:240]}"
                )
            if invalid or any(k in body for k in (
                'invalid code', 'incorrect code', 'wrong code', 'expired code',
                'code is invalid', 'code was invalid', '验证码无效', '验证码错误', '验证码已过期',
                '認証コードが無効', 'コードが正しく',
            )):
                raise RuntimeError(f"invalid_phone_code: {(last.get('bodyText') or '')[:240]}")
            continue
        reason = _classify_phone_page_failure(last)
        if reason:
            raise RuntimeError(f"{reason}: {(last.get('bodyText') or '')[:240]}")
    # 超时后再看一次：如果已经离开手机号流程，视为通过；如果仍在验证码页但没明确错误，交给后续流程继续试。
    current = str(getattr(driver, "current_url", "") or "")
    if _is_callback_url(current):
        return "callback"
    last = _phone_page_state(driver)
    if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
        return "left_phone_flow"
    if _is_phone_code_state(last):
        # ★ 这条日志是判「号用过了」还是「提交没生效」的唯一依据：
        #   still_code_page = 页面没前进、也没有错误文案。以前这里什么都不打，
        #   于是「OpenAI 拒了这个号」和「点了提交但 React 没提交」在日志里长得一样。
        #   实测同一张卡第 2 次接码就停在这里，必须能看见页面到底说了什么。
        body = str(last.get('bodyText') or '').replace('\n', ' ')[:400]
        inputs = last.get('inputs') or []
        logger.warning(
            "[激活] 手机验证码提交后仍停在验证码页（未前进、无错误文案）"
            " ariaInvalid=%s inputs=%s body=%r",
            [str(i.get('ariaInvalid') or '') for i in inputs],
            len(inputs), body,
        )
        return "still_code_page"
    return "unknown"


def _classify_phone_page_failure(state: dict) -> str:
    if _is_phone_code_state(state):
        return ''
    # ★ 账号/会话失效优先：号还没填就先失效，换号同样无用（2026-10-16）
    if _account_dead_reason(state.get('bodyText') or ''):
        return 'account_dead'
    # WhatsApp 用 DOM radio value 判断；其它发送失败用服务端/页面错误文本兜底。
    radios = state.get('radios') or []
    if any('whatsapp' in str(r.get('value','')).lower().replace(' ', '') and r.get('checked') for r in radios):
        return 'whatsapp_channel'
    text = str(state.get('bodyText') or '').lower()
    if 'invalid_auth_step' in text or 'invalid auth step' in text:
        return 'invalid_auth_step'
    # ★ 不能靠「正文里出现 whatsapp」判定只有 WhatsApp 通道：
    #   add-phone 页面本身就把 WhatsApp / SMS 两个选项一起列出来，正文里必然有这个词，
    #   结果是把所有发送失败（包括真正的 invalid_phone、以及号码没进 React state）
    #   一律误报成 whatsapp_channel，真实原因被盖住，排查被带偏（2026-10-03 实测）。
    #   只有页面上压根没有 SMS 选项时，才算真的只能走 WhatsApp。
    values = [str(r.get('value', '')).lower().replace(' ', '') for r in radios]
    if any('whatsapp' in v for v in values) and not any('sms' in v for v in values):
        return 'whatsapp_channel'
    if any(k in text for k in ('invalid phone', 'not a valid phone', 'phone number is not valid', '号码无效', '手机号无效')):
        return 'invalid_phone'
    if any(k in text for k in (
        'cannot send', 'could not send', 'unable to send', 'failed to send', 'send failed',
        '发送失败', '发送失败了', '无法发送', '不能发送', '无法向',
        '送信できません', '送信に失敗', '送信できなかった',
    )):
        return 'delivery_refused'
    if any(k in text for k in ('too many', 'rate limit', 'throttle', '频繁', '限流')):
        return 'send_limited'
    return ''

def _sleep_before_phone_retry(attempt: int, max_retries: int, *, prefix: str = "[激活]") -> None:
    """换号前随机等待。

    ★ 2026-10-05 P1 提速：原来是 `uniform(3.0, 8.0)`。
      实测一轮里换 12 次号，光这一项就烧掉 36~96 秒
      （日志里实测：7.6 / 5.6 / 8.0 / 7.4 / 3.1 / 5.2 / 7.1 秒…）。

      降到 **1.2~2.5 秒**的理由：换号本身要先等号池下单（那已经是一段真实延迟），
      再叠 3~8 秒纯等没意义；而且真撞上上游限流，有 `_phone_failure_kind()`
      的 `send_limited` 识别兜着，不靠这个 sleep 防。
    """
    if attempt >= max_retries:
        return
    seconds = random.uniform(1.2, 2.5)
    logger.info("%s 换号前随机等待 %.1f 秒", prefix, seconds)
    time.sleep(seconds)


def _do_phone_verification_if_present(
    driver,
    sms_client=None,
    *,
    max_retries: int = 5,
    code_wait: int = 120,
    human_waiter=None,
) -> None:
    """如果授权流程要求手机号验证，则用接码平台自动完成。

    与原实现的差异仅在于接码调用方式：原实现直接使用 core.sms_provider 的模块级
    配置和 curl 会话，这里改为调用方传入的 core.sms.SmsClient，其余页面动作、
    换号重试与止损逻辑保持不变。

    ``human_waiter(reason)`` 是**半自动模式**注入的「请人手动过验证」回调；
    不传（默认 None）时本函数行为与以前**一模一样**。
    """
    if sms_client is None:
        logger.info("[激活] 未配置接码平台，跳过手机验证步骤")
        return

    # 页面没有手机号输入框就直接返回。
    # ★ 这里以前只等 8 秒。实测：选完账号后 /add-phone 的渲染经常超过 8 秒
    #   （走代理时更慢），于是被判成「不是手机流程」跳过，接着在 add-phone 页
    #   干等到 callback 超时——注册明明成功了却拿不到凭证。
    try:
        def _phone_page_present() -> bool:
            return _has_strict_add_phone_form(driver) or _is_phone_code_page(driver)

        def _wait_phone_page(seconds: float) -> None:
            end_at = time.time() + seconds
            while time.time() < end_at and not _phone_page_present():
                time.sleep(0.5)

        _wait_phone_page(35)
        # ★★ 2026-10-16 老板实测：人机验证页「点一下刷新就回到正常页面」。
        #   这里的 35 秒等不到 add-phone，最常见的两个原因是挑战页没渲染完、
        #   或者 SPA 卡住 —— 先原地刷新一次再等一轮（不烧号：号码是探测**之后**才 acquire）。
        if not _phone_page_present():
            from .browser_flow import reload_and_settle as _reload_and_settle
            _reload_and_settle(driver, reason="手机验证页 35 秒没渲染出来")
            _wait_phone_page(25)
        if not _phone_page_present():
            # ★ 半自动：这里最可能踩到 Cloudflare 挑战页。以前的处理是走下面的
            #   「未检测到手机号验证页 → 跳过手机步骤」，结果是**激活静默跳过了
            #   手机验证**，交付出去的号是残缺的，而日志只留一句 INFO。
            #   所以先请人手动过验证（不关浏览器），再重新探测一遍。
            #   （号码是在这个探测**之后**才 acquire 的，所以这里还不会烧号。）
            if human_waiter is not None:
                human_waiter("手机验证页没渲染出来（多半被人机验证挡住了）")
                _wait_phone_page(60)
        if not _phone_page_present():
            raise RuntimeError("not_phone_flow")
    except Exception:
        try:
            where = str(getattr(driver, "current_url", "") or "")
        except Exception:  # noqa: BLE001
            where = "?"
        logger.info("[激活] 未检测到手机号验证页，跳过手机步骤（当前页 %s）", where)
        return

    last_err = None
    # 每次都记一笔 (号段, 原因标签)：只用于最后那句报错，**不做任何止损**（见上面注释）。
    attempt_notes: list[tuple[str, str]] = []
    for attempt in range(1, max_retries + 1):
        # ★ 用户在界面上点了「停止」要能立刻停住。
        #   这个换号循环以前不查停止标志，点停止之后还会继续烧号
        #   （2026-10-03 实测：点了停止，状态还是 running）。
        from .stopflag import check_stop

        check_stop()
        activation_id = None
        # ★ 跟 activation_id 一样先置空：下面 except 里要用 phone 算号段，
        #   而 acquire() 自己抛错时 phone 还没赋值 —— 不初始化就是 NameError。
        phone = ""
        try:
            activation_id, phone = sms_client.acquire()
            logger.info("[激活] 手机验证尝试 %s/%s，平台=%s", attempt, max_retries, sms_client.label)
            _ensure_add_phone_input(driver, reason=f"attempt-{attempt}")
            if attempt == 1:
                # 只在第一轮采一次现场，够诊断又不拖慢换号
                _dump_phone_dom(driver, f"attempt-{attempt}")
            # ★★ 顺序很关键：**先切 SMS 通道，再填号码**。
            #    以前是「填号 → 校验 → 切通道 → 重填」。点通道会让表单重渲染，
            #    把国家选择器打回默认（美国）；重填时 JS 看到 DOM 里 select.value
            #    已经是 CO 就跳过切换，可 React 的 state 已经变回美国 ——
            #    号码于是被按 +1 重排成 (301) 986-7674，每一单都死在这里（2026-10-03 实测）。
            #    先把通道定下来，后面填的东西就不会被清。
            # ★★ 通道和号码互相踩：点 SMS 通道会让表单重渲染、把国家/号码打回默认；
            #    反过来填号码又会把通道顶回 WhatsApp（2026-10-03 实测，两个方向都撞过）。
            #    所以不指望一次到位，改成收敛循环：反复「切通道 → 填号」，
            #    直到两者同时就位（一般 1~2 次就稳）。
            phone_fill = {}
            country_after = {}
            ok_country = False
            want_dial = _dial_of(phone)
            for settle in range(1, 4):
                _select_sms_channel_or_raise(driver)
                # ★ 切完通道必须等 React 重渲染完再填号。
                #   不等的话我们的国家写入赶在重渲染之前，会被它覆盖回默认国家，
                #   号码就按错误国家格式化了（2026-10-03 实测：第 2 圈「切完立刻填」
                #   必然失败，第 1 圈因为有这段停顿就正常）。
                _blur_active_input_and_wait(driver, label=f"短信通道确认（第 {settle} 圈）")
                # ★ 不用 Python 去「点」国家选择器：它是原生 <select> 被一个 <button> 盖住，
                #   Playwright 三种策略全失败（element is covered by <BUTTON>，实测）。
                #   真正管用的是 _set_phone_value 里那次原生 setter 切换。
                # ★ 不要用键盘逐字输入号码：号码框带格式化面具，一位一位打会被它
                #   按当前国家边打边重排，实测哥伦比亚号被打成 (572) 217-2403。
                # 第 2 次起额外用真实键盘复核一遍：JS 写值 React 有时不认
                # （DOM 全对、React state 是空的，提交时页面说「需要电话号码」）。
                # 第 1 次不加，因为 JS 一次写入通常就够，加键盘会慢一点。
                phone_fill = _set_phone_value(
                    driver, f"+{phone}", timeout=10, keyboard=(attempt >= 2)
                )
                country_after = _read_phone_country(driver)
                if _sms_channel_checked(driver):
                    break
                logger.warning("[激活] 填号把 SMS 通道顶回 WhatsApp 了，重来一次（%s/3）", settle)
            else:
                logger.warning("[激活] 通道与号码没能同时就位，仍尝试提交")

            # 国家选择器我们不再主动切（切了也会被 React 改回去），
            # 改成让页面按填进去的完整 E.164 自己识别国家。这里只记录实况供排查。
            logger.info("[激活] 手机号写入结果：e164=%s 可见框=%s 国家控件=%s（页面自行识别）",
                        phone_fill.get("e164"), phone_fill.get("visibleValue"), country_after)
            _blur_active_input_and_wait(driver, label="手机号输入完成")
            _verify_add_phone_value_before_submit(driver, str(phone_fill.get("e164") or f"+{phone}"))
            # ★ 2026-10-21 现场实证：提交前页面若仍显示「Phone number required」，
            #   说明 React 的受控输入还是空的（号码只进了 tel 面具框和 hidden 框）。
            #   这时点 Continue 等于空点 —— 就是老板看到的「号码页不填号一直点继续」。
            #   先用真实键盘在当前焦点重打一遍，仍不行就明确报错（别再把 5 个号烧光）。
            if _phone_required_error_visible(driver):
                logger.warning("[激活] 提交前页面仍报「Phone number required」，改用页面级真实键盘重打号码")
                try:
                    _type_phone_with_keyboard(driver, phone_fill if isinstance(phone_fill, dict) else {"e164": f"+{phone}"})
                except Exception as kb_exc:  # noqa: BLE001
                    logger.warning("[激活] 键盘重打号码失败：%s", str(kb_exc)[:160])
                _blur_active_input_and_wait(driver, label="键盘重打手机号")
            if _phone_required_error_visible(driver):
                if os.environ.get("GRL_PHONE_TRACE", "").strip() not in ("", "0"):
                    _dump_phone_debug(driver, _phone_page_state(driver), "phone_required_after_fill")
                raise RuntimeError(
                    "phone_number_required: 号码写不进页面受控输入（页面仍要求填号码）。"
                    "这不是号码被拒，先别换号 —— 换了也是同一个坑。 "
                    f"fill={phone_fill}"
                )
            logger.info("[激活] 手机号提交前校验通过")
            if os.environ.get("GRL_PHONE_TRACE", "").strip() not in ("", "0"):
                _dump_phone_debug(driver, _phone_page_state(driver), "before_submit")
            _click_add_phone_continue_button(driver, timeout=10)
            _wait_page_settle_after_submit()

            # 等页面进入 phone-verification；号码无效/无法发送/WhatsApp 通道时立刻换号。
            _wait_after_phone_send(driver, timeout=15)
            logger.info("[激活] 已进入手机验证码页")

            sms_client.set_status(activation_id, 1)
            sms_code = sms_client.wait_code(activation_id, max_wait=code_wait)
            logger.info("[激活] 已收到手机验证码")
            _type_otp(driver, sms_code)
            human_delay("otp_input")
            if not _click_if_present(driver, ["button[type='submit']", "input[type='submit']"], timeout=10):
                raise RuntimeError(
                    f"verify_submit_missing: 未找到手机验证提交按钮 state={_phone_page_state(driver)}"
                )
            outcome = _wait_after_phone_otp_submit(driver, timeout=25)
            # ★ 2026-10-06：still_code_page 表示「点过提交，但页面既没前进、也没有任何
            #   错误文案」。以前这里不管结果一律 continue，于是这一轮注定在 callback
            #   处超时 5 分钟，而且日志里**看不出**是「号被 OpenAI 拒了」还是「提交没生效」。
            #   邮箱 OTP 那条路早就用「强制同步 + 重试」治过同一个毛病，这里照做。
            submit_retries = 0
            while outcome == "still_code_page" and submit_retries < PHONE_OTP_SUBMIT_RETRIES:
                submit_retries += 1
                logger.warning(
                    "[激活] 手机验证码提交后页面没动，强制同步并重试提交（第 %s/%s 次）",
                    submit_retries, PHONE_OTP_SUBMIT_RETRIES,
                )
                _set_otp_value_via_js(driver, sms_code)
                human_delay("otp_input")
                if not _click_if_present(
                    driver, ["button[type='submit']", "input[type='submit']"], timeout=10
                ):
                    break
                outcome = _wait_after_phone_otp_submit(driver, timeout=25)
                logger.info("[激活] 手机 OTP 重试提交后状态：%s", outcome)
            if outcome == "still_code_page":
                # 到这里说明真没被接受。抛错而不是硬着头皮等 callback：
                # 让上面的换号逻辑去换一个号，也把真实原因留在错误信息里。
                # ★ 文案只说**观测到的**事实，不写推断出来的原因：
                #   「不是验证码错误、也不是号无效」是能证的（那两种情况会走
                #   invalid_phone_code / _classify_phone_page_failure 抛别的错），
                #   但「OpenAI 收了码没放行」证不了 —— 原始页面状态在上一行的
                #   WARNING 日志里（ariaInvalid / inputs / body 原文），要判因去看它。
                raise RuntimeError(
                    "phone_otp_not_accepted: 提交手机验证码后页面始终没有前进"
                    f"（已强制同步并重试 {submit_retries} 次），"
                    "且既不是「验证码错误」也不是「号无效」；"
                    "页面原文见上一条 WARNING 日志"
                )
            logger.info("[激活] 手机 OTP 提交后状态：%s", outcome)
            # ★ 2026-10-17：把「这一单真正吃下的号」挂到 driver 上，由调用方写进账号记录。
            #   长效号（长效号源）不可退但**有效期 22 天**，能不能复用同一个号吃第二单，
            #   是它成本能否压到近零的关键 —— 没有这个字段就永远统计不了。
            try:
                setattr(driver, "_grl_phone_accepted", phone)
            except Exception:  # noqa: BLE001
                pass
            sms_client.complete(activation_id)
            return
        except Exception as exc:
            last_err = exc
            err_text = str(exc) or ""
            logger.warning("[激活] 手机验证失败，准备换号：%s", err_text[:240])
            if activation_id:
                sms_client.cancel(activation_id)
            # 余额不足 / 无号：重试多少次都不会成功，立即止损。
            # ★ 2026-10-06：先按**异常类型**判断，关键字表只作兜底。
            #   号池的「上游暂时没有可用资源」（ACCOUNT_POOL_UPSTREAM_BUSY）走的是
            #   SmsNoNumbers，但它的文案一个关键字都不命中 —— 那才是真正的漏网点。
            #   类型判断不会被文案改动骗过，这正是上次漏掉它的原因。
            if isinstance(exc, SmsNoNumbers) or any(key in err_text for key in (
                "NO_BALANCE", "NO_NUMBERS", "余额不足", "暂无可用号码",
                "没有可用号码", "insufficient", "not enough balance",
            )):
                raise RuntimeError(
                    f"接码平台余额不足或无可用号码，已停止换号止损：{err_text[:180]}"
                ) from exc
            if "invalid_auth_step" in err_text:
                raise RuntimeError(
                    "手机号流程进入 invalid_auth_step，授权状态未正常跳转，已停止继续换号"
                ) from exc
            # ★ 2026-10-21：号码根本写不进页面受控输入 —— 换号也是同一个坑，
            #   一轮 5 个号全白烧（现场：号码页一直空着、Continue 被反复点）。
            #   立刻停，别再买号。
            if "phone_number_required" in err_text:
                raise RuntimeError(
                    "phone_number_required: 号码写不进页面受控输入（页面仍报「Phone number required」），"
                    "已停止本单激活，不再换号白烧。本次号码：" + (phone or "?")
                ) from exc
            # ★★ 账号/会话级失效：**立刻停**，别再买号（2026-10-16 老板实测）：
            #   账号已封时页面写 "Session ended … no longer valid"，OTP 提交那步
            #   会把它归成"提交后页面没动"，于是又换了 3 个号 —— 那 3 个号都收到了码，
            #   纯浪费（还污染号段战绩）。会话失效换多少号都没用。
            dead = _account_dead_reason(err_text) or ("account_dead" in err_text and "account_dead")
            if dead:
                raise RuntimeError(
                    f"账号或登录会话已失效（{dead}），换号无用，已立即停止本轮激活"
                    f"（本次已换 {attempt} 个号）：{err_text[:200]}"
                ) from exc
            # ★ 这里**只记一笔，不止损**（2026-10-15 老板口径：「跑满 12 个」）。
            #   号段/国家的淘汰交给号池：它按号段记账（5 次失败拉黑）、
            #   下单时主动避开黑名单号段、并按成功率+探索+轮转换国家。
            #   工具提前停 = 号池攒不到样本 = 坏号段永远拉不黑。
            section = _phone_section_key(phone)
            label = _phone_failure_label(err_text)
            attempt_notes.append((section or "未知", label))
            logger.warning(
                "[激活] 第 %s/%s 个号失败：%s · 号段 %s · 号码 %s（本次累计 %s）",
                attempt, max_retries, label, section or "未知", phone or "?",
                " ".join(f"{s}-{n}x{c}" for s, n, c in _tally_attempts(attempt_notes)),
            )
            # 已经离开手机号相关页面说明通过了；否则下一轮回 add-phone 重新填号。
            try:
                if _is_phone_code_page(driver):
                    logger.info("[激活] 仍在手机验证码页，下一轮将重新设置号码")
                else:
                    _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
            except Exception:
                if _is_add_phone_page(driver) or _is_phone_code_page(driver):
                    logger.info("[激活] 仍处于手机号流程，继续换号重试")
                else:
                    logger.info("[激活] 手机输入页已消失，继续后续流程")
                    return
            if attempt < max_retries:
                _refresh_add_phone_for_retry(driver, reason=str(exc)[:120])
            _sleep_before_phone_retry(attempt, max_retries, prefix="[激活]")
    # ★ 最后这句是给人看的**结论**，不是「白费」两个字：
    #   说清 12 个号分别废在哪、以及下一步该动哪里（换出口 IP）。
    if attempt_notes:
        details = "、".join(f"{label} {n} 个（号段 {section}）"
                          for section, label, n in _tally_attempts(attempt_notes))
        sections = sorted({s for s, _ in attempt_notes})
    else:
        details, sections = "（一次都没取到号）", []
    advice = (
        "下一步：① 先等 20~30 分钟再跑 —— 拒号是**成波**出现的，硬砸没意义；"
        "② 如果连着几波都这样，再换一条出口（界面上「代理」那一格）；"
        "③ 号池侧不用你管：它按号段记战绩，坏号段会自动在取号时被跳过。"
    )
    raise RuntimeError(
        f"换号 {max_retries} 次都没成功。本次明细：{details}"
        f"（涉及号段 {'/'.join(s or '未知' for s in sections) or '无'}）。"
        f"最后错误：{last_err}。{advice}"
    )


def _finish_consent_workspace(driver) -> str:
    """点击 Codex consent/workspace 页面里的继续/允许按钮，直到 callback。"""
    end = time.time() + int(CALLBACK_TIMEOUT)
    while time.time() < end:
        callback = _extract_callback_url_from_any_window(driver)
        if callback:
            return callback
        current = str(driver.current_url or "")
        clicked = False
        for selectors in [
            ["//button[contains(., 'Allow')]", "//button[contains(., 'Authorize')]", "//button[contains(., 'Continue')]"],
            ["//button[contains(., 'Select')]", "//button[contains(., 'Use workspace')]", "//button[contains(., 'Confirm')]"],
            ["//button[contains(., '允许')]", "//button[contains(., '授权')]", "//button[contains(., '继续')]", "//button[contains(., '确认')]"],
            ["button[type='submit']"],
        ]:
            if _click_if_present(driver, selectors, timeout=2):
                clicked = True
                human_delay("form")
                break
        if not clicked:
            time.sleep(0.8)
    return _wait_for_callback(driver, timeout=5)




def clear_browser_auth_state(driver) -> None:
    """清空当前 Roxy 浏览器里的 OpenAI/ChatGPT 登录态与缓存，用于注册后复用同一环境跑 Codex。"""
    origins = [
        "https://auth.openai.com",
        "https://chatgpt.com",
        "https://openai.com",
        "https://platform.openai.com",
    ]
    logger.info("[激活] 复用注册窗口：开始清理 Cookie / localStorage / sessionStorage / cache")
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        logger.info("[激活] 已清理浏览器 Cookie")
    except Exception as exc:
        logger.info("[激活] 清理 Cookie 失败，继续尝试其它缓存：%s", str(exc)[:160])
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        logger.info("[激活] 已清理浏览器 Cache")
    except Exception as exc:
        logger.info("[激活] 清理 Cache 失败，继续：%s", str(exc)[:160])
    for origin in origins:
        try:
            driver.execute_cdp_cmd("Storage.clearDataForOrigin", {
                "origin": origin,
                "storageTypes": "all",
            })
            logger.info("[激活] 已清理站点数据：%s", origin)
        except Exception as exc:
            logger.debug("[激活] 清理站点数据失败 %s: %s", origin, exc)
    try:
        driver.get("about:blank")
    except Exception:
        pass
    time.sleep(1.0)
    logger.info("[激活] 注册窗口登录态清理完成，准备开始 Codex 授权")
