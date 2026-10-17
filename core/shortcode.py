# -*- coding: utf-8 -*-
"""「短效接码」token 页客户端（longnotes.cn 那类 `/m/<token>` 访问页）。

这类页面是**转售商自己包的一层**（不是 SMS-Activate handler API，也不是 miaoic 的
`/recv/*`），它把上游藏在自己的服务端，前端只暴露几个动作：

    POST <token>/state          读订单状态（号码 / 短信 / 过期时间 / 换号配额）
    POST <token>/number         取号或换号（消耗「换号次数」）
    POST <token>/cancel         取消当前号码
    POST <token>/change-number  换号（另一个入口）

★ 关键差异（会造成行为差异，务必记住）：
1. **一个 token 页同一时刻只持有一个号码**，没有 activation_id 的概念 ——
   所以 ``acquire()`` 返回的 id 本身就是号码（和 miaoic 一样，满足上层契约）。
   ⇒ 并发上限 = token 数；`concurrency>1` 配一个 token 会互相抢号。
2. **换号次数有限**（页面上写「剩余换号次数」；拿到的这单是 ``shortChangeMaxTimes: 0``）。
   所以默认 ``reuse_current=True``：**优先用页面上已经拿到的那个号**，
   不为了"取一个新的"白白消耗次数。换号重试到没次数时会抛 ``SmsNoNumbers``，
   走工具里那条「无号止损」路径。
3. 短信在 ``data.smsMessages`` 里，是**这个号码收过的全部短信** ——
   和 miaoic 一样要记「进入时的条数」做基线，只看基线之后新出现的，
   否则会把上一轮遗留的旧码当成新码（假成功）。
"""
from __future__ import annotations

import logging
import re
import threading
import time

import requests

from .sms import SmsCodeTimeout, SmsError, SmsNoBalance, SmsNoNumbers

logger = logging.getLogger(__name__)

_DIGITS_RE = re.compile(r"\d+")
_CODE_RE = re.compile(r"^\d{4,8}$")

# 转售商的错误文案是中文、没有文档 —— 只能按关键字映射，映射不中就退化成 SmsError
# （**不能**误判成「无号」，否则会白白触发换号止损）。真跑起来按日志回补这张表。
_NO_BALANCE_HINTS = ("次数不足", "余额不足", "次数用尽", "没有次数", "欠费", "充值", "余额")
_NO_NUMBER_HINTS = ("换号次数", "没有可用", "暂无可用", "无可用号码", "号码不足", "库存不足", "稍后再试")

# 「拿到码之后」不要主动 cancel：号码的回收由页面自己的窗口管，
# 而且 cancel 掉之后这一单就作废了，没有意义（钱已经花了）。


def normalize_phone(raw: str) -> str:
    """统一成纯数字、不带 +（与 ``SmsClient.acquire()`` 的约定一致）。"""
    return "".join(_DIGITS_RE.findall(str(raw or "")))


def extract_code(messages: list) -> str:
    """从 ``smsMessages`` 里抠验证码。

    条目结构**未知**（这单还没收到过短信，``smsMessages`` 是空的），所以采取
    「先看 code 字段、再扫全部字符串值里的 4~8 位纯数字」的保守策略：
    宁可返回空、让调用方继续轮询，也不要返回一个猜错的码。
    """
    if not isinstance(messages, list) or not messages:
        return ""
    latest = messages[-1]
    if not isinstance(latest, dict):
        text = str(latest or "")
        found = re.findall(r"\b(\d{4,8})\b", text)
        return found[0] if found else ""
    for key in ("code", "smsCode", "verifyCode", "captcha", "otp", "messageCode"):
        value = str(latest.get(key) or "").strip()
        if _CODE_RE.match(value):
            return value
    for key in ("content", "text", "message", "sms", "body"):
        value = str(latest.get(key) or "")
        found = re.findall(r"\b(\d{4,8})\b", value)
        if found:
            return found[0]
    # 兜底：把这一条的字符串值全扫一遍
    for value in latest.values():
        if isinstance(value, str):
            found = re.findall(r"\b(\d{4,8})\b", value)
            if found:
                return found[0]
    return ""


class ShortCodeSmsClient:
    """把「短效接码」token 页包成和 :class:`core.sms.SmsClient` 一样的形状。

    方法签名与 ``SmsClient`` / ``SitePoolSmsClient`` / ``MiaoicSmsClient`` 一致：
    ``acquire / wait_code / set_status / complete / cancel``。
    """

    provider = "shortcode"
    label = "短效接码"

    def __init__(
        self,
        token_url: str,
        *,
        timeout: int = 30,
        reuse_current: bool = True,
        poll_interval: int = 5,
    ) -> None:
        url = str(token_url or "").strip().rstrip("/")
        if not url.lower().startswith(("http://", "https://")):
            raise SmsError("短效接码页面地址要以 http:// 或 https:// 开头")
        self.token_url = url
        self.timeout = max(5, int(timeout))
        self.reuse_current = bool(reuse_current)
        self.poll_interval = max(2, int(poll_interval))

        self._session = requests.Session()
        # 和 sitepool 同一个坑：trust_env 会把本机 Clash 的系统代理读进来
        self._session.trust_env = False
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        self._phone = ""
        self._baseline_len = 0
        self._consumed = False   # 这个号码是不是本次已经用过了（防止重复"复用"）
        self._lock = threading.Lock()

    # ------------------------------------------------------------- HTTP

    def _post(self, action: str, payload: dict | None = None) -> dict:
        url = f"{self.token_url}/{action.lstrip('/')}"
        try:
            response = self._session.post(
                url, json=payload or {}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SmsError(f"{self.label} 请求失败：{type(exc).__name__}") from exc

        if response.status_code in (401, 403, 404):
            raise SmsError(f"{self.label} 页面失效（HTTP {response.status_code}），链接可能已过期")
        if response.status_code != 200:
            raise SmsError(f"{self.label} HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise SmsError(f"{self.label} 返回不是 JSON：{response.text[:120]}") from exc
        if not isinstance(body, dict):
            raise SmsError(f"{self.label} 返回格式异常：{str(body)[:120]}")

        if str(body.get("code")) not in ("0", "None") and body.get("code") is not None:
            self._raise_for_message(str(body.get("msg") or body.get("message") or ""))
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _raise_for_message(self, message: str) -> None:
        text = str(message or "")
        if any(hint in text for hint in _NO_BALANCE_HINTS):
            raise SmsNoBalance(f"{self.label} 次数/余额不足：{text}")
        if any(hint in text for hint in _NO_NUMBER_HINTS):
            raise SmsNoNumbers(f"{self.label} 没有可用号码：{text}")
        raise SmsError(f"{self.label} 接口报错：{text}")

    # ------------------------------------------------------------ 状态 / 取号

    def state(self) -> dict:
        return self._post("state")

    def _adopt(self, data: dict) -> str:
        phone = normalize_phone(data.get("phoneNumber") or data.get("phone") or "")
        messages = data.get("smsMessages") or []
        if not isinstance(messages, list):
            messages = []
        self._phone = phone
        self._baseline_len = len(messages)
        return phone

    def acquire(self) -> tuple[str, str]:
        """拿一个号，返回 ``(号码, 号码)``（token 页没有独立的 activation_id）。"""
        with self._lock:
            data = self.state()
            held = normalize_phone(data.get("phoneNumber") or "")
            # ★ 默认复用页面上已经拿到的号：换号次数是按次卖的，别为了"取新的"白烧一次
            if self.reuse_current and held and not self._consumed:
                phone = self._adopt(data)
                logger.info("[短效接码] 复用页面上已有的号码 %s（不消耗换号次数）", phone)
                self._consumed = True
                return phone, phone

            data = self._post("number")
            phone = self._adopt(data)
            if not phone:
                raise SmsNoNumbers(f"{self.label} 取号返回空号码：{str(data)[:160]}")
            logger.info("[短效接码] 取号成功 号码=%s 过期=%s", phone, data.get("activationExpiresAt"))
            self._consumed = True
            return phone, phone

    # ------------------------------------------------------------ 收码

    def wait_code(
        self,
        activation_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 5,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询 ``/state``，只认**基线之后**新出现的短信（防止拿到旧码）。"""
        deadline = time.time() + max(10, int(max_wait))
        interval = max(2, int(poll_interval or self.poll_interval))
        rounds = 0
        logger.info("[短效接码] 等待短信验证码，最长 %ss（号码 %s）", max_wait, self._phone)

        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise SmsError("用户已请求停止")
            rounds += 1
            data = self.state()

            messages = data.get("smsMessages") or []
            if not isinstance(messages, list):
                messages = []
            if len(messages) > self._baseline_len:
                code = extract_code(messages)
                if code:
                    logger.info("[短效接码] 第 %s 轮收到验证码（第 %s 条短信）",
                                rounds, len(messages))
                    return code
                logger.warning("[短效接码] 收到新短信但抠不出码：%s", str(messages[-1])[:200])

            state = str(data.get("orderState") or "")
            if state in ("CANCELED", "CANCELLED", "EXPIRED", "FAILED", "CLOSED"):
                raise SmsCodeTimeout(f"{self.label} 号码已作废（orderState={state}）")

            remaining = int(deadline - time.time())
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise SmsCodeTimeout(f"{self.label} 等待短信超时（>{max_wait}s）")

    # ------------------------------------------------------------- 状态上报

    def set_status(self, activation_id: str, status: int) -> str:
        if int(status) == 8:  # STATUS_CANCEL
            return str(self._post("cancel").get("orderState") or "")
        logger.debug("[短效接码] set_status(%s) 无对应接口，忽略", status)
        return ""

    def complete(self, activation_id: str) -> None:
        """拿到码就完事：**不主动 cancel**（号码回收由页面自己的窗口管）。"""
        logger.info("[短效接码] 号码 %s 已收到码，结束（页面窗口会自动回收）", self._phone)

    def cancel(self, activation_id: str) -> None:
        """没收到码就退号。换号次数用尽时这里会报错 —— 只记日志，不抛。"""
        try:
            self._post("cancel")
            logger.info("[短效接码] 号码 %s 已退回", self._phone)
        except SmsError as exc:
            logger.warning("[短效接码] 退号失败（不影响本次结果）：%s", exc)
