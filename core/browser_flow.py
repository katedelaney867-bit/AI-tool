# -*- coding: utf-8 -*-
"""ChatGPT 注册页面流程（提炼自 turb-gpt-free-register/core/roxy_registration.py）。

本模块只做一件事：拿一个 Selenium 风格的 driver，把 ChatGPT 注册流程从
「打开登录页」走到「拿到 accessToken」。不负责浏览器启动、邮箱取码、落库、
并发调度 —— 那些在各自模块里。

被剥离的外围依赖（原文件同名函数的外部耦合共 6 处）：
    config.roxybrowser      -> 本地常量 SELENIUM_TIMEOUT
    config.humanize         -> 本地常量 HUMANIZE_BROWSER_ACTIONS
    config.register         -> 本地常量 REGISTER_PASSWORD
    core.registration_service.check_stop_requested -> core.stopflag.check_stop

请勿手工编辑本文件；改动请改 tools/extract_page_flow.py 后重新生成。
"""
from __future__ import annotations

import logging
import random
import string
import threading
import time
import uuid
from typing import Callable

from .humanize import delay as human_delay

from .stopflag import check_stop

logger = logging.getLogger(__name__)

# ---- 提炼时从原 config 折叠进来的常量 ----
# 这两个由调用方在启动任务前赋值（GUI 可覆盖）。
SELENIUM_TIMEOUT: int = 90
# ★ 注册时用的密码必须是**线程局部**的（2026-10-04 修）：
#   小工具支持并发（concurrency>1），每个 register_one 跑在自己的线程里。
#   以前这是个模块级全局，并发时两个线程互相覆盖 ——
#   结果「记下来的密码」和「真正写进账号的密码」不是同一个，
#   号交付出去买家登不上，而且**完全不会有报错**，是最难查的那种坏。
_REGISTER_STATE = threading.local()


def _set_register_password(value: str) -> None:
    _REGISTER_STATE.password = str(value or "")


def _get_register_password() -> str:
    return str(getattr(_REGISTER_STATE, "password", "") or "")


# 兼容旧引用（只读语义）；真正的读写走上面两个函数。
REGISTER_PASSWORD: str = ""

# 验证码写进输入框后回读不一致时，清空重填的最大次数。
# 分格输入框丢字/跳格错位会让「刚发的正确验证码」被上游判成错码，
# 必须在提交前自己发现并重填，否则会白白烧掉重试次数。
OTP_TYPE_ATTEMPTS: int = 3

# 验证码页刚跳过来时 React 还没渲染完，input 一个都没有。
# 原来是一次性 find_elements，抢在渲染前就抛「找不到 OTP 输入框」，白烧一个码。
# 现在轮询等这么久（秒）。
OTP_INPUT_WAIT: float = 25.0

def _log_prefix(driver=None) -> str:
    """按当前浏览器实现返回注册日志前缀。

    CloakBrowser 复用 Roxy 的页面操作函数；这些共享函数必须跟随实际 driver
    输出 `[Cloak注册]`，避免 Cloak 流程里混入 `[Roxy注册]` 日志。
    """
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
        if driver is not None and driver.__class__.__name__ == "CloakSeleniumDriver":
            return "[Cloak注册]"
    except Exception:
        pass
    return "[Roxy注册]"


def _wait(driver, timeout: int | None = None):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver, timeout or int(SELENIUM_TIMEOUT))


def _safe_get(driver, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = ()) -> None:
    """带容错的页面跳转。

    Roxy/Chrome 150 偶发 `Timed out receiving message from renderer`，实际页面可能已经可用。
    这里超时后先 `window.stop()`，只要当前 URL/DOM 已进入目标页就继续；否则重试一次。
    """
    from selenium.common.exceptions import TimeoutException, WebDriverException

    last_exc: Exception | None = None
    # ★ 这里原来写的是 `getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90)`，而 `_cfg`
    #   在这个文件里**从未定义**（RoxyBrowser 时代的残留）⇒ 每次调用 `_safe_get`
    #   都直接 NameError。本工具对应的常量就是模块级的 SELENIUM_TIMEOUT。
    old_timeout = int(SELENIUM_TIMEOUT or 90)
    hosts = tuple(h.lower() for h in (accept_hosts or ()))
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                driver.set_page_load_timeout(max(10, int(timeout)))
                driver.set_script_timeout(8)
            except Exception:
                pass
            driver.get(url)
            return
        except TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "%s 页面加载超时，尝试停止加载后检查 DOM：url=%s attempt=%s/%s error=%s",
                _log_prefix(driver), url, attempt, attempts, str(exc).splitlines()[0] if str(exc) else "TimeoutException",
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            time.sleep(1.0)
            try:
                current = str(driver.current_url or "").lower()
            except Exception:
                current = ""
            try:
                ready = str(driver.execute_script("return document.readyState || ''") or "")
                has_body = bool(driver.execute_script("return !!document.body"))
            except Exception:
                ready = ""
                has_body = False
            target_ok = any(h in current for h in hosts) if hosts else (url.split("/", 3)[2].lower() in current)
            if target_ok and has_body:
                logger.info(
                    "%s 页面加载虽超时但 DOM 可用，继续流程：current=%s readyState=%s",
                    _log_prefix(driver), current[:180], ready or "-",
                )
                return
            if attempt < attempts:
                try:
                    driver.get("about:blank")
                except Exception:
                    pass
                time.sleep(1.5 * attempt)
                continue
        except WebDriverException as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning("%s 页面跳转失败，准备重试：url=%s attempt=%s/%s error=%s", _log_prefix(driver), url, attempt, attempts, exc)
                time.sleep(1.5 * attempt)
                continue
            raise
        finally:
            try:
                driver.set_page_load_timeout(old_timeout)
            except Exception:
                pass
    raise last_exc or RuntimeError(f"页面跳转失败: {url}")


def _visible(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:
        return False


def _browser_actions_enabled() -> bool:
    try:
        from . import humanize as _hm
        return bool(_hm.ENABLE_HUMANIZE_BROWSER_ACTIONS)
    except Exception:
        return True


def _apply_browser_automation_mask(driver) -> None:
    """连接 Selenium 后尽量降低明显自动化特征；失败不影响主流程。"""
    if not _browser_actions_enabled():
        return
    try:
        script = r"""
        Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (originalQuery) {
          window.navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications'
              ? Promise.resolve({ state: Notification.permission })
              : originalQuery(parameters)
          );
        }
        """
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        try:
            driver.execute_script(script)
        except Exception:
            pass
        logger.info("%s 已注入浏览器自动化特征弱化脚本", _log_prefix(driver))
    except Exception as exc:
        logger.debug("%s 注入自动化特征弱化脚本失败：%s", _log_prefix(driver), exc)


def _human_scroll_to(driver, el) -> None:
    try:
        block = random.choice(["center", "nearest", "center"])
        driver.execute_script("arguments[0].scrollIntoView({block: arguments[1], inline:'nearest'});", el, block)
        if _browser_actions_enabled():
            time.sleep(random.uniform(0.08, 0.35))
            # 轻微滚动抖动，避免每次都精准居中。
            driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(-90, 90))
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el)
    except Exception:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass


def _human_click(driver, el, *, label: str = "") -> None:
    """快速人工化点击。

    之前用 ActionChains 在 Roxy/Chrome 150 上偶发卡住 1-2 分钟，导致邮箱提交很慢。
    这里改为 CDP 派发鼠标事件；没有 CDP 时再用 JS/原生 click 兜底。
    """
    _human_scroll_to(driver, el)
    if not _browser_actions_enabled():
        time.sleep(0.2)
        el.click()
        return
    try:
        human_delay("click")
        point = driver.execute_script(r"""
        const el = arguments[0];
        const r = el.getBoundingClientRect();
        const x = r.left + r.width * (0.30 + Math.random() * 0.40);
        const y = r.top + r.height * (0.35 + Math.random() * 0.30);
        return {x, y, w:r.width, h:r.height};
        """, el) or {}
        x = float(point.get("x") or 0)
        y = float(point.get("y") or 0)
        if hasattr(driver, "execute_cdp_cmd") and x > 0 and y > 0:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            time.sleep(random.uniform(0.035, 0.13))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        else:
            driver.execute_script(r"""
            const el = arguments[0];
            el.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            el.click();
            """, el)
    except Exception as exc:
        logger.debug("%s 人工化点击失败，回退 el.click label=%s err=%s", _log_prefix(driver), label, exc)
        time.sleep(random.uniform(0.12, 0.45))
        try:
            driver.execute_script("arguments[0].click();", el)
        except Exception:
            el.click()


def _human_type_text(driver, el, value: str, *, clear: bool = True, chunk: bool = True) -> None:
    """按字符/小段输入，触发真实 key events；失败时回退 JS setter。

    ``chunk=False`` 关闭「偶尔两个字符一组」的拟人化：对着 React 受控的验证码
    输入框，一次 ``send_keys("79")`` 的两个按键事件会和组件重渲染抢跑，
    字符顺序可能颠倒（实测把 169479 打成了 169497）。验证码一律逐字符。
    """
    if not _browser_actions_enabled():
        if clear:
            try:
                el.clear()
            except Exception:
                pass
        el.send_keys(value)
        return
    try:
        _human_scroll_to(driver, el)
        try:
            _human_click(driver, el, label="input_focus")
        except Exception:
            driver.execute_script("arguments[0].focus();", el)
        if clear:
            from selenium.webdriver.common.keys import Keys
            mod = Keys.COMMAND
            try:
                import platform
                if platform.system().lower() != "darwin":
                    mod = Keys.CONTROL
            except Exception:
                pass
            try:
                el.send_keys(mod, "a")
                time.sleep(random.uniform(0.04, 0.16))
                el.send_keys(Keys.BACKSPACE)
            except Exception:
                try:
                    el.clear()
                except Exception:
                    pass
        text = str(value)
        i = 0
        while i < len(text):
            # 邮箱/密码整体仍逐字符，但偶尔 2 字符一组，节奏更自然。
            step = 2 if (chunk and random.random() < 0.12 and i + 1 < len(text)) else 1
            el.send_keys(text[i:i + step])
            i += step
            human_delay("keystroke")
            if i < len(text) and random.random() < 0.08:
                human_delay("typing_pause")
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            el,
        )
    except Exception as exc:
        logger.debug("%s 人工化输入失败，回退 JS setter err=%s", _log_prefix(driver), exc)
        _set_element_value(driver, el, value)


def _page_warmup(driver, *, reason: str = "") -> None:
    if not _browser_actions_enabled():
        return
    try:
        human_delay("page_warmup")
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(80, 360),
                "y": random.randint(80, 260),
            })
    except Exception:
        pass


def _find_any(driver, selectors: list[str], timeout: int | None = None):
    from selenium.webdriver.common.by import By

    end = time.time() + (timeout or int(SELENIUM_TIMEOUT))
    last = None
    while time.time() < end:
        for selector in selectors:
            try:
                by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
                items = driver.find_elements(by, selector)
                for item in items:
                    if _visible(item):
                        return item
            except Exception as exc:
                last = exc
        time.sleep(0.4)
    raise RuntimeError(f"找不到页面元素: {selectors}; last={last}")


def _click_any(driver, selectors: list[str], timeout: int | None = None) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_click(driver, el, label="click_any")


def _type_any(driver, selectors: list[str], value: str, timeout: int | None = None, clear: bool = True) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_type_text(driver, el, value, clear=clear)


_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input#email-input",
    "input[autocomplete='email']",
]


def _email_entry_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const attrText = el => [
          el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
          el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
          el.getAttribute('data-auth-provider'), el.getAttribute('href'), el.getAttribute('action'),
          el.getAttribute('formaction'), el.getAttribute('value')
        ].filter(Boolean).join(' ').toLowerCase();
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''
        })).slice(0, 30);
        const actions = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({tag: el.tagName, type: el.getAttribute('type') || '', attrs: attrText(el)})).slice(0, 40);
        return {url: location.href, title: document.title, inputs, actions};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _find_visible_email_input_js(driver):
    return driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const selectors = [
      'input[type="email"]',
      'input[name="email"]',
      'input[name="username"]',
      'input#email-input',
      'input[autocomplete="email"]'
    ];
    for (const sel of selectors) {
      const el = [...document.querySelectorAll(sel)].find(visible);
      if (el) return el;
    }
    return null;
    """)


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。"""
    try:
        return bool(driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        if (/oauth|authorize|consent/.test(url) && !/login|signup|identifier|email-verification/.test(url)) return true;
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        if (formsWithEmail) return false;
        const actions = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"]')]
          .map(el => [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('href'),
            el.getAttribute('formaction'), el.value, el.className].filter(Boolean).join(' ').toLowerCase())
          .join(' ');
        return /oauth|authorize|consent|grant|allow/.test(actions) && !/email|username/.test(actions);
        """))
    except Exception:
        return False


def _is_external_idp_url(url: str) -> bool:
    u = str(url or '').lower()
    return any(x in u for x in (
        'accounts.google.', 'google.com/o/oauth', 'appleid.apple.', 'login.microsoftonline.',
        'login.live.', 'github.com/login/oauth', 'facebook.com/', 'saml', 'sso'
    ))


def _assert_not_external_idp(driver, label: str = '') -> None:
    try:
        current = str(driver.current_url or '')
    except Exception:
        current = ''
    if _is_external_idp_url(current):
        raise RuntimeError(f"误入第三方账号授权页（{label}）：{current}")


def _click_email_entry_option(driver) -> bool:
    """点击“邮箱方式”入口；只看 DOM 技术属性，不看按钮可见文案，并显式排除 Google 等第三方。"""
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，跳过邮箱入口兜底点击", _log_prefix(driver))
        return False
    target = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const attrText = el => {
      const own = [
        el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
        el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
        el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'), el.getAttribute('href'), el.getAttribute('action'),
        el.getAttribute('formaction'), el.getAttribute('value'), el.getAttribute('aria-label'), el.className
      ].filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' ')).join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
    const good = /(^|[^a-z])(email|mail|username|passwordless|otp|magic)([^a-z]|$)/;
    const candidates = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(visible)
      .map(el => ({el, attrs: attrText(el), hasLogo: !!el.querySelector('img,svg,use')}))
      .filter(x => good.test(x.attrs) && !bad.test(x.attrs) && !x.hasLogo);
    if (candidates.length !== 1) return null;
    candidates[0].el.scrollIntoView({block:'center'});
    return candidates[0].el;
    """)
    if target:
        _human_click(driver, target, label="email_entry")
        return True
    return False


def _wait_for_email_input(driver, timeout: int | None = None):
    """进入邮箱登录/注册方式并返回已找到的可见邮箱输入框。"""
    end = time.time() + (timeout or int(SELENIUM_TIMEOUT))
    last_state = None
    clicked_email_option = False
    while time.time() < end:
        el = _find_visible_email_input_js(driver)
        if el:
            return el
        last_state = _email_entry_state(driver)
        if not clicked_email_option and _click_email_entry_option(driver):
            clicked_email_option = True
            time.sleep(1.0)
            _assert_not_external_idp(driver, "点击邮箱入口后")
            continue
        time.sleep(0.4)
    raise RuntimeError(f"找不到邮箱输入框/邮箱入口（未使用文字识别），state={last_state}")


def _type_email_address(driver, email: str, timeout: int | None = None) -> None:
    """进入邮箱登录/注册方式并填写邮箱。全程不依赖页面可见文字。"""
    el = _wait_for_email_input(driver, timeout=timeout)
    _human_type_text(driver, el, email, clear=True)


def _submit_nearest_form_for_active_input(driver) -> bool:
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，禁止执行邮箱提交", _log_prefix(driver))
        return False
    result = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_email_input'};
    const value = String(input.value || '').trim();
    if (!value || !value.includes('@')) return {ok:false, reason:'email_value_not_ready', value};
    const form = input.closest('form');
    if (!form) return {ok:false, reason:'missing_form'};

    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|sso|saml|idp|provider|authorize|consent|grant|allow/;
    const attrText = el => {
      const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
        el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
        el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
        .filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' '))
        .join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const inputRect = input.getBoundingClientRect();
    const formId = form.getAttribute('id') || '';
    const scopedButtons = [
      ...form.querySelectorAll('button,input[type="submit"]'),
      ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
    ].filter((el, idx, arr) => arr.indexOf(el) === idx);
    const rawButtons = scopedButtons
      .filter(visible)
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const attrs = attrText(el);
        const hasLogo = !!el.querySelector('img,svg,use');
        const isBad = bad.test(attrs) || hasLogo;
        const belowInput = r.top >= inputRect.bottom - 10;
        const distance = Math.max(0, r.top - inputRect.bottom) + Math.abs((r.left + r.right) / 2 - (inputRect.left + inputRect.right) / 2) / 10;
        const cls = String(el.className || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        // ChatGPT 新版邮箱页的主按钮形如：
        // <button class="... btn-primary ... w-full ..." type="submit"><div>続行</div></button>
        // 优先选择同 form 下的 primary submit，而不是因为多个按钮距离接近误判歧义。
        const isPrimarySubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && type === 'submit'
          && (/\bbtn-primary\b/.test(cls) || /\b_primary_/.test(cls) || /\bw-full\b/.test(cls));
        const score = (isPrimarySubmit ? 1000 : 0) + (type === 'submit' ? 100 : 0) - distance;
        return {el, idx, attrs, isBad, hasLogo, belowInput, distance, score, isPrimarySubmit, tag: el.tagName, type};
      });
    const safe = rawButtons.filter(x => !x.isBad && x.belowInput)
      .sort((a,b) => b.score - a.score || a.distance - b.distance || a.idx - b.idx);
    if (!safe.length) {
      return {ok:false, reason:'no_safe_submit', buttons: rawButtons.map(x => ({idx:x.idx, isBad:x.isBad, hasLogo:x.hasLogo, belowInput:x.belowInput, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    // 多个安全按钮时，若没有明确 primary submit，且距离接近，才认为页面歧义。
    if (!safe[0].isPrimarySubmit && safe.length > 1 && Math.abs(safe[0].distance - safe[1].distance) < 8) {
      return {ok:false, reason:'ambiguous_submit', buttons: safe.slice(0,3).map(x => ({idx:x.idx, distance:x.distance, score:x.score, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    const target = safe[0].el;
    target.scrollIntoView({block:'center'});
    window.__roxy_email_submit_debug = {at: Date.now(), targetAttrs: safe[0].attrs.slice(0,240), buttonCount: rawButtons.length, primary:safe[0].isPrimarySubmit};
    return {ok:true, reason:safe[0].isPrimarySubmit ? 'primary_submit' : 'safe_submit', target, targetAttrs:safe[0].attrs.slice(0,160), primary:safe[0].isPrimarySubmit};
    """) or {}
    if result.get("ok"):
        target = result.get("target")
        if target:
            _human_click(driver, target, label="email_submit")
        else:
            logger.warning("%s 邮箱提交未返回目标元素，回退 requestSubmit", _log_prefix(driver))
            driver.execute_script("document.querySelector('form')?.requestSubmit?.();")
        logger.info("%s 邮箱表单安全提交：%s", _log_prefix(driver), result)
        time.sleep(0.8)
        _assert_not_external_idp(driver, "提交邮箱后")
        return True
    logger.warning("%s 未执行邮箱提交：%s", _log_prefix(driver), result)
    return False


def _current_email_input_value(driver) -> str:
    try:
        state = _email_input_value_state(driver)
        for item in state.get("inputs") or []:
            value = str(item.get("value") or "").strip()
            if "@" in value:
                return value
    except Exception:
        pass
    return ""


def _stabilize_email_input_before_submit(driver, email: str) -> dict:
    """提交前把 DOM value / React 受控状态 / blur-change 状态统一稳定下来。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;

        // 让 React/表单校验尽量收到完整输入链路。
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        return {
          ok:true,
          value: input.value,
          active: document.activeElement === input,
          hasForm: !!form,
          hasSubmit: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : null,
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_form_stable(driver, email: str) -> dict:
    """第一次提交就按“补交成功”的方式执行：稳定 value 后 Enter + DOM click。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const editable = el => visible(el) && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(editable);
        if (!input) return {ok:false, reason:'missing_email_input'};
        if (!email || !email.includes('@')) return {ok:false, reason:'empty_email', value: email};

        const form = input.closest('form');
        if (!form) return {ok:false, reason:'missing_form'};

        const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
        const attrText = el => {
          const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
            el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
            .filter(Boolean).join(' ');
          const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
            .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
              x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
              .filter(Boolean).join(' '))
            .join(' ');
          return `${own} ${desc}`.toLowerCase();
        };

        const formId = form.getAttribute('id') || '';
        const buttons = [
          ...form.querySelectorAll('button,input[type="submit"]'),
          ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
        ].filter((el, idx, arr) => arr.indexOf(el) === idx)
          .filter(el => visible(el) && !bad.test(attrText(el)) && !el.querySelector('img,svg,use'));
        const submit = buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0] || null;
        if (!submit) return {ok:false, reason:'missing_safe_submit'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        submit.scrollIntoView({block:'center', inline:'nearest'});

        // 不要在 execute_script 同步执行 submit.click()：
        // ChromeDriver 会等前端 submit/navigation，Roxy/Chrome 150 上可能卡到 page/script timeout。
        // setTimeout 让 Selenium 先返回，点击在页面事件循环里异步发生，和补交逻辑一致。
        setTimeout(() => {
          try {
            input.focus();
            // 只触发一种提交路径。旧实现先派发 Enter 再 click，React 表单可能
            // 发出两次 NextAuth signin，第二次会带着已消费的 CSRF/state 落到
            // /api/auth/error。
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);

        window.__roxy_email_submit_debug = {
          at: Date.now(),
          mode: 'stable_async_enter_click',
          value: input.value,
          submitAttrs: attrText(submit).slice(0, 240)
        };
        return {
          ok:true,
          reason:'stable_async_enter_click',
          value: input.value,
          submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          submitAttrs: attrText(submit).slice(0, 180),
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_step(driver, email: str | None = None) -> None:
    # 不再优先走浏览器内 NextAuth fetch：
    # Roxy/Chrome 150 下 execute_async_script + fetch 偶发卡到 script timeout；
    # 实测 UI 首次提交后若停在 /auth/login?email=...，由 _recover_email_submit_if_stuck 补交表单更稳定。
    email_value = str(email or _current_email_input_value(driver) or "").strip()
    stable = _stabilize_email_input_before_submit(driver, email_value)
    logger.info("%s 邮箱提交前状态稳定：%s", _log_prefix(driver), stable)
    time.sleep(random.uniform(0.8, 1.8) if _browser_actions_enabled() else 0.4)

    stable_submit = _submit_email_form_stable(driver, email_value)
    if stable_submit.get("ok"):
        logger.info("%s 邮箱稳定表单提交：%s", _log_prefix(driver), stable_submit)
        time.sleep(1.0)
        _assert_not_external_idp(driver, "稳定表单提交邮箱后")
        return
    logger.warning("%s 邮箱稳定表单提交失败，回退 UI 点击提交：%s", _log_prefix(driver), stable_submit)
    if _submit_nearest_form_for_active_input(driver):
        return
    raise RuntimeError(f"无法提交邮箱步骤（拒绝按页面文字或首个 submit 兜底，避免误点第三方登录），state={_email_entry_state(driver)}")


def _recover_email_submit_if_stuck(driver, email: str) -> dict:
    """邮箱提交后停在 /auth/login?email= 且输入框被清空时，补一次原生表单提交。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        setTimeout(() => {
          try {
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);
        return {ok:true, reason:'resubmitted_email_form', value: input.value, hasForm: !!form, hasSubmit: !!submit};
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_via_browser_nextauth(driver, email: str) -> dict:
    """在 Roxy 浏览器上下文里调用 ChatGPT NextAuth signin。

    UI submit 在 Roxy/Chrome 150 上会偶发只跳到 `/auth/login?email=...` 后停住。
    这里改走浏览器页面内 fetch，仍使用当前 Roxy 浏览器的 cookie / 指纹环境，
    拿到 auth.openai.com authorize URL 后让浏览器跳转。
    """
    try:
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" not in current:
            return {"ok": False, "reason": "not_on_chatgpt", "url": current[:180]}
    except Exception:
        current = ""

    did = str(uuid.uuid4())
    auth_log_id = str(uuid.uuid4())
    old_script_timeout = int(SELENIUM_TIMEOUT or 90)
    try:
        try:
            driver.set_script_timeout(25)
        except Exception:
            pass
        result = driver.execute_async_script(r"""
        const email = String(arguments[0] || '').trim();
        const did = String(arguments[1] || '');
        const authLogId = String(arguments[2] || '');
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const csrfResp = await fetch('/api/auth/csrf', {
              method: 'GET',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              }
            });
            const csrfText = await csrfResp.text();
            let csrfData = {};
            try { csrfData = JSON.parse(csrfText); } catch (_) {}
            const csrfToken = csrfData.csrfToken || '';
            if (!csrfResp.ok || !csrfToken) {
              done({ok:false, stage:'csrf', status:csrfResp.status, body:csrfText.slice(0, 500)});
              return;
            }

            const q = new URLSearchParams({
              prompt: 'login',
              'ext-oai-did': did,
              auth_session_logging_id: authLogId,
              'ext-passkey-client-capabilities': '11111',
              screen_hint: 'login_or_signup',
              login_hint: email
            });
            const body = new URLSearchParams({
              callbackUrl: 'https://chatgpt.com/',
              csrfToken,
              json: 'true'
            });
            const resp = await fetch('/api/auth/signin/openai?' + q.toString(), {
              method: 'POST',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              },
              body: body.toString()
            });
            const text = await resp.text();
            let data = {};
            try { data = JSON.parse(text); } catch (_) {}
            let url = data.url || '';
            if (!resp.ok || !url) {
              done({ok:false, stage:'signin', status:resp.status, body:text.slice(0, 700)});
              return;
            }

            try {
              const u = new URL(url, location.href);
              if (!u.searchParams.get('screen_hint')) u.searchParams.set('screen_hint', 'login_or_signup');
              if (!u.searchParams.get('login_hint')) u.searchParams.set('login_hint', email);
              if (!u.searchParams.get('ext-oai-did')) u.searchParams.set('ext-oai-did', did);
              if (!u.searchParams.get('auth_session_logging_id')) u.searchParams.set('auth_session_logging_id', authLogId);
              url = u.toString();
            } catch (_) {}
            window.location.assign(url);
            done({ok:true, stage:'redirect', url:url.slice(0, 260)});
          } catch (e) {
            done({ok:false, stage:'exception', error:String(e && (e.stack || e.message) || e).slice(0, 700)});
          }
        })();
        """, email, did, auth_log_id) or {}
        return result if isinstance(result, dict) else {"ok": False, "reason": "invalid_result", "result": str(result)[:300]}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            driver.set_script_timeout(old_script_timeout)
        except Exception:
            pass


def _email_input_value_state(driver) -> dict:
    """读取当前可见邮箱框状态，用于提交后确认是否真的进入下一步。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .filter(visible)
          .map(el => ({type: el.getAttribute('type') || '', name: el.name || '', id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''}));
        return {url: location.href, inputs};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_login_page_still_present(driver) -> bool:
    state = _email_input_value_state(driver)
    return bool(state.get("inputs"))


def _wait_email_submit_next_state(driver, email: str, timeout: int = 18) -> str:
    """邮箱提交后等待进入 password / otp / logged_in；仍停留邮箱页则返回 email_page。

    Cloak/Playwright 路径里，点击 submit 后页面经常先发生一次 SPA 导航：
    `chatgpt.com/auth/login?email=...`，同时 React 会短暂把 email input 清空。
    旧逻辑一看到空 input 就立刻返回 `email_cleared`，导致在真正跳到
    `auth.openai.com/...` 前过早重填，形成“提交 -> 清空 -> 重填”的循环。
    这里对 email_cleared 做去抖：只记录并继续观察几秒；若期间进入
    password/otp/login_password/logged_in 则按真实状态返回，持续清空才让上层重试。
    """
    end = time.time() + timeout
    last = None
    cleared_seen_at: float | None = None
    cleared_last_log_at = 0.0
    cleared_recover_done = False
    expected_email = str(email or "").strip().lower()
    while time.time() < end:
        current_url = str(getattr(driver, "current_url", "") or "")
        if "/api/auth/error" in current_url:
            try:
                diagnostic = driver.execute_script(r"""
                return {
                  url: location.href,
                  title: document.title || '',
                  text: (document.body?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 900)
                };
                """) or {}
            except Exception as exc:
                diagnostic = {"url": current_url, "error": f"{type(exc).__name__}: {exc}"}
            network = list(getattr(driver, "_auth_network_events", []) or [])[-12:]
            logger.error("%s ChatGPT 认证错误页：page=%s network=%s", _log_prefix(driver), diagnostic, network)
            return "auth_error"
        if _has_access_token(driver):
            return "logged_in"
        if _is_login_password_page(driver):
            return "login_password"
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        state = _email_input_value_state(driver)
        last = state
        inputs = state.get("inputs") or []
        if inputs:
            values = [str(i.get("value") or "") for i in inputs]
            url = str(state.get("url") or "")
            has_blank = any(v == "" for v in values)
            has_expected = any(v.strip().lower() == expected_email for v in values)
            if has_blank and not has_expected:
                now = time.time()
                if cleared_seen_at is None:
                    cleared_seen_at = now
                # URL 已带 email 查询参数时更像是提交后的中间态，给它更长观察窗口。
                debounce = 18.0 if ("/auth/login" in url and "email=" in url) else 5.0
                if now - cleared_last_log_at > 2.0:
                    logger.info(
                        "%s 邮箱提交后检测到输入框短暂清空，继续等待跳转：elapsed=%.1fs debounce=%.1fs url=%s",
                        _log_prefix(driver), now - cleared_seen_at, debounce, url[:180],
                    )
                    cleared_last_log_at = now
                if (
                    not cleared_recover_done
                    and "/auth/login" in url
                    and "email=" in url
                    and now - cleared_seen_at >= 2.0
                ):
                    recover = _recover_email_submit_if_stuck(driver, email)
                    cleared_recover_done = True
                    logger.info("%s 邮箱提交后仍停留在 login?email，中途补交一次表单：%s", _log_prefix(driver), recover)
                if now - cleared_seen_at >= debounce:
                    return "email_cleared"
            else:
                cleared_seen_at = None
            # 仍是当前邮箱页，继续短等。
        time.sleep(0.8)
    logger.info("%s 邮箱提交后等待下一步超时，最后邮箱页状态=%s", _log_prefix(driver), last)
    return "email_page" if _is_email_login_page_still_present(driver) else "unknown"


def _is_navigation_error(exc: BaseException) -> bool:
    """页面在 JS 评估途中跳走导致的报错。

    这类错不是业务失败：提交邮箱本身可能已经生效，只是我们把状态读崩了。
    以前它直接冒泡出去，整轮注册就报废（实测连挂两次）。
    """
    text = str(exc).lower()
    return any(key in text for key in (
        "execution context was destroyed",
        "execution context destroyed",
        "cannot find context with specified id",
        "most likely because of a navigation",
        "target closed",
        "page closed",
        "session deleted",
    ))


# Cloudflare 挑战页的标题（各语言都遇到过），配合「找不到邮箱输入框」一起判定。
_CF_CHALLENGE_TITLES = (
    "just a moment", "请稍候", "稍等", "um momento", "un momento", "un instant",
    "einen moment", "juste un moment", "ひととき", "少々お待ち", "잠시만",
    "momentaufnahme", "attendez",
)


def _looks_like_cloudflare_challenge(driver) -> bool:
    """登录页是不是被 Cloudflare 挡在门外了。

    轮换型住宅代理经常抽到被拦的出口 IP，表现出来就是「登录页没有邮箱输入框」。
    识别出来就能换一条连接（=换一张 IP）重试，而不是直接把整轮判死。
    """
    try:
        title = str(driver.execute_script("return document.title || '';") or "")
    except Exception:  # noqa: BLE001
        return False
    low = title.strip().lower()
    if not any(key in low for key in _CF_CHALLENGE_TITLES):
        # 标题还没变成 "Just a moment" 时，退一步看 Cloudflare 挑战页的特征：
        # 挑战平台脚本/iframe，或者那句 "Enable JavaScript and cookies to continue"。
        try:
            marks = driver.execute_script(
                "return !!(document.querySelector('script[src*=\"/cdn-cgi/challenge-platform\"]')"
                " || document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]')"
                " || /enable javascript and cookies to continue/i.test(document.body"
                " ? (document.body.innerText || '') : ''));"
            )
        except Exception:  # noqa: BLE001
            return False
        if not marks:
            return False
    try:
        has_email = bool(driver.execute_script(
            "return !![...document.querySelectorAll('input')].find(e => "
            "(e.type === 'email') || /email|username/i.test((e.name || '') + (e.id || '')));"
        ))
    except Exception:  # noqa: BLE001
        return True
    return not has_email


def _page_is_empty_and_stuck(driver) -> bool:
    """页面卡在「一个可见输入框都没有」的状态。

    两种情况都算：
      - auth.openai.com 上 Auth0 的前端 bundle 没渲染出来；
      - chatgpt.com/auth/login 上是 Cloudflare 挑战页（标题可能还没变成 Just a moment）。
    表现一样是「出口 IP 被拦」，换一条连接通常就好，所以不该一次判死。
    """
    try:
        url = str(getattr(driver, "current_url", "") or "")
        if not any(k in url for k in ("chatgpt.com", "auth.openai.com", "openai.com")):
            return False
        return not bool(driver.execute_script(
            "return [...document.querySelectorAll('input')]"
            ".some(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length));"
        ))
    except Exception:  # noqa: BLE001
        return False


def _login_page_ready(driver, timeout: int = 12) -> str:
    """等登录页就绪，返回 'ready' / 'challenge' / 'stuck'。

    ★ 不能只在 driver.get 之后瞄一眼就下结论：挑战页是异步渲染的，
      刚导航完标题还可能是空的，这样会漏判，然后在下游报「找不到邮箱输入框」。
    """
    end = time.time() + max(1, int(timeout))
    while time.time() < end:
        if _looks_like_cloudflare_challenge(driver):
            return "challenge"
        try:
            ready = bool(driver.execute_script(
                "return !![...document.querySelectorAll('input')].find(e => "
                "(e.type === 'email') || /email|username/i.test((e.name || '') + (e.id || '')));"
            ))
        except Exception:  # noqa: BLE001
            ready = False
        if ready:
            return "ready"
        time.sleep(0.8)
    return "stuck"


def _detect_email_next_state(driver) -> str:
    """不等待，只看「现在」邮箱这一步之后到哪了。空串 = 还没过去。

    用途：等待下一跳超时时，判断到底是真没提交成功，还是页面只是慢、
    其实已经进了验证码页 —— 后者以前会被当成失败去重填邮箱，
    而验证码页没有邮箱输入框，只会白等到超时报「找不到邮箱输入框」。
    """
    try:
        if _has_access_token(driver):
            return "logged_in"
        if _is_email_verification_page(driver):
            return "otp"
        # 注册密码页才算数；登录密码页另有语义（那说明邮箱已注册），不能当成进步。
        if _is_signup_password_page(driver) and not _is_login_password_page(driver):
            return "password"
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _submit_email_and_wait_next(
    driver,
    email: str | None,
    attempts: int = 3,
    email_supplier: Callable[[], str] | None = None,
) -> str:
    """填写并提交邮箱，必须确认进入 password/otp/logged_in 才返回。"""
    last_state = None
    current_email = str(email or "").strip()
    for attempt in range(1, attempts + 1):
        # ★ 每轮先确认「是不是已经过了邮箱这一步」：上一轮可能只是等待超时，
        #   页面随后才跳转。不先看这一眼，就会去验证码页里找邮箱输入框。
        progressed = _detect_email_next_state(driver)
        if progressed:
            logger.info("%s 邮箱这一步已经过了（当前 %s），不再重填", _log_prefix(driver), progressed)
            return progressed
        try:
            if current_email:
                _type_email_address(driver, current_email, timeout=20)
            else:
                # 先确认页面已有可用输入框，再领取邮箱；不能把领取动作放在页面导航之前。
                email_input = _wait_for_email_input(driver, timeout=20)
                if email_supplier is None:
                    raise RuntimeError("已找到邮箱输入框，但未提供邮箱分配器")
                current_email = str(email_supplier() or "").strip()
                if not current_email:
                    raise RuntimeError("邮箱分配器返回了空邮箱地址")
                _human_type_text(driver, email_input, current_email, clear=True)
            state = _email_input_value_state(driver)
            last_state = state
            values = [str(i.get("value") or "") for i in (state.get("inputs") or [])]
            if not any(v.strip().lower() == current_email.lower() for v in values):
                logger.warning("%s 邮箱写入校验失败，准备重试：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, state)
                time.sleep(0.8)
                continue
            logger.info("%s 已填写邮箱并校验通过：%s", _log_prefix(driver), current_email)
            human_delay("form")
            _submit_email_step(driver, current_email)
            logger.info("%s 已提交邮箱，等待进入密码页或验证码页（%s/%s）", _log_prefix(driver), attempt, attempts)
            state_name = _wait_email_submit_next_state(driver, current_email, timeout=20)
            if state_name == "login_password":
                raise RuntimeError(f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}")
            if state_name == "auth_error":
                raise RuntimeError("邮箱提交后进入 ChatGPT /api/auth/error；已记录脱敏页面与网络状态")
            if state_name in ("password", "otp", "logged_in"):
                logger.info("%s 邮箱提交后已进入下一步：%s", _log_prefix(driver), state_name)
                return state_name
            logger.warning("%s 邮箱提交后仍未进入下一步：%s，准备重填重试", _log_prefix(driver), state_name)
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            if not _is_navigation_error(exc):
                # 找不到邮箱输入框时再看一眼：页面很可能已经进了验证码页/密码页，
                # 那就当这一步成功，别把一轮好注册判死。
                if "找不到邮箱输入框" in str(exc):
                    progressed = _detect_email_next_state(driver)
                    if progressed:
                        logger.info("%s 邮箱输入框已不可见，但页面已在下一步（%s），按成功继续",
                                    _log_prefix(driver), progressed)
                        return progressed
                raise
            logger.warning("%s 邮箱提交过程中页面跳转，本次作废重来（%s/%s）：%s",
                           _log_prefix(driver), attempt, attempts, str(exc)[:140])
            time.sleep(1.2)
            continue
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={last_state}")


def _set_otp_value_via_js(driver, code: str) -> bool:
    """兜底：直接改 DOM 值并派发 input/change 事件（React 受控输入也认）。

    键盘打字怎么都对不上时用它，避免整轮注册因为一个字符顺序而报废。
    """
    try:
        return bool(driver.execute_script(r"""
        const code = arguments[0];
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).filter(el => {
          const a = [el.type, el.name, el.id, el.autocomplete, el.inputMode,
                     el.getAttribute('aria-label')].join(' ').toLowerCase();
          return /one-time|otp|code|numeric|tel/.test(a);
        });
        if (!inputs.length) return false;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        const put = (el, v) => {
          if (setter) setter.call(el, v); else el.value = v;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        };
        if (inputs.length === 1) {
          put(inputs[0], code);
          return inputs[0].value === code;
        }
        for (let i = 0; i < code.length && i < inputs.length; i++) put(inputs[i], code[i]);
        return inputs.slice(0, code.length).map(e => e.value).join('') === code;
        """, code))
    except Exception:
        return False


def _read_back_value(el) -> str:
    """回读输入框当前值；元素已失效（页面跳走了）返回 None。"""
    try:
        return str(el.get_attribute("value") or "")
    except Exception:
        return None


def _collect_otp_targets(driver, code_len: int) -> tuple[str, list]:
    """找验证码输入框，返回 ('single', [el]) 或 ('boxes', [els])，没有则 ('', [])。

    单独抽出来是为了能在 _type_otp 里轮询等 —— 页面刚跳过来时一个 input 都没有。
    """
    from selenium.webdriver.common.by import By

    for selector in (
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[type='tel']",
    ):
        try:
            els = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(e)]
        except Exception:  # noqa: BLE001
            return "", []
        if len(els) == 1:
            return "single", els

    try:
        boxes = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if _visible(e)]
    except Exception:  # noqa: BLE001
        return "", []
    numeric_boxes = []
    for e in boxes:
        try:
            attrs = " ".join(str(e.get_attribute(k) or "")
                             for k in ("inputmode", "autocomplete", "aria-label", "name", "id", "type"))
        except Exception:  # noqa: BLE001
            continue
        if any(x in attrs.lower() for x in ("numeric", "one-time", "code", "otp", "tel")):
            numeric_boxes.append(e)
    if len(numeric_boxes) >= code_len:
        return "boxes", numeric_boxes[:code_len]
    return "", []


def _type_otp(driver, code: str) -> None:
    code = str(code or "").strip()
    if not code:
        raise RuntimeError("验证码为空，拒绝填写")

    # ★ 先等输入框渲染出来，别抢在 React 前面报错（实测踩过：拿到码 5 秒后就说找不到输入框）。
    kind, targets = "", []
    started = time.time()
    deadline = started + OTP_INPUT_WAIT
    left_count = 0
    while time.time() < deadline:
        kind, targets = _collect_otp_targets(driver, len(code))
        if kind:
            break
        # 页面已经自动跳走（码其实填进去了）就别死等 —— 但要连续两次确认，
        # 免得被一次瞬时抖动骗过去、把整步验证码跳过去。
        if time.time() - started >= 4.0:
            try:
                if _is_email_verification_page(driver):
                    left_count = 0
                else:
                    left_count += 1
                    if left_count >= 2:
                        logger.info("%s[OTP] 等待输入框期间页面已离开验证码页，按已通过处理",
                                    _log_prefix(driver))
                        return
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.6)

    if kind == "single":
        els = targets
        last = ""
        for attempt in range(1, OTP_TYPE_ATTEMPTS + 1):
            # ★★ 2026-10-05 P0 提速：**第 1 次就用 JS 一次性写值**。
            #   实测逐字符打 6 位数字大约一半概率被受控输入框吃掉或重排
            #   （日志里满屏「回读不一致：期望 023068 实际 020368」），
            #   每次不一致都要清空重打 —— 单次烧 14~16 秒，一轮里出现 2~4 次。
            #   6 位纯数字逐字符打本来就没有反风控价值。键盘留作第 2 次兜底。
            if attempt == 1 and _set_otp_value_via_js(driver, code):
                pass  # JS 已写值，下面直接回读校验
            else:
                _human_type_text(driver, els[0], code, clear=True, chunk=False)
            time.sleep(0.3)
            got = _read_back_value(els[0])
            if got is None:
                # 页面已经跳走 = 输入完成触发了跳转，视为成功。
                logger.info("%s[OTP] 单输入框填写后页面已跳转", _log_prefix(driver))
                return
            if got.strip() == code:
                return
            last = got
            logger.warning("%s[OTP] 单输入框回读不一致（第 %s/%s 次）：期望 %r 实际 %r",
                           _log_prefix(driver), attempt, OTP_TYPE_ATTEMPTS, code, got)
            # ★ 键盘连错两次就别再打了：每次错值都会被服务端当成一次错误尝试，
            #   而且受控输入框抢跑这种毛病再打第三次也基本不会对。直接交 JS 兜底。
            if attempt >= 2:
                break
            time.sleep(0.4)
        # 键盘怎么打都对不上（受控输入框抢跑），改用 JS 直接写值再回读。
        if _set_otp_value_via_js(driver, code):
            logger.warning("%s[OTP] 键盘填写不一致，已改用 JS 写值成功", _log_prefix(driver))
            return
        raise RuntimeError(f"验证码填进单输入框后回读不一致：期望 {code!r} 实际 {last!r}")

    # 6 个分格输入框 —— 最容易丢字/跳格错位，必须回读校验。
    if kind == "boxes":
        last = ""
        for attempt in range(1, OTP_TYPE_ATTEMPTS + 1):
            # ★ 同上：第 1 次用 JS 一次性写值（分格输入框它也支持 —— 每个框写一位），
            #   之后才逐格键盘补打。分格最容易丢字错位，所以回读校验照旧保留。
            if not (attempt == 1 and _set_otp_value_via_js(driver, code)):
                _clear_otp_inputs(driver)
                time.sleep(0.2)
                for e, ch in zip(targets, code):
                    if _browser_actions_enabled():
                        _human_scroll_to(driver, e)
                        time.sleep(random.uniform(0.04, 0.18))
                    e.send_keys(ch)
                    if _browser_actions_enabled():
                        human_delay("keystroke")
            got = _read_back_value(targets[0])
            if got is None:
                logger.info("%s[OTP] 分格输入框填写后页面已跳转", _log_prefix(driver))
                return
            joined = "".join(str(_read_back_value(e) or "") for e in targets).strip()
            if joined == code:
                return
            last = joined
            logger.warning("%s[OTP] 分格输入框回读不一致（第 %s/%s 次）：期望 %r 实际 %r",
                           _log_prefix(driver), attempt, OTP_TYPE_ATTEMPTS, code, joined)
            time.sleep(0.4)
        if _set_otp_value_via_js(driver, code):
            logger.warning("%s[OTP] 分格键盘填写不一致，已改用 JS 写值成功", _log_prefix(driver))
            return
        raise RuntimeError(f"验证码填进分格输入框后回读不一致：期望 {code!r} 实际 {last!r}")

    raise RuntimeError(
        "找不到 OTP 输入框（等了 %.0fs，url=%s）"
        % (OTP_INPUT_WAIT, str(getattr(driver, "current_url", "") or "?"))
    )


def _email_otp_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', inputmode: el.getAttribute('inputmode') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const buttons = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', value: el.getAttribute('value') || '',
          action: el.getAttribute('data-dd-action-name') || '', aria: el.getAttribute('aria-label') || '',
          disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
        }));
        const errors = [...document.querySelectorAll('.react-aria-FieldError,[slot="errorMessage"],[id$="-error"],[aria-invalid="true"] + *,[class*="error"]')]
          .filter(visible).map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        return {url: location.href, title: document.title, inputs, buttons, errors, text: (document.body?.innerText || '').slice(0, 1200)};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, 'current_url', ''), "error": f"{type(exc).__name__}: {exc}"}


def _auth_error_page_reason(driver) -> str:
    """当前页面是不是 OpenAI 的「出错 / 重试」页？是就返回命中的证据，否则空串。

    ★★ 为什么需要它（2026-10-04 实测）：在 `auth.openai.com/log-in/password` 上
      点「用一次性验证码登录」（`intent=passwordless_login_send_otp`）时，
      **点击确实落地了、请求确实发出去了**，但服务端直接回了一个错误页：

        <A data-dd-action-name='Try again' href='https://auth.openai.com/log-in'
           text='Tentar de novo'>          ← 葡语的「再试一次」

      ⚠️ 关键：**URL 不变**（还停在 `/log-in/password`），只有 DOM 换了。
      所以只看 URL 的页面判定会把它当成「还在密码页」，一路静默下去。
      必须按 DOM 判，否则这种情况和「页面没反应」长得一模一样。
    """
    try:
        state = _email_otp_page_state(driver)
    except Exception:  # noqa: BLE001
        return ""
    dd = " ".join(str(b.get("action") or "") for b in (state.get("buttons") or [])).lower()
    if "try again" in dd:
        return f"页面出现 Try again 按钮（title={state.get('title') or '-'}）"
    blob = f"{state.get('title') or ''} {state.get('text') or ''}".lower()
    for marker in ("route error", "invalid content type", "糟糕，出错了", "something went wrong"):
        if marker in blob:
            return f"页面文本含「{marker}」"
    return ""


def _is_email_verification_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return False
    # ★ 设密码页也不是验证码页。漏掉这条会把它当成 OTP 页，
    #   于是调用方一直在「等验证码」，而页面其实是「设新密码」（2026-10-04）。
    if '/create-account/password' in url or '/signup/password' in url or '/u/signup/password' in url:
        return False
    if 'email-verification' in url:
        return True
    state = _email_otp_page_state(driver)
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','inputmode')) for i in (state.get('inputs') or [])).lower()
    return 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs


def _clear_otp_inputs(driver) -> None:
    try:
        driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).filter(el => {
          const attrs = [el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label')].join(' ').toLowerCase();
          return /one-time|otp|code|numeric|tel/.test(attrs);
        });
        for (const el of inputs) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, ''); else el.value = '';
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """)
    except Exception:
        pass


def _click_resend_email_otp(driver, timeout: int = 20) -> dict:
    """点击重新发送邮箱验证码。优先按 DOM 属性识别，文本仅兜底。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const candidates = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=button],input[type=submit]')].filter(visible);
            const attrHit = candidates.find(el => {
              if (!enabled(el)) return false;
              const attrs = [el.id, el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
                .join(' ').toLowerCase();
              const name = String(el.getAttribute('name') || '').toLowerCase();
              const value = String(el.getAttribute('value') || '').toLowerCase();
              if (name === 'intent' && value === 'resend') return true;
              return /resend|send.*new|new.*code|again/.test(attrs);
            });
            if (attrHit) return attrHit;
            // 兜底：多语言文本，避免因页面没有稳定属性时卡死。
            return candidates.find(el => enabled(el) && /resend|send\s+(?:a\s+)?new\s+code|send\s+again|重新发送|重新发送电子邮件|重发|再次发送|再送信|新しい|届かない/.test((el.innerText || el.textContent || '').toLowerCase())) || null;
            """)
            if btn:
                text = str(btn.text or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                _human_click(driver, btn, label="resend_otp")
                logger.info("%s[OTP] 已点击重新发送验证码按钮：%s", _log_prefix(driver), text or '-')
                time.sleep(random.uniform(1.1, 2.4) if _browser_actions_enabled() else 1.5)
                return {"ok": True, "text": text}
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"找不到可点击的重新发送验证码按钮: last={last}, state={_email_otp_page_state(driver)}")


_CLICKABLE_DUMP_JS = r"""
return (() => {
  const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
    && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  return [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')]
    .filter(vis).map(el => ({
      tag: el.tagName,
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      value: el.getAttribute('value') || '',
      dd: el.getAttribute('data-dd-action-name') || '',
      testid: el.getAttribute('data-testid') || '',
      aria: el.getAttribute('aria-label') || '',
      href: (el.getAttribute('href') || '').slice(0, 60),
      disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
      text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
    }));
})();
"""


def _dump_clickables(driver, label: str = "") -> list[dict]:
    """把当前页所有可见可点元素的**协议属性**打出来。

    ★ 为什么要它（2026-10-04）：认不到元素时的老行为是**什么都不说**，
      然后调用方一路降级成「等一封永远不来的邮件」，白烧几分钟还查不出原因。
      现在只要没认到，就把页面原貌打出来 —— 下次换任何国家的代理都能一眼看出
      该匹配哪个 name/value。
    """
    try:
        items = driver.execute_script(_CLICKABLE_DUMP_JS) or []
        logger.warning(
            "%s[页面] %s 可见可点元素 %s 个：",
            _log_prefix(driver), label or "dump", len(items),
        )
        for it in items:
            logger.warning(
                "%s[页面]   <%s type=%r name=%r value=%r dd=%r testid=%r aria=%r href=%r disabled=%s text=%r>",
                _log_prefix(driver), it.get("tag"), it.get("type"), it.get("name"),
                it.get("value"), it.get("dd"), it.get("testid"), it.get("aria"),
                it.get("href"), it.get("disabled"), it.get("text"),
            )
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s[页面] dump 失败：%s: %s", _log_prefix(driver), type(exc).__name__, exc)
        return []


def _click_protocol_button(
    driver,
    *,
    intents: tuple[str, ...] = (),
    href_contains: tuple[str, ...] = (),
    dd_names: tuple[str, ...] = (),
    exclude_intents: tuple[str, ...] = (),
    fallback_texts: tuple[str, ...] = (),
    timeout: float = 8.0,
    label: str = "",
    dump_on_miss: bool = True,
) -> dict:
    """按**协议属性**点按钮，文案只做最后兜底。

    ★★ 为什么不按界面文案认元素（2026-10-04 定案）：
      `cloak_driver` 启动时 `geoip=True` 是**故意**让 locale 跟着出口 IP 走的
      （防关联），所以页面语言 = 用户代理所在国，**必然任意**。
      按 `button` 里的人类文字找元素，等于假设用户用哪国代理 ——
      换一次国家就静默失效一次，而且不报错，只表现为「卡住」。

      正确的识别键是 OpenAI auth 页自己的**表单协议属性**，它们跟着功能走、
      不跟界面语言走。实测（巴西出口、pt-BR 页面）拿到的稳定键：

        <BUTTON type='submit' name='intent' value='email'>                      邮箱提交
        <BUTTON              name='intent' value='google'/'apple'/'microsoft'>  第三方登录
        <BUTTON type='submit' name='intent' value='validate'>                   验证码提交
        <BUTTON              name='intent' value='resend'>                      重发验证码
        <BUTTON              name='intent' value='passwordless_login_send_otp'> 一次性验证码登录
        data-dd-action-name='Continue' / 'Continue with Google' / 'Select account'

      `intents` 传 ``("intent=validate", ...)`` 这种 ``name=value`` 小写串。
      `href_contains` 传 ``href`` 的**小写子串**（比如 ``/create-account/password``）。
      `dd_names` 传 ``data-dd-action-name`` 的**小写子串**。
      `exclude_intents` 用来排掉危险按钮（比如页面同时有「验证」和「重发」，
      绝不能点到重发 —— 那会把手上正确的码作废）。

    返回 ``{"ok": bool, "reason": str, "match": "intent"|"dd-action-name"|"text", ...}``。
    失败时**自动 dump 页面**，不留静默失败。
    """
    intents_l = [str(x).lower() for x in intents]
    hrefs_l = [str(x).lower() for x in href_contains]
    dds_l = [str(x).lower() for x in dd_names]
    excl_l = [str(x).lower() for x in exclude_intents]
    texts_l = [str(x) for x in fallback_texts]
    end = time.time() + max(0.0, float(timeout))
    last: dict = {"ok": False, "reason": "not_attempted"}

    while time.time() < end:
        try:
            result = driver.execute_script(r"""
            const intents = arguments[0] || [], dds = arguments[1] || [],
                  excl = arguments[2] || [], texts = arguments[3] || [],
                  hrefs = arguments[4] || [];
            const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const enabled = el => !el.disabled
              && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const key = el => (String(el.getAttribute('name') || '').toLowerCase() + '='
                              + String(el.getAttribute('value') || '').toLowerCase());
            const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
            const cands = [...document.querySelectorAll(
              'button,a,input[type="submit"],[role="button"],[role="link"]'
            )].filter(el => vis(el) && enabled(el));
            const excluded = el => excl.includes(key(el));
            const pack = (el, match) => {
              // ★★ 点击必须在**这个 JS 里**点完，不能把元素返回给 Python 再点。
              //   这个 CloakSeleniumDriver 从 execute_script 返回的 DOM 元素会被
              //   序列化成普通对象；Python 侧再拿它调 scrollIntoView/click 只会报
              //   `el.scrollIntoView is not a function`。
              //   后果极具迷惑性：日志显示「已点击」，页面却纹丝不动 ——
              //   之前几轮排查全被这一点带偏（2026-10-04）。
              try { el.scrollIntoView({ block: 'center' }); } catch (e) {}
              try { el.focus && el.focus(); } catch (e) {}
              try { el.click(); } catch (e) {}
              return {
                ok: true, match,
                tag: el.tagName, type: el.getAttribute('type') || '',
                name: el.getAttribute('name') || '', value: el.getAttribute('value') || '',
                dd: el.getAttribute('data-dd-action-name') || '',
                href: el.getAttribute('href') || '',
                text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 48),
              };
            };
            let el = cands.find(e => !excluded(e) && intents.includes(key(e)));
            if (el) return pack(el, 'intent');
            el = cands.find(e => {
              if (excluded(e)) return false;
              const h = String(e.getAttribute('href') || '').toLowerCase();
              return h && hrefs.some(x => h.includes(x));
            });
            if (el) return pack(el, 'href');
            el = cands.find(e => {
              if (excluded(e)) return false;
              const dd = String(e.getAttribute('data-dd-action-name') || '').toLowerCase();
              return dd && dds.some(d => dd.includes(d));
            });
            if (el) return pack(el, 'dd-action-name');
            el = cands.find(e => !excluded(e)
              && texts.some(t => norm(e.textContent).includes(norm(t))));
            if (el) return pack(el, 'text');
            return {ok: false, reason: 'no_match', candidates: cands.length};
            """, intents_l, dds_l, excl_l, texts_l, hrefs_l) or {}
        except Exception as exc:  # noqa: BLE001
            last = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
            time.sleep(0.5)
            continue

        if result.get("ok"):
            # 点击已经在上面那段 JS 里做完了（原因见 pack 里的注释）。
            logger.info(
                "%s[点击] %s ← %s（<%s name=%r value=%r dd=%r href=%r text=%r>）",
                _log_prefix(driver), label or "-", result.get("match"),
                result.get("tag"), result.get("name"), result.get("value"),
                result.get("dd"), result.get("href"), result.get("text"),
            )
            return result
        last = result
        time.sleep(0.5)

    logger.warning(
        "%s[点击] 认不到按钮：label=%r intents=%s dd=%s exclude=%s — %s",
        _log_prefix(driver), label, intents_l, dds_l, excl_l, last.get("reason"),
    )
    # ★ 轮询场景（比如等验证码页上的入口渲染出来）每次都 dump 会刷几百行日志，
    #   把真正有用的信息淹掉。由调用方决定要不要 dump。
    if dump_on_miss:
        _dump_clickables(driver, label=f"认不到 {label or '按钮'}")
    return {"ok": False, "reason": str(last.get("reason") or "timeout")}


def _fmt_net_events(events) -> list[str]:
    """把 network 事件压成一行一条的可读串，保留 `req` / `resp` 的方向。

    ★ 为什么要带方向（2026-10-04）：卡在验证码页时，
    「点击没发出去（没有 req）」和「发了但上游挂着不回（有 req、没有配对的 resp）」
    是两种完全不同的病因，压掉方向就分不出来了。
    """
    out = []
    for e in list(events or []):
        arrow = "->" if str(e.get("kind") or "") == "req" else "<-"
        body = str(e.get("body") or "")
        suffix = f" body={body[:120]}" if body else ""
        out.append(f"{arrow}{e.get('method')} {e.get('status')} {str(e.get('url'))[-46:]}{suffix}")
    return out


def _click_email_otp_submit(driver) -> bool:
    """点邮箱验证码页的「验证/继续」按钮，**显式避开「重新发送」**。

    ★ 这个页面同时有两个 `type=submit` 按钮：「验证」和「重新发送」。
      通用选择器 `button[type=submit]` 取的是第一个，顺序一变就会点到「重新发送」——
      那会把一个正确的码作废（2026-10-03 实测代价：新码 180s 没来，整个账号报废）。
      所以这里先认 value=validate，再按文案匹配并排除重发语义。
    """
    try:
        return bool(driver.execute_script(r"""
        const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const resend = /resend|reenviar|re-?send|再次发送|重发|重新发送|再送|再送信|send again/i;
        const ok = /continue|continuar|verify|confirmar|enviar c|验证|确认|次へ|続行|送信/i;
        const btns = [...document.querySelectorAll('button')].filter(vis);
        const validate = btns.find(b => (b.getAttribute('value') || '').toLowerCase() === 'validate')
          || btns.find(b => {
               const t = ((b.innerText || b.textContent || '') + ' ' + (b.getAttribute('value') || '')).trim();
               return !resend.test(t) && ok.test(t);
             });
        if (!validate) return false;
        validate.click();
        return true;
        """))
    except Exception:  # noqa: BLE001
        return False


def _wait_after_email_otp_submit(driver, timeout: int = 30) -> str:
    """提交 OTP 后等待页面离开验证码页。

    只有页面明确出现验证码错误（aria-invalid / 错误文案）才判定为无效；
    网络慢时页面跳转可能超过 10s，超时后只要没有错误标记就按 accepted 处理，
    避免把已提交成功的验证码误判为失败后误点“重新发送”把流程搞乱。
    """
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(0.5)
        last = _email_otp_page_state(driver)
        page_text = f"{last.get('title') or ''} {last.get('text') or ''}".lower()
        if 'route error' in page_text or 'invalid content type' in page_text or '糟糕，出错了' in page_text:
            logger.error(
                "%s[OTP] 提交后进入明确错误页：snapshot=%s network=%s",
                _log_prefix(driver),
                last,
                list(getattr(driver, '_auth_network_events', []) or [])[-12:],
            )
            return 'fatal'
        if not _is_email_verification_page(driver):
            return 'accepted'
        invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
        if invalid or (last.get('errors') or []):
            # ★ 以前这里直接 return，什么线索都不留：事后只能看到「验证码被拒」，
            #   分不清是码错、少打一位、还是上游风控。把页面原话打出来。
            logger.warning(
                "%s[OTP] 页面标记验证码无效：errors=%s inputs=%s network=%s",
                _log_prefix(driver),
                last.get('errors'),
                [{'value': i.get('value'), 'ariaInvalid': i.get('ariaInvalid')}
                 for i in (last.get('inputs') or [])],
                list(getattr(driver, '_auth_network_events', []) or [])[-8:],
            )
            return 'invalid'
    if _is_email_verification_page(driver):
        # 超时仍停留：若无明确错误标记，判定为提交成功、跳转缓慢，按 accepted 放行。
        has_error_mark = bool(last.get('errors')) or any(
            str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or [])
        )
        if has_error_mark:
            logger.warning("%s[OTP] 提交后仍停留验证码页且存在错误标记，按验证码无效处理 snapshot=%s", _log_prefix(driver), last)
            return 'invalid'
        # ★ 不能直接按「跳转缓慢」放行：实测最常见的情况是**提交按钮压根没点上**
        #   （码已经填对、input 里有值，页面却一动不动）。放行之后调用方会去等资料页，
        #   一路等到超时，整个账号白跑。所以先补点一次提交，再看页面走没走。
        logger.warning(
            "%s[OTP] 提交后 %ss 仍停在验证码页且无错误标记，补点一次提交 network=%s snapshot=%s",
            _log_prefix(driver), timeout,
            _fmt_net_events(list(getattr(driver, '_auth_network_events', []) or [])[-6:]),
            last
        )
        try:
            _click_continue(driver)
        except Exception as exc:  # noqa: BLE001
            logger.info("%s[OTP] 补点提交没找到按钮：%s", _log_prefix(driver), str(exc)[:120])
        retry_end = time.time() + 8
        while time.time() < retry_end:
            time.sleep(0.5)
            if not _is_email_verification_page(driver):
                logger.info("%s[OTP] 补点提交后页面已跳转", _log_prefix(driver))
                return 'accepted'
        logger.warning("%s[OTP] 补点提交后仍未跳转，交给上层用同一个码再试", _log_prefix(driver))
        return 'stalled'
    return 'accepted'


def _click_continue(driver) -> None:
    _click_any(driver, [
        "button[type='submit']",
        "//button[@data-dd-action-name='Continue']",
        "//button[@data-dd-action-name='continue']",
        "//button[@data-login-web-auth-control='true' and @type='submit']",
        "//button[contains(., 'Continue')]",
        "//button[contains(., '続行')]",
        "//button[contains(., '继续')]",
        "//button[contains(., 'Sign up')]",
        "//button[contains(., 'Create')]",
        "//button[contains(., 'Next')]",
    ], timeout=20)


def _maybe_accept(driver) -> None:
    # 只处理明确的 cookie/consent 弹层按钮；不要用 “Continue” 兜底，
    # 非日本出口时 “Continue with Google” 也会命中，导致误点 Google 登录。
    for selectors in ([
        "button#onetrust-accept-btn-handler",
        "button[data-testid='cookie-accept']",
        "button[data-testid='accept-cookies']",
        "//button[contains(., 'Accept')]",
        "//button[contains(., '同意')]",
        "//button[contains(., 'Agree')]",
    ],):
        try:
            _click_any(driver, selectors, timeout=3)
            time.sleep(0.5)
        except Exception:
            pass


def _page_snapshot(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const inputs = [...document.querySelectorAll('input,select,textarea')].map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
          autocomplete: el.getAttribute('autocomplete') || '', aria: el.getAttribute('aria-label') || '',
          value: el.value || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).filter(x => x.visible).slice(0, 30);
        const buttons = [...document.querySelectorAll('button,a[role=button],input[type=submit]')].map(el => ({
          text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
          type: el.getAttribute('type') || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
          disabled: !!el.disabled
        })).filter(x => x.visible).slice(0, 30);
        const widgets = [...document.querySelectorAll('[role=spinbutton], .react-aria-Select, [data-testid="hidden-select-container"] select')].map(el => ({
          tag: el.tagName, role: el.getAttribute('role') || '', dataType: el.getAttribute('data-type') || '',
          aria: el.getAttribute('aria-label') || '', text: (el.innerText || el.textContent || '').trim().slice(0, 80),
          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 2000), inputs, buttons, widgets};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _has_access_token(driver) -> bool:
    try:
        result = driver.execute_async_script(r"""
        const done = arguments[0];
        fetch('https://chatgpt.com/api/auth/session', {credentials:'include'})
          .then(r => r.json()).then(j => done(Boolean(j && j.accessToken)))
          .catch(() => done(false));
        """)
        return bool(result)
    except Exception:
        return False


def _is_profile_like(snapshot: dict) -> bool:
    """资料页识别：兼容 about-you/profile；年龄/生日控件可能不是 input，而是 React Aria widget。"""
    url = str(snapshot.get('url') or '').lower()
    inputs = snapshot.get('inputs') or []
    widgets = snapshot.get('widgets') or []
    attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('name', 'id', 'placeholder', 'autocomplete', 'aria', 'type')).lower()
        for i in inputs
    )
    widget_attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('role', 'dataType', 'aria', 'text', 'tag')).lower()
        for i in widgets
    )
    has_profile_url = any(x in url for x in ('about-you', 'profile', 'signup/profile', 'create-account/profile'))
    has_name_field = (
        'autocomplete name' in attrs
        or ' name ' in f' {attrs} '
        or 'fullname' in attrs
        or 'full_name' in attrs
        or 'firstname' in attrs
        or 'lastname' in attrs
    )
    has_age_or_birth_field = any(x in f' {attrs} {widget_attrs} ' for x in (
        ' age', '-age', '_age', 'birth', 'birthday', 'birthdate',
        ' month', '-month', '_month', 'data-type month',
        ' day', '-day', '_day', 'data-type day',
        ' year', '-year', '_year', 'data-type year',
        'spinbutton', 'react-aria-select', 'type number',
    ))
    # about-you/profile URL 本身已经足够强；部分新版页面会用无 name 的 React Aria 控件。
    return has_profile_url and (has_name_field or has_age_or_birth_field or bool(inputs) or bool(widgets))


def _set_element_value(driver, el, value: str) -> None:
    """兼容 React 受控输入框：用原生 setter 设置值并派发 input/change。"""
    driver.execute_script(r"""
    const el = arguments[0];
    const value = String(arguments[1]);
    const tag = (el.tagName || '').toLowerCase();
    el.scrollIntoView({block:'center'});
    el.focus();
    if (tag === 'select') {
      el.value = value;
    } else {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.blur();
    """, el, value)


def _select_or_type(driver, selectors: list[str], value: str, timeout: int = 3) -> bool:
    try:
        el = _find_any(driver, selectors, timeout=timeout)
    except Exception:
        return False
    try:
        tag = (el.tag_name or '').lower()
        if tag == 'select':
            if el.__class__.__name__ == 'CloakElement':
                driver.execute_script(r"""
                const el = arguments[0], value = String(arguments[1]);
                const n = parseInt(value, 10);
                const opts = [...el.options];
                const match = opts.find(o => o.value === value)
                  || opts.find(o => (o.textContent || '').trim() === value)
                  || opts[Math.max(0, n - 1)];
                if (match) el.value = match.value; else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """, el, str(value))
            else:
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_value(str(int(value)))
                except Exception:
                    try:
                        sel.select_by_visible_text(str(int(value)))
                    except Exception:
                        # 月份 select 可能是 0-based，也可能是 1-based；先 value/text，不行再 index。
                        sel.select_by_index(max(0, int(value)-1))
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", el)
        else:
            # ★★ 2026-10-05 P0 提速：**先 JS 一次性写值**，不再逐字符打。
            #   实测逐字符打短文本会被受控输入框吃掉/重排 —— 日志里「填写姓名」
            #   那一步实测 34 秒（16 个字符打字本身只要 1~3 秒），
            #   多出来的全是「打完 → 回读不一致 → 清空重打」。
            #   顺带修掉老毛病：逐字符打会把姓名弄坏（`Henry King` → `Hennry Kig`）。
            #   键盘留作兜底（受控输入偶尔只认真实按键），**顺序和以前正好反过来**。
            target = str(value)
            _set_element_value(driver, el, target)
            if _read_back_value(el) == target:
                return True
            logger.debug('%s JS 写值回读不一致，改用键盘兜底 selectors=%s 期望=%r',
                         _log_prefix(driver), selectors, target)
            _human_type_text(driver, el, target, clear=True)
            got = _read_back_value(el)
            if got is not None and got != target:
                logger.warning('%s 这个字段 JS 和键盘都填不准 selectors=%s 期望=%r 实际=%r',
                               _log_prefix(driver), selectors, target, got)
        return True
    except Exception as exc:
        logger.debug('%s 填写字段失败 selectors=%s value=%s err=%s', _log_prefix(driver), selectors, value, exc)
        return False


def _fill_birthday_or_age(driver, birthday: str, age: int) -> str | None:
    """填写 about-you 的年龄/生日控件。

    参考 FlowPilot：优先处理直接年龄 input；否则兼容 hidden birthday/date、原生年月日
    select/input、React Aria hidden native select、role=spinbutton[data-type=year/month/day]。
    返回 age / birthday / ymd / react_select / spinbutton / None。
    """
    y, m, d = birthday.split('-')
    result = driver.execute_script(r"""
    const birthday = String(arguments[0]);
    const year = String(arguments[1]);
    const month = String(Number(arguments[2]));
    const month2 = String(arguments[2]).padStart(2, '0');
    const day = String(Number(arguments[3]));
    const day2 = String(arguments[3]).padStart(2, '0');
    const age = String(arguments[4]);
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const setValue = (el, value) => {
      if (!el) return false;
      el.scrollIntoView?.({block:'center'});
      el.focus?.();
      const tag = (el.tagName || '').toLowerCase();
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
        : tag === 'select' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, String(value)); else el.value = String(value);
      if (tag === 'select') {
        [...el.options].forEach(opt => { opt.selected = String(opt.value) === String(value); });
      }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.blur?.();
      return true;
    };
    const ageInput = [...document.querySelectorAll('input[name="age"], input#age, input[id$="-age"], input[type="number"]')]
      .find(visible);
    if (ageInput && setValue(ageInput, age)) return {ok:true, mode:'age'};

    const dateInput = [...document.querySelectorAll('input[name="birthdate"], input[type="date"], input[name="birthday"]')]
      .find(el => visible(el) || String(el.getAttribute('type') || '').toLowerCase() === 'date');
    if (dateInput && setValue(dateInput, birthday)) return {ok:true, mode:'birthday'};

    const setFirst = (selectors, values) => {
      for (const sel of selectors) {
        for (const el of [...document.querySelectorAll(sel)]) {
          if (!visible(el)) continue;
          for (const val of values) {
            if (el.tagName === 'SELECT') {
              const has = [...el.options].some(o => String(o.value) === String(val) || String(o.textContent || '').trim() === String(val));
              if (!has) continue;
            }
            if (setValue(el, val)) return true;
          }
        }
      }
      return false;
    };
    const yOk = setFirst(['select[name="year"]','input[name="year"]','select[id*="year"]','input[id*="year"]'], [year]);
    const mOk = setFirst(['select[name="month"]','input[name="month"]','select[id*="month"]','input[id*="month"]'], [month, month2]);
    const dOk = setFirst(['select[name="day"]','input[name="day"]','select[id*="day"]','input[id*="day"]'], [day, day2]);
    if (yOk && mOk && dOk) {
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'ymd'};
    }

    // React Aria Select 通常有 hidden native select；不依赖标签文字，按 option 数值范围和 DOM 顺序推断年/月/日。
    const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select, .react-aria-Select select, select')]
      .filter(el => !el.disabled);
    const nums = sel => [...sel.options].map(o => Number(o.value)).filter(Number.isFinite);
    const maxNum = sel => Math.max(...nums(sel), -Infinity);
    const minNum = sel => Math.min(...nums(sel), Infinity);
    const hasOption = (sel, val) => [...sel.options].some(o => String(o.value) === String(val));
    const yearSelects = selects.filter(sel => hasOption(sel, year) && maxNum(sel) > 1900);
    const smallSelects = selects.filter(sel => !yearSelects.includes(sel));
    const monthSelects = smallSelects.filter(sel => (hasOption(sel, month) || hasOption(sel, month2)) && minNum(sel) <= 1 && maxNum(sel) <= 12);
    const daySelects = smallSelects.filter(sel => (hasOption(sel, day) || hasOption(sel, day2)) && maxNum(sel) >= 28);
    if (yearSelects.length && monthSelects.length && daySelects.length) {
      const ys = yearSelects[0];
      let ms = monthSelects[0];
      let ds = daySelects.find(x => x !== ms) || daySelects[0];
      setValue(ys, year);
      setValue(ms, hasOption(ms, month) ? month : month2);
      setValue(ds, hasOption(ds, day) ? day : day2);
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'react_select'};
    }

    const spinYear = document.querySelector('[role="spinbutton"][data-type="year"]');
    const spinMonth = document.querySelector('[role="spinbutton"][data-type="month"]');
    const spinDay = document.querySelector('[role="spinbutton"][data-type="day"]');
    if (spinYear && spinMonth && spinDay) return {ok:false, mode:'spinbutton_needed'};
    return {ok:false, mode:'missing'};
    """, birthday, y, m, d, str(age)) or {}
    if result.get('ok'):
        return str(result.get('mode') or 'birthday')
    if result.get('mode') != 'spinbutton_needed':
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        mod = Keys.COMMAND
        try:
            import platform
            if platform.system().lower() != 'darwin':
                mod = Keys.CONTROL
        except Exception:
            pass
        for selector, value in [
            ('[role="spinbutton"][data-type="year"]', y),
            ('[role="spinbutton"][data-type="month"]', str(m).zfill(2)),
            ('[role="spinbutton"][data-type="day"]', str(d).zfill(2)),
        ]:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", el)
            time.sleep(0.1)
            el.send_keys(mod, 'a')
            time.sleep(0.05)
            el.send_keys(str(value))
            time.sleep(0.1)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true})); arguments[0].blur();", el)
        driver.execute_script(r"""
        const hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
          const value = arguments[0];
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(hidden, value); else hidden.value = value;
          hidden.dispatchEvent(new Event('input', {bubbles:true}));
          hidden.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """, birthday)
        return 'spinbutton'
    except Exception as exc:
        logger.debug('%s spinbutton 生日填写失败：%s', _log_prefix(driver), exc)
        return None


def _generate_roxy_password() -> str:
    """参考 FlowPilot 密码策略：8~64 位，含大小写、数字、符号。"""
    upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    lower = 'abcdefghjkmnpqrstuvwxyz'
    digits = '23456789'
    symbols = '!@#$%^&*?_-+=' 
    groups = [upper, lower, digits, symbols]
    all_chars = ''.join(groups)
    chars = [random.choice(g) for g in groups]
    while len(chars) < 14:
        chars.append(random.choice(all_chars))
    random.shuffle(chars)
    return ''.join(chars)


def _registration_password() -> str:
    try:
        # ★ 读线程局部的那个（见 _REGISTER_STATE 的注释），别读模块全局。
        configured = str(_get_register_password() or REGISTER_PASSWORD or '').strip()
        if configured:
            return configured
    except Exception:
        pass
    return _generate_roxy_password()


def _password_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', visible: visible(el), value: el.type === 'password' ? '<password>' : (el.value || '')
        })).slice(0, 30);
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const buttons = [...document.querySelectorAll('button,input[type="submit"]')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          disabled: !!el.disabled, visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, inputs, forms, buttons};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_signup_password_page(driver) -> bool:
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    if any(x in url for x in ('/create-account/password', '/u/signup/password', '/signup/password')):
        return True
    if '/log-in/password' in url:
        return False
    inputs = state.get('inputs') or []
    return any(
        i.get('visible') and (
            str(i.get('type') or '').lower() == 'password'
            or 'password' in str(i.get('name') or '').lower()
            or str(i.get('autocomplete') or '').lower() == 'new-password'
        )
        for i in inputs
    )


def _is_login_password_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return True
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    return '/log-in/password' in url


def _click_passwordless_signup_if_present(driver) -> dict:
    """
    新版注册/登录流在 password 页可能默认要求密码。
    如果页面提供“使用一次性验证码”按钮，优先点击进入邮箱 OTP 页面。

    ★★ 2026-10-04 重写：识别键从「界面文案」换成**表单协议属性**。

      实测（巴西出口 / pt-BR 页面，真实 DOM）这个按钮是：

        <BUTTON name='intent' value='passwordless_login_send_otp'
                text='Entre com um código de uso único'>

      `value` 是语言无关的，而原先那 18 条中/英/日文案在葡语页面上**一条都不命中**。
      更糟的是老代码认不到时只返回 `missing_passwordless_button` 却不打日志，
      调用方接着就去等一封「从没被请求过的」验证码邮件，白烧几分钟。

      文案仍然保留 —— 但**只做最后兜底**。
    """
    try:
        result = _click_protocol_button(
            driver,
            intents=(
                "intent=passwordless_login_send_otp",
                "intent=passwordless_signup_send_otp",
                "intent=passwordless_login_send_code",
                "intent=passwordless_signup_send_code",
            ),
            dd_names=("passwordless", "one-time code"),
            fallback_texts=(
                # 中文（简/繁）
                "使用一次性验证码注册", "使用一次性验证码登录", "使用一次性验证码",
                "使用一次性驗證碼註冊", "使用一次性驗證碼登入", "一次性验证码", "一次性驗證碼",
                # 日文
                "メールでコード", "ワンタイムコード", "認証コード",
                # 英文
                "Use a one-time code to log in", "Use a one-time code to sign up",
                "Continue with a one-time code", "Log in with a one-time code",
                "Sign up with a one-time code", "one-time code",
                # 葡语（当前巴西出口实测就是这个）
                "Entre com um código de uso único", "Usar um código de uso único",
                "Continuar com um código de uso único", "código de uso único",
            ),
            timeout=2.0,
            label="passwordless_otp",
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if result.get("ok"):
        result["reason"] = "clicked_passwordless_send_otp"
        result.pop("button", None)
        return result
    return {"ok": False, "reason": result.get("reason") or "missing_passwordless_button"}


def _click_continue_with_password_if_present(driver) -> dict:
    """在邮箱验证码页点击“使用密码继续”，进入 /create-account/password。"""
    try:
        result = _click_protocol_button(
            driver,
            # ★ 老代码这里是 `attrs.includes("data-dd-action-name")` —— 判的是
            #   「元素身上有没有这个属性」。实测该页几乎每个可点元素都带它
            #   （Continue / Continue with Google / ...），所以这个条件恒真，
            #   会点到页面上第一个可点元素。已删掉。
            #   ★ 这个入口的 `intent` 值目前没有实测样本（它在 OTP 页上，
            #   而探针只打到了 /log-in/password），所以先只认 href 与明确的动作名；
            #   真认不到时 _click_protocol_button 会把整页属性 dump 出来，下次照着补。
            href_contains=("/create-account/password",),
            dd_names=("continue with password", "use password", "create password"),
            fallback_texts=(
                "使用密码继续", "使用密碼繼續", "使用密码", "改用密码",
                "Continue with password", "Continue with a password", "Use password",
                "Continuar com uma senha", "Continuar com senha", "Usar uma senha",
                "パスワードで続行", "パスワードを使用",
            ),
            timeout=2.0,
            label="continue_with_password",
            # 这个方法会被轮询调用（等入口渲染），每次失败都 dump 会把日志刷爆。
            dump_on_miss=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if result.get("ok"):
        # ★★ 靠 href 命中时**直接导航**，别指望 JS 点击能触发 React Router
        #   （2026-10-04 实测：点是点上了、日志显示 clicked，但页面纹丝不动、
        #   密码页永远不出现）。这个 <A> 带的是真 href，直接 get 最稳。
        href = str(result.get("href") or "").strip()
        if result.get("match") == "href" and href:
            from urllib.parse import urljoin

            try:
                current = str(driver.current_url or "")
            except Exception:  # noqa: BLE001
                current = ""
            target = urljoin(current or "https://auth.openai.com/", href)
            logger.info("%s 使用密码继续：直接导航到 %s", _log_prefix(driver), target)
            try:
                driver.get(target)
                time.sleep(1.2)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 直接导航失败：%s", _log_prefix(driver), exc)
        result["reason"] = "clicked_continue_with_password"
        result.pop("button", None)
        return result
    return {"ok": False, "reason": result.get("reason") or "missing_continue_with_password"}


def _wait_left_signup_password_page(driver, timeout: float = 30.0) -> bool:
    """等页面离开 /create-account/password。"""
    end = time.time() + timeout
    while time.time() < end:
        if _is_email_verification_page(driver) or _has_access_token(driver):
            return True
        if not _is_signup_password_page(driver):
            return True
        time.sleep(0.5)
    return False


def _playwright_set_signup_password(driver, password: str) -> dict:
    """在 /create-account/password 上填新密码并提交（**Playwright 版**）。

    ★★ 为什么必须用 Playwright，而不是我们自己那套「JS 原生 setter + 派发 input」：
      实测那样写完之后 **React 的 state 并没有真的更新**。现场 dump：

        {'url': '/create-account/password', 'pwCount': 1, 'pwLens': [16],
         'pwInvalid': [''], 'errors': [],
         'buttons': [..., {'text': 'Continuar', 'type': 'submit', 'disabled': False}],
         'bodyText': '... Sua senha deve conter: Pelo menos 12 caracteres . Completo. ...'}

      框里看得到 16 位、页面也说「完全符合要求」、按钮 enabled，**可点 Continue
      就是不动**。Playwright 的 `fill()` / `click()` 是专门为这种受控输入做的：
      事件发得全，`click()` 还会**自动等元素变为可操作**。
    """
    page = getattr(driver, "page", None)
    if page is None:
        return {"ok": False, "reason": "no_page"}
    try:
        pw = page.locator("input[type='password']").first
        pw.wait_for(state="visible", timeout=8000)
        pw.fill(password)
        got = str(pw.input_value() or "")
        if got != password:
            return {"ok": False, "reason": f"fill_mismatch len={len(got)}"}
        human_delay("form", minimum=1.2, maximum=2.4)
        btn = page.locator("form:has(input[type='password']) button[type='submit']").first
        try:
            btn.wait_for(state="visible", timeout=5000)
        except Exception:  # noqa: BLE001
            btn = page.locator("button[type='submit']").first
        btn.click(timeout=10000)   # 自动等 enabled / stable
        return {"ok": True, "reason": "playwright_filled_and_clicked"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _fill_password_page_if_present(
    driver, email: str, timeout: int = 25, *, allow_password_page: bool = False,
) -> str | None:
    """邮箱提交后兼容 create-account/password。返回本次设置的 OpenAI 账号密码；未遇到密码页返回 None。

    ``allow_password_page`` 由「账号加固」开关决定，**默认 False**：
    没要求加固就绝不往设密码那条路走。见下面那段回归说明。
    """
    end = time.time() + timeout
    last = {}
    otp_page_since: float | None = None
    while time.time() < end:
        if _is_email_verification_page(driver):
            if not allow_password_page:
                # ★★ 回归修复（2026-10-04）：**没勾加固就别拐进设密码页**。
                #
                #   老代码因为认不出葡语的「Continuar com uma senha」，
                #   实际上**永远走不到**这条路 —— 于是「默认不带密码」这个行为
                #   是靠一个 bug 在维持。我把入口修好之后，它就变成**默认**拐进去，
                #   而那条路一出错（页面回 `Ops, ocorreu um erro!`）整轮注册直接报废。
                #   实测代价：并发 2 两个号全挂，而老大根本没开这个功能。
                return None
            result = _click_continue_with_password_if_present(driver)
            if result.get("ok"):
                logger.info("%s 邮箱验证码页已点击“使用密码继续”：email=%s detail=%s", _log_prefix(driver), email, result)
                time.sleep(0.8)
                continue
            # ★★ 别在这一刻就放弃（2026-10-04 实测踩坑）：
            #   刚跳到验证码页那一瞬间，页面上的可见可点元素可能是 **0 个**
            #   ——「使用密码继续」那个 <A> 还没挂上去。老代码在这里直接
            #   `return None`，于是「注册时设密码」这条路**从来没被走到过**
            #   （日志里永远只有一句 no_match，看起来像页面上根本没这个入口）。
            #   实测等 ~1s 后按钮就出现了。所以这里继续轮询 —— 但要有个上限，
            #   否则每次注册都会白等满 25s。
            if otp_page_since is None:
                otp_page_since = time.time()
            # ★★ 别死等那个入口渲染出来（2026-10-04 再踩一次）：
            #   实测它在验证码页上**有时 50 多秒才挂出来**，而验证码本身是有寿命的，
            #   死等要么超时放弃、要么把码等废。
            #   那个 <A> 的 href 就是 `/create-account/password` ——
            #   直接导航过去完全等价，且不受它何时渲染影响。
            if time.time() - otp_page_since > 6.0:
                from urllib.parse import urljoin

                try:
                    current = str(driver.current_url or "")
                except Exception:  # noqa: BLE001
                    current = ""
                target = urljoin(current or "https://auth.openai.com/", "/create-account/password")
                logger.info("%s 入口没渲染出来，直接导航到设密码页：%s", _log_prefix(driver), target)
                try:
                    driver.get(target)
                    time.sleep(2.0)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s 直接导航设密码页失败：%s", _log_prefix(driver), exc)
                otp_page_since = None
                continue
            time.sleep(0.8)
            continue
        otp_page_since = None
        last = _password_page_state(driver)
        is_signup_password = _is_signup_password_page(driver)
        is_login_password = _is_login_password_page(driver)
        if not (is_signup_password or is_login_password):
            # ★★ 登录态判断必须放在密码页判断**之后**（2026-10-04 实测踩坑）：
            #   老代码先 `_has_access_token()` 再判密码页，于是「使用密码继续」
            #   一跳过去、那边一探测到 session 就立刻 `return None` ——
            #   刚打开的设密码页被静默跳过，日志里只剩一堆「等待资料页中」。
            if _has_access_token(driver):
                return None
            time.sleep(0.5)
            continue
        passwordless = _click_passwordless_signup_if_present(driver) if is_login_password else {"ok": False, "reason": "signup_password_prefers_password"}
        if passwordless.get('ok'):
            logger.info("%s 检测到 password 页，已点击一次性验证码入口：email=%s detail=%s", _log_prefix(driver), email, passwordless)
            wait_end = time.time() + 20
            while time.time() < wait_end:
                if _is_email_verification_page(driver):
                    logger.info("%s 一次性验证码入口已进入邮箱验证码页", _log_prefix(driver))
                    return None
                if _has_access_token(driver):
                    logger.info("%s 一次性验证码入口后已检测到登录态", _log_prefix(driver))
                    return None
                time.sleep(0.5)
            logger.info("%s 已点击一次性验证码入口，未立即检测到 OTP 页，交给后续 OTP 阶段继续处理", _log_prefix(driver))
            return None
        if is_login_password:
            logger.info("%s 当前是登录密码页但未找到一次性验证码入口，跳过密码填写并交给 OTP 阶段：state=%s", _log_prefix(driver), last)
            return None
        password = _registration_password()
        logger.info("%s 检测到 create-account/password，准备设置密码（%s 位）：email=%s", _log_prefix(driver), len(password), email)
        # ★★ 先用 Playwright 正路填+提交（见 _playwright_set_signup_password 的说明）。
        #    它成了就直接走，省掉下面那套 JS 路径；
        #    它没成再退回老 JS 路径重试一次，两条腿走路。
        _pw = _playwright_set_signup_password(driver, password)
        logger.info("%s 密码页 Playwright 填写+提交：%s", _log_prefix(driver), _pw)
        if _pw.get("ok") and _wait_left_signup_password_page(driver, timeout=35):
            logger.info("%s 密码已设置（Playwright 路径）", _log_prefix(driver))
            return password
        if _pw.get("ok"):
            logger.warning("%s Playwright 提交后页面没走，回退到 JS 路径重试", _log_prefix(driver))
        else:
            logger.warning("%s Playwright 路径失败，回退到 JS 路径：%s", _log_prefix(driver), _pw.get("reason"))
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
          .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_submit'};
        // ★★ 直接在这个 JS 里把密码写进去。
        //   **绝对不要把 DOM 元素返回给 Python 再操作** —— 这个 CloakSeleniumDriver
        //   从 execute_script 返回的元素会被序列化成普通对象，再传回去就报
        //   `el.scrollIntoView is not a function`（2026-10-04 实测）。
        //   「注册时设密码」这条路从来没跑通过，根子就在这里。
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(input, arguments[0]);
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        input.focus();
        return {ok:true, reason:'password_filled', filled: input.value === arguments[0],
                valueLen: (input.value || '').length};
        """, password) or {}
        if not result.get('ok'):
            raise RuntimeError(f"密码页处理失败：{result} state={last}")
        logger.info("%s 已把密码写进 create-account/password 输入框：%s", _log_prefix(driver), result)
        # React/Auth0 会在 input/change 后异步校验密码强度并启用 Continue。
        # 之前输入完 0.4~1.4s 就点，偶发点在按钮还未真正可提交/事件未绑定完成时，页面无反应。
        human_delay("form", minimum=2.0, maximum=3.6)
        submit_result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !!el && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const pass = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
          .find(el => visible(el) && !el.disabled && !el.readOnly);
        const form = pass ? pass.closest('form') : document.querySelector('form');
        const scope = form || document;
        const candidates = [...scope.querySelectorAll('button,input[type="submit"],[role="button"]')]
          .filter(el => visible(el) && enabled(el));
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const scored = candidates.map((el, idx) => {
          const attrs = [
            el.getAttribute('type'), el.getAttribute('data-dd-action-name'),
            el.getAttribute('data-login-web-auth-control'), el.getAttribute('aria-label'),
            el.getAttribute('name'), el.getAttribute('value'), el.textContent
          ].join(' ').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('value') || '');
          let score = 0;
          if ((el.getAttribute('type') || '').toLowerCase() === 'submit') score += 80;
          if ((el.getAttribute('data-dd-action-name') || '').toLowerCase() === 'continue') score += 90;
          if (String(el.getAttribute('data-login-web-auth-control') || '').toLowerCase() === 'true') score += 70;
          if (/continue|next|submit|create|続行/.test(attrs) || /continue|next|submit|create|続行/.test(text)) score += 50;
          return {el, idx, score};
        }).sort((a,b) => b.score - a.score || a.idx - b.idx);
        const target = scored[0]?.el;
        if (!target) return {ok:false, reason:'missing_enabled_submit'};
        // ★ 同理：点击也在 JS 里做，不把元素返给 Python（见上面那段注释）。
        target.scrollIntoView({block:'center'});
        target.click();
        return {
          ok:true,
          reason:'enabled_submit_clicked',
          text: (target.textContent || target.getAttribute('value') || '').trim().slice(0, 80),
          type: target.getAttribute('type') || '',
          dd: target.getAttribute('data-dd-action-name') || '',
          ariaDisabled: target.getAttribute('aria-disabled') || ''
        };
        """) or {}
        if not submit_result.get("ok"):
            raise RuntimeError(f"密码页找不到可点击的 Continue 按钮：{submit_result} state={_password_page_state(driver)}")
        logger.info("%s 已填写并点击密码页 Continue：detail=%s", _log_prefix(driver), submit_result)
        # 提交密码后通常进入邮箱验证码页，最多等一段时间。
        wait_end = time.time() + 30
        retried_submit = False
        while time.time() < wait_end:
            if _is_email_verification_page(driver):
                logger.info("%s 密码提交后已进入邮箱验证码页", _log_prefix(driver))
                return password
            if _has_access_token(driver):
                logger.info("%s 密码提交后已检测到登录态", _log_prefix(driver))
                return password
            if not retried_submit and time.time() > wait_end - 22 and _is_signup_password_page(driver):
                retried_submit = True
                logger.info("%s 密码页点击后仍未跳转，改用协议属性再点一次提交", _log_prefix(driver))
                # ★ 别再猜了：把「点了没反应」那一刻的现场打出来 ——
                #   密码框还在不在、值还在不在、页面有没有报错文案。
                #   这段是 2026-10-04 为了定位「偶发提交无反应」加的。
                try:
                    probe = driver.execute_script(r"""
                    const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                    const pw = [...document.querySelectorAll('input')].filter(vis)
                      .filter(el => (el.getAttribute('type') || '') === 'password'
                                 || /password/i.test(el.getAttribute('name') || ''));
                    const errs = [...document.querySelectorAll('[role="alert"],[aria-live],[class*="error" i],[id*="error" i]')]
                      .filter(vis).map(el => (el.textContent || '').replace(/\s+/g,' ').trim()).filter(Boolean).slice(0, 5);
                    const btns = [...document.querySelectorAll('button,input[type=submit]')].filter(vis)
                      .map(el => ({text: (el.textContent||'').trim().slice(0,24),
                                   type: el.getAttribute('type')||'', disabled: !!el.disabled,
                                   dd: el.getAttribute('data-dd-action-name')||''}));
                    return {url: location.href, title: document.title,
                            pwCount: pw.length,
                            pwLens: pw.map(el => (el.value || '').length),
                            pwInvalid: pw.map(el => el.getAttribute('aria-invalid') || ''),
                            errors: errs, buttons: btns.slice(0, 8),
                            bodyText: (document.body?.innerText || '').replace(/\s+/g,' ').trim().slice(0, 300)};
                    """) or {}
                    logger.warning("%s[密码页现场] %s", _log_prefix(driver), probe)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s[密码页现场] 探测失败：%s", _log_prefix(driver), exc)
                human_delay("form", minimum=1.2, maximum=2.2)
                # ★ 老代码这里调 `_click_continue`，而它的选择器是
                #   Continue / 続行 / 继续 / Sign up / Create / Next —— **全是按文案**。
                #   葡语（当前巴西出口）页面上一条都不命中，所以这个重试形同虚设，
                #   密码页一卡就必然拖到资料页超时（2026-10-04 实测踩到）。
                _click_protocol_button(
                    driver,
                    intents=("intent=validate", "intent=password", "intent=next", "intent=continue"),
                    dd_names=("continue", "next", "submit"),
                    fallback_texts=("Continuar", "继续", "Continue", "Next", "下一步", "Próximo"),
                    timeout=6.0,
                    label="password_submit_retry",
                )
            if not _is_signup_password_page(driver):
                return password
            time.sleep(0.5)
        # ★★ 到这里说明密码这条路没走通，而且**页面已经被带离了验证码页**。
        #   绝不能因为一个可选增强把整个账号弄死（2026-10-04 实测：
        #   密码页提交没反应 → 后面 OTP 阶段撞上 `Ops, ocorreu um erro!` → 整轮报废）。
        #   这里主动退回邮箱验证码页，把账号交给后面的 OTP 阶段救回来。
        #   注意返回 None：调用方就不会把一个「其实没设上」的密码写进账号记录。
        logger.warning("%s 设密码没成功，回退到邮箱验证码流程救账号（可选功能不该拖死注册）",
                       _log_prefix(driver))
        try:
            driver.get("https://auth.openai.com/email-verification")
            time.sleep(2.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 回退到验证码页失败：%s", _log_prefix(driver), exc)
        return None
    logger.info("%s 未检测到密码页，继续后续流程 last=%s", _log_prefix(driver), last)
    return None


def _accept_profile_consents(driver) -> int:
    """about-you/profile 下出现韩国/日本个人信息同意协议时，默认全部勾选。

    不依赖可见文字；优先处理 allCheckboxes，再处理所有必选 consent checkbox。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const isChecked = el => el.checked === true || String(el.getAttribute('aria-checked') || el.closest('[role="checkbox"]')?.getAttribute('aria-checked') || '').toLowerCase() === 'true';
        const mark = el => {
          if (!el || isChecked(el)) return false;
          const label = el.closest('label');
          try {
            (label && visible(label) ? label : el).scrollIntoView({block:'center'});
            (label && visible(label) ? label : el).click();
          } catch (_) {}
          if (!isChecked(el)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
            if (setter) setter.call(el, true); else el.checked = true;
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return isChecked(el);
        };
        const all = [...document.querySelectorAll('input[type="checkbox"]')]
          .filter(el => visible(el) || visible(el.closest('label')));
        if (!all.length) return {count:0, names:[]};
        const byName = name => all.find(el => String(el.name || '').toLowerCase() === name.toLowerCase());
        const ordered = [];
        const add = el => { if (el && !ordered.includes(el)) ordered.push(el); };
        add(byName('allCheckboxes'));
        for (const name of ['personalInfoConsent', 'thirdPartyConsent', 'overseasTransferConsent']) add(byName(name));
        for (const el of all) {
          const n = String(el.name || '').toLowerCase();
          const id = String(el.id || '').toLowerCase();
          if (/consent|checkbox|agree|required|personal|third|overseas/.test(`${n} ${id}`)) add(el);
        }
        // about-you/profile 页面里的 checkbox 基本都是必选 consent；剩余可见 checkbox 也全部勾选。
        for (const el of all) add(el);
        const clicked = [];
        for (const el of ordered) {
          if (mark(el)) clicked.push(el.name || el.id || 'checkbox');
        }
        return {count: clicked.length, names: clicked};
        """) or {}
        count = int(result.get('count') or 0)
        if count:
            logger.info("%s 已勾选 about-you/profile 同意协议复选框：%s", _log_prefix(driver), result.get('names'))
        return count
    except Exception as exc:
        logger.debug('%s 勾选 profile consent 失败：%s', _log_prefix(driver), exc)
        return 0


def _complete_profile_page(driver, name: str, birthday: str, timeout: int = 45) -> bool:
    """等待并完成姓名/生日页；若已经登录成功则返回 False，不把它当失败。"""
    end = time.time() + timeout
    y, m, d = birthday.split('-')
    from datetime import date
    today = date.today()
    age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    last_snapshot = {}
    while time.time() < end:
        time.sleep(1)
        if _has_access_token(driver):
            logger.info('%s 已检测到登录态，资料页可能已跳过', _log_prefix(driver))
            return False
        snap = _page_snapshot(driver)
        last_snapshot = snap
        if not _is_profile_like(snap):
            logger.info('%s 等待资料页中：url=%s', _log_prefix(driver), snap.get('url'))
            continue

        logger.info('%s 检测到资料页，开始填写姓名生日：url=%s inputs=%s', _log_prefix(driver), snap.get('url'), snap.get('inputs'))
        name_ok = False
        # 常见单姓名字段
        for selectors in [
            ["input[name='name']", "input[name='fullName']", "input[name='full_name']", "input[autocomplete='name']"],
            ["input[placeholder*='Name']", "input[placeholder*='name']", "input[aria-label*='Name']", "input[aria-label*='name']"],
        ]:
            if _select_or_type(driver, selectors, name, timeout=3):
                logger.info("%s 已填写姓名字段：%s", _log_prefix(driver), name)
                name_ok = True
                break
        # 兼容 first/last 分开
        if not name_ok:
            parts = name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else 'User'
            first_ok = _select_or_type(driver, ["input[name='firstName']", "input[name='first_name']", "input[placeholder*='First']", "input[aria-label*='First']"], first, timeout=2)
            last_ok = _select_or_type(driver, ["input[name='lastName']", "input[name='last_name']", "input[placeholder*='Last']", "input[aria-label*='Last']"], last, timeout=2)
            name_ok = first_ok or last_ok

        birth_mode = _fill_birthday_or_age(driver, birthday, age)
        birth_ok = bool(birth_mode)
        if birth_ok:
            if birth_mode == 'age':
                logger.info("%s 已填写年龄字段：%s", _log_prefix(driver), age)
            else:
                logger.info("%s 已填写生日字段 mode=%s value=%s", _log_prefix(driver), birth_mode, birthday)

        if not name_ok or not birth_ok:
            logger.warning('%s 资料页字段未填完整 name_ok=%s birth_ok=%s snapshot=%s', _log_prefix(driver), name_ok, birth_ok, snap)
            continue

        _accept_profile_consents(driver)
        human_delay('form')
        for _ in range(3):
            if _click_if_enabled_submit(driver):
                logger.info('%s 已点击资料页提交按钮，等待 OAuth 跳转', _log_prefix(driver))
                return True
            time.sleep(1)
        logger.warning('%s 找不到可点击的资料页提交按钮 snapshot=%s', _log_prefix(driver), _page_snapshot(driver))
    raise RuntimeError(f'等待/填写资料页超时，最后页面：{last_snapshot}')


def _click_if_enabled_submit(driver) -> bool:
    """提交资料页：优先 form.requestSubmit/button[type=submit]，不依赖按钮文字。"""
    try:
        target = driver.execute_script(r"""
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const forms = [...document.querySelectorAll('form')].filter(visible);
        for (const form of forms) {
          const submit = form.querySelector('button[type="submit"], input[type="submit"]');
          if (submit && visible(submit) && !submit.disabled) {
            submit.scrollIntoView({block:'center'});
            return submit;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return 'submitted_by_requestSubmit';
          }
        }
        const submitters = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .filter(el => visible(el) && !el.disabled);
        if (submitters.length) {
          submitters[0].scrollIntoView({block:'center'});
          return submitters[0];
        }
        // 兜底：页面只有一个可点击 button 时点击它，但仍不读文字。
        const buttons = [...document.querySelectorAll('button:not([disabled])')].filter(visible);
        if (buttons.length === 1) {
          buttons[0].scrollIntoView({block:'center'});
          return buttons[0];
        }
        return null;
        """)
        if not target:
            return False
        if isinstance(target, str):
            return True
        _human_click(driver, target, label="profile_submit")
        return True
    except Exception:
        return False


def _read_chatgpt_session_once(driver) -> dict | None:
    """当前页面必须在 chatgpt.com；读取 /api/auth/session，拿不到 token 返回 None。"""
    script = r"""
    const done = arguments[0];
    fetch('/api/auth/session', {credentials: 'include'})
      .then(r => r.json())
      .then(j => done({ok: true, data: j}))
      .catch(e => done({ok: false, error: String(e)}));
    """
    result = driver.execute_async_script(script)
    if result and result.get("ok"):
        data = result.get("data") or {}
        if data.get("accessToken"):
            logger.info("%s /api/auth/session 已返回 accessToken", _log_prefix(driver))
            return data
        # 只有 WARNING_BANNER 之类的键时，说明服务端拒绝下发 session（账号被标记）。
        # 原文以前被丢掉了，事后只能看到一个键名，所以把内容打出来。
        logger.warning(
            "%s 等待 ChatGPT session 写入 accessToken，当前响应 keys=%s banner=%s",
            _log_prefix(driver), list(data.keys()),
            str(data.get("WARNING_BANNER") or data.get("warningBanner") or "")[:300],
        )
    elif result is not None:
        logger.warning("%s 读取 session 失败：%s", _log_prefix(driver), str(result.get("error"))[:200])
    return None


def _switch_to_chatgpt_window_if_any(driver) -> bool:
    """有些浏览器/适配层会在新窗口完成 callback；尝试切到已有 chatgpt.com 句柄。"""
    try:
        handles = list(getattr(driver, "window_handles", []) or [])
        current_handle = None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:
            current_handle = None
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in str(getattr(driver, "current_url", "") or ""):
                    return True
            except Exception:
                continue
        if current_handle is not None:
            try:
                driver.switch_to.window(current_handle)
            except Exception:
                pass
    except Exception:
        pass
    return False


def _fetch_chatgpt_session(driver, timeout: int = 90, auto_jump_wait: int = 15) -> dict:
    """等待页面完成跳转并从 ChatGPT 页面内读取登录 session/accessToken。

    旧逻辑会在 auth.openai.com 上一直等到总超时，Cloak/部分 Chromium 场景下
    实际账号已创建成功但当前句柄 URL 没及时更新，导致白等 120 秒。现在只给
    自动跳转 `auto_jump_wait` 秒；超过后立即主动打开 chatgpt.com 读 session。
    """
    end = time.time() + timeout
    auto_jump_end = time.time() + max(3, int(auto_jump_wait or 15))
    last_data = None
    forced_chatgpt_open = False

    while time.time() < end:
        try:
            current = str(driver.current_url or '')
        except Exception:
            current = ''

        if 'chatgpt.com' not in current:
            if _switch_to_chatgpt_window_if_any(driver):
                current = str(getattr(driver, "current_url", "") or "")
            elif time.time() >= auto_jump_end and not forced_chatgpt_open:
                try:
                    logger.info("%s 未在 %ss 内观察到当前窗口跳转 chatgpt.com，主动打开 ChatGPT 内读取 session", _log_prefix(driver), int(auto_jump_wait or 15))
                    _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
                    forced_chatgpt_open = True
                    time.sleep(3)
                    current = str(getattr(driver, "current_url", "") or "")
                except Exception as exc:
                    last_data = f"{type(exc).__name__}: {exc}"
            else:
                time.sleep(1)
                continue

        if 'chatgpt.com' in current:
            try:
                data = _read_chatgpt_session_once(driver)
                if data:
                    return data
                last_data = "session 暂无 accessToken"
            except Exception as exc:
                last_data = f"{type(exc).__name__}: {exc}"
        time.sleep(2)

    raise RuntimeError(f"等待 /api/auth/session accessToken 超时，最后响应: {str(last_data)[:800]}")


def _check_manual_stop() -> None:
    check_stop()
