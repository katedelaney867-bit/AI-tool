# -*- coding: utf-8 -*-
"""单账号注册端到端编排。

流程（与 turb 原项目 cloack 驱动一致，去掉邮箱池/流量统计/任务队列）：

    分配邮箱 → 启动 CloakBrowser → 打开登录页 → 提交邮箱 → 设置密码
    → 邮箱 OTP → 填写资料 → 读取 accessToken → 查活 →（可选）桌面端激活

页面交互全部来自 core/browser_flow.py（提炼自原项目，未改动逻辑）；
这里只负责串流程、取码、错误归类与落库。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Callable

from . import browser_flow as flow
from .cloak_driver import build_cloak_driver
from .humanize import delay as human_delay
from .mailbox.base import Inbox, MailboxError
from .profile import random_birthday, random_display_name
from .settings import Config
from .stopflag import StopRequested, check_stop
from .store import AccountRecord

logger = logging.getLogger(__name__)

LOGIN_URL = "https://chatgpt.com/auth/login"
OTP_MAX_ATTEMPTS = 3

# ★「重新发送」到底值不值得点，实测分两套链路，结论完全相反（2026-10-04 统计全量日志）：
#
#   直连上游 直连时代：点 1 次 → 3 秒后拿到新码 → 注册成功        （22:58，1/1）
#   号池时代：      点 4 次 → 一封新码都没有，白烧 4×180s=12 分钟（20:45/20:48/18:48/18:51，0/4）
#
#   号池走的是 `GET /v1/pool/mail/orders/{id}/code`，只吐第一封的码，重发根本捞不到；
#   更要命的是**每点一次重发都会把手上那个正确的码作废**，等于自己把账号判死。
#   所以号池下只给「重新发送」一次机会，且等待上限收紧到 90s —— 正常邮件 60~80s 必到，
#   等满 180s 只是白等。老逻辑点两次 = 每次失败多烧 6 分钟。
OTP_RESEND_MAX = 1
OTP_RESEND_WAIT = 90

EventFn = Callable[[str, str], None]


def _noop(stage: str, detail: str = "") -> None:
    return None


# ------------------------------------------------------------------ 工具


def _jwt_claims(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _find_account_id(value: Any) -> str:
    """从 session/claims 里挖出 chatgpt_account_id。"""
    if isinstance(value, dict):
        for key in ("chatgpt_account_id", "account_id", "accountId", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for nested in value.values():
            found = _find_account_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_account_id(nested)
            if found:
                return found
    return ""


def extract_credentials(session_info: dict) -> dict:
    """从 /api/auth/session 响应里取出导出需要的字段。"""
    access_token = str(session_info.get("accessToken") or "").strip()
    claims = _jwt_claims(access_token)
    auth_block = claims.get("https://api.openai.com/auth")
    account_id = (
        _find_account_id(auth_block)
        or _find_account_id(session_info.get("account"))
        or _find_account_id(claims)
    )
    expires = session_info.get("expires")
    if not expires and isinstance(claims.get("exp"), (int, float)):
        expires = claims["exp"]
    return {
        "access_token": access_token,
        "account_id": account_id,
        "expires_at": str(expires or ""),
    }


# ------------------------------------------------------------------ OTP


def _submit_otp_with_retries(
    driver,
    inbox: Inbox,
    mailbox,
    config: Config,
    *,
    email: str,
    otp_after_ts: float,
    on_event: EventFn,
) -> float:
    """取码 → 填码 → 判断结果，最多 OTP_MAX_ATTEMPTS 轮。返回最新的取码基准时间。"""
    current_otp: str | None = None
    route_error_recovered = False
    # 「重新发送」的剩余次数与当前等待上限：只在整段 OTP 流程里共享，
    # 一旦点过重发就把等待上限从 180s 收到 OTP_RESEND_WAIT（见文件头常量处的实测结论）。
    resend_left = OTP_RESEND_MAX
    wait_timeout = config.otp_timeout

    for attempt in range(1, OTP_MAX_ATTEMPTS + 1):
        check_stop()

        if current_otp is None:
            on_event("wait_otp", f"等待邮箱验证码（{attempt}/{OTP_MAX_ATTEMPTS}）")
            try:
                current_otp = mailbox.wait_for_otp(
                    inbox, after_ts=otp_after_ts, timeout=wait_timeout
                )
            except MailboxError as exc:
                if resend_left <= 0 or attempt >= OTP_MAX_ATTEMPTS:
                    # ★ 重发已用尽（号池下 0/4），再等一轮 180s 纯属白烧 —— 直接判死这个账号。
                    logger.warning(
                        "[注册] 未收到验证码且不再重发（重发剩余 %s 次，轮次 %s/%s），放弃该账号",
                        resend_left, attempt, OTP_MAX_ATTEMPTS,
                    )
                    raise
                resend_left -= 1
                wait_timeout = OTP_RESEND_WAIT
                # 没收到就点一次「重新发送」再等。
                logger.warning("[注册] 未收到验证码，点击重发（剩余 %s 次，之后等 %ss）：%s",
                               resend_left, OTP_RESEND_WAIT, exc)
                on_event("resend_otp", "未收到验证码，已点击重新发送")
                otp_after_ts = time.time()
                # ★ 点不上重发**不该判死账号**（2026-10-04 实测踩到）：
                #   页面可能正在跳转、当时一个可点元素都没有（buttons: []），
                #   `_click_resend_email_otp` 直接抛 RuntimeError，
                #   把「邮件慢」升级成「整轮注册失败」。点不上就照常去等码。
                try:
                    flow._click_resend_email_otp(driver, timeout=25)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[注册] 点重发失败，跳过继续等码：%s", str(exc)[:120])
                human_delay("api")
                current_otp = None
                continue

        on_event("submit_otp", "已收到验证码，正在填写")
        # ★ 2026-10-17：OTP 这几步各打一条耗时日志。起因：两次"收到码之后静默 18 分钟"
        #   （最后等邮箱窗口过期），而这段路一行日志都没有 —— 无法判断卡在哪个调用。
        otp_step_t0 = time.time()
        flow._clear_otp_inputs(driver)
        logger.info("[注册][OTP] 清空旧码耗时 %.1fs", time.time() - otp_step_t0)
        otp_step_t0 = time.time()
        flow._type_otp(driver, current_otp)
        logger.info("[注册][OTP] 填入新码耗时 %.1fs", time.time() - otp_step_t0)
        human_delay("otp_input")

        # 新版验证码页可能在输入满 6 位后自动提交，先观察跳转，避免重复提交。
        auto_submitted = False
        auto_deadline = time.time() + 4.0
        while time.time() < auto_deadline:
            snapshot = flow._email_otp_page_state(driver)
            page_text = f"{snapshot.get('title') or ''} {snapshot.get('text') or ''}".lower()
            if any(marker in page_text for marker in ("route error", "invalid content type", "糟糕，出错了")):
                raise RuntimeError(f"邮箱 OTP 自动提交后进入错误页: {snapshot}")
            if not flow._is_email_verification_page(driver):
                auto_submitted = True
                logger.info("[注册] 输入完成后页面已自动进入下一步")
                break
            time.sleep(0.5)

        otp_step_t0 = time.time()
        if not auto_submitted:
            try:
                # 优先点「验证」按钮：这个页面有「验证」和「重新发送」两个 type=submit，
                # 通用选择器有可能点到「重新发送」，把正确的码作废。
                if not flow._click_email_otp_submit(driver):
                    flow._click_continue(driver)
            except Exception as exc:
                logger.info("[注册] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])
        click_elapsed = time.time() - otp_step_t0

        otp_step_t0 = time.time()
        outcome = "accepted" if auto_submitted else flow._wait_after_email_otp_submit(driver, timeout=12)
        logger.info("[注册][OTP] 点击提交耗时 %.1fs、提交后等待 %.1fs → %s",
                    click_elapsed, time.time() - otp_step_t0, outcome)

        if outcome == "accepted":
            return otp_after_ts

        if outcome == "stalled":
            # ★ 页面没跳转，多半是「提交按钮没点上」，而不是码有问题。
            #   这时候千万不能去点「重新发送」—— 那会把一个正确的码作废。
            #   实测代价：点了重发之后新码 180s 没来，整个账号报废
            #   （2026-10-03，georgejackson016771 就是这么死的）。
            #   先用同一个码多试几次提交，全都不动才考虑重发。
            #
            # ★★ 2026-10-04 补：**回读匹配 ≠ 组件状态同步**。
            #   回读读的是 DOM 的 value，而 React 提交时用的是自己的 state；
            #   受控输入抢跑时两者会分叉 —— DOM 里明明是对的码，提交却像没填一样
            #   （页面既不报错、也不发请求，18:44 那次就是这样卡死的）。
            #   所以先用原生 setter + input/change 事件把码**强制**再写一遍，
            #   逼组件把 state 对齐到 DOM，然后再点提交。
            if flow._set_otp_value_via_js(driver, current_otp):
                logger.info("[注册] 提交无反应，已用 JS 强制同步验证码到组件状态后重试")
                human_delay("otp_input")
            for retry in range(1, 4):
                on_event("submit_otp", f"验证码提交后页面没动，重试提交（{retry}/3）")
                try:
                    # 优先点「验证」按钮，别点到「重新发送」
                    if not flow._click_email_otp_submit(driver):
                        flow._click_continue(driver)
                except Exception as exc:  # noqa: BLE001
                    logger.info("[注册] 重试提交时没找到提交按钮：%s", str(exc)[:120])
                human_delay("api")
                if flow._wait_after_email_otp_submit(driver, timeout=15) == "accepted":
                    logger.info("[注册] 用同一个码第 %s 次重试提交后已进入下一步", retry)
                    return otp_after_ts
            logger.warning("[注册] 同一个码连续提交都不跳转，重发额度剩余 %s 次", resend_left)

        if outcome == "fatal":
            # 校验接口偶尔触发挑战页；保留同一浏览器会话点一次 Try again。
            if not route_error_recovered and attempt < OTP_MAX_ATTEMPTS:
                route_error_recovered = True
                logger.warning("[注册] OTP 校验触发挑战页，尝试点击 Try again")
                on_event("recover_otp", "验证码校验触发挑战页，正在恢复")
                try:
                    flow._click_any(driver, [
                        "button[data-dd-action-name='Try again']",
                        "button[data-dd-action-name='try-again']",
                    ], timeout=8)
                except Exception as exc:
                    raise RuntimeError(f"邮箱 OTP 校验进入错误页且无法点击 Try again: {exc}") from exc

                recover_deadline = time.time() + 15
                recovered = False
                while time.time() < recover_deadline:
                    if not flow._is_email_verification_page(driver):
                        recovered = True
                        break
                    snapshot = flow._email_otp_page_state(driver)
                    if snapshot.get("inputs"):
                        # 回到验证码页，沿用当前邮件里的码重试一次。
                        current_otp = None
                        break
                    time.sleep(0.6)
                if recovered:
                    return otp_after_ts
                if current_otp is None:
                    continue
            raise RuntimeError(f"邮箱 OTP 提交进入错误页: {flow._email_otp_page_state(driver)}")

        if attempt >= OTP_MAX_ATTEMPTS:
            raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")

        if resend_left <= 0:
            # ★ 号池下重发实测 0/4，而且每点一次都会把手上那个正确的码作废 ——
            #   额度用完就别再点了：直接判死换新号，比老逻辑「再点一次 + 再等 180s」
            #   省 3 分钟，还少一次 180s 的纯空等。
            raise RuntimeError(
                "验证码提交后页面始终不跳转，「重新发送」额度也已用尽，"
                "放弃该账号（请开新号重试）"
            )
        resend_left -= 1
        wait_timeout = OTP_RESEND_WAIT
        logger.warning("[注册] 走重新发送（剩余 %s 次，之后等待上限收到 %ss）",
                       resend_left, OTP_RESEND_WAIT)
        on_event("resend_otp", "验证码被拒，已点击重新发送")
        otp_after_ts = time.time()
        try:
            flow._click_resend_email_otp(driver, timeout=25)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[注册] 点重发失败，跳过继续等码：%s", str(exc)[:120])
        human_delay("api")
        current_otp = None

    return otp_after_ts


# ------------------------------------------------------------------ 主流程

# 登录页被 Cloudflare 挑战时，最多关掉浏览器重开几条连接（每条连接换一张出口 IP）。
# 检测改成轮询之后，一条脏 IP 约 4~14 秒就能判出来，所以多试几条很便宜。
# 实测（2026-10-03）：美国住宅池单条连接通过率约 27%（3/11 成功），8 条整体约 92%；
# 系统代理（本机 Clash 的机房 IP）实测 0/12，那种出口再多试也没用，会快速失败。
BROWSER_TRIES = 8


def _safe_url(driver) -> str:
    """取当前 URL 用于日志，浏览器已经关掉时不炸。"""
    try:
        return str(getattr(driver, "current_url", "") or "")
    except Exception:  # noqa: BLE001
        return "<已关闭>"


def _email_step_fatal(exc: BaseException) -> bool:
    """邮箱这一步的失败，哪些是「换代理也没用」的。

    邮箱已注册、页面进登录密码页、ChatGPT 直接报 auth error —— 这些是邮箱/账号问题，
    换出口 IP 重试只会白白多烧几条连接和邮箱额度。
    """
    text = str(exc)
    return any(key in text for key in ("登录密码页", "auth_error", "auth/error", "已注册"))


def _wait_for_human(
    driver,
    *,
    emit: EventFn,
    config: Config,
    reason: str,
    probe: Callable[[object], str],
    want: tuple[str, ...] = ("ready",),
    poll: float = 3.0,
    beat: float = 15.0,
    timeout: float | None = None,
) -> str:
    """ ★ 半自动模式的核心：**不退出浏览器**，把窗口留在屏幕上等人手动过验证。

    为什么要这样设计（而不是继续「关掉浏览器换一条连接」）：

    - 小白的痛点是**搞不到住宅代理**。而只要不退出浏览器，出口就是**他自己家的宽带 IP**
      —— 本来就是干净的住宅，比任何买来的池子都稳；
    - 人机验证是唯一**没法自动化**的环节，那就交给**人** —— 人比任何代理池都强；
    - 关掉浏览器反而把这两条优势一起丢掉：IP 换了、人刚点完的验证也白点了。

    ``probe`` 由调用方给：登录页用 ``flow._login_page_ready``，
    提交邮箱之后的页面用「页面是不是空/卡住」——**不同阶段的「就绪」定义不一样**，
    写死一个探测函数会让等待器在第二个场景里永远等不到。

    返回最后一次 probe 的结果；命中 ``want`` 就是「人处理好了」。
    超时返回最后结果，由调用方决定是判死还是继续换连接。
    """
    timeout = (float(timeout) if timeout is not None
               else float(max(30, int(config.semi_auto_wait or 600))))
    deadline = time.time() + timeout
    emit("human", f"⏸ 需要你手动处理：{reason}")
    emit("human", f"   请在浏览器窗口里完成人机验证，工具会一直等你（最多 {timeout}s）")
    logger.warning("[注册] 半自动：等人手动处理（最多 %ss）—— %s", timeout, reason)

    last = ""
    last_beat = 0.0
    while time.time() < deadline:
        check_stop()
        time.sleep(max(1.0, poll))
        try:
            last = str(probe(driver) or "")
        except Exception as exc:  # noqa: BLE001
            # 页面正在重渲染时探测会抛 —— 那是正常的，继续等
            logger.debug("[注册] 半自动：探测失败（继续等）%s: %s", type(exc).__name__, exc)
            continue
        if last in want:
            logger.info("[注册] 半自动：检测到页面已就绪（%s），接着跑自动流程", last)
            emit("human", "✔ 验证已通过，继续自动注册")
            return last
        now = time.time()
        if now - last_beat >= beat:
            last_beat = now
            emit("human", f"   仍在等你手动完成验证（还剩 {int(deadline - now)}s）")

    logger.warning("[注册] 半自动：等了 %ss 仍未就绪（最后探测=%s）", timeout, last or "?")
    emit("human", f"✘ 等了 {timeout}s 还没通过，这一轮作废")
    return last


def register_one(
    *,
    config: Config,
    index: int,
    mailbox,
    on_event: EventFn | None = None,
    on_email: Callable[[str], None] | None = None,
    name: str | None = None,
    birthday: str | None = None,
    keep_browser: bool = False,
    driver_out: list | None = None,
) -> AccountRecord:
    """注册一个账号。任何异常都会被归到 record 上，不向外抛（只有 StopRequested 例外）。

    ``on_email`` 在**刚分到邮箱**那一刻就回调一次。GUI 的进度卡片靠它知道
    「这个任务已经在用哪个邮箱了」—— 以前要等整轮结束才拿得到邮箱，
    于是卡片整轮都显示「等待分配邮箱」。

    ``keep_browser`` 只给**诊断探针**用：注册完不关浏览器，好接着去看登录态后面的
    页面（安全设置页之类）。★ 千万别在正常跑批时打开 —— 会泄漏浏览器和 Playwright 的
    事件循环，把后续的浏览器重试全部毒化成
    「Playwright Sync API inside the asyncio loop」（2026-10-04 亲自踩过：
    探针用猴补丁拦 quit 时，连**重试分支的 quit** 也一起拦掉了，后 7 次全挂）。
    """
    emit = on_event or _noop
    record = AccountRecord(status="pending", proxy=config.proxy, mailbox=mailbox.name)
    driver = None
    inbox: Inbox | None = None
    password: str | None = None

    # 让页面流程读到本次运行的超时与固定密码。
    flow.SELENIUM_TIMEOUT = int(config.page_load_timeout or 90)
    # ★ 勾了「注册后加固」就**预先定好**密码：注册流程在第 5 步遇到
    #   「设置密码」页时会用它（`_fill_password_page_if_present`）。
    #   这是设密码的**主路径** —— 它跟邮箱验证码同一条链，不用再等第二封邮件。
    #   设置页那条路（点「添加密码」）实测要**第二封邮件码**，而一单号池只给一封。
    if config.harden_after_register:
        from .security_setup import generate_password as _gen_pw
        # ★ 必须写线程局部的那个：并发下模块全局会被别的线程覆盖，
        #   导致「记下来的密码」≠「真正设进账号的密码」，买家登不上还不报错。
        flow._set_register_password(_gen_pw())
    else:
        flow._set_register_password("")

    try:
        # ---- 0. 内核守卫：必须在**分配邮箱之前** ----
        # ★ 包为了体积（84MB vs 带内核 ~600MB）不带浏览器内核，第一次用要先联网下
        #   （约 150MB）。不在这里拦的话会一路走到 build_cloak_driver 才炸，
        #   而且被重试循环当成「连接不行」重试 8 次。
        #
        #   ★★ 更贵的是：那时**邮箱已经分配出去了**，而号池对已 delivered 的邮箱单
        #      不退（只有卡在 provisioning 的「交付超时」单才退，见 AGENTS.md）。
        #      2026-10-11 内测第一台 Win10 就这么白烧了一个邮箱（¥0.04），
        #      报错还是句看不懂的 GeoIP lookup failed。
        #
        #   界面上「下载内核」那个按钮**有进度显示**，而 CloakBrowser 自己偷偷下
        #   没有任何提示（用户会以为卡死）—— 所以这里明确让用户走那个按钮。
        from .preflight import kernel_present as _kernel_present

        _kernel_ok, _kernel_detail = _kernel_present()
        if not _kernel_ok:
            raise RuntimeError(
                "浏览器内核还没准备好，先点界面上「开始前检查」里的「下载内核」"
                f"（约 150MB，只下一次），下完再开始注册。当前状态：{_kernel_detail}"
            )

        # ---- 1. 分配邮箱 ----
        emit("allocate", "正在分配邮箱")
        check_stop()
        inbox = mailbox.allocate(
            target_id=config.mailbox_target_id,
            idempotency_key=f"grl:{index}:{int(time.time())}",
        )
        record.email = inbox.email
        record.inbox_token = inbox.service_token
        if on_email and record.email:
            try:
                on_email(record.email)
            except Exception:  # noqa: BLE001
                logger.debug("[注册] on_email 回调失败，忽略", exc_info=True)
        emit("allocate", f"已分配邮箱 {inbox.email}")

        # ---- 2+3. 启动浏览器并打开登录页 ----
        # ★ 出口被 Cloudflare 挑战时（轮换型住宅代理经常抽到脏 IP），
        #   关掉浏览器重开一条连接 = 换一张 IP，再试。以前这里一次就判死。
        otp_after_ts = time.time()
        driver = None
        for browser_try in range(1, BROWSER_TRIES + 1):
            emit("browser", "正在启动浏览器")
            check_stop()

            # ★ 起浏览器 + 打开登录页这段必须包在重试里。
            #   代理抖一下就会 ERR_TUNNEL_CONNECTION_FAILED / ERR_PROXY_CONNECTION_FAILED，
            #   以前这段在 try 外面，一次网络抖动就判死一整个账号（实测踩过）。
            #   这类错误和「被 Cloudflare 拦」一样，换一条连接多半就好。
            try:
                driver, _opened = build_cloak_driver(
                    proxy=config.proxy or None,
                    headless=config.headless,
                    humanize=config.humanize,
                    geoip=config.geoip,
                    license_key=config.cloak_license_key,
                    page_load_timeout=config.page_load_timeout,
                )
                # 诊断探针要留住这个会话（配合 keep_browser=True）。
                if driver_out is not None:
                    driver_out.append(driver)

                emit("navigate", "正在打开 ChatGPT 登录页")
                driver.get(LOGIN_URL)
                human_delay("navigate")
                flow._maybe_accept(driver)
                check_stop()

                # ★ 必须轮询等待，不能看一眼就下结论：挑战页是异步渲染的，
                #   刚 driver.get 完标题往往还是空的，单次检查会漏判，
                #   放行之后才在下游报「找不到邮箱输入框」，白白浪费一次重试机会。
                verdict = flow._login_page_ready(driver)
            except Exception as exc:  # noqa: BLE001
                verdict = ""
                if browser_try < BROWSER_TRIES:
                    logger.warning("[注册] 这条连接连不上（%s: %s），换连接重试 %s/%s",
                                   type(exc).__name__, str(exc)[:100],
                                   browser_try, BROWSER_TRIES)
                    emit("navigate", f"这条连接连不上，换一条重试（{browser_try}/{BROWSER_TRIES}）")
                    try:
                        if driver is not None:
                            driver.quit()
                    except Exception:  # noqa: BLE001
                        pass
                    driver = None
                    time.sleep(3)
                    continue
                raise

            # ★ 半自动模式：被挑战时**不退出浏览器**，把窗口留给用户手动过验证。
            #   退出就等于「换掉出口 IP」+「丢掉用户刚点完的验证」，两次白费。
            #
            # ★★ 但必须**只在真的是人机验证页**时才等人。
            #   实测踩到：失败是 `ERR_CONNECTION_CLOSED`（出口直接连不上 chatgpt.com），
            #   那种页面**人也没法点** —— 如果照「页面没就绪」就等，会白等满
            #   semi_auto_wait（默认 600s）× BROWSER_TRIES（8）= 80 分钟，
            #   而且什么都没解决。等待只该用在「人真的能解决」的场景。
            # ★★ 2026-10-16 老板实测：「碰到人机验证时我点击刷新就到了正常的邮箱验证码页面」。
            #   ⇒ **先走最便宜的一条路：原地刷新**（半自动开不开都走，刷新不丢会话、
            #     不用重开浏览器）。以前只有「半自动等人」和「关浏览器换连接」两条，
            #     而 semi_auto 关着时只剩换连接 —— 把一个刷一下就能救的会话整个丢掉。
            if verdict != "ready" and (verdict == "challenge"
                                       or flow._looks_like_cloudflare_challenge(driver)
                                       or flow._page_is_empty_and_stuck(driver)):
                for reload_round in (1, 2):
                    flow.reload_and_settle(
                        driver,
                        reason=f"登录页未就绪（{verdict}），自动刷新 {reload_round}/2",
                    )
                    verdict = flow._login_page_ready(driver)
                    emit("navigate", f"刷新后登录页状态：{verdict}")
                    if verdict == "ready":
                        break

            if (config.semi_auto
                    and verdict != "ready"
                    and flow._looks_like_cloudflare_challenge(driver)):
                verdict = _wait_for_human(
                    driver,
                    emit=emit,
                    config=config,
                    reason="登录页被人机验证挡住了",
                    probe=flow._login_page_ready,
                )

            if verdict == "ready":
                # 登录页干净，直接进提交邮箱。提交这一段也可能踩到「页面没渲染出来」，
                # 那说明这条连接的出口也被拦了 —— 一起放进重试里。
                emit("email", "正在提交邮箱")
                try:
                    flow._submit_email_and_wait_next(driver, inbox.email, attempts=3)
                    check_stop()
                    break
                except Exception as exc:  # noqa: BLE001
                    # ★ 不能只靠「再探一次 DOM」来判定：报错那一刻页面可能正在重渲染，
                    #   execute_script 一抛异常探测函数就返回 False，白白放过一次重试机会
                    #   （2026-10-03 实测踩过：明明 inputs=[]，却没触发重试）。
                    #   报错文本本身「找不到邮箱输入框」就足以说明这条出口不可用。
                    retryable = (not _email_step_fatal(exc)
                                 and ("找不到邮箱输入框" in str(exc)
                                      or "未进入密码页" in str(exc)
                                      or "未进入下一步" in str(exc)
                                      or flow._page_is_empty_and_stuck(driver)))
                    # ★★ 先自动刷新一次再重试提交（2026-10-16 老板实测那条：刷新就正常了）。
                    #   刷新失败才轮到「半自动等人」，最后才是「换连接」。
                    if retryable and (flow._looks_like_cloudflare_challenge(driver)
                                      or flow._page_is_empty_and_stuck(driver)):
                        flow.reload_and_settle(driver, reason="提交邮箱后页面没有前进")
                        try:
                            flow._submit_email_and_wait_next(driver, inbox.email, attempts=3)
                            check_stop()
                            break
                        except Exception:  # noqa: BLE001
                            logger.warning("[注册] 自动刷新后重试提交邮箱仍失败")
                    # 半自动：提交邮箱后页面又卡住，多半是挑战异步来的 ——
                    # 先给用户一次手动机会，再去换连接（换连接会关掉浏览器，很贵）。
                    if config.semi_auto and retryable and flow._looks_like_cloudflare_challenge(driver):
                        if _wait_for_human(
                            driver,
                            emit=emit,
                            config=config,
                            reason="提交邮箱后页面没有前进（可能又弹了人机验证）",
                            # 这一步的「就绪」不是「登录页就绪」——页面已经走到验证码页了，
                            # 所以用「页面不是空的/没卡住」当判据。写死登录页探测会永远等不到。
                            probe=lambda d: "ok" if not flow._page_is_empty_and_stuck(d) else "stuck",
                            want=("ok",),
                        ) == "ok":
                            try:
                                flow._submit_email_and_wait_next(driver, inbox.email, attempts=3)
                                check_stop()
                                break
                            except Exception:  # noqa: BLE001
                                logger.warning("[注册] 半自动：人工处理后重试提交邮箱仍失败")
                    if browser_try < BROWSER_TRIES and retryable:
                        logger.warning("[注册] 邮箱页不可用（URL=%s，原因=%s），"
                                       "判定出口被拦，换连接重试 %s/%s",
                                       _safe_url(driver), str(exc)[:60],
                                       browser_try, BROWSER_TRIES)
                        emit("navigate",
                             f"页面没渲染出来，换一条连接重试（{browser_try}/{BROWSER_TRIES}）")
                        try:
                            driver.quit()
                        except Exception:  # noqa: BLE001
                            pass
                        driver = None
                        time.sleep(3)
                        continue
                    raise

            logger.warning("[注册] 登录页不可用（%s，URL=%s），换连接重试 %s/%s",
                           verdict, _safe_url(driver), browser_try, BROWSER_TRIES)
            emit("navigate",
                 f"{'出口被 Cloudflare 拦了' if verdict == 'challenge' else '页面没渲染出来'}，"
                 f"换一条连接重试（{browser_try}/{BROWSER_TRIES}）")
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
            driver = None
            if browser_try >= BROWSER_TRIES:
                if not (config.proxy or "").strip():
                    raise RuntimeError(
                        f"连着 {BROWSER_TRIES} 条连接都被 Cloudflare 拦下了。"
                        "当前代理框是空的，走的是本机系统代理（Clash 之类），"
                        "它的出口 IP 一直被拦 —— 请在注册页填一条住宅代理再跑。"
                    )
                raise RuntimeError(
                    f"连着 {BROWSER_TRIES} 条连接都被 Cloudflare 拦下了，"
                    "当前代理的出口 IP 不可用；请换一个代理（或稍后再试）"
                )
            time.sleep(3)

        # ---- 4. 提交邮箱 ----
        # ★ 这一步已经在上面那个重试循环里做过了：循环要么成功提交并 break，
        #   要么抛异常/换连接。这里再调一次等于在重试保护之外又提交一次，
        #   页面一旦卡住就会直接判死整轮（2026-10-03 实测踩过），所以删掉。

        # ---- 5. 密码页（新版流程才会出现）----
        password = flow._fill_password_page_if_present(
            driver, inbox.email, timeout=25,
            allow_password_page=bool(config.harden_after_register),
        )
        record.password = password or ""
        check_stop()

        # ---- 6. 邮箱 OTP ----
        otp_after_ts = _submit_otp_with_retries(
            driver, inbox, mailbox, config,
            email=inbox.email, otp_after_ts=otp_after_ts, on_event=emit,
        )
        check_stop()

        # ---- 6.5 接口级设密码（★ 不需要第二封邮箱验证码）----
        # ★ 位置是这件事的关键：**必须趁还在 auth.openai.com 源上**。
        #   `POST /api/accounts/password/add` 是**绝对地址 + cookie 鉴权**，
        #   从 chatgpt.com 发属于跨域，会被 CORS 拦掉；而且这个 auth 会话
        #   在步骤 8 拿到 accessToken 之后基本就被消费掉了 ——
        #   所以步骤 9.5 那条兜底里再试，几乎必然打不通（那条只是白捡）。
        #   **这里才是它唯一真正能生效的时刻。**
        # 依据：HAR 里 `create-account/password` 路由 client action 的源码 ——
        #   那个「设置密码页面」只是个表单壳，干的就是这一发 POST
        #   （完整源码见 `account_api.set_password_in_auth_session` 的长注释）。
        # 安全约束：只在勾了加固、且注册流程还没设上密码时才试；
        #   失败一律吞掉照常往下走，**绝不改变注册成败**。
        if config.harden_after_register and not record.password:
            try:
                from .account_api import set_password_in_auth_session

                _pw = str(flow._get_register_password() or "").strip()
                _ok, _note = set_password_in_auth_session(driver, password=_pw, emit=emit)
                if _ok and _pw:
                    record.password = _pw
                    logger.info("[注册] 步骤 6.5 会话内接口级设密码成功（无需第二封码）")
                    emit("harden", "登录密码已设置（会话内接口级，无需第二封码）")
                else:
                    logger.info("[注册] 步骤 6.5 会话内接口级没成（%s），留给后面兜底", _note)
            except Exception as exc:  # noqa: BLE001
                logger.info("[注册] 步骤 6.5 会话内接口级异常，忽略：%s", exc)

        # ---- 7. 资料页 ----
        emit("profile", "正在填写姓名与生日")
        real_name = name or random_display_name()
        real_birthday = birthday or random_birthday()
        flow._complete_profile_page(driver, real_name, real_birthday, timeout=60)
        human_delay("post_auth")
        check_stop()

        # ---- 8. 读取 accessToken ----
        emit("session", "正在读取账号凭证")
        session_info = flow._fetch_chatgpt_session(driver, timeout=120)
        credentials = extract_credentials(session_info)
        if not credentials["access_token"]:
            raise RuntimeError("已进入 ChatGPT，但未能读取到 accessToken")
        record.access_token = credentials["access_token"]
        record.account_id = credentials["account_id"]
        record.expires_at = credentials["expires_at"]
        emit("session", "已拿到 accessToken")

        # 先落一条成功记录：即使后面的查活/激活失败，账号本身已经注册成功。
        record.status = "success"
        record.password = record.password or (password or "")

        # ---- 8.5 可选加固②：绑 TOTP 2FA（★ 必须在 240 秒窗口内，所以放这里）----
        # ★★ 位置是这件事的成败关键（2026-10-05 实测定案）：
        #   `mfa/enroll` 要求「**认证后 240 秒内**」，判定依据是 AT 里的 `pwd_auth_time`
        #   （HAR 前端原码：`VVe = 240`，`Date.now()/1e3 - pwd_auth_time/1e3 > VVe` 就拒）。
        #   而「注册认证 → 手机激活」要 5~10 分钟 —— 实测放到激活之后相隔 **562 秒**，
        #   必得 401 `recent_auth_required`。
        #   放在这里时：认证 → 拿到 AT 只差 **108 秒**，稳在窗口内。
        #   代价：后面激活的 OAuth 登录会撞上 MFA 挑战 —— 由
        #   `oauth_flow._fill_mfa_challenge_if_present` 用下面刚登记的密钥自动填。
        if config.harden_after_register and not record.totp_secret:
            emit("harden", "正在绑定 2FA 认证器（接口级 · 抢在 240 秒窗口内）")
            try:
                from .account_api import setup_totp

                _tfa_ok, _tfa_note, _secret = setup_totp(driver, record.access_token, emit=emit)
            except Exception as exc:  # noqa: BLE001
                _tfa_ok, _tfa_note, _secret = False, f"{type(exc).__name__}: {str(exc)[:150]}", ""
            if _tfa_ok and _secret:
                record.totp_secret = _secret
                # ★ 登记进 TOTP 注册表，激活流程遇到 MFA 挑战时要用它现算验证码
                try:
                    from .oauth_flow import register_account_totp

                    register_account_totp(record.email, _secret)
                    logger.info("[注册] 步骤 8.5 已绑 2FA 并登记密钥（激活可自动过 MFA）")
                    emit("harden", "2FA 已绑定，激活流程可自动过 MFA 挑战")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[注册] 登记 TOTP 密钥失败（激活可能要手工过）：%s", exc)
            else:
                logger.warning("[注册] 步骤 8.5 绑 2FA 没成：%s", _tfa_note)
                record.security_note = f"2FA 未绑（步骤 8.5）：{_tfa_note}"

        # ---- 9. 查活 ----
        if config.liveness_check:
            emit("liveness", "正在检测账号有效性")
            from .liveness import check_liveness
            status, note, detail = check_liveness(
                access_token=record.access_token,
                account_id=record.account_id,
                proxy=config.proxy,
                timeout=config.liveness_timeout,
            )
            record.liveness = status
            record.liveness_note = note
            record.plan = str(detail.get("plan_type") or "")
            emit("liveness", f"查活结果：{status} · {note}")

        # ---- 9.5 顺带查一次生图额度 ----
        # 注册刚拿到 token，这会儿查最省事；不然账号池里一直是「未查」，
        # 还得回账号池再点一次「查生图额度」。查不到不影响注册结果。
        emit("quota", "正在查生图额度")
        try:
            from .quota import fetch as fetch_quota

            q = fetch_quota(record.access_token, proxy=config.proxy,
                            timeout=config.liveness_timeout)
            record.quota_at = str(q.get("checked_at") or "")
            if q.get("ok"):
                record.quota = int(q.get("quota", -1))
                record.quota_note = str(q.get("note") or "")
                if q.get("plan"):
                    record.plan = str(q["plan"])
                emit("quota", f"生图额度：{record.quota_note}")
            else:
                record.quota = -1
                record.quota_note = str(q.get("error") or "查询失败")
                emit("quota", f"生图额度没查到：{record.quota_note}", )
        except Exception as exc:  # noqa: BLE001
            record.quota = -1
            record.quota_note = f"{type(exc).__name__}: {exc}"
            logger.warning("[注册] 顺带查生图额度失败：%s", record.quota_note)
            emit("quota", f"生图额度没查到（不影响注册）：{record.quota_note}")

        # ---- 9.5 可选加固①：设登录密码 ----
        # ★ 刻意放在**激活之前**：激活一旦掉到 auth.openai.com/log-in，
        #   没有密码就只能走「一次性验证码登录」，而那条路实测被 OpenAI 服务端拒
        #   （见 oauth.py 里那段注释）。先把密码设上，登录页就有密码路径可走。
        password_ok = bool(record.password)
        if config.harden_after_register and password_ok:
            emit("harden", "登录密码已随注册流程设置")
            logger.info("[注册] 登录密码已随注册流程设置（长度 %s）", len(record.password))
            record.security_note = "密码已随注册流程设置"
        elif config.harden_after_register:
            # 注册流程里没遇到设密码页 → 兜底。
            # ★ 顺序（2026-10-05 改）：
            #   ① **接口级 reauth 链**（account_api.set_password_protocol）—— 不做 DOM，
            #      跟 HAR 实证的接口走，稳得多；
            #   ② 设置页 DOM 那条（security_setup.set_password）—— 老办法，留作最后手段。
            #   两条都**需要第二封邮箱验证码**，而一单号池通常只给一封，
            #   所以它们本质都是兜底；主线仍是注册流程里那个设密码页（不需要第二封）。
            from .security_setup import generate_password
            want_pw = generate_password()
            password_ok, pw_note = False, ""

            # ★★ 零成本的那条放最前面：**会话内**接口级设密码，不需要第二封验证码。
            #   依据是 HAR 里路由 client action 的源码（详见 account_api 的长注释）——
            #   「设置密码」那个页面只是个表单壳，它干的就是
            #   `POST /api/accounts/password/add {"password": ...}`。
            #   放在这里是**安全的**：这个分支本来就是「注册时没设上密码」的兜底，
            #   而下面那条 reauth 链因为号池只给一封码而几乎必然失败；
            #   先花 1~2 秒试这条，失败就照原样往下走，**不改变任何既有行为**。
            try:
                from .account_api import set_password_in_auth_session

                password_ok, pw_note = set_password_in_auth_session(
                    driver, password=want_pw, emit=emit,
                )
                logger.info("[注册] 会话内接口级设密码：ok=%s note=%s", password_ok, pw_note)
            except Exception as exc:  # noqa: BLE001
                password_ok, pw_note = False, f"会话内接口级：{type(exc).__name__}: {str(exc)[:130]}"

            if not password_ok:
                # 会话内不行（多半是 auth 会话已经被消费掉）→ 退回原来的 reauth 链。
                emit("harden", "注册流程没设成密码，改走接口级加密码（需要一封新验证码）")
                try:
                    from .account_api import set_password_protocol

                    def _code_provider(after_ts: float) -> str:
                        return str(mailbox.wait_for_otp(
                            inbox, after_ts=float(after_ts or 0),
                            timeout=int(config.otp_timeout or 180),
                        ) or "")

                    password_ok, pw_note = set_password_protocol(
                        driver,
                        email=record.email,
                        access_token=record.access_token,
                        code_provider=_code_provider,
                        password=want_pw,
                        emit=emit,
                        otp_timeout=int(config.otp_timeout or 180),
                    )
                except Exception as exc:  # noqa: BLE001
                    password_ok, pw_note = False, f"接口级：{type(exc).__name__}: {str(exc)[:130]}"

            if not password_ok:
                logger.warning("[注册] 接口级设密码没成（%s），退回设置页 DOM 路径", pw_note)
                emit("harden", "接口级没成，改从安全设置页设置（DOM）")
                try:
                    from .security_setup import set_password

                    dom_ok, dom_note = set_password(
                        driver, mailbox=mailbox, inbox=inbox, config=config,
                        emit=emit, password=want_pw,
                    )
                except Exception as exc:  # noqa: BLE001
                    dom_ok, dom_note = False, f"设置页：{type(exc).__name__}: {str(exc)[:130]}"
                pw_note = "；".join(x for x in (f"接口级失败：{pw_note}", dom_note) if x) if dom_ok else \
                          f"接口级失败：{pw_note}；设置页也失败：{dom_note}"
                password_ok = dom_ok

            if password_ok:
                record.password = want_pw
                logger.info("[注册] 兜底路径设好了登录密码")
                emit("harden", "登录密码已设置")
            else:
                logger.warning("[注册] 设密码失败：%s", pw_note)
                emit("harden", f"设密码失败：{pw_note}")
            # ★ 累加而不是覆盖：步骤 8.5 可能已经写过「2FA 未绑」的原因，
            #   直接赋值会把它冲掉（改前就是赋值，导致 2FA 的失败原因常常看不到）。
            record.security_note = "；".join(x for x in (record.security_note, pw_note) if x)

        # ---- 10. 可选：ChatGPT 桌面端激活 ----
        if config.enable_activation:
            emit("activation", "正在执行 ChatGPT 桌面端激活")
            from .oauth import run_desktop_activation
            ok, note = run_desktop_activation(
                driver=driver,
                record=record,
                config=config,
                inbox=inbox,
                mailbox=mailbox,
                on_event=emit,
            )
            record.activation = "success" if ok else "failed"
            record.activation_note = note
            emit("activation", f"激活结果：{record.activation} · {note}")

        # ---- 11. 可选加固②：绑定 TOTP 2FA（兜底 · 正常已在步骤 8.5 绑好）----
        # ★★ 2026-10-05 改版：主路径挪到**步骤 8.5**（拿到 AT 之后立刻绑）。
        #   原因：`mfa/enroll` 要求「认证后 240 秒内」（前端原码 `VVe = 240`），
        #   而这里已经是激活之后 —— 实测相隔 562 秒，**必然 401 recent_auth_required**。
        #   （原来这里写着「2FA 必须放在激活之后，因为没有地方能自动填 TOTP」——
        #    那半句已经过时：`_fill_mfa_challenge_if_present` 早就写好了填码逻辑，
        #    之前失效只是因为 `_account_totp_code_for_email` 被掏空成 `return ""`。）
        #   这里保留成**兜底**：万一 8.5 那次失败，再试一次，成功也算白捡。
        if config.harden_after_register:
            if record.totp_secret:
                logger.info("[注册] 步骤 11：2FA 已在步骤 8.5 绑好，跳过重复绑定")
                tfa_ok, tfa_note = True, "已绑定 TOTP 2FA（接口级 · 步骤 8.5）"
            else:
                emit("harden", "正在绑定 2FA 认证器（接口级 · 兜底重试）")
                # ★ 走接口，不做 DOM：HAR 证实这些本来就是纯接口调用，鉴权就是同一个 AT：
                #     POST /backend-api/accounts/mfa/enroll                   → {"secret": ...}
                #     POST /backend-api/accounts/mfa/user/activate_enrollment → 填 6 位码确认
                from .account_api import setup_totp
                try:
                    tfa_ok, tfa_note, secret = setup_totp(driver, record.access_token, emit=emit)
                except Exception as exc:  # noqa: BLE001
                    tfa_ok, tfa_note, secret = False, f"{type(exc).__name__}: {str(exc)[:150]}", ""
                if tfa_ok and secret:
                    record.totp_secret = secret
            record.security_note = "；".join(
                x for x in (record.security_note, tfa_note) if x
            )
            # 两个都成 = success；只成一个 = partial；都不成 = failed
            record.security = (
                "success" if (password_ok and tfa_ok)
                else "partial" if (password_ok or tfa_ok)
                else "failed"
            )
            emit("harden", f"加固结果：{record.security}")

        return record

    except StopRequested:
        record.status = "cancelled"
        record.error = "用户停止"
        logger.info("[注册] 任务 %s 已取消", index)
        return record
    except Exception as exc:
        record.status = "failed"
        record.error = f"{type(exc).__name__}: {str(exc)[:400]}"
        logger.error("[注册] 任务 %s 失败: %s", index, record.error)
        return record
    finally:
        record.finished_at = _now()
        # keep_browser 只给诊断探针用（见函数 docstring）：留住登录态会话去看后面的页面。
        if driver is not None and not keep_browser:
            try:
                driver.quit()
            except Exception:
                pass


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
