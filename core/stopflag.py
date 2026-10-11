# -*- coding: utf-8 -*-
"""全局停止信号。

页面流程里散落着 `_check_manual_stop()` 调用点：用户在 GUI 上点「停止」后，
正在跑的注册线程要在下一个安全点退出，而不是被强杀。

原项目走 core.registration_service 的线程上下文；这里用一个进程级 Event，
对「一个进程跑一批任务」的模型来说更简单也更可靠。
"""
from __future__ import annotations

import threading


class StopRequested(RuntimeError):
    """用户请求停止。注册流程抛出它，由任务引擎记为「已取消」。"""


_stop = threading.Event()


def request_stop() -> None:
    """请求停止；已排队的任务不再开始，正在跑的在下一个检查点退出。"""
    _stop.set()


def clear_stop() -> None:
    """开始新一批任务前清除停止状态。"""
    _stop.clear()


def is_stop_requested() -> bool:
    return _stop.is_set()


def check_stop() -> None:
    """在流程检查点调用；已请求停止则抛出 StopRequested。"""
    if _stop.is_set():
        raise StopRequested("用户已请求停止")
