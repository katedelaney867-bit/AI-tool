# -*- coding: utf-8 -*-
"""轻量后台任务：查额度、生图、推送号池都用它。

注册/激活那套用 ``core/engine.py``（有并发编排、队列、快照）。这三件事都是
「点一下跑一遍」的单发任务，借用 engine 会把简单事复杂化，所以单独做一个
最小实现：一个 kind 同一时间只跑一个任务，日志滚动保留，前端轮询拿增量。
"""
from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_LOGS = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Task:
    """一个后台任务的运行时状态。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.lock = threading.RLock()
        self.running = False
        self.started_at = ""
        self.finished_at = ""
        self.total = 0
        self.done = 0
        self.logs: list[str] = []
        self.result: dict = {}
        self.error = ""
        self.cancel = threading.Event()

    def log(self, message: str) -> None:
        with self.lock:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.logs.append(f"[{stamp}] {message}")
            if len(self.logs) > MAX_LOGS:
                del self.logs[: len(self.logs) - MAX_LOGS]

    def progress(self, done: int, total: int | None = None) -> None:
        with self.lock:
            self.done = done
            if total is not None:
                self.total = total

    def snapshot(self, since_log: int = 0) -> dict:
        with self.lock:
            start = max(0, min(int(since_log or 0), len(self.logs)))
            return {
                "kind": self.kind,
                "running": self.running,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "total": self.total,
                "done": self.done,
                "logs": self.logs[start:],
                "log_cursor": len(self.logs),
                "result": dict(self.result),
                "error": self.error,
            }


_TASKS: dict[str, Task] = {}
_REGISTRY_LOCK = threading.RLock()


def task(kind: str) -> Task:
    with _REGISTRY_LOCK:
        if kind not in _TASKS:
            _TASKS[kind] = Task(kind)
        return _TASKS[kind]


def is_running(kind: str) -> bool:
    return task(kind).running


def start(kind: str, worker) -> tuple[bool, str]:
    """启动一个后台任务。``worker(job: Task)`` 在子线程里执行。

    返回 ``(是否启动, 提示)``。同一 kind 已在跑时返回 False。
    """
    job = task(kind)
    with job.lock:
        if job.running:
            return False, "这个任务正在运行中"
        job.running = True
        job.started_at = _now()
        job.finished_at = ""
        job.total = 0
        job.done = 0
        job.logs = []
        job.result = {}
        job.error = ""
        job.cancel = threading.Event()

    def _runner() -> None:
        try:
            worker(job)
        except Exception as exc:  # 后台线程里的异常必须自己吞掉
            logger.exception("[任务 %s] 未捕获异常", kind)
            job.error = f"{type(exc).__name__}: {exc}"
            job.log(f"执行失败：{job.error}")
            job.log(traceback.format_exc(limit=3).strip().splitlines()[-1])
        finally:
            with job.lock:
                job.running = False
                job.finished_at = _now()
            hook = _ON_FINISH.get(kind)
            if hook:
                try:
                    hook(job)
                except Exception:
                    logger.exception("[任务 %s] 收尾回调失败", kind)

    threading.Thread(target=_runner, name=f"task-{kind}", daemon=True).start()
    return True, ""


_ON_FINISH: dict = {}


def on_finish(kind: str, hook) -> None:
    """注册任务结束后的回调（比如把额度刷进账号库）。"""
    _ON_FINISH[kind] = hook


def stop(kind: str) -> None:
    task(kind).cancel.set()


def reset(kind: str) -> None:
    job = task(kind)
    with job.lock:
        if job.running:
            return
        job.logs = []
        job.result = {}
        job.error = ""
        job.done = 0
        job.total = 0
