# -*- coding: utf-8 -*-
"""AI 小工具 —— 本地 Web GUI 服务端。

三块功能：注册账号 / 账号池 / 测试生图。
只监听 127.0.0.1，界面在 ui/index.html，数据在 data/ 下。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import paths as paths_mod  # noqa: E402

# ★★ 必须在**任何 cloakbrowser 调用之前**执行（所以放在所有 core 导入的最前面）：
#    把浏览器内核缓存指到「程序目录旁边」，让整个文件夹可以整体搬走 / 预置内核。
#    老用户机器上已经下好的 ~/.cloakbrowser 会继续被沿用，不会让他重新下 150MB。
BROWSER_CACHE_DIR = paths_mod.apply_runtime_env()

from core import store  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core.engine import engine  # noqa: E402
from core.imagegen import IMAGES_DIR  # noqa: E402
from core.paths import data_dir, logs_dir, resource  # noqa: E402

UI_DIR = resource("ui")
SECRET_FIELDS = ("sitepool_tool_key", "remail_api_key", "sms_api_key", "cloak_license_key")

APP_NAME = "AI 小工具"
QUOTA_JOB = "quota"
IMAGE_JOB = "image"

# 额度查询的并发度。它只是 HTTP 调用，不像注册要开浏览器，所以可以比 concurrency 高。
# 上限也会被账号数夹住；走代理时别调太高，避免被上游限速。
QUOTA_CONCURRENCY = 6

# 上游实际用的生图模型名属于内部信息：不进日志、不进 /api/image/state 的 result、
# 也不进 /api/images 的条目。只留在 data/images/*.json 里备查。
_MODEL_ONLY_KEYS = ("model_slug", "model_requested")


def _setup_logging() -> None:
    """控制台 + 文件双写。文件日志方便小白出问题时直接发过来排查。"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "app.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


_setup_logging()
logger = logging.getLogger("gui")

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ 模型


class ConfigPatch(BaseModel):
    mailbox_provider: str | None = None
    sitepool_base: str | None = None
    sitepool_tool_key: str | None = None
    sitepool_phone_sku: str | None = None
    remail_api_key: str | None = None
    mailbox_target_id: str | None = None
    register_count: int | None = None
    concurrency: int | None = None
    proxy: str | None = None
    headless: bool | None = None
    cloak_license_key: str | None = None
    page_load_timeout: int | None = None
    humanize: bool | None = None
    geoip: bool | None = None
    remail_api_base: str | None = None
    mailbox_service_mode: str | None = None
    mailbox_supply_policy: str | None = None
    mailbox_project: str | None = None
    mailbox_suffix: str | None = None
    otp_timeout: int | None = None
    enable_activation: bool | None = None
    sms_provider: str | None = None
    sms_api_key: str | None = None
    sms_service: str | None = None
    sms_country: str | None = None
    sms_max_price: str | None = None
    sms_max_retries: int | None = None
    sms_code_wait: int | None = None
    oauth_timeout: int | None = None
    liveness_check: bool | None = None
    liveness_timeout: int | None = None
    liveness_concurrency: int | None = None
    image_timeout: int | None = None


class ImportBody(BaseModel):
    text: str = ""


class KeysBody(BaseModel):
    keys: list[str] = []


class ImageBody(BaseModel):
    prompt: str = ""
    key: str = ""
    aspect: str = ""


# ------------------------------------------------------------------ 静态


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> Response:
    # 204 不能带 body。之前这里返回 JSONResponse({}, 204)，浏览器拿到声明
    # Content-Length 却没有对应数据，h11 会抛 "Too much data for declared
    # Content-Length" 并掐断连接。
    return Response(status_code=204)


# ------------------------------------------------------------------ 配置


def _mask(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:4]}{'•' * 8}{text[-4:]}"


def _config_view() -> dict:
    config = settings_mod.load()
    data = config.to_dict()
    # 密钥不明文回传；空字符串表示「未修改」。
    for field in SECRET_FIELDS:
        data[f"{field}_mask"] = _mask(data.get(field, ""))
        data[field] = ""
    return data


@app.get("/api/config")
def get_config() -> dict:
    return {"ok": True, "config": _config_view()}


@app.post("/api/config")
def update_config(patch: ConfigPatch) -> dict:
    payload = {key: value for key, value in patch.model_dump().items() if value is not None}
    # 密钥留空 = 保持原值。
    for field in SECRET_FIELDS:
        if field in payload and not str(payload[field]).strip():
            payload.pop(field)
    config = settings_mod.update(payload)
    # 用户可能刚开了/关了代理软件，系统代理缓存要跟着刷新。
    from core import netproxy

    netproxy.refresh()
    problems = config.validate()
    detected = netproxy.resolve(config.proxy)
    return {"ok": True, "config": _config_view(), "problems": problems,
            "proxy_in_use": detected}


# ------------------------------------------------------------------ 邮箱目标


def _mailbox_provider():
    """按配置选邮箱来源：默认本站号池，也保留直连 Remail 的老路。

    构造逻辑统一放在 core.mailbox.build_from_config —— 后台引擎用的是同一个函数，
    避免「加载目标能走本站、真注册却去打 Remail」这种两端漂移。
    """
    config = settings_mod.load()
    from core import mailbox as mailbox_pkg

    try:
        return mailbox_pkg.build_from_config(config)
    except Exception as exc:
        # MailboxError 之类的自定义异常，类名前缀对用户没意义，去掉。
        raise HTTPException(status_code=400, detail=str(exc) or "邮箱配置无效") from exc


def _mailbox_call(fn):
    """跑一次 Remail 调用，把异常翻译成小白看得懂的话。"""
    from core.mailbox.base import MailboxError

    try:
        return fn()
    except HTTPException:
        raise
    except MailboxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/api/mailbox/status")
def mailbox_status() -> dict:
    """读 Remail 账户状态（余额/额度），不发单。"""
    return {"ok": True, "status": _mailbox_call(lambda: _mailbox_provider().status())}


@app.get("/api/mailbox/targets")
def mailbox_targets(project: str = "", suffix: str = "", all: int = 0) -> dict:
    """列出可用邮箱目标（项目 × 后缀 × 库存 × 单价），供下拉框使用。

    默认按配置里的 ``mailbox_project`` / ``mailbox_suffix`` 收窄（chatgpt + outlook.com），
    这样只需要 2 次上游请求；``all=1`` 才全量拉（60+ 项目，几十秒）。
    """
    config = settings_mod.load()
    if all:
        project_filter = suffix_filter = ""
    else:
        project_filter = project or config.mailbox_project
        suffix_filter = suffix or config.mailbox_suffix
    targets = _mailbox_call(lambda: _mailbox_provider().targets(
        project_filter=project_filter, suffix_filter=suffix_filter))
    return {
        "ok": True,
        "targets": targets,
        "filters": {"project": project_filter, "suffix": suffix_filter},
    }


# ------------------------------------------------------------------ 注册任务


@app.get("/api/state")
def state(since_log: int = 0) -> dict:
    return {"ok": True, **engine().snapshot(since_log=max(0, since_log))}


@app.post("/api/register/start")
def register_start() -> dict:
    config = settings_mod.load()
    ok, message = engine().start_registration(config)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True}


@app.post("/api/register/stop")
def register_stop() -> dict:
    engine().stop()
    return {"ok": True}


# ------------------------------------------------------------------ 账号池


def _account_view(record: store.AccountRecord) -> dict:
    data = record.to_dict()
    # 前端不需要完整 token，留个尾巴做区分就够了，避免整条密钥进浏览器历史。
    token = str(data.pop("access_token", "") or "")
    data["access_token_tail"] = token[-12:] if token else ""
    data["has_token"] = bool(token)
    data["key"] = store.identity_of(record)
    data["refresh_token"] = "有" if data.get("refresh_token") else ""
    data["id_token"] = "有" if data.get("id_token") else ""
    return data


@app.get("/api/accounts")
def accounts() -> dict:
    records = store.load_all()
    records.reverse()
    return {
        "ok": True,
        "items": [_account_view(item) for item in records],
        "summary": store.summarize(),
    }


@app.post("/api/accounts/import")
def accounts_import(body: ImportBody) -> dict:
    """粘贴导入：session JSON / token 数组 / 裸 token 都行。"""
    from core import importer

    parsed, notes = importer.parse(body.text, source="粘贴导入")
    if not parsed:
        raise HTTPException(status_code=400, detail="；".join(notes) or "没解析出账号")

    records: list[store.AccountRecord] = []
    for item in parsed:
        records.append(store.AccountRecord(
            email=item.get("email", ""),
            status="success",
            access_token=item.get("access_token", ""),
            refresh_token=item.get("refresh_token", ""),
            id_token=item.get("id_token", ""),
            account_id=item.get("account_id", ""),
            expires_at=item.get("expires_at", ""),
            plan=item.get("plan", ""),
            mailbox=item.get("source", "导入"),
            liveness="unknown",
        ))
    result = store.upsert(records)
    result["notes"] = notes
    return {"ok": True, **result}


@app.delete("/api/accounts")
def accounts_delete(body: KeysBody) -> dict:
    if engine().is_running():
        raise HTTPException(status_code=409, detail="注册任务运行中，无法删除")
    removed = store.remove(body.keys)
    return {"ok": True, "removed": removed}


@app.post("/api/accounts/clear")
def accounts_clear() -> dict:
    if engine().is_running():
        raise HTTPException(status_code=409, detail="任务运行中，无法清空")
    store.clear()
    return {"ok": True}


def _jwt_exp_iso(token: str) -> str:
    """从 JWT 里取 exp，返回 ISO8601（UTC）。取不到返回空串。"""
    try:
        import base64

        payload = str(token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
        exp = claims.get("exp")
        if not exp:
            return ""
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(exp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return ""


def _load_codex_credentials(record) -> dict | None:
    """读 data/codex_credentials/<email>.json；没有就返回 None。"""
    from core.oauth import CREDENTIAL_DIR

    email = str(record.email or "")
    if not email:
        return None
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", email)
    path = CREDENTIAL_DIR / f"{safe}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/delivery/export")
def delivery_export(keys: str = "") -> FileResponse:
    """导出**可交付凭证**：一个纯文本，一行一个账号。

        邮箱----密码----2FA密钥

    ★ 2026-10-05 改（老大要求）：原来是 zip（`credentials.csv` + 二维码 PNG + `README.txt`），
      **现在只要一个 .txt**，字段用四个短横线分隔，**otpauth 链接也去掉了**。
      缺的字段写「无」而不是留空 —— 空字段在 `----` 分隔里读不出边界。

    和 `/api/accounts/export` 的区别：那个是给 sub2api 导入用的内部格式（JSON），
    这个是**给买家看的**。

    ``keys`` 传逗号分隔的 key 时只导这几个；不传就导全部「注册成功」的。

    ★ 2026-10-06：**只导「买家能自己登录」的号**（邮箱 + 密码；绑了 2FA 就必须
      带得出密钥）。只有邮箱、没有密码的旧号一律跳过 —— 买家拿在手里登不进去。
      它们仍然留在 `accounts.jsonl` 里给运营自用。
      判定规则只写在 `core/credential_export.deliverable` 一处，前后端不会漂移；
      跳过数量通过响应头 `X-Delivery-Skipped` 返回，前端可以据此提示。
    """
    from core import credential_export

    wanted = {item.strip() for item in str(keys or "").split(",") if item.strip()}
    records = [r for r in store.load_all() if r.ok]
    if wanted:
        records = [r for r in records if store.identity_of(r) in wanted]

    picked, skipped = credential_export.split_deliverable(records)
    # 有密钥/密码的先排前面：买家实际最常用到这些
    picked.sort(key=lambda r: (not getattr(r, "totp_secret", ""), not getattr(r, "password", "")))

    if skipped:
        reasons: dict[str, int] = {}
        for _record, reason in skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        logger.info("[交付] 跳过 %d 个不可交付的号：%s", len(skipped), reasons)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = os.path.join(tempfile.gettempdir(), f"delivery-{stamp}.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(credential_export.export_text(picked))
    logger.info("[交付] 导出 %d 行 -> %s", len(picked), path)
    return FileResponse(
        path,
        filename=f"凭证包-{stamp}.txt",
        media_type="text/plain; charset=utf-8",
        headers={
            "X-Delivery-Rows": str(len(picked)),
            "X-Delivery-Skipped": str(len(skipped)),
        },
    )


@app.get("/api/accounts/export")
def accounts_export(keys: str = "", format: str = "sub2api-data") -> FileResponse:
    """导出账号。

    三种格式（``format=``）：

    ``sub2api-data``（默认）：sub2api 后台 **账号管理 → 更多操作 → 导入** 认的备份格式。
        顶层是对象、``proxies``/``accounts`` 必须是数组，缺一个前端就直接判
        「不是受支持的导出数据文件」。这是最省事的一条路。

    ``sub2api``：sub2api 的 **Codex 会话导入**格式（一行一条 JSON）。
        入口是 新建账号 → OpenAI → OAuth → 「Codex JSON / AT 批量输入」，是**粘贴**不是传文件。

    ``raw``：小工具自己的整条账号记录。

    ``keys`` 传逗号分隔的 key 时只导这几个；不传就导全部「注册成功」的。
    """
    wanted = {item.strip() for item in str(keys or "").split(",") if item.strip()}
    records = [record for record in store.load_all() if record.ok]
    if wanted:
        records = [record for record in records if store.identity_of(record) in wanted]

    fmt = str(format or "sub2api-data").strip().lower()

    if fmt == "raw":
        store.EXPORT_PATH.write_text(
            json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return FileResponse(store.EXPORT_PATH, filename="accounts_raw.json",
                            media_type="application/json")

    if fmt == "sub2api":
        entries: list[dict] = []
        missing: list[str] = []
        for record in records:
            cred = _load_codex_credentials(record)
            if cred:
                entries.append(cred)
            else:
                missing.append(record.email or store.identity_of(record))
        out = data_dir() / "accounts_sub2api_codex_session.json"
        # 一行一条（NDJSON）：sub2api 的解析器对 NDJSON 有逐行兜底，最抗粘贴事故。
        out.write_text(
            "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)
            + ("\n" if entries else ""),
            encoding="utf-8",
        )
        logger.info("[导出] Codex 会话格式：%d 条，跳过 %d 条（未接码）", len(entries), len(missing))
        if missing:
            logger.info("[导出] 被跳过：%s", "、".join(missing[:10]))
        return FileResponse(out, filename="sub2api-codex-session.json",
                            media_type="application/json")

    # 默认：sub2api 备份（数据导入）格式
    from datetime import datetime, timezone

    from core.oauth import CODEX_CLIENT_ID

    accounts: list[dict] = []
    missing: list[str] = []
    for record in records:
        cred = _load_codex_credentials(record)
        if not cred:
            missing.append(record.email or store.identity_of(record))
            continue
        access_token = str(cred.get("access_token") or "")
        credentials = {
            "access_token": access_token,
            "refresh_token": str(cred.get("refresh_token") or ""),
            "id_token": str(cred.get("id_token") or ""),
            "client_id": CODEX_CLIENT_ID,
            "expires_at": _jwt_exp_iso(access_token) or str(cred.get("expired") or ""),
            "email": str(cred.get("email") or record.email or ""),
            "chatgpt_account_id": str(cred.get("account_id") or ""),
        }
        # 空值不写：sub2api 那边 refresh_token 为空会走「无续期」分支并警告。
        credentials = {k: v for k, v in credentials.items() if str(v).strip()}
        accounts.append({
            "name": record.email or store.identity_of(record),
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
            "concurrency": 3,
            "priority": 50,
        })

    payload = {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
    }
    out = data_dir() / "accounts_sub2api_data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[导出] sub2api 数据导入格式：%d 个账号，跳过 %d 个（未接码）",
                len(accounts), len(missing))
    if missing:
        logger.info("[导出] 被跳过：%s", "、".join(missing[:10]))
    return FileResponse(out, filename="sub2api-data.json", media_type="application/json")


@app.post("/api/accounts/liveness")
def accounts_liveness() -> dict:
    config = settings_mod.load()
    records = store.load_all()
    if not records:
        raise HTTPException(status_code=400, detail="还没有账号")
    ok, message = engine().start_liveness(config, records)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True}


@app.post("/api/credentials/refresh")
def credentials_refresh() -> dict:
    """批量刷新 Codex 凭证（refresh_token 换新 AT）。"""
    config = settings_mod.load()
    ok, message = engine().start_credential_refresh(config)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True}


# ------------------------------------------------------------------ 额度


def _resolve_keys(keys: list[str] | None, *, only_usable: bool = True) -> list[store.AccountRecord]:
    records = store.load_all()
    wanted = {str(item or "").strip() for item in (keys or []) if str(item or "").strip()}
    out: list[store.AccountRecord] = []
    for record in records:
        if wanted and store.identity_of(record) not in wanted:
            continue
        if not record.access_token:
            continue
        if only_usable and not record.ok:
            continue
        out.append(record)
    return out


def _quota_worker(job, targets: list[store.AccountRecord]) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from core import quota

    config = settings_mod.load()
    job.progress(0, len(targets))
    if not targets:
        job.log("没有可查询的账号")
        return

    # ★ 额度查询是互不依赖的 HTTP 调用，串行做 N 个账号就是 N × 单次延迟
    #（走代理时一次好几秒，几十个账号要等到天荒地老）。这里并发跑，
    # 进度按「完成数」推进；job.log / job.progress 内部有锁，多线程安全。
    workers = max(1, min(QUOTA_CONCURRENCY, len(targets)))
    job.log(f"开始查询 {len(targets)} 个账号的生图额度（并发 {workers}）")

    updates: list[dict] = []
    guard = threading.Lock()
    finished = 0

    def check_one(record: store.AccountRecord) -> None:
        nonlocal finished
        if job.cancel.is_set():
            return
        key = store.identity_of(record)
        label = record.email or key
        token = record.access_token
        if quota.token_expired(token):
            job.log(f"{label} → token 本地已过期")
            item = {"key": key, "quota": -1, "note": "token 已过期", "dead": True}
        else:
            try:
                result = quota.fetch(token, proxy=config.proxy, timeout=config.liveness_timeout)
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if result.get("ok"):
                note = str(result.get("note") or "")
                extra = f"，{result['restore_at']} 重置" if result.get("restore_at") else ""
                job.log(f"{label} → {note}{extra}")
                item = {
                    "key": key,
                    "quota": int(result.get("quota", -1)),
                    "quota_unknown": bool(result.get("quota_unknown")),
                    "note": note,
                    "plan": result.get("plan", ""),
                    "checked_at": result.get("checked_at", ""),
                    "dead": False,
                }
            else:
                job.log(f"{label} → 失败：{result.get('error')}")
                item = {"key": key, "quota": -1,
                        "note": str(result.get("error") or "查询失败"),
                        "dead": bool(result.get("dead"))}
        with guard:
            updates.append(item)
            finished += 1
            job.progress(finished, len(targets))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quota") as pool:
        futures = [pool.submit(check_one, record) for record in targets]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[额度] 单账号查询线程异常：%s", exc)
            if job.cancel.is_set():
                job.log("已取消（已在跑的几个请求会自然结束）")
                break

    store.apply_quota(updates)
    usable = sum(1 for item in updates if int(item.get("quota", -1)) > 0)
    job.result.update({"checked": len(updates), "usable": usable})


@app.post("/api/quota/check")
def quota_check(body: KeysBody) -> dict:
    from core import jobs

    records = _resolve_keys(body.keys)
    if not records:
        raise HTTPException(status_code=400, detail="没有可查询的账号")
    if jobs.is_running(QUOTA_JOB):
        raise HTTPException(status_code=409, detail="额度查询正在进行中")

    def worker(job):
        _quota_worker(job, records)

    ok, message = jobs.start(QUOTA_JOB, worker)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True}


@app.get("/api/quota/state")
def quota_state(since_log: int = 0) -> dict:
    from core import jobs

    return {"ok": True, **jobs.task(QUOTA_JOB).snapshot(since_log)}


@app.post("/api/quota/stop")
def quota_stop() -> dict:
    from core import jobs

    jobs.stop(QUOTA_JOB)
    return {"ok": True}


# ------------------------------------------------------------------ 生图


@app.get("/api/image/state")
def image_state(since_log: int = 0) -> dict:
    from core import jobs

    return {"ok": True, **jobs.task(IMAGE_JOB).snapshot(since_log)}


@app.post("/api/image/generate")
def image_generate(body: ImageBody) -> dict:
    from core import jobs

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="请先写提示词")
    if jobs.is_running(IMAGE_JOB):
        raise HTTPException(status_code=409, detail="正在生成中，请稍候")

    targets = _resolve_keys([body.key] if body.key else [])
    if not targets:
        raise HTTPException(status_code=400, detail="没有可用账号，请先注册或导入账号")
    record = targets[0]

    config = settings_mod.load()
    aspect = (body.aspect or "").strip()
    if aspect:
        prompt = f"{prompt}\n\n（画面比例 {aspect}）"

    def worker(job):
        from core import imagegen

        job.log(f"使用账号 {record.email or store.identity_of(record)}")

        def on_event(stage: str, detail: str = "") -> None:
            job.log(detail or stage)

        try:
            # 模型固定 auto：服务端自己路由，总是当前可用的最新生图模型。
            result = imagegen.generate(
                record.access_token, prompt,
                proxy=config.proxy, model="auto",
                on_event=on_event, timeout=config.image_timeout,
            )
        except Exception as exc:
            job.result["error"] = str(exc)
            job.log(f"失败：{exc}")
            # ★ 2026-10-11 加：token 结构没问题、也没过期，却被服务端 401 拒绝
            #   ⇒ 这个号已被吊销/封禁（实测：机房 IP 注册出来的号当场就死）。
            #   **必须当场落盘标记失效** —— 否则它还会被当成「可交付」账号
            #   导出卖出去，买家登不上就来找你退钱。
            if getattr(exc, "revoked", False):
                try:
                    changed = store.mark_dead(
                        [store.identity_of(record)],
                        "生图被拒 401（token 已被吊销，多半账号已封）")
                    if changed:
                        job.log("已把这个号标记为「已失效」，它不会再进入交付包")
                except Exception:  # noqa: BLE001
                    logger.warning("[生图] 标记账号失效失败", exc_info=True)
            raise
        job.result.update({k: v for k, v in result.items() if k not in _MODEL_ONLY_KEYS})
        first = (result.get("images") or [{}])[0]
        job.log(f"完成：{first.get('width')}×{first.get('height')}")
        job.progress(1, 1)

    ok, message = jobs.start(IMAGE_JOB, worker)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "account": record.email or store.identity_of(record)}


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}


@app.get("/api/images")
def image_list() -> dict:
    """按时间倒序列出已生成的图片。"""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        (item for item in IMAGES_DIR.iterdir()
         if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    out = []
    for item in files[:80]:
        meta = {}
        # 图片名为 ``20261002-153000-1.png``，元数据是同前缀的 ``20261002-153000.json``。
        sidecar = IMAGES_DIR / f"{item.stem.rsplit('-', 1)[0]}.json"
        if sidecar.exists():
            try:
                import json

                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        first = (meta.get("images") or [{}])[0] if isinstance(meta.get("images"), list) else {}
        out.append({
            "name": item.name,
            "url": f"/api/images/{item.name}",
            "size": item.stat().st_size,
            "mtime": int(item.stat().st_mtime),
            "prompt": meta.get("prompt", ""),
            "width": first.get("width") or 0,
            "height": first.get("height") or 0,
        })
    return {"ok": True, "items": out, "dir": str(IMAGES_DIR)}


@app.get("/api/images/{name}")
def image_file(name: str) -> FileResponse:
    # 只允许取 images 目录下的单层文件名，杜绝 ../ 穿越。
    safe = Path(name).name
    if not safe or safe != name or Path(safe).suffix.lower() not in IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail="文件名不合法")
    path = IMAGES_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path, media_type=IMAGE_MEDIA.get(path.suffix.lower(), "application/octet-stream"))


# ------------------------------------------------------------------ 杂项


@app.get("/health")
def health() -> dict:
    return {"ok": True, "running": engine().is_running()}


def _proxy_in_use() -> str:
    """当前实际会用的出口。空串表示直连。"""
    from core import netproxy

    try:
        return netproxy.resolve(settings_mod.load().proxy)
    except Exception:
        return ""


@app.get("/api/meta")
def meta(probe: int = Query(default=0)) -> dict:
    """一次性拿到界面初始化需要的所有东西。"""
    from core import jobs

    return {
        "ok": True,
        "app": APP_NAME,
        "data_dir": str(data_dir()),
        "images_dir": str(IMAGES_DIR),
        "proxy_in_use": _proxy_in_use(),
        "browser_dir": BROWSER_CACHE_DIR,
        "register_running": engine().is_running(),
        "jobs": {
            "quota": jobs.task(QUOTA_JOB).running,
            "image": jobs.task(IMAGE_JOB).running,
        },
    }


# ------------------------------------------------------------ 开跑前自检


@app.get("/api/preflight")
def preflight(deep: int = 0) -> dict:
    """开跑前自检。

    ``deep=0``（默认）只做本地快检：工具密钥 + 浏览器内核是否就位。毫秒级，
    启动时前端会自动跑一次。
    ``deep=1`` 才联网并真启动一次浏览器：+ 号池余额 + 代理出口 + 浏览器能不能起来。

    ★ 内核没下载完时，深检**不会**去启动浏览器 —— 一启动就会开始下 150MB，
      用户会以为程序卡死。那种情况只报「内核未就绪」，引导走下面的下载接口。
    """
    from core import preflight as preflight_mod

    try:
        result = preflight_mod.run(settings_mod.load(), deep=bool(deep))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[自检] 整体失败：%s", exc)
        raise HTTPException(status_code=500, detail=f"自检失败：{exc}") from exc
    return {"ok": True, **result}


@app.post("/api/preflight/kernel")
def preflight_download_kernel() -> dict:
    """下载浏览器内核（首次运行要的那 ~150MB）。

    ★ 这一步可能要几分钟。前端必须显示「正在下载，请耐心等」，
      否则用户会当成卡死然后强杀进程。
    已经就绪时直接返回，不会重复下载。
    """
    from core import preflight as preflight_mod

    ready, detail = preflight_mod.kernel_present()
    if ready:
        logger.info("[自检] 内核已就绪，跳过下载：%s", detail)
        return {"ok": True, "already": True, "detail": detail}

    try:
        from cloakbrowser.download import ensure_binary
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"cloakbrowser 不可用，无法下载内核：{exc}") from exc

    logger.info("[自检] 开始下载浏览器内核 -> %s", BROWSER_CACHE_DIR)
    try:
        path = ensure_binary(
            license_key=str(settings_mod.load().cloak_license_key or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[自检] 内核下载失败：%s", exc)
        raise HTTPException(
            status_code=502,
            detail=("下载内核失败：" + str(exc)[:300] +
                    "。常见原因：网络不通（主源 cloakbrowser.dev，回退 GitHub）、"
                    "被安全软件拦截、磁盘空间不够。可换网络后重试。"),
        ) from exc

    ready, detail = preflight_mod.kernel_present()
    if not ready:
        raise HTTPException(status_code=502,
                            detail="下载流程结束但没找到内核，请重试或换网络")
    logger.info("[自检] 内核就绪：%s -> %s", detail, path)
    return {"ok": True, "already": False, "detail": detail, "path": str(path)}


# ------------------------------------------------------------------ 启动


def _open_browser_later(url: str) -> None:
    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Timer(1.2, _open).start()


def _pick_free_port(host: str, preferred: int, tries: int = 20) -> tuple[int, bool]:
    """找一个能绑上的端口，返回 ``(端口, 是否被迫换了端口)``。

    ★ 小白「打不开界面」最常见的原因之一，就是 8770 被上一个没关干净的进程占着，
      而 uvicorn 只会抛一段英文栈（黑窗一闪或者满屏红字）。
      这里直接往后让路，并把**实际**地址打印出来 / 用它打开浏览器。
    """
    import socket

    for offset in range(max(1, tries)):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port, offset != 0
    return preferred, False


def main() -> None:
    import uvicorn

    host = os.getenv("GRL_HOST", "127.0.0.1").strip() or "127.0.0.1"
    preferred = int(os.getenv("GRL_PORT", "8770"))
    port, moved = _pick_free_port(host, preferred)
    url = f"http://{host}:{port}"

    print("=" * 62)
    print(f"  {APP_NAME}")
    print(f"  界面地址：{url}")
    if moved:
        print(f"  （{preferred} 端口被占用，已自动改用 {port}）")
    print(f"  数据目录：{data_dir()}")
    print(f"  浏览器内核：{BROWSER_CACHE_DIR}")
    print("  关闭本窗口即停止程序")
    print("=" * 62)
    if os.getenv("GRL_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes"}:
        _open_browser_later(url)

    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
