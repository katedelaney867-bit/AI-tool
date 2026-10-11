# -*- coding: utf-8 -*-
"""注册完给账号加「登录密码 + 2FA」。

★ 为什么要这个（老大的需求）：
  现在 OTP 注册出来的号**没有密码**，二次登录只能再拿邮箱 —— 交付出去基本是
  「一次性登录」。而且 Codex 激活一旦掉到登录页，就只能走「一次性验证码登录」，
  那条路实测被 OpenAI 服务端拒（见 `oauth.py` 里那段注释）。设了密码之后
  登录页就有密码路径可走。

★★ 识别键一律用 `data-testid` / 表单协议属性，**绝不看界面文字**：
  `cloak_driver` 启动时 `geoip=True` 是故意让 locale 跟着出口 IP 走的（防关联），
  所以页面语言 = 用户代理所在国，必然任意。按文案找元素 = 换个国家就静默失效。

实测拿到的稳定键（巴西出口 / pt-BR 页面，真实 DOM）：

    [data-testid='security-tab']              安全设置页
    [data-testid='password-setting']          密码 → 添加（点了会跳转到邮箱验证页）
    [data-testid='mfa-authenticator-toggle']  MFA 认证器
    [data-testid='mfa-sms-toggle']            MFA 短信

**设密码的流程**（实测）：点 `password-setting` 会**导航**到
`https://auth.openai.com/email-verification` 发一封验证码邮件 → 填码 →
才进到「设置新密码」表单。所以设密码必须能取到这个号的邮箱验证码。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import string
import struct
import time

logger = logging.getLogger(__name__)

SECURITY_URL = "https://chatgpt.com/#settings/Security"

TESTID_SECURITY_TAB = "security-tab"
TESTID_PASSWORD_SETTING = "password-setting"
TESTID_MFA_AUTHENTICATOR = "mfa-authenticator-toggle"

# 生成密码用的字符集：刻意不含容易在受控输入框里被搞混/被转义的字符。
PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 16


def _noop(stage: str, detail: str = "") -> None:
    return None


# --------------------------------------------------------------------- 口令

def generate_password(length: int = PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def totp_now(secret_b32: str, *, digits: int = 6, period: int = 30, at: float | None = None) -> str:
    """标准 TOTP（RFC 6238，HMAC-SHA1）。

    自己实现是为了**不引入 pyotp 依赖** —— 逻辑就这十来行，多个包多个坑。
    """
    raw = str(secret_b32 or "").strip().replace(" ", "").upper()
    if not raw:
        raise ValueError("TOTP secret 为空")
    key = base64.b32decode(raw + "=" * (-len(raw) % 8))
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# --------------------------------------------------------------- 页面小工具

def _safe_url(driver) -> str:
    try:
        return str(driver.current_url or "")
    except Exception:  # noqa: BLE001
        return ""


def _dump(driver, label: str) -> None:
    from .browser_flow import _dump_clickables

    logger.warning("[加固] %s 页面 url=%s", label, _safe_url(driver))
    # ★ 设置页的入口常常是**弹窗**而不是跳转，弹窗正文里有「下一步」和密钥，
    #   不抓就完全瞎。踩过：2FA 那条失败时只看到「没读到 secret」，
    #   却不知道弹窗压根没开、还是开了没给密钥。
    try:
        dialog = driver.execute_script(r"""
        const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const d = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"],.react-aria-Modal,[data-testid*="modal" i]')]
          .filter(vis)[0];
        return d ? (d.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 900) : '';
        """)
        if dialog:
            logger.warning("[加固] %s 弹窗正文：%s", label, dialog)
        else:
            logger.warning("[加固] %s 页面上没有可见弹窗", label)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[加固] %s 弹窗探测失败：%s", label, exc)
    # ★ 也要把可见 input 列出来：踩过「找不到验证码输入框」，结果 dump 里
    #   只有按钮没有输入框，等于什么都没说。
    try:
        inputs = driver.execute_script(r"""
        const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        return [...document.querySelectorAll('input')].filter(vis).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', testid: el.getAttribute('data-testid') || '',
          autocomplete: el.getAttribute('autocomplete') || '',
          inputmode: el.getAttribute('inputmode') || '',
          maxlength: el.getAttribute('maxlength') || '',
          placeholder: (el.getAttribute('placeholder') || '').slice(0, 30),
        }));
        """) or []
        logger.warning("[加固] %s 可见 input %s 个：", label, len(inputs))
        for i in inputs:
            logger.warning("[加固]   <input type=%r name=%r id=%r testid=%r autocomplete=%r "
                           "inputmode=%r maxlength=%r placeholder=%r>",
                           i.get("type"), i.get("name"), i.get("id"), i.get("testid"),
                           i.get("autocomplete"), i.get("inputmode"), i.get("maxlength"),
                           i.get("placeholder"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[加固] %s input dump 失败：%s", label, exc)
    _dump_clickables(driver, f"加固-{label}")


def _find_testid(driver, testid: str):
    try:
        return driver.execute_script(r"""
        const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        return [...document.querySelectorAll('[data-testid="' + arguments[0] + '"]')].find(vis) || null;
        """, testid)
    except Exception:  # noqa: BLE001
        return None


def _wait_testid(driver, testid: str, timeout: float = 20.0):
    end = time.time() + timeout
    while time.time() < end:
        el = _find_testid(driver, testid)
        if el is not None:
            return el
        time.sleep(0.5)
    return None


def _click_testid(driver, testid: str, timeout: float = 20.0) -> bool:
    """按 data-testid 点。

    ★ 点击策略（2026-10-04 实测踩坑）：设置页这些是 React 受控按钮，
      `_human_click` 走坐标点击时**经常完全没反应**（页面纹丝不动、
      既不跳转也不弹窗，而且不报错）。实测**裸 JS `el.click()` 一跳就走**。
      所以顺序反过来：先裸点击，确认没反应再退回拟人点击。
      （设置页本身风控压力低，这里用裸点击是安全的。）
    """
    el = _wait_testid(driver, testid, timeout)
    if el is None:
        return False
    try:
        driver.execute_script(r"""
        const el = arguments[0];
        el.scrollIntoView({block: 'center'});
        el.focus && el.focus();
        el.click();
        """, el)
        logger.info("[加固] 已点击 [data-testid=%r]（裸 JS）", testid)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[加固] 裸点击 %r 失败：%s，退回拟人点击", testid, exc)
    try:
        from .browser_flow import _human_click

        _human_click(driver, el, label=f"testid:{testid}")
        logger.info("[加固] 已点击 [data-testid=%r]（拟人）", testid)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[加固] 拟人点击 %r 也失败：%s", testid, exc)
        return False


def _visible_password_inputs(driver) -> int:
    try:
        return int(driver.execute_script(r"""
        const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        return [...document.querySelectorAll('input[type="password"]')].filter(vis).length;
        """) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _on_email_verification(driver) -> bool:
    if "email-verification" in _safe_url(driver).lower():
        return True
    try:
        from .browser_flow import _is_email_verification_page

        return bool(_is_email_verification_page(driver))
    except Exception:  # noqa: BLE001
        return False


def _wait_state(driver, timeout: float = 45.0) -> str:
    """等页面落到可识别的一步：'verify'（邮箱验证码页）/ 'form'（密码表单）/ 'other'。"""
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        url = _safe_url(driver)
        if url != last:
            logger.info("[加固] 页面：%s", url or "-")
            last = url
        if _on_email_verification(driver):
            return "verify"
        if _visible_password_inputs(driver) > 0:
            return "form"
        time.sleep(0.6)
    return "other"


def _open_security_settings(driver) -> bool:
    """打开「安全设置」页。返回是否成功。

    ★ `#settings/Security` 是 SPA 的 **hash 路由**：如果浏览器已经停在 chatgpt.com 上
      （注册刚结束就是这样），单纯 `driver.get()` 改个 hash 属于**同文档导航**，
      可能压根不重载，设置界面也就不渲染 —— 表现成「安全设置页没加载出来」
      （2026-10-04 实测踩到）。

      所以：先回首页落一下地，再进设置；还不行就手动改 hash + 派发 hashchange 兜底。
    """
    for attempt in (1, 2, 3):
        if attempt == 2:
            try:
                driver.get("https://chatgpt.com/")
                time.sleep(4)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[加固] 回首页失败：%s", exc)
        if attempt == 3:
            # ★ 前两次都是「同文档 hash 导航」，SPA 可能压根不重载。
            #   加个无意义的 query 强制**真导航**回首页，再改 hash。
            try:
                driver.get("https://chatgpt.com/?_r=%d" % int(time.time()))
                time.sleep(6)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[加固] 强制重载首页失败：%s", exc)
        try:
            driver.get(SECURITY_URL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[加固] 打开安全设置页失败（第 %s 次）：%s", attempt, exc)
            continue
        if _wait_testid(driver, TESTID_SECURITY_TAB, 25) is not None:
            logger.info("[加固] 安全设置页已就绪（第 %s 次）", attempt)
            return True
        # hash 没触发重载时手动补一刀
        try:
            driver.execute_script(
                "location.hash = '#settings/Security';"
                "try { window.dispatchEvent(new HashChangeEvent('hashchange')); } catch (e) {}"
            )
        except Exception:  # noqa: BLE001
            pass
        if _wait_testid(driver, TESTID_SECURITY_TAB, 15) is not None:
            logger.info("[加固] 安全设置页已就绪（第 %s 次·hash 兜底）", attempt)
            return True
        logger.warning("[加固] 第 %s 次打开安全设置页没成功，当前 url=%s", attempt, _safe_url(driver))
    return False


# --------------------------------------------------------------------- 设密码

def set_password(
    driver,
    *,
    mailbox,
    inbox,
    config,
    emit=_noop,
    password: str = "",
) -> tuple[bool, str]:
    """给当前已登录的账号设置登录密码。返回 ``(ok, note)``，异常一律转成 (False, 原因)。"""
    from . import browser_flow as flow

    password = str(password or "").strip() or generate_password()
    try:
        # ---- 1. 打开安全设置 ----
        emit("harden", "正在打开账号安全设置")
        if not _open_security_settings(driver):
            _dump(driver, "安全设置页没出来")
            return False, "安全设置页没加载出来"
        _click_testid(driver, TESTID_SECURITY_TAB, 15)
        time.sleep(4)

        # ---- 2. 点「密码 → 添加」----
        if _wait_testid(driver, TESTID_PASSWORD_SETTING, 25) is None:
            _dump(driver, "找不到添加密码入口")
            return False, "找不到「添加密码」入口"
        emit("harden", "正在点「添加密码」")
        # ★ 这一步会**导航**到 auth.openai.com/email-verification，所以时间戳要在点之前取。
        after_ts = time.time()
        _click_testid(driver, TESTID_PASSWORD_SETTING)

        state = _wait_state(driver, timeout=45)
        logger.info("[加固] 点完「添加密码」后落到：%s", state)

        # ---- 3. 邮箱验证码 ----
        if state == "verify":
            emit("harden", "设密码需要邮箱验证码，正在取码")
            code = ""
            try:
                code = str(mailbox.wait_for_otp(
                    inbox, after_ts=after_ts, timeout=int(config.otp_timeout or 180),
                ) or "").strip()
            except Exception as exc:  # noqa: BLE001
                return False, f"设密码取验证码失败：{type(exc).__name__}: {str(exc)[:120]}"
            if not code:
                return False, "设密码需要邮箱验证码，但没取到"
            emit("harden", "已取到验证码，正在填写")
            flow._clear_otp_inputs(driver)
            flow._type_otp(driver, code)
            if not flow._click_email_otp_submit(driver):
                flow._click_continue(driver)
            state = _wait_state(driver, timeout=45)
            logger.info("[加固] 提交验证码后落到：%s", state)

        if state != "form":
            _dump(driver, f"没进到设密码表单（state={state}）")
            return False, f"没进到设置密码表单（当前 {_safe_url(driver) or '未知页'}）"

        # ---- 4. 填新密码 ----
        emit("harden", "正在设置新密码")
        filled = _fill_new_password(driver, password)
        if not filled:
            _dump(driver, "没找到新密码输入框")
            return False, "没找到新密码输入框"
        emit("harden", "已提交新密码")

        # ---- 5. 判断结果 ----
        ok, note = _wait_password_saved(driver, timeout=40)
        if ok:
            return True, f"已设置登录密码（长度 {len(password)}）"
        return False, note
    except Exception as exc:  # noqa: BLE001
        _dump(driver, "设密码异常")
        return False, f"设密码异常：{type(exc).__name__}: {str(exc)[:160]}"


def _fill_new_password(driver, password: str) -> bool:
    """把新密码填进所有可见的 password 输入框（新密码 + 确认），然后提交。

    不按 name/placeholder 认（那些会跟着语言/版本变），只按
    `input[type=password]` 这个**协议属性**。填几个就填几个：
    现在账号没有密码，所以正常情况下就是「新密码 + 确认」两个。
    """
    from .browser_flow import _human_type_text

    try:
        n = _visible_password_inputs(driver)
        if n <= 0:
            return False
        logger.info("[加固] 可见 password 输入框 %s 个，逐个填入", n)
        for idx in range(n):
            el = driver.execute_script(r"""
            const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
              && !el.disabled && !el.readOnly;
            const list = [...document.querySelectorAll('input[type="password"]')].filter(vis);
            return list[arguments[0]] || null;
            """, idx)
            if el is None:
                continue
            _human_type_text(driver, el, password, clear=True)
            time.sleep(0.3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[加固] 填密码失败：%s", exc)
        return False

    # 提交：协议属性优先（intent=validate 是这个家族一贯的提交键），文案只兜底。
    from .browser_flow import _click_protocol_button

    r = _click_protocol_button(
        driver,
        intents=("intent=validate", "intent=password", "intent=set_password", "intent=save"),
        dd_names=("continue", "save", "submit", "confirm"),
        fallback_texts=(
            "保存", "确认", "继续", "设置密码", "更新密码",
            "Save", "Continue", "Confirm", "Set password", "Update password",
            "Salvar", "Continuar", "Confirmar", "Definir senha",
        ),
        timeout=6.0,
        label="set_password_submit",
    )
    return bool(r.get("ok"))


def _wait_password_saved(driver, timeout: float = 40.0) -> tuple[bool, str]:
    """等设密码流程收尾。

    成功的样子：回到 chatgpt.com 的设置页，且「密码」那一项不再是「添加」。
    失败的常见样子：还停在密码表单（说明被拒/没提交上），或出现错误提示。
    """
    end = time.time() + timeout
    while time.time() < end:
        url = _safe_url(driver)
        if "chatgpt.com" in url and "#settings" in url:
            # 回到设置页了，看密码项的状态
            el = _find_testid(driver, TESTID_PASSWORD_SETTING)
            if el is not None:
                try:
                    txt = str(el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    txt = ""
                logger.info("[加固] 回到设置页，密码项显示：%r", txt[:60])
                return True, f"已设置登录密码（密码项：{txt[:40]}）"
            return True, "已设置登录密码"
        if _visible_password_inputs(driver) == 0 and "chatgpt.com" not in url:
            time.sleep(1)
            continue
        time.sleep(0.8)
    _dump(driver, "设密码后没收到明确结果")
    return False, f"设置密码后没等到明确结果（当前 {_safe_url(driver) or '未知页'}）"


# ----------------------------------------------------------------------- 2FA

_B32_RE = r"[A-Z2-7]{16,64}"


def _read_totp_secret(driver) -> str:
    """从页面上找 TOTP secret。

    OpenAI 的认证器绑定页通常给二维码 + 「手动输入密钥」。密钥可能出现在：
      - `otpauth://totp/...?secret=XXXX` 形式的 URI（在 img src / 文本里）
      - 纯 base32 文本
    两种都试。
    """
    try:
        found = driver.execute_script(r"""
        const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const blob = [];
        for (const el of document.querySelectorAll('img[src], a[href], code, kbd, samp, span, div, p, textarea, input')) {
          if (!vis(el)) continue;
          const s = el.getAttribute('src') || el.getAttribute('href') || el.value || el.textContent || '';
          if (s) blob.push(String(s));
        }
        const joined = blob.join('\n');
        const m = joined.match(/secret=([A-Za-z2-7]{16,64})/);
        if (m) return {secret: m[1].toUpperCase(), how: 'otpauth'};
        const m2 = joined.match(/\b([A-Z2-7]{32})\b/);
        if (m2) return {secret: m2[1], how: 'raw32'};
        return {secret: '', how: 'none', scanned: blob.length};
        """) or {}
        return str(found.get("secret") or "")
    except Exception:  # noqa: BLE001
        return ""


_FILL_TOTP_JS = r"""
const code = arguments[0];
const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
  && !el.disabled && !el.readOnly;
const meta = el => [el.getAttribute('name'), el.id, el.getAttribute('data-testid'),
                    el.getAttribute('autocomplete'), el.getAttribute('inputmode'),
                    el.getAttribute('aria-label'), el.getAttribute('placeholder')]
                   .join(' ').toLowerCase();
const all = [...document.querySelectorAll('input')].filter(vis).map(el => ({
  type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
  testid: el.getAttribute('data-testid') || '',
  autocomplete: el.getAttribute('autocomplete') || '',
  inputmode: el.getAttribute('inputmode') || '',
  maxlength: el.getAttribute('maxlength') || '',
  placeholder: (el.getAttribute('placeholder') || '').slice(0, 30),
}));
// ★ 只考虑「可能是验证码框」的输入；把搜索框之类明确排掉。
//   踩过的坑：没排除时它把 6 位码填进了设置页的 settings-search 搜索框
//   （因为那个弹窗没有 role=dialog，作用域退化成整个 document）。
const cands = [...document.querySelectorAll('input')].filter(vis).filter(el => {
  const t = (el.getAttribute('type') || 'text').toLowerCase();
  if (['password', 'email', 'hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'search'].includes(t)) return false;
  const m = meta(el);
  if (/search|pesquis|buscar|pesquisa/.test(m)) return false;
  return true;
});
if (!cands.length) return {ok: false, reason: 'no_candidate_input', allInputs: all};
const score = el => {
  let s = 0;
  const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
  const im = (el.getAttribute('inputmode') || '').toLowerCase();
  const ml = parseInt(el.getAttribute('maxlength') || '0', 10);
  const m = meta(el);
  if (ac.includes('one-time-code')) s += 5;
  if (im === 'numeric') s += 4;
  if (ml === 6) s += 4;
  if (/code|otp|totp|verif/.test(m)) s += 3;
  if ((el.getAttribute('type') || '') === 'text') s += 1;
  return s;
};
cands.sort((a, b) => score(b) - score(a));
const el = cands[0];
const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
setter.call(el, code);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
el.focus();
return {ok: el.value === code, value: el.value, pickedScore: score(el),
        attrs: {type: el.getAttribute('type'), name: el.getAttribute('name'), id: el.id,
                testid: el.getAttribute('data-testid'),
                autocomplete: el.getAttribute('autocomplete'),
                inputmode: el.getAttribute('inputmode'),
                maxlength: el.getAttribute('maxlength')},
        allInputs: all};
"""


def _fill_totp_code(driver, code: str, attempts: int = 3) -> bool:
    """把 6 位 TOTP 填进弹窗里的验证码框。

    ★★ 用 Playwright 的 `locator.fill()`，**不要**用我们自己那套
      「JS 原生 setter + dispatch input/change」：
      实测那样写完之后 **React 的 state 并没有更新** ——
      框里明明有 6 位码，`Verificar` 按钮却一直是 `disabled`
      （2026-10-04 dump 证据：`<BUTTON disabled=True text='Verificar'>`），
      于是 `requestSubmit()` 提交上去的是空值，流程跑偏到 chat 页。
      `locator.fill()` 是 Playwright 专门为受控输入做的，事件发得全。
    """
    page = getattr(driver, "page", None)
    if page is None:
        logger.warning("[加固] driver 上没有 page，拿不到 Playwright 定位器")
        return False
    selectors = ("#totp_otp", "input[name='totp_otp']", "input[autocomplete='one-time-code']")
    for i in range(1, max(1, attempts) + 1):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=5000)
                loc.fill(code)
                got = str(loc.input_value() or "")
                if got == code:
                    logger.info("[加固] TOTP 已用 Playwright fill 填入 %s（第 %s 次）", sel, i)
                    return True
                logger.warning("[加固] fill 后回读不一致 %s：期望 %r 实际 %r", sel, code, got)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[加固] fill %s 失败：%s", sel, type(exc).__name__)
        time.sleep(1.0)
    return False


_SUBMIT_2FA_JS = r"""
const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
const enabled = el => !!el && !el.disabled
  && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
// 以验证码框为锚点，把范围收进它所在的表单 —— 比全页找按钮准得多。
const code = document.querySelector('#totp_otp, input[name="totp_otp"], input[autocomplete="one-time-code"]');
const form = code ? code.closest('form') : document.querySelector('form');
const scope = form || document;
const cands = [...scope.querySelectorAll('button,input[type=submit],[role=button]')]
  .filter(el => vis(el) && enabled(el));
const okText = /continuar|verificar|confirmar|ativar|salvar|continue|verify|confirm|enable|activate|save|next|下一步|验证|确认|启用/;
// ★ 主判据是 `type=submit`（协议属性、语言无关）；文案只兜底。
const btn = cands.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit')
         || cands.find(el => okText.test(norm(el.textContent || el.getAttribute('value') || '')));
if (btn) {
  btn.scrollIntoView({ block: 'center' });
  btn.click();
  return {ok: true, how: 'button', text: (btn.textContent || '').trim().slice(0, 40),
          disabled: !!btn.disabled};
}
if (form && form.requestSubmit) {
  try { form.requestSubmit(); return {ok: true, how: 'requestSubmit'}; } catch (e) {}
}
return {ok: false, reason: 'no_enabled_submit', cands: cands.length,
        all: [...scope.querySelectorAll('button,input[type=submit],[role=button]')]
               .map(el => ({text: (el.textContent || '').trim().slice(0, 30),
                            type: el.getAttribute('type') || '', disabled: !!el.disabled}))};
"""


def _submit_2fa(driver, timeout: float = 25.0) -> bool:
    """提交 2FA 绑定。

    ★★ 用 Playwright 的 `locator.click()`，别用 JS `el.click()` / `form.requestSubmit()`：
      - 那个「Verificar」按钮**填码前是 disabled** 的，而且它没有 `name=intent` /
        `data-dd-action-name` 这类协议属性，只能靠结构定位；
      - `locator.click()` 会**自动等元素变为可操作**，正好治这个；
      - 实测 JS 的 `requestSubmit()` 会提交到不相干的表单上，把页面带到
        `chatgpt.com/c/...` 聊天页去（2026-10-04 现场证据）。

    定位方式：以验证码框为锚点收窄到它所在的 form，再取 `type=submit` 按钮。
    """
    page = getattr(driver, "page", None)
    if page is None:
        logger.warning("[加固] driver 上没有 page，拿不到 Playwright 定位器")
        return False
    deadline = time.time() + timeout
    last = ""
    selectors = (
        "form:has(input[name='totp_otp']) button[type='submit']",
        "form:has(input[name='totp_otp']) button",
        "form:has(input[autocomplete='one-time-code']) button[type='submit']",
        "button:text-is('Verificar')",
        "button:text-is('Verify')",
        "button:text-is('确认')",
    )
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                loc.click(timeout=5000)          # 自动等 enabled / stable
                logger.info("[加固] 2FA 已提交（Playwright click %s）", sel)
                return True
            except Exception as exc:  # noqa: BLE001
                last = f"{sel}: {type(exc).__name__}"
        time.sleep(1.0)
    logger.warning("[加固] 2FA 提交按钮一直点不上：%s", last)
    return False


def enable_authenticator_2fa(driver, *, emit=_noop) -> tuple[bool, str, str]:
    """给当前已登录账号绑定 TOTP 认证器。返回 ``(ok, note, secret)``。"""
    from .browser_flow import _click_protocol_button, _dump_clickables

    try:
        emit("harden", "正在打开 2FA 绑定")
        if _wait_testid(driver, TESTID_SECURITY_TAB, 3) is None:
            if not _open_security_settings(driver):
                _dump(driver, "2FA-安全设置页没出来")
                return False, "安全设置页没加载出来", ""
        _click_testid(driver, TESTID_SECURITY_TAB, 15)
        time.sleep(4)
        if _wait_testid(driver, TESTID_MFA_AUTHENTICATOR, 25) is None:
            _dump(driver, "2FA-找不到认证器开关")
            return False, "找不到 MFA 认证器开关", ""
        _click_testid(driver, TESTID_MFA_AUTHENTICATOR)

        # ★ 认证器绑定一般分两步：先弹一个介绍弹窗（里面有「下一步/设置」按钮），
        #   点进去才显示二维码和手动输入的密钥。所以这里边等边推进一步。
        secret = ""
        advanced = False
        end = time.time() + 45
        while time.time() < end:
            secret = _read_totp_secret(driver)
            if secret:
                break
            if not advanced:
                r = _click_protocol_button(
                    driver,
                    intents=("intent=validate", "intent=next", "intent=setup", "intent=enable"),
                    dd_names=("continue", "next", "set up", "setup", "begin", "enable"),
                    fallback_texts=(
                        "下一步", "继续", "设置", "开始", "启用",
                        "Next", "Continue", "Set up", "Begin", "Enable",
                        "Próximo", "Continuar", "Configurar", "Ativar",
                    ),
                    timeout=2.0,
                    label="2fa_advance",
                )
                if r.get("ok"):
                    advanced = True
                    logger.info("[加固] 2FA 弹窗已推进一步")
            time.sleep(1.5)
        if not secret:
            _dump(driver, "2FA-没读到 secret（弹窗正文见上）")
            return False, "没从页面上读到 TOTP secret", ""

        logger.info("[加固] 读到 TOTP secret（长度 %s）", len(secret))
        emit("harden", "已拿到 2FA 密钥，正在用一次一密确认")
        if not _fill_totp_code(driver, code := totp_now(secret)):
            # 填不进去就 dump —— 「找不到 OTP 输入框」这种话对定位毫无帮助，
            # 得看到那个框的真实属性（name/id/testid/autocomplete）。
            _dump(driver, "2FA-找不到验证码输入框")
            return False, "没找到 TOTP 验证码输入框", secret

        if not _submit_2fa(driver, timeout=25.0):
            _dump(driver, "2FA-提交按钮不可用")
            return False, "没找到可用的 2FA 确认按钮", secret

        time.sleep(6)
        # 收尾：有些流程会再要一次 code，或者直接显示恢复码。
        if _wait_testid(driver, TESTID_MFA_AUTHENTICATOR, 20) is not None:
            return True, "已绑定认证器 2FA", secret
        _dump(driver, "2FA-结果不明")
        return False, "2FA 绑定后没等到明确结果", secret
    except Exception as exc:  # noqa: BLE001
        _dump_clickables(driver, "2FA-异常")
        return False, f"2FA 绑定异常：{type(exc).__name__}: {str(exc)[:160]}", ""
