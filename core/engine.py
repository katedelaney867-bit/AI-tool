# -*- coding: utf-8 -*-
"""并发任务引擎。

一个进程跑一批任务：注册 N 个账号（并发 M），或对已有账号批量查活。
GUI 通过轮询 :meth:`RegistrationEngine.snapshot` 拿实时状态和日志。

为什么不用 asyncio：注册流程本质是「Playwright 同步 API + 阻塞式取码轮询」，
每个任务一个线程最直白，出问题也好定位。一个账号一个浏览器实例，线程数就是
并发数，和 CloakBrowser 的会话模型一一对应。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import store
from .settings import Config
from .stopflag import clear_stop, is_stop_requested, request_stop

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 800


@dataclass
class TaskState:
    index: int
    email: str = ""
    stage: str = "pending"
    status: str = "pending"      # pending/running/success/failed/cancelled
    detail: str = ""
    error: str = ""
    liveness: str = ""
    activation: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Run:
    kind: str = ""               # register / liveness
    total: int = 0
    concurrency: int = 1
    started_at: str = ""
    finished_at: str = ""
    running: bool = False
    error: str = ""
    tasks: dict[int, TaskState] = field(default_factory=dict)
    logs: list[dict] = field(default_factory=list)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class RegistrationEngine:
    """进程级单例：同一时刻只允许一批任务在跑。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._run = _Run()
        self._pool: ThreadPoolExecutor | None = None
        self._watcher: threading.Thread | None = None

    # ------------------------------------------------------------ 状态

    def is_running(self) -> bool:
        with self._lock:
            return self._run.running

    def snapshot(self, *, since_log: int = 0) -> dict:
        """给 GUI 的轮询快照。since_log 用于只取增量日志（按行号）。"""
        with self._lock:
            run = self._run
            logs = run.logs[since_log:] if since_log >= 0 else run.logs
            tasks = [run.tasks[key].to_dict() for key in sorted(run.tasks)]
            counts: dict[str, int] = {}
            for task in tasks:
                counts[task["status"]] = counts.get(task["status"], 0) + 1
            return {
                "kind": run.kind,
                "running": run.running,
                "total": run.total,
                "concurrency": run.concurrency,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "error": run.error,
                "counts": counts,
                "tasks": tasks,
                "logs": logs,
                "log_cursor": len(run.logs),
                "accounts": store.summarize(),
                "stopping": is_stop_requested(),
            }

    def log(self, message: str, *, index: int = 0, level: str = "info") -> None:
        with self._lock:
            self._run.logs.append({
                "ts": _now(), "index": index, "level": level, "message": str(message)[:600],
            })
            if len(self._run.logs) > MAX_LOG_LINES:
                del self._run.logs[: len(self._run.logs) - MAX_LOG_LINES]

    # ------------------------------------------------------------ 启动

    def start_registration(self, config: Config) -> tuple[bool, str]:
        """开始一批注册任务。"""
        problems = config.validate()
        if problems:
            return False, "；".join(problems)

        with self._lock:
            if self._run.running:
                return False, "已有任务在运行，请先停止或等待完成"
            mailbox = self._build_mailbox(config)
            if mailbox is None:
                return False, "邮箱来源初始化失败"
            clear_stop()
            self._run = _Run(
                kind="register",
                total=int(config.register_count),
                concurrency=int(config.concurrency),
                started_at=_now(),
                running=True,
            )
            for index in range(1, int(config.register_count) + 1):
                self._run.tasks[index] = TaskState(index=index, email="")

        self.log(f"开始注册 {config.register_count} 个账号，并发 {config.concurrency}")
        self._pool = ThreadPoolExecutor(
            max_workers=int(config.concurrency), thread_name_prefix="gpt-reg"
        )
        for index in range(1, int(config.register_count) + 1):
            self._pool.submit(self._register_task, config, mailbox, index)
        self._pool.shutdown(wait=False)
        self._start_watcher()
        return True, ""

    def start_liveness(self, config: Config, records: list[store.AccountRecord]) -> tuple[bool, str]:
        """对已有账号批量查活。"""
        targets = [item for item in records if item.access_token]
        if not targets:
            return False, "没有可查活的账号（缺少 access_token）"

        with self._lock:
            if self._run.running:
                return False, "已有任务在运行，请先停止或等待完成"
            clear_stop()
            self._run = _Run(
                kind="liveness",
                total=len(targets),
                # ★ 用独立配置项，不再跟注册并发共用（见 settings.Config 的说明）
                concurrency=max(1, int(config.liveness_concurrency)),
                started_at=_now(),
                running=True,
            )
            for index, record in enumerate(targets, start=1):
                self._run.tasks[index] = TaskState(index=index, email=record.email, status="pending")

        self.log(f"开始查活 {len(targets)} 个账号，并发 {config.liveness_concurrency}")
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(config.liveness_concurrency)), thread_name_prefix="gpt-live"
        )
        for index, record in enumerate(targets, start=1):
            self._pool.submit(self._liveness_task, config, record, index)
        self._pool.shutdown(wait=False)
        self._start_watcher()
        return True, ""

    def start_credential_refresh(self, config: Config) -> tuple[bool, str]:
        """批量刷新 Codex 凭证（refresh_token 换新 AT）。"""
        from .refresh import iter_credential_files

        files = iter_credential_files()
        if not files:
            return False, "还没有 Codex 凭证可刷新（需要先开启「ChatGPT 桌面端激活」并跑成功）"

        with self._lock:
            if self._run.running:
                return False, "已有任务在运行，请先停止或等待完成"
            clear_stop()
            self._run = _Run(
                kind="refresh",
                total=len(files),
                concurrency=max(1, int(config.concurrency)),
                started_at=_now(),
                running=True,
            )
            for index, path in enumerate(files, start=1):
                self._run.tasks[index] = TaskState(
                    index=index, email=path.stem, status="pending"
                )

        self.log(f"开始刷新 {len(files)} 个 Codex 凭证，并发 {config.concurrency}")
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(config.concurrency)), thread_name_prefix="gpt-refresh"
        )
        for index, path in enumerate(files, start=1):
            self._pool.submit(self._refresh_task, config, path, index)
        self._pool.shutdown(wait=False)
        self._start_watcher()
        return True, ""

    # ------------------------------------------------------------ 停止

    def stop(self) -> None:
        request_stop()
        self.log("已收到停止请求，正在等待当前任务退出…", level="warn")

    # ------------------------------------------------------------ 内部

    def _build_mailbox(self, config: Config):
        try:
            from . import mailbox as mailbox_pkg

            # 走共用的构造路径，别在这里硬编码来源 ——
            # 否则「列表能加载、真注册却报 Key 错」，用户只会看到莫名其妙的问题。
            return mailbox_pkg.build_from_config(config)
        except Exception as exc:
            self.log(f"邮箱来源初始化失败：{type(exc).__name__}: {exc}", level="error")
            return None

    def _set_task(self, index: int, **fields) -> None:
        with self._lock:
            task = self._run.tasks.get(index)
            if task is None:
                return
            for key, value in fields.items():
                if hasattr(task, key):
                    setattr(task, key, value)

    def _register_task(self, config: Config, mailbox, index: int) -> None:
        from .registration import register_one

        if is_stop_requested():
            self._set_task(index, status="cancelled", finished_at=_now(), detail="已停止")
            self.log("任务已取消（提交前收到停止请求）", index=index, level="warn")
            return

        self._set_task(index, status="running", started_at=_now(), stage="starting")

        def on_event(stage: str, detail: str) -> None:
            self._set_task(index, stage=stage, detail=detail)
            if detail:
                self.log(detail, index=index)

        def on_email(email: str) -> None:
            # 分到邮箱就立刻写进任务状态，别等整轮结束 ——
            # 否则进度卡片整个注册过程都显示「等待分配邮箱」。
            self._set_task(index, email=email)

        record = register_one(
            config=config, index=index, mailbox=mailbox,
            on_event=on_event, on_email=on_email,
        )

        if record.email:
            self._set_task(index, email=record.email)
        store.append(record)

        status = {"success": "success", "failed": "failed", "cancelled": "cancelled"}.get(
            record.status, "failed"
        )
        self._set_task(
            index,
            status=status,
            finished_at=_now(),
            detail="完成" if status == "success" else (record.error or record.status),
            error=record.error,
            liveness=record.liveness,
            activation=record.activation,
        )
        if status == "success":
            self.log(f"✔ {record.email} 注册成功（{record.liveness}）", index=index)
        elif status == "cancelled":
            self.log(f"■ {record.email or '（未分配邮箱）'} 已取消", index=index, level="warn")
        else:
            self.log(f"✘ {record.email or '（未分配邮箱）'} 失败：{record.error}", index=index, level="error")

    def _liveness_task(self, config: Config, record: store.AccountRecord, index: int) -> None:
        from .liveness import check_liveness

        if is_stop_requested():
            self._set_task(index, status="cancelled", finished_at=_now(), detail="已停止")
            return

        self._set_task(index, status="running", started_at=_now(), stage="liveness")
        status, note, detail = check_liveness(
            access_token=record.access_token,
            account_id=record.account_id,
            proxy=config.proxy,
            timeout=config.liveness_timeout,
        )
        self._set_task(
            index,
            status="success" if status == "alive" else ("failed" if status == "dead" else "unknown"),
            finished_at=_now(), detail=note, liveness=status,
        )
        symbol = {"alive": "✔", "dead": "✘", "unknown": "?"}.get(status, "?")
        level = "info" if status == "alive" else ("error" if status == "dead" else "warn")
        plan = detail.get("plan_type") or ""
        self.log(f"{symbol} {record.email} {status} {plan} {note}", index=index, level=level)

        # ★★ 必须落盘。只更新内存任务状态的话，查活跑完等于没跑：
        #    账号池的「已失效」永远是 0、「删除失效账号」删不掉、
        #    而且**交付门禁看不到结论 ⇒ 查出来的死号照样能被导出卖出去**。
        #    （2026-10-11 补：原来这里一行都没写回 accounts.jsonl。）
        try:
            store.apply_liveness([{
                "key": store.identity_of(record),
                "liveness": status,
                "note": note,
                "plan": plan,
            }])
        except Exception:  # noqa: BLE001
            logger.warning("[查活] 结果写回账号库失败：%s", record.email, exc_info=True)

    def _refresh_task(self, config: Config, path, index: int) -> None:
        from .refresh import refresh_credential_file

        if is_stop_requested():
            self._set_task(index, status="cancelled", finished_at=_now(), detail="已停止")
            return

        self._set_task(index, status="running", started_at=_now(), stage="refresh")
        ok, note = refresh_credential_file(
            path, proxy=config.proxy, timeout=config.oauth_timeout
        )
        self._set_task(
            index,
            status="success" if ok else "failed",
            finished_at=_now(),
            detail=note,
            error="" if ok else note,
        )
        self.log(("✔ " if ok else "✘ ") + note, index=index, level="info" if ok else "error")

    def _start_watcher(self) -> None:
        def _watch() -> None:
            pool = self._pool
            if pool is not None:
                pool.shutdown(wait=True)
            with self._lock:
                self._run.running = False
                self._run.finished_at = _now()
                succeeded = sum(1 for t in self._run.tasks.values() if t.status == "success")
                total = self._run.total
            self.log(f"本批任务结束：成功 {succeeded}/{total}")
            try:
                path = store.export_successful()
                self.log(f"已导出成功账号：{path}")
            except Exception as exc:
                self.log(f"导出失败：{type(exc).__name__}: {exc}", level="warn")

        self._watcher = threading.Thread(target=_watch, name="gpt-watch", daemon=True)
        self._watcher.start()


_ENGINE: RegistrationEngine | None = None
_ENGINE_LOCK = threading.Lock()


def engine() -> RegistrationEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = RegistrationEngine()
        return _ENGINE
