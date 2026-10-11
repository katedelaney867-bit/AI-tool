# -*- coding: utf-8 -*-
"""接码平台：SMSBower / GrizzlySMS。

两家都用 SMS-Activate 兼容的 handler API，所以一套代码通吃，只是 base 和 key 不同。

提炼自 turb-gpt-free-register/core/sms_provider.py，砍掉本地 L/H 取号服务
（需要用户自建 localhost:8788 后台，对小白不现实），只保留两家公开平台。
"""
from __future__ import annotations

import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, dict] = {
    "smsbower": {
        "label": "SMSBower",
        "api_base": "https://smsbower.online/stubs/handler_api.php",
    },
    "grizzly": {
        "label": "GrizzlySMS",
        "api_base": "https://api.grizzlysms.com/stubs/handler_api.php",
    },
}

# setStatus 取值
STATUS_READY = 1       # 号码已就绪，短信已发出
STATUS_RESEND = 3      # 请求重发短信
STATUS_COMPLETE = 6    # 激活完成
STATUS_CANCEL = 8      # 取消激活


class SmsError(RuntimeError):
    """接码平台通用错误。"""


class SmsNoNumbers(SmsError):
    """平台暂无可用号码。"""


class SmsNoBalance(SmsError):
    """平台余额不足。"""


class SmsCodeTimeout(SmsError):
    """等短信超时 —— 上层应当换号重试。"""


class SmsClient:
    """一家接码平台的会话。"""

    def __init__(
        self,
        provider: str,
        api_key: str,
        *,
        service: str = "dr",
        country: str = "10",
        max_price: str = "",
        timeout: int = 30,
    ) -> None:
        key = str(provider or "").strip().lower()
        if key not in PROVIDERS:
            raise SmsError(f"不支持的接码平台：{provider}")
        self.provider = key
        self.label = PROVIDERS[key]["label"]
        self.api_base = PROVIDERS[key]["api_base"]
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise SmsError(f"{self.label} API Key 未填写")
        self.service = str(service or "dr").strip()
        self.country = str(country or "10").strip()
        self.max_price = str(max_price or "").strip()
        self.timeout = max(5, int(timeout))

    # ------------------------------------------------------------- HTTP

    def _call(self, params: dict) -> str:
        query = {"api_key": self.api_key, **params}
        try:
            response = requests.get(self.api_base, params=query, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SmsError(f"{self.label} 请求失败: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise SmsError(f"{self.label} API Key 无效或无权访问（HTTP {response.status_code}）")
        if response.status_code != 200:
            raise SmsError(f"{self.label} HTTP {response.status_code}")

        text = str(response.text or "").strip()
        if text == "BAD_KEY":
            raise SmsError(f"{self.label} API Key 无效（BAD_KEY）")
        if text == "NO_BALANCE":
            raise SmsNoBalance(f"{self.label} 余额不足（NO_BALANCE），请先充值")
        if text == "NO_NUMBERS":
            raise SmsNoNumbers(f"{self.label} 暂无可用号码（NO_NUMBERS）")
        if text == "SERVICE_UNAVAILABLE_REGION":
            raise SmsError(f"{self.label} 该地区受限（SERVICE_UNAVAILABLE_REGION），请换代理国家")
        if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
            raise SmsError(f"{self.label} 请求参数错误：{text}")
        if text == "NO_ACTIVATION":
            raise SmsError(f"{self.label} 激活 ID 不存在（NO_ACTIVATION）")
        if text.startswith("The service is prohibited"):
            raise SmsError(f"{self.label} 该服务被平台禁售：{text[:120]}")
        return text

    # ---------------------------------------------------------- 取号/收码

    def acquire(self) -> tuple[str, str]:
        """取一个号，返回 (activation_id, 手机号)。手机号不带 + 前缀。"""
        params = {"action": "getNumber", "service": self.service, "country": self.country}
        if self.max_price:
            params["maxPrice"] = self.max_price
        text = self._call(params)
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsError(f"{self.label} getNumber 响应异常：{text[:160]}")
        parts = text.split(":")
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            raise SmsError(f"{self.label} getNumber 格式异常：{text[:160]}")
        activation_id, phone = parts[1].strip(), parts[2].strip()
        # 号码记进日志：不然「哪个号接到的码」事后完全查不到（租来的号已经释放，
        # 上游那边也翻不回来）。这是用完即弃的临时号，记下来只用于排查。
        logger.info("[SMS:%s] 取号成功 id=%s 号码=%s", self.label, activation_id, phone)
        return activation_id, phone

    def wait_code(
        self,
        activation_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 5,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询 getStatus 直到拿到短信验证码。"""
        deadline = time.time() + max(10, int(max_wait))
        interval = max(2, int(poll_interval))
        rounds = 0
        logger.info("[SMS:%s] 等待短信验证码，最长 %ss", self.label, max_wait)

        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise SmsError("用户已请求停止")
            rounds += 1
            text = self._call({"action": "getStatus", "id": activation_id})

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info("[SMS:%s] 第 %s 轮收到验证码", self.label, rounds)
                return code
            if text == "STATUS_CANCEL":
                raise SmsError(f"{self.label} 激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY / STATUS_WAIT_RESEND → 继续等

            remaining = int(deadline - time.time())
            if remaining <= 0:
                break
            logger.debug("[SMS:%s] 第 %s 轮未收到（%s），剩 %ss", self.label, rounds, text[:40], remaining)
            time.sleep(min(interval, remaining))

        raise SmsCodeTimeout(f"{self.label} 等待短信超时（>{max_wait}s）")

    # ------------------------------------------------------------- 状态

    def set_status(self, activation_id: str, status: int) -> str:
        try:
            return self._call({"action": "setStatus", "status": str(int(status)), "id": activation_id})
        except SmsError as exc:
            logger.debug("[SMS:%s] setStatus(%s) 失败: %s", self.label, status, exc)
            return ""

    def complete(self, activation_id: str) -> None:
        """标记激活完成（编号 6）。失败不影响主流程。"""
        self.set_status(activation_id, STATUS_COMPLETE)

    def cancel(self, activation_id: str) -> None:
        """取消并释放号码（编号 8）。失败不影响主流程。"""
        self.set_status(activation_id, STATUS_CANCEL)
