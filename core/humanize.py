# -*- coding: utf-8 -*-
"""人工操作节奏。

真实用户操作浏览器会有页面加载、阅读、输入、切回邮箱的停顿。这里集中放
随机延迟区间，避免全流程固定节拍被风控识别。

提炼自 turb-gpt-free-register/config/humanize.py，去掉 .env 热加载层。
"""
from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger(__name__)

# 总开关：关闭后 delay() 立刻返回。
ENABLE_HUMANIZE_DELAY: bool = True

# 全局倍率：批量跑得太慢时调小。
HUMANIZE_DELAY_FACTOR: float = 1.0

# 浏览器动作随机化（点击前滚动/移动、逐字符输入等）。
ENABLE_HUMANIZE_BROWSER_ACTIONS: bool = True

# 每类动作的随机停顿区间（秒）。
HUMANIZE_DELAYS: dict[str, tuple[float, float]] = {
    "api": (0.45, 1.35),            # 页面 JS 发完请求后处理状态
    "navigate": (1.2, 3.2),         # 跳转/重定向后等页面稳定
    "challenge": (0.8, 2.4),        # Sentinel/Turnstile/PoW 类等待
    "otp_input": (2.5, 8.0),        # 收到验证码后切回页面输入
    "form": (1.8, 5.0),             # 填写姓名生日等表单
    "post_auth": (1.5, 4.0),        # 注册完成后进入应用、拉 session
    "job_stagger": (0.4, 1.8),      # 并发任务错峰
    "click": (0.15, 0.85),          # 点击前观察/移动鼠标
    "keystroke": (0.035, 0.18),     # 单字符输入间隔
    "typing_pause": (0.18, 0.75),   # 输入中偶尔停顿
    "page_warmup": (0.7, 2.2),      # 页面打开后的短暂观察
}


def delay(kind: str = "api", *, minimum: float | None = None, maximum: float | None = None) -> float:
    """按区间随机 sleep，返回实际秒数。"""
    if not ENABLE_HUMANIZE_DELAY:
        return 0.0
    if minimum is None or maximum is None:
        lo, hi = HUMANIZE_DELAYS.get(kind, (0.4, 1.2))
        minimum = lo if minimum is None else minimum
        maximum = hi if maximum is None else maximum
    factor = float(HUMANIZE_DELAY_FACTOR or 1.0)
    lo = max(0.0, float(minimum) * factor)
    hi = max(lo, float(maximum) * factor)
    seconds = random.uniform(lo, hi)
    logger.debug("[Humanize] delay kind=%s seconds=%.2f", kind, seconds)
    time.sleep(seconds)
    return seconds
