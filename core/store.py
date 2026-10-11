# -*- coding: utf-8 -*-
"""账号落库：一行一个 JSON 对象，追加写。

为什么不用 SQLite：这个程序的产出就是「一堆账号」，用户会直接拷走文件。
JSONL 可以边跑边看、能被 Excel/脚本直接读、坏了也只坏一行。原项目的
``core/db.py`` 有 144KB，绝大部分是邮箱池、迁移和查询分页，这里用不上。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone

from .paths import data_dir

logger = logging.getLogger(__name__)

DATA_DIR = data_dir()
ACCOUNTS_PATH = DATA_DIR / "accounts.jsonl"
EXPORT_PATH = DATA_DIR / "accounts_export.json"

_LOCK = threading.RLock()


@dataclass
class AccountRecord:
    """一个注册任务的结果。字段按「用户真正会用到」排序。"""

    email: str = ""
    status: str = "pending"          # pending/success/failed/cancelled
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    account_id: str = ""
    expires_at: str = ""

    password: str = ""               # 注册流程中设置的 OpenAI 密码
    totp_secret: str = ""            # 绑定的 TOTP 2FA 密钥（base32，自己实现、不依赖 pyotp）
    recovery_codes: str = ""         # 2FA 恢复码（上游给了才填；换行分隔）
    inbox_token: str = ""            # Remail 订单取件凭证，重取码/换绑时要用
    mailbox: str = ""                # 邮箱来源标识
    proxy: str = ""

    liveness: str = "unknown"        # alive/dead/unknown
    liveness_note: str = ""
    plan: str = ""                   # 查活时顺带读到的套餐类型
    activation: str = "skipped"      # skipped/running/success/failed
    activation_note: str = ""
    # 「注册后设密码 + 绑 2FA」的结果。跟 activation 分开记：
    # 注册成功的号本来就该能二次登录，这个加固失败不该把整条记录判死。
    security: str = "skipped"        # skipped/success/failed
    security_note: str = ""

    quota: int = -1                  # 生图剩余额度；-1 表示还没查过
    quota_note: str = ""
    quota_at: str = ""

    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "AccountRecord":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in (raw or {}).items() if key in known})

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def append(record: AccountRecord) -> AccountRecord:
    """追加一条记录。"""
    with _LOCK:
        _ensure_dir()
        with ACCOUNTS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return record


def load_all() -> list[AccountRecord]:
    """读全部记录；损坏的行跳过，不让一行坏数据毁掉整个列表。"""
    with _LOCK:
        if not ACCOUNTS_PATH.exists():
            return []
        out: list[AccountRecord] = []
        for line in ACCOUNTS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("[落库] 跳过损坏的记录行")
                continue
            if isinstance(payload, dict):
                out.append(AccountRecord.from_dict(payload))
        return out


def summarize() -> dict:
    """给 GUI 用的统计。"""
    records = load_all()
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    return {
        "total": len(records),
        "success": counts.get("success", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "alive": sum(1 for item in records if item.liveness == "alive"),
        "activated": sum(1 for item in records if item.activation == "success"),
        "path": str(ACCOUNTS_PATH),
    }


def export_successful() -> Path:
    """把注册成功的账号导出成一个 JSON 文件，方便直接打包带走。"""
    records = [item for item in load_all() if item.ok]
    with _LOCK:
        _ensure_dir()
        EXPORT_PATH.write_text(
            json.dumps([item.to_dict() for item in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return EXPORT_PATH


def clear() -> None:
    """清空账号库（GUI 上的「清空列表」）。"""
    with _LOCK:
        if ACCOUNTS_PATH.exists():
            ACCOUNTS_PATH.unlink()


def identity_of(record: AccountRecord | dict) -> str:
    """一条账号的唯一标识：优先邮箱，其次 account_id，最后取 token 尾部。

    用 token 尾部而不是整条 token 做键，是为了让日志和前端传参不出现长密钥。

    ★ 三样都没有的记录（比如在「分配邮箱」就失败、连邮箱都没拿到）以前返回空串，
      而 :func:`remove` 会把空键丢掉 —— 那条记录在界面上勾得中、确认框也弹，
      但永远删不掉，只会提示「已删除 0 个」。这里补一个稳定的兜底键。
    """
    data = record.to_dict() if isinstance(record, AccountRecord) else dict(record or {})
    email = str(data.get("email") or "").strip().lower()
    if email:
        return email
    account_id = str(data.get("account_id") or "").strip()
    if account_id:
        return account_id
    token = str(data.get("access_token") or "").strip()
    if token:
        return token[-24:]
    # 兜底键 = 创建时间 + 整条记录的内容指纹。
    # 用内容指纹而不是随机值，是为了让「同一条失败记录」每次算出来都一样，
    # upsert 的按身份去重才不会失效。
    stamp = str(data.get("created_at") or "").strip() or "na"
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return "anon:%s:%s" % (stamp, hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12])


def save_all(records: list[AccountRecord]) -> None:
    """整体重写账号库（导入、刷新额度、删除时用）。"""
    with _LOCK:
        _ensure_dir()
        tmp = ACCOUNTS_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        tmp.replace(ACCOUNTS_PATH)


def upsert(records: list[AccountRecord]) -> dict:
    """按 identity 合并一批记录：已存在的补空字段，不存在的新增。

    为什么是「补空」而不是「覆盖」：导入进来的 session 往往没有密码、
    没有邮箱订单凭证，不能把注册流程辛苦记下来的字段冲掉。
    """
    added = 0
    updated = 0
    with _LOCK:
        existing = load_all()
        index = {identity_of(item): item for item in existing}
        order = [identity_of(item) for item in existing]

        for record in records:
            key = identity_of(record)
            if not key:
                continue
            current = index.get(key)
            if current is None:
                order.append(key)
                index[key] = record
                added += 1
                continue
            changed = False
            for field_name, value in record.to_dict().items():
                if not value:
                    continue
                if field_name in {"created_at", "status", "liveness", "liveness_note",
                                  "activation", "activation_note"}:
                    continue
                if not getattr(current, field_name, ""):
                    setattr(current, field_name, value)
                    changed = True
            if changed:
                updated += 1

        save_all([index[key] for key in order])
    return {"added": added, "updated": updated, "total": len(order)}


def remove(keys: list[str]) -> int:
    """按 identity 删除账号。"""
    wanted = {str(item or "").strip() for item in (keys or []) if str(item or "").strip()}
    if not wanted:
        return 0
    with _LOCK:
        records = load_all()
        kept = [item for item in records if identity_of(item) not in wanted]
        removed = len(records) - len(kept)
        if removed:
            save_all(kept)
        return removed


def mark_dead(keys: list[str], note: str) -> int:
    """把账号标记为「已失效」并落盘。返回改动的条数。

    ★ 为什么必须走这个函数、而不是就地改字段就完事：
      ``credential_export.deliverable()`` 会把 ``liveness == "dead"`` 的号
      **排除出交付包**。不落盘标记的话，一个已经被封的号**照样会被导出卖出去**
      （2026-10-11 实测踩到：机房 IP 注册出来的号当场被封，生图只报一句
      看不懂的 401，而这个号仍然算「可交付」）。

    调用点：查活、查额度、以及生图时服务端 401 拒绝 token
    （后者见 imagegen.ImageGenError.revoked）。

    时间戳写进 ``liveness_note``，**不新增字段** —— `accounts.jsonl` 还被别的
    工具读，能不加字段就不加。
    """
    wanted = {str(k or "").strip() for k in (keys or []) if str(k or "").strip()}
    if not wanted:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _LOCK:
        records = load_all()
        touched = 0
        for record in records:
            if identity_of(record) not in wanted:
                continue
            was = record.liveness
            record.liveness = "dead"
            base = str(note or "").strip() or "已失效"
            record.liveness_note = f"{base}（{stamp}）"
            if was != "dead":
                touched += 1
                logger.info("[账号] 标记为已失效：%s —— %s", identity_of(record), base)
        if touched:
            save_all(records)
        return touched


def apply_liveness(updates: list[dict]) -> int:
    """把**查活**结果写回账号库。``updates`` 每项需含 ``key`` 与 ``liveness``。

    ★ 为什么必须有它（2026-10-11 查出来的第二个洞）：

      ``engine._liveness_task`` 原来只更新**引擎的内存任务状态**再打一行日志，
      **一行都没写回 accounts.jsonl**。后果是「查活」跑完等于没跑：

        · 账号池的「已失效」统计永远是 0
        · 「删除失效账号」按 ``liveness === "dead"`` 过滤 —— 永远删不掉
        · ★ 交付门禁（credential_export.deliverable 排除 liveness=dead）
          也看不到查活结论 ⇒ **查出来的死号照样能被导出卖出去**

      换句话说：缺个测活按钮只是不方便，缺落盘才是真漏钱。

    和 apply_quota 的分工：那条路是「查额度顺带判死活」，这条是「专门查活」。
    """
    wanted: dict[str, dict] = {}
    for item in (updates or []):
        key = str(item.get("key") or "").strip()
        if key:
            wanted[key] = item
    if not wanted:
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _LOCK:
        records = load_all()
        touched = 0
        for record in records:
            patch = wanted.get(identity_of(record))
            if not patch:
                continue
            status = str(patch.get("liveness") or "unknown").strip().lower()
            if status not in ("alive", "dead", "unknown"):
                status = "unknown"
            note = str(patch.get("note") or "").strip()
            record.liveness = status
            record.liveness_note = f"{note}（{stamp}）" if note else f"查活（{stamp}）"
            if patch.get("plan"):
                record.plan = str(patch["plan"])
            touched += 1
        if touched:
            save_all(records)
        return touched


def apply_quota(updates: list[dict]) -> int:
    """把查到的额度写回账号库。``updates`` 每项需含 ``key`` 字段。"""
    if not updates:
        return 0
    table = {str(item.get("key") or ""): item for item in updates if item.get("key")}
    with _LOCK:
        records = load_all()
        touched = 0
        for record in records:
            patch = table.get(identity_of(record))
            if not patch:
                continue
            record.quota = int(patch.get("quota", -1))
            record.quota_note = str(patch.get("note") or "")
            record.quota_at = str(patch.get("checked_at") or "")
            if patch.get("plan"):
                record.plan = str(patch["plan"])
            # 查额度顺带能确认 token 死没死，比 liveness 更省事。
            if patch.get("dead"):
                record.liveness = "dead"
                record.liveness_note = record.quota_note or "token 已失效"
            elif record.quota >= 0 or patch.get("quota_unknown"):
                record.liveness = "alive"
                if record.liveness_note in ("", "token 已失效"):
                    record.liveness_note = record.quota_note
            touched += 1
        if touched:
            save_all(records)
        return touched
