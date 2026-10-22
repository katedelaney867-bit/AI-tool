# -*- coding: utf-8 -*-
"""miaoic.shop 接码客户端。

站点**没有公开 API** —— 这里用的是它自己的网页接口（``/recv/*``），身份是 cookie
``bj_user``。

与 SMSBower 那类 SMS-Activate handler API 的关键区别（会造成行为差异，务必记住）：

1. **一个 Token 同一时刻只持有一个号码**，没有 activation_id 的概念。
   ⇒ **并发上限 = Token 数**。`concurrency=2` 配一个 Token 会互相抢号。
   所以 ``acquire()`` 返回的 activation_id 就是号码本身，不做跨请求寻址。
2. 号码在手期间可以 ``switch``（有 ``freeze`` / ``switch_cooldown`` 限制）或 ``release``。
3. 计费模型未用真 Token 验证。UI 上有「可用 / 暂扣 / 已成功扣次」三个计数器 + 一个
   release 接口，**指向「取号暂扣、成功才真扣、失败释放退回」**，但这是推断。

★ 号码复用风险：``/recv/state`` 的 ``codes[]`` 是**该号码收过的全部短信**。若上一轮
遗留的号码还在手上，直接读 ``code`` 会拿到**上一条旧码**。所以 ``acquire()`` 会记下
进入时的 ``codes`` 长度做基线，``wait_code()`` 只接受**基线之后**新出现的码。
（同 ``_wait_for_fresh_email_otp`` 的 after_ts 语义。）
"""
from __future__ import annotations

import logging
import re
import threading
import time

import requests

from .sms import SmsCodeTimeout, SmsError, SmsNoBalance, SmsNoNumbers

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://miaoic.shop"

# 站点只有 HTTP，没有 TLS。这是**对方**的现状，不是我们的选择。
# 后果：Token 明文过公网 —— 配置里不要用主账号 Token，按次充值的 Token 即可。

_CODE_RE = re.compile(r"^\d{4,8}$")
_DIGITS_RE = re.compile(r"\d+")

# 站点错误文案只能靠关键字猜（没有文档）。猜不中就退化成 SmsError，不会误判成「无号」
# 而白白换号。命中哪种是**未验证**的，真跑起来要按日志回头修这张表。
_NO_BALANCE_HINTS = ("次数不足", "余额不足", "次数用尽", "没有次数", "欠费", "充值")
_NO_NUMBER_HINTS = ("没有可用", "暂无可用", "无可用号码", "号码不足", "库存不足", "稍后再试")

# switch 的 freeze 可能很长，别把整个注册流程挂死
_MAX_SWITCH_WAIT = 180


def normalize_phone(raw: str) -> str:
    """站点可能返回 '+1 571-234-5678' 这类带格式的号码；统一成纯数字（不带 +）。

    与 ``core.sms.SmsClient.acquire()`` 的约定一致：手机号不带 + 前缀。
    """
    digits = "".join(_DIGITS_RE.findall(str(raw or "")))
    return digits


class MiaoicSmsClient:
    """把 miaoic 的 ``/recv/*`` 包装成和 :class:`core.sms.SmsClient` 一样的形状。

    方法签名与 ``SmsClient`` / ``SitePoolSmsClient`` 保持一致：
    ``acquire / wait_code / set_status / complete / cancel``。
    """

    provider = "miaoic"
    label = "miaoic"

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        release_on_complete: bool = True,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.token = str(token or "").strip()
        if not self.token:
            raise SmsError("miaoic Token 未填写")
        self.timeout = max(5, int(timeout))
        # 拿到码之后要不要主动释放号码。默认释放：一个 Token 只持一个号，
        # 不释放的话下一个账号的 acquire() 会被迫走 switch（多花一次切换）。
        self.release_on_complete = bool(release_on_complete)

        self._session = requests.Session()
        # 与 sitepool 同一个坑：requests 默认 trust_env=True，会把本机 Clash 的
        # 系统代理读进来。miaoic 是普通站点，没必要烧计费住宅代理的流量。
        self._session.trust_env = False
        self._session.headers.update({"X-Requested-With": "fetch"})
        self._session.cookies.set("bj_user", self.token, domain=self._cookie_domain())

        # acquire() 建立的「新鲜度基线」，见模块 docstring 的号码复用风险
        self._baseline_len = 0
        self._baseline_code = ""
        self._phone = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------- HTTP

    def _cookie_domain(self) -> str:
        host = self.base_url.split("//")[-1].split("/")[0]
        return host.split(":")[0]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method, url, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise SmsError(f"{self.label} 请求失败: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise SmsError(f"{self.label} Token 无效或无权访问（HTTP {response.status_code}）")
        if response.status_code != 200:
            raise SmsError(f"{self.label} HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise SmsError(f"{self.label} 返回不是 JSON：{response.text[:120]}") from exc
        if not isinstance(data, dict):
            raise SmsError(f"{self.label} 返回格式异常：{str(data)[:120]}")

        if not data.get("ok", True):
            self._raise_for_message(str(data.get("msg") or data.get("error") or ""))
        return data

    def _raise_for_message(self, message: str) -> None:
        text = str(message or "")
        if "没有 Token" in text or "Token" in text and "无效" in text:
            raise SmsError(f"{self.label} Token 无效或已失效：{text}")
        if any(hint in text for hint in _NO_BALANCE_HINTS):
            raise SmsNoBalance(f"{self.label} 次数不足：{text}（去 16688 买 CDK 充入）")
        if any(hint in text for hint in _NO_NUMBER_HINTS):
            raise SmsNoNumbers(f"{self.label} 暂无可用号码：{text}")
        raise SmsError(f"{self.label} 接口报错：{text}")

    # ---------------------------------------------------------- 只读接口

    @staticmethod
    def stock(base_url: str = DEFAULT_BASE_URL, timeout: int = 10) -> dict:
        """公开库存（**不需要 Token**）：``{"available": 100, "total": 1009}``。

        拿它做「这个上游还剩多少余量」的探针 —— 注册前先看一眼，掉到个位数就该警觉。
        """
        try:
            response = requests.get(
                f"{str(base_url).rstrip('/')}/phones/stock", timeout=max(5, int(timeout))
            )
            return dict(response.json() or {})
        except Exception as exc:  # noqa: BLE001 — 探针不该抛
            return {"error": f"{type(exc).__name__}: {exc}"}

    def state(self) -> dict:
        """当前号码状态：phone / code / success / window_left / can_switch / freeze …"""
        return self._request("GET", "/recv/state")

    # ------------------------------------------------------------ 取号

    def _adopt(self, state: dict) -> str:
        """把 state 里的号码与收码基线记下来，返回规范化号码。"""
        phone = normalize_phone(state.get("phone") or "")
        codes = state.get("codes") or []
        if not isinstance(codes, list):
            codes = []
        self._phone = phone
        self._baseline_len = len(codes)
        self._baseline_code = str(state.get("code") or "").strip()
        return phone

    def open_number(self) -> dict:
        return self._request("POST", "/recv/open")

    def switch(self, *, wait: bool = True, stop_event: threading.Event | None = None) -> dict:
        """换号。``wait=True`` 时会先按站点给的 ``freeze`` / ``switch_cooldown`` 等够。

        站点用这两个字段做防刷：不等就打，返回的很可能是**同一个号码**，
        在换号重试循环里表现为「换了半天还是那个号」，最终被误判成上游没号。
        """
        state = self.state()
        if wait:
            delay = max(int(state.get("freeze") or 0), int(state.get("switch_cooldown") or 0))
            delay = min(delay, _MAX_SWITCH_WAIT)
            if delay > 0:
                logger.info("[miaoic] 换号冷却中，等 %ss", delay)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if stop_event is not None and stop_event.is_set():
                        raise SmsError("用户已请求停止")
                    time.sleep(min(1.0, deadline - time.monotonic()))
        return self._request("POST", "/recv/switch")

    def release(self) -> dict:
        return self._request("POST", "/recv/release")

    def acquire(self) -> tuple[str, str]:
        """取一个号，返回 ``(手机号, 手机号)``。

        ★ 一个 Token 只持有一个号码，所以这里**没有真正的 activation_id** ——
        两个位置都放号码，是为了满足 ``SmsClient`` 的 ``(id, phone)`` 契约，
        让上层流程（``oauth.py`` / ``registration.py``）一行都不用改。

        手上若还有号码（上一轮遗留/上一账号失败），直接 ``open`` 会拿到同一个，
        所以改成 ``switch`` 换一个新的。
        """
        with self._lock:
            state = self.state()
            held = normalize_phone(state.get("phone") or "")
            if held and not state.get("deducted"):
                # ★ 实测：release + open 会**免费**换一个随机新号码
                #   （balance 4→3→4，号码每次不同），而 switch 有 8~9 秒冷却
                #   且换号期间 `can_switch` 常为 false。
                #   所以换号重试走 release→open，不要走 switch。
                logger.info("[miaoic] 手上仍有号码 %s，释放后重新取", held)
                self.release()
                state = self.open_number()
            else:
                state = self.open_number()

            phone = self._adopt(state)
            if not phone:
                raise SmsNoNumbers(f"{self.label} 取号返回空号码：{str(state)[:160]}")
            logger.info("[miaoic] 取号成功 号码=%s 窗口=%ss", phone, state.get("window_left"))
            return phone, phone

    # ------------------------------------------------------------ 收码

    def wait_code(
        self,
        activation_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 3,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询 ``/recv/state`` 直到出现**基线之后**的新验证码。"""
        deadline = time.time() + max(10, int(max_wait))
        interval = max(2, int(poll_interval))
        rounds = 0
        logger.info("[miaoic] 等待短信验证码，最长 %ss（号码 %s）", max_wait, self._phone)

        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise SmsError("用户已请求停止")
            rounds += 1
            state = self.state()

            err = str(state.get("error") or "").strip()
            if err:
                # 窗口到期 / 号码被回收 —— 换号重试，而不是干等到超时
                raise SmsCodeTimeout(f"{self.label} 号码不可用：{err}")

            code = str(state.get("code") or "").strip()
            codes = state.get("codes") or []
            fresh = False
            if isinstance(codes, list) and len(codes) > self._baseline_len:
                latest = codes[-1]
                if isinstance(latest, dict):
                    code = str(latest.get("code") or code).strip()
                fresh = True
            elif code and code != self._baseline_code:
                fresh = True

            if fresh and _CODE_RE.match(code):
                logger.info("[miaoic] 第 %s 轮收到验证码", rounds)
                return code

            remaining = int(deadline - time.time())
            if remaining <= 0:
                break
            logger.debug(
                "[miaoic] 第 %s 轮未收到（code=%r len=%s），剩 %ss",
                rounds, code, len(codes) if isinstance(codes, list) else "?", remaining,
            )
            time.sleep(min(interval, remaining))

        raise SmsCodeTimeout(f"{self.label} 等待短信超时（>{max_wait}s）")

    # ------------------------------------------------------------- 状态

    def set_status(self, activation_id: str, status: int) -> str:
        """站点没有「报状态」接口，只有 release。取消语义映射到 release。"""
        if int(status) == 8:  # STATUS_CANCEL
            return str(self.release().get("ok"))
        logger.debug("[miaoic] set_status(%s) 无对应接口，忽略", status)
        return ""

    def complete(self, activation_id: str) -> None:
        """拿到码之后释放号码，好让下一个账号能直接 open（否则要 switch）。"""
        if not self.release_on_complete:
            return
        try:
            self.release()
            logger.info("[miaoic] 号码 %s 已释放", self._phone)
        except SmsError as exc:
            # 释放失败不影响交付：号码会在窗口到期后自然回收
            logger.warning("[miaoic] 释放号码失败（不影响本次结果）：%s", exc)

    def cancel(self, activation_id: str) -> None:
        """没收到码就退号。若站点是「成功才扣费」，这里能省下一次的钱。"""
        try:
            self.release()
            logger.info("[miaoic] 号码 %s 已退回（未收到码）", self._phone)
        except SmsError as exc:
            logger.warning("[miaoic] 退号失败：%s", exc)
