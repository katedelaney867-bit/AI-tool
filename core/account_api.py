# -*- coding: utf-8 -*-
"""账号级操作，走 `chatgpt.com/backend-api` —— **不碰 DOM**。

## 为什么要有这个模块

原来「绑 2FA」是 DOM 自动化：点 `[data-testid='mfa-authenticator-toggle']`、
填 `#totp_otp`、点「Verificar」。这条路踩了一堆坑：
React 受控输入 state 不更新、提交按钮一直 disabled、`requestSubmit()` 提交到
不相干的表单把页面带到聊天页……全是「跟 React 较劲」的副产品。

从老大手动操作的 HAR 里确认，这些操作**本来就是纯接口调用**：

    GET  /backend-api/accounts/mfa_info
    POST /backend-api/accounts/mfa/enroll                     {"factor_type":"totp","source":"settings"}
         → {"secret":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "factor":{"id":"...","factor_type":"totp"}}
           ★ 示例里的 secret 是编的。原来这里抄的是 HAR 里抓到的**真密钥**，
             而本文件随包发出 —— 又是一处「文档里的真凭据」。
             改文档示例时请一并想一下：这东西会不会跟着包发给客户。
    POST /backend-api/accounts/mfa/user/activate_enrollment    ← 填 6 位码确认（接口名从 HAR 的前端 JS 里挖出来的）
    POST /backend-api/accounts/mfa/user/disable_in_house

而且**鉴权就是 registration 拿到的那个 accessToken**（HAR 里请求头是
`authorization: Bearer eyJhbGci...` + `oai-device-id`），不需要 auth 流程的会话。

## 为什么在浏览器里 fetch，而不是用 requests

- **cookie 自动带上**（`credentials: 'include'`）；
- **TLS 指纹是浏览器的** —— 这些接口在 Cloudflare 后面，裸 requests 容易被挑战；
- **自动走浏览器那条代理**，出口 IP 和注册时一致（换 IP 打接口容易被风控）。

一句话：用浏览器当 HTTP 客户端，但逻辑走接口，不做 DOM 操作。
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

BACKEND = "https://chatgpt.com/backend-api"
DEVICE_ID_JS = "return (window.__oai_device_id || (document.cookie.match(/oai-did=([^;]+)/)||[])[1] || '');"


def _noop(stage: str, detail: str = "") -> None:
    return None


def in_browser_fetch(
    driver,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    form: dict | None = None,
    access_token: str = "",
    timeout: float = 30.0,
) -> dict:
    """在浏览器里发一个 fetch，返回 ``{"status": int, "json": dict, "text": str, "error": str}``。

    ``path`` 可以是相对路径（`/backend-api/...`，用当前站点）或完整 URL。
    ``body`` 走 JSON；``form`` 走 `application/x-www-form-urlencoded`
    （`/api/auth/signin/openai` 那个入口就是表单编码的）。
    """
    script = r"""
    const path = arguments[0], method = arguments[1], body = arguments[2], at = arguments[3], form = arguments[4];
    const headers = {'oai-language': 'en-US'};
    let payload;
    if (form) {
      headers['content-type'] = 'application/x-www-form-urlencoded';
      payload = new URLSearchParams(form).toString();
    } else if (body) {
      headers['content-type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    if (at) headers['authorization'] = 'Bearer ' + at;
    fetch(path, {
      method: method,
      headers: headers,
      body: payload,
      credentials: 'include',
      mode: 'cors',
    }).then(async (r) => {
      let t = '';
      try { t = await r.text(); } catch (e) { t = ''; }
      __cloak_done({status: r.status, text: t, url: r.url});
    }).catch((e) => {
      __cloak_done({status: 0, text: '', error: String(e)});
    });
    """
    try:
        raw = driver.execute_async_script(
            script, path, method.upper(), body or None, access_token or "", form or None
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "json": {}, "text": "", "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(raw, dict):
        return {"status": 0, "json": {}, "text": "", "error": f"返回类型异常: {type(raw).__name__}"}
    if raw.get("__cloak_timeout"):
        return {"status": 0, "json": {}, "text": "", "error": "fetch 超时"}
    text = str(raw.get("text") or "")
    out = {
        "status": int(raw.get("status") or 0), "text": text, "json": {},
        "error": str(raw.get("error") or ""), "url": str(raw.get("url") or ""),
    }
    if text:
        try:
            out["json"] = json.loads(text)
        except Exception:  # noqa: BLE001
            out["json"] = {}
    return out


_CSRF_JS = (
    "const m = document.cookie.match(/(?:^|;\\s*)csrf-token=([^;]+)/);"
    "return m ? decodeURIComponent(m[1]) : '';"
)


def csrf_token(driver) -> str:
    """读 chatgpt.com 的 `csrf-token` cookie —— `/api/auth/signin/openai` 那个入口要它。"""
    try:
        return str(driver.execute_script(_CSRF_JS) or "")
    except Exception:  # noqa: BLE001
        return ""


def device_id(driver) -> str:
    try:
        return str(driver.execute_script(DEVICE_ID_JS) or "")
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ MFA / 2FA

def mfa_info(driver, access_token: str) -> dict:
    r = in_browser_fetch(driver, f"{BACKEND}/accounts/mfa_info", access_token=access_token)
    return r.get("json") or {}


def enroll_totp(driver, access_token: str) -> dict:
    """登记 TOTP，返回 ``{"secret": "...", "factor": {...}}``（服务端下发密钥）。

    ★ 失败时返回**整个响应**（含 `status` / `text`），**不再返回 `{}`**。
      踩过：原来失败就 `return {}`，调用方只能记下一句
      `mfa/enroll 没返回密钥：{}` —— **看不出到底是 401 还是别的**，
      排查 240 秒「近期认证」窗口那件事时白绕了一大圈。
    """
    r = in_browser_fetch(
        driver, f"{BACKEND}/accounts/mfa/enroll",
        method="POST", body={"factor_type": "totp", "source": "settings"},
        access_token=access_token,
    )
    if r.get("status") != 200:
        logger.warning("[2FA] enroll 失败 status=%s body=%s", r.get("status"), r.get("text", "")[:200])
        return r                      # ← 原样带出去，别丢 status/text
    return r.get("json") or {}


def activate_enrollment(
    driver,
    access_token: str,
    code: str,
    *,
    factor_type: str = "totp",
    session_id: str = "",
    source: str = "settings",
) -> dict:
    """用 6 位 TOTP 码确认登记 —— 这一步不做，2FA 等于没开。

    ★★ 2026-10-05 真机实测：**服务端已强制要求 `factor_type` + `session_id`。**
      只送 `{"code": ...}` 会返回 **422**：

        {"detail":[{"type":"missing","loc":["body","session_id"],"msg":"Field required"},
                   {"type":"missing","loc":["body","factor_type"],"msg":"Field required"}]}

    **`session_id` 的来源**（HAR 前端原码，铁证）：

        _.mutateAsync({factor:`totp`, source:l}).then(e => {
            _o(Ebt, {sessionId: e.session_id, secret: e.secret, ...})   // ← 就是 enroll 的响应
        })
        ...
        await f.mutateAsync({factor:`totp`, code:p.trim(), sessionId:a, source:l})

    ⚠️ 我们以前**只从 enroll 响应里取了 `secret`，把 `session_id` 整个丢掉** ——
      所以 10-04 那次能成（当时服务端不强制），现在必 422。

    ★★ 返回值是**整个响应**（含 `status`），不是 body。
      踩过：原来写 `return r.get("json") or {}`，而成功时服务端可能回**空体**，
      于是 `{}` 被调用方当成失败 —— 明明绑上了却报"确认失败"。
      **判成功要看 HTTP 状态，不要看 body 是否非空。**
    """
    body: dict = {"code": str(code), "factor_type": factor_type}
    if session_id:
        body["session_id"] = session_id
    if source:
        body["source"] = source
    r = in_browser_fetch(
        driver, f"{BACKEND}/accounts/mfa/user/activate_enrollment",
        method="POST", body=body, access_token=access_token,
    )
    if r.get("status") != 200:
        logger.warning("[2FA] activate 失败 status=%s body=%s", r.get("status"), r.get("text", "")[:200])
    return r


def disable_mfa(driver, access_token: str) -> bool:
    r = in_browser_fetch(
        driver, f"{BACKEND}/accounts/mfa/user/disable_in_house",
        method="POST", body={}, access_token=access_token,
    )
    return r.get("status") == 200


# ------------------------------------------------------- 密码（接口级 · reauth 链）
#
# 全部来自老大手动操作的 HAR，**每一步的下一步都在响应体的 `continue_url` 里**：
#
#   GET  /backend-api/accounts/add_password/eligibility           → {"eligible":true}
#   POST /api/auth/signin/openai?...&reauth=password&post_login_add_password=true
#        表单体 callbackUrl=&csrfToken=&json=true
#        → {"url":"https://auth.openai.com/api/accounts/authorize?..."}
#        ★ 这一步在 **chatgpt.com** 上发，且要读 csrf-token cookie
#   POST auth.openai.com/api/accounts/passwordless/send-otp
#        → {"continue_url":"https://auth.openai.com/email-verification",
#           "page":{"type":"email_otp_verification"}}
#   POST auth.openai.com/api/accounts/email-otp/validate {"code":"438391"}
#        → {"continue_url":"https://auth.openai.com/reset-password/new-password",
#           "page":{"type":"reset_password_new_password"}}
#   POST auth.openai.com/api/accounts/password/add {"password":"..."}
#        → {"continue_url":"https://chatgpt.com/api/auth/callback/openai?code=ac_..."}

SITE = "https://chatgpt.com"
AUTH = "https://auth.openai.com"


def password_eligibility(driver, access_token: str) -> bool:
    """这个号现在能不能加密码。HAR 里实测返回 ``{"eligible":true}``。"""
    r = in_browser_fetch(driver, f"{BACKEND}/accounts/add_password/eligibility",
                         access_token=access_token)
    return bool((r.get("json") or {}).get("eligible"))


def start_add_password(driver, email: str, access_token: str = "") -> str:
    """发起「加密码」的 reauth 会话，返回要去打开的 auth 地址。

    必须在 **chatgpt.com** 源上发；`csrfToken` 从 `csrf-token` cookie 读。
    返回空串表示没拿到地址。
    """
    did = device_id(driver)
    csrf = csrf_token(driver)
    if not csrf:
        logger.warning("[密码] 读不到 csrf-token cookie，入口会失败")
    qs = urlencode({
        "login_hint": email,
        "reauth": "password",
        "post_login_add_password": "true",
        "max_age": "0",
        "ext-oai-did": did,
    })
    r = in_browser_fetch(
        driver, f"{SITE}/api/auth/signin/openai?{qs}",
        method="POST",
        form={"callbackUrl": f"{SITE}/", "csrfToken": csrf, "json": "true"},
        access_token=access_token,
    )
    url = str((r.get("json") or {}).get("url") or "")
    if not url:
        logger.warning("[密码] signin/openai 没给 auth 地址：status=%s body=%s",
                       r.get("status"), (r.get("text") or "")[:200])
    return url


def _goto(driver, url: str, wait: float = 3.0) -> None:
    try:
        driver.get(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[密码] 导航失败 %s：%s", url[:80], exc)
    time.sleep(wait)


def set_password_protocol(
    driver,
    *,
    email: str,
    access_token: str,
    code_provider,
    password: str,
    emit=_noop,
    otp_timeout: int = 180,
) -> tuple[bool, str]:
    """接口级设密码（reauth 链）。返回 ``(ok, note)``。

    ``code_provider`` 是一个可调用对象：``code_provider(after_ts) -> str``，
    返回比 ``after_ts`` 更新的邮箱验证码（就是注册时那个取码逻辑）。

    ⚠️ **这条链要一封新的邮箱验证码**（`passwordless/send-otp` 会重新发一封）。
    一单号池通常只给一封码，所以它**不是**主线 —— 主线是注册流程里的设密码页
    （那条用注册会话，不需要第二封）。这里是「注册时没设上」时的兜底，
    以及将来换了能多次取码的邮箱之后的正式路径。

    ★ 每一步都用响应里的 `continue_url` 推进，不猜 URL。
    """
    if not password:
        return False, "密码为空"

    # ---- 0. 必须在 chatgpt.com 源上 ----
    try:
        url = str(driver.current_url or "")
    except Exception:  # noqa: BLE001
        url = ""
    if "chatgpt.com" not in url:
        _goto(driver, f"{SITE}/")

    # ---- 1. 资格检查 ----
    if access_token and not password_eligibility(driver, access_token):
        return False, "该账号当前不符合「加密码」条件（add_password/eligibility = false）"

    # ---- 2. 发起 reauth 会话 ----
    emit("harden", "正在发起加密码流程（接口）")
    auth_url = start_add_password(driver, email, access_token)
    if not auth_url:
        return False, "signin/openai 没返回 auth 地址（多半是 csrf-token 读不到）"
    _goto(driver, auth_url, wait=4)

    # ---- 3. 让服务端发验证码邮件 ----
    after_ts = time.time()
    r = in_browser_fetch(driver, f"{AUTH}/api/accounts/passwordless/send-otp", method="POST")
    page_type = str(((r.get("json") or {}).get("page") or {}).get("type") or "")
    cont = str((r.get("json") or {}).get("continue_url") or "")
    logger.info("[密码] send-otp → status=%s page=%s continue=%s", r.get("status"), page_type, cont[:70])
    if r.get("status") != 200:
        return False, f"send-otp 失败：{r.get('status')} {str(r.get('text') or '')[:120]}"
    if cont:
        _goto(driver, cont, wait=2)

    # ---- 4. 取码 + 校验 ----
    emit("harden", "加密码需要一封新的邮箱验证码，正在取码")
    try:
        code = str(code_provider(after_ts) or "").strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"加密码取码失败：{type(exc).__name__}: {str(exc)[:120]}"
    if not code:
        return False, "加密码需要新的邮箱验证码，但没取到"

    r = in_browser_fetch(driver, f"{AUTH}/api/accounts/email-otp/validate",
                         method="POST", body={"code": code})
    page_type = str(((r.get("json") or {}).get("page") or {}).get("type") or "")
    cont = str((r.get("json") or {}).get("continue_url") or "")
    logger.info("[密码] email-otp/validate → status=%s page=%s", r.get("status"), page_type)
    if r.get("status") != 200:
        return False, f"验证码校验失败：{r.get('status')} {str(r.get('text') or '')[:120]}"
    if cont:
        _goto(driver, cont, wait=2)

    # ---- 5. 提交新密码 ----
    emit("harden", "正在提交新密码（接口）")
    r = in_browser_fetch(driver, f"{AUTH}/api/accounts/password/add",
                         method="POST", body={"password": password})
    cont = str((r.get("json") or {}).get("continue_url") or "")
    logger.info("[密码] password/add → status=%s continue=%s", r.get("status"), cont[:80])
    if r.get("status") != 200:
        return False, f"提交密码失败：{r.get('status')} {str(r.get('text') or '')[:120]}"
    # 跟着 continue_url 回到 chatgpt.com 收尾（建立登录态）
    if cont:
        _goto(driver, cont, wait=4)
    emit("harden", "密码已设置（接口级）")
    return True, "已设置登录密码（接口级 reauth 链）"


def set_password_in_auth_session(
    driver,
    *,
    password: str,
    emit=_noop,
    follow_continue: bool = True,
) -> tuple[bool, str]:
    """在**当前 auth 会话里**直接打接口设密码 —— **不需要第二封邮箱验证码**。

    ## 为什么可以不打那个页面

    HAR（2026-10-05）把 `create-account/password` 与 `reset-password/new-password`
    两个路由的 **client action 源码**完整带出来了，它们是**同一段代码**：

    ```js
    async function ss({request:r}){
      const e = await r.formData(), s = g(e,{schema:P});
      const {post_login_add_password:d} = await ie(r),
            u = d ? "/password/add" : "/password/reset";
      return de(r, `https://auth.openai.com/api/accounts${u}`, {
        method:"POST",
        headers:{"Content-Type":"application/json", ...Z(i)},
        body: JSON.stringify({password: s.value["new-password"]})
      }, {intercept: ...})
    }
    ```

    也就是说：**那个「设置密码」页面只是个表单壳**，它干的事就是向
    `/api/accounts/password/add`（或 `/password/reset`）POST `{"password": ...}`。
    所以我们根本不需要渲染、点击、填那个 react-aria 页面 ——
    直接在**同一个 auth 会话**里打同一个接口即可。

    ## 和 `set_password_protocol` 的区别（这是关键）

    | | 入口 | 要不要第二封邮箱码 |
    |---|---|---|
    | `set_password_protocol` | 从 **chatgpt.com** 重新发起 `reauth=password` 登录链 | **要**（链中间有 `passwordless/send-otp` + `email-otp/validate`） |
    | **本函数** | 假设**会话已经就绪**，一跳直达 `password/add` | **不要** |

    号池一单通常只给**一封**邮箱验证码，所以 reauth 链基本用不上；
    本函数是目前**唯一能做到「接口级 + 不要第二封码」**的路子。

    ## 判成功

    一律按 **HTTP 状态 200** 判，**不看返回体**。
    ⚠️ 这个坑在 `activate_enrollment` 上踩过：服务端成功时可能回**空体**，
    用「返回体非空」判会把成功误判成失败。

    ## 前置条件（不满足就直接放弃，让调用方走 DOM）

    必须在 **`auth.openai.com` 源**上：目标 URL 是绝对地址
    （`https://auth.openai.com/api/accounts/...`），从 chatgpt.com 发属于跨域，
    会被 CORS 拦掉。所以本函数**不自己导航** —— 不满足条件时返回 False，
    由调用方决定退路，避免在注册流程中途乱跳页面。
    """
    if not password:
        return False, "密码为空"

    try:
        cur = str(driver.current_url or "")
    except Exception:  # noqa: BLE001
        cur = ""
    if "auth.openai.com" not in cur:
        return False, f"当前不在 auth 源上（{cur[:60]}），跳过会话内接口级设密码"

    # 前端按 `post_login_add_password` 二选一：
    #   add   → 账号本来没密码（我们的注册号就是这种）
    #   reset → 改已有密码
    # 我们不去猜那个 flag（它藏在 auth URL 的 query 里），两个都试，先 add。
    # 只有「选错了分支」类错误才需要试第二个，其它错误直接放弃。
    last = ""
    for path, why in (
        ("/api/accounts/password/add", "add"),
        ("/api/accounts/password/reset", "reset"),
    ):
        r = in_browser_fetch(driver, f"{AUTH}{path}", method="POST",
                             body={"password": password})
        st = int(r.get("status") or 0)
        cont = str((r.get("json") or {}).get("continue_url") or "")
        logger.info("[密码] %s → status=%s continue=%s", path, st, cont[:80])
        if st == 200:
            if cont and follow_continue:
                _goto(driver, cont, wait=4)
            emit("harden", "密码已设置（会话内接口级）")
            return True, f"已设置登录密码（会话内接口级 {why}）"
        last = f"{path} → {st} {str(r.get('text') or '')[:120]}"
        if st not in (400, 404, 405, 415, 422):
            break
    return False, f"会话内接口级失败：{last}"


# --------------------------------------------------------- 近期认证（recent_auth）
#
# ★★ 2026-10-05 从 HAR 的前端 JS 里挖出来的**原始判定逻辑**：
#
#     function NVe(e){ return e.pwd_auth_time }        // 取自 accessToken 的 claims
#     function PVe(e){
#       let t = NVe(e);
#       if(!t) return !0;                              // 没有该 claim → 需要重认证
#       return Date.now()/1e3 - t/1e3 > VVe            // 太旧 → 也需要
#     }
#
# 阈值就在 MFA 那个模块里，紧挨着 factor 类型定义：
#
#     VVe = 240,
#     Ej = function(e){ return e.PushAuth=`push_auth`, e.Totp=`totp`, e.Sms=`sms`, e }({}),
#     Dj = function(e){ return e.Enable=`enable`, e.Disable=`disable`, e }({})
#
# 超窗时 `POST /backend-api/accounts/mfa/enroll` 返回
# 401 `recent_auth_required` —— "User must re-authenticate to enroll/disable a factor"。
#
# **实测对照**：
#   · 2026-10-04 那次成功：全程 2 分 35 秒 → 落在窗口内 ✓
#   · 2026-10-05 那次失败：认证到 2FA 相隔 **562 秒** → 超窗 ✗
#     （原因是加了手机激活，把流程拖长了；不是上游加了门槛）
#
# ⚠️ 前端对这个错误的处理**只是弹一句「Sign in again」**，没有自动恢复的接口。
#    要过这道门槛只有两条路：**在窗口内做**，或**重新认证**（密码登录最快，
#    因为不需要第二封邮箱验证码）。
RECENT_AUTH_WINDOW = 240.0


def pwd_auth_time(access_token: str) -> float | None:
    """从 accessToken 里解出 `pwd_auth_time`（秒）。解不出就返回 None。

    纯本地解码，不发请求。**这是判断「还能不能绑 2FA」的唯一依据。**
    """
    tok = str(access_token or "").strip()
    if tok.count(".") < 2:
        return None
    try:
        import base64
        import json as _json

        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(p))
    except Exception:  # noqa: BLE001
        return None
    raw = claims.get("pwd_auth_time")
    if not raw:
        return None
    try:
        return float(raw) / 1000.0
    except Exception:  # noqa: BLE001
        return None


def auth_freshness(access_token: str) -> tuple[float | None, bool]:
    """返回 ``(距今秒数, 是否在 240 秒窗口内)``。

    解不出 `pwd_auth_time` 时返回 ``(None, False)`` ——
    按前端的逻辑「没有该 claim 也要重认证」，所以算**不新鲜**。
    """
    t = pwd_auth_time(access_token)
    if t is None:
        return None, False
    delta = time.time() - t
    return delta, delta <= RECENT_AUTH_WINDOW


def fresh_session_token(driver) -> str:
    """重读 `/api/auth/session` 拿当前会话的 accessToken（拿不到返回空串）。

    用途：注册/激活流程里浏览器可能已经**换过一次会话**，
    重读一次有机会拿到 `pwd_auth_time` 更新的 AT —— 那就可能重新落回 240 秒窗口内。
    纯附加值：拿不到就返回空，调用方保持原 token。
    """
    try:
        r = in_browser_fetch(driver, f"{SITE}/api/auth/session")
        return str(((r.get("json") or {}).get("accessToken")) or "")
    except Exception:  # noqa: BLE001
        return ""


def setup_totp(driver, access_token: str, *, emit=_noop, clock_skew_retry: int = 2) -> tuple[bool, str, str]:
    """一条龙：登记 → 用自己算的码确认 → 校验已生效。

    返回 ``(ok, note, secret)``。**只有 activate 成功、且 mfa_info 显示
    mfa_enabled=true，才算真的绑上** —— 半成品密钥（只有 enroll）生成出来的码
    OpenAI 不认，交付出去买家会被锁在门外。
    """
    from .security_setup import totp_now

    if not access_token:
        return False, "没有 accessToken，打不了账号接口", ""

    # ★ 必须在 chatgpt.com 源上发请求：`/backend-api/*` 是**同源**接口，
    #   从别的域发会被 CORS 拦。注册刚结束时一般就在 chatgpt.com，
    #   但如果中间跑过激活（跑到了 auth.openai.com），就得先回来。
    try:
        url = str(driver.current_url or "")
    except Exception:  # noqa: BLE001
        url = ""
    if "chatgpt.com" not in url:
        logger.info("[2FA] 当前不在 chatgpt.com（%s），先导航回去", url or "-")
        try:
            driver.get("https://chatgpt.com/")
            time.sleep(3)
        except Exception as exc:  # noqa: BLE001
            return False, f"导航回 chatgpt.com 失败：{exc}", ""

    # ---- ★ 240 秒「近期认证」窗口检查（这一段决定绑不绑得上）----
    #   先看传进来的 AT 有多旧；再**重读一次 session** ——
    #   注册/激活流程里浏览器可能已经换过会话，重读有机会拿到
    #   `pwd_auth_time` 更新的 AT，也就是重新落回窗口内（= 最便宜的修法）。
    cur_secs, cur_ok = auth_freshness(access_token)
    reread = fresh_session_token(driver)
    if reread and reread != access_token:
        new_secs, new_ok = auth_freshness(reread)
        # 只在「确实更新」时才换 —— 别拿一个更旧或解不出的 token 把好的换掉
        if new_secs is not None and (cur_secs is None or new_secs < cur_secs - 1):
            logger.info("[2FA] 重读 session 拿到更新的 AT：距今 %s → %s 秒",
                        f"{cur_secs:.0f}" if cur_secs is not None else "?",
                        f"{new_secs:.0f}")
            access_token, cur_secs, cur_ok = reread, new_secs, new_ok
    if cur_secs is None:
        logger.warning("[2FA] 这个 AT 里解不出 pwd_auth_time —— 按前端逻辑算「不新鲜」，"
                       "mfa/enroll 大概率 401 recent_auth_required")
    else:
        logger.info("[2FA] pwd_auth_time 距今 %.0f 秒（窗口 %.0f 秒）→ %s",
                    cur_secs, RECENT_AUTH_WINDOW, "窗口内 ✓" if cur_ok else "已超窗 ✗")
    if not cur_ok:
        _age = f"{cur_secs:.0f}" if cur_secs is not None else "未知"
        emit("harden", f"注意：AT 认证时间距今 {_age} 秒，超过 "
                       f"{RECENT_AUTH_WINDOW:.0f} 秒窗口，绑 2FA 可能被要求重新认证")

    info = mfa_info(driver, access_token)
    if info.get("mfa_enabled"):
        existing = (info.get("factors") or {}).get("totp") or []
        if existing:
            return True, "该账号已经开过 TOTP 2FA", ""
    logger.info("[2FA] 起始状态 mfa_enabled=%s", info.get("mfa_enabled"))

    emit("harden", "正在登记 TOTP 2FA（接口）")
    enroll = enroll_totp(driver, access_token)
    secret = str(enroll.get("secret") or "").strip()
    # ★ `session_id` 必须留着 —— `activate_enrollment` 要它（见那个函数的长注释）。
    #   以前只取 secret 把它丢了，导致 422。
    session_id = str(enroll.get("session_id") or "").strip()
    if secret:
        logger.info("[2FA] enroll 成功，响应字段 = %s", sorted(enroll.keys()))
        if session_id:
            logger.info("[2FA] 拿到 session_id（activate 要用）")
        else:
            logger.warning("[2FA] ★ enroll 响应里**没有** session_id —— activate 大概率 422。"
                           "上游改过字段名的话，看这行日志里的字段列表。")
    if not secret:
        body = str(enroll.get("text") or "")
        if "recent_auth" in body or int(enroll.get("status") or 0) == 401:
            _age = f"{cur_secs:.0f}" if cur_secs is not None else "未知"
            return False, (
                f"mfa/enroll 被要求「重新认证」（401 recent_auth_required）："
                f"AT 的 pwd_auth_time 距今 {_age} 秒，超过 {RECENT_AUTH_WINDOW:.0f} 秒窗口。"
                "解法：把绑 2FA 提到更早（认证后 4 分钟内），"
                "或先用**密码**重新登录一次刷新认证时间再绑"
            ), ""
        return False, f"mfa/enroll 没返回密钥：{str(enroll)[:160]}", ""

    emit("harden", "已拿到密钥，正在用一次一密确认")
    last = ""
    for attempt in range(1, max(1, clock_skew_retry) + 1):
        code = totp_now(secret)
        r = activate_enrollment(driver, access_token, code,
                                factor_type="totp", session_id=session_id)
        # ★ 按 **HTTP 状态** 判成功（成功时服务端可能回空体，不能看 body 是否非空）
        if r.get("status") == 200:
            # 确认后必须回读一次：接口 200 不等于真的开了
            check = mfa_info(driver, access_token)
            if check.get("mfa_enabled") or ((check.get("factors") or {}).get("totp")):
                logger.info("[2FA] 已激活并回读确认（第 %s 次）", attempt)
                emit("harden", "2FA 已绑定并确认生效")
                return True, "已绑定 TOTP 2FA（接口级）", secret
            last = f"activate 返回 200 但 mfa_info 仍显示未开启：{str(check)[:120]}"
        else:
            last = f"activate_enrollment 返回 {r.get('status')}：{str(r.get('text') or '')[:120]}"
        # 时钟偏移或上一个码刚过期时，等下一个 30s 窗口再来
        if attempt < clock_skew_retry:
            wait = 30 - int(time.time()) % 30 + 1
            logger.warning("[2FA] 第 %s 次确认没成（%s），等 %ss 换一个码重试", attempt, last, wait)
            time.sleep(min(wait, 32))
    return False, f"2FA 确认失败：{last}", secret
