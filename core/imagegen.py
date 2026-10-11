# -*- coding: utf-8 -*-
"""ChatGPT 官网生图（纯协议，不需要 Codex OAuth）。

走的是 ChatGPT 官网图片工作台链路，与 ChatGPT2API 的 ``gpt-image-2`` 同一条路：

    1. POST /backend-api/sentinel/chat-requirements   拿 sentinel token（带 legacy PoW）
    2. 解 PoW（SHA3-512 哈希碰撞）
    3. POST /backend-api/f/conversation/prepare       拿 conduit_token
    4. POST /backend-api/f/conversation               SSE 生图（model=gpt-5-5 + picture_v2）
    5. GET  /backend-api/conversation/{cid}           兜底轮询结果
    6. GET  /backend-api/files/download/{fid}?...     拿下载地址（必须带认证头）
    7. 下载并保存到 data/images/

已实测：免费账号可用（persona = chatgpt-freeaccount），每次消耗 1 次 image_gen 额度。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import netproxy
from .paths import data_dir
from .quota import BASE, USER_AGENT

logger = logging.getLogger(__name__)

POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
IMAGE_MODEL = "gpt-5-5"
SYSTEM_HINT = "picture_v2"
IMAGES_DIR = data_dir() / "images"


class ImageGenError(RuntimeError):
    """生图失败。

    ``sent_unknown=True``  表示可能已经扣了额度但没拿到结果。
    ``revoked=True``       表示**服务端明确拒绝了这个 token**（HTTP 401），
                           而 token 结构本身是好的（没过期、没被改过）——
                           这种情况几乎只有一个解释：**账号已被吊销/封禁**。
                           调用方应当据此把账号标记为已失效（见 store.mark_dead）。
    """

    def __init__(self, message: str, *, sent_unknown: bool = False,
                 revoked: bool = False):
        super().__init__(message)
        self.sent_unknown = sent_unknown
        self.revoked = revoked


# --------------------------------------------------------------------- PoW


def _legacy_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def _pow_config() -> list:
    return [
        random.choice([3000, 4000, 5000]), _legacy_time(), 4294705152, 0, USER_AGENT,
        POW_SCRIPT, "", "en-US", "en-US,es-US,en,es", 0,
        random.choice(["webdriver−false", "language−zh-CN", "vendor−Google Inc.",
                       "hardwareConcurrency−32", "cookieEnabled−true"]),
        random.choice(["_reactListeningo743lnnpvdg", "location"]),
        random.choice(["0", "window", "self", "document", "location"]),
        time.perf_counter() * 1000, str(uuid.uuid4()), "",
        random.choice([8, 16, 24, 32]),
        time.time() * 1000 - (time.perf_counter() * 1000),
    ]


def solve_pow(seed: str, difficulty: str, config: list, limit: int = 2_000_000) -> tuple[str, bool]:
    """SHA3-512 前缀碰撞。返回 ``(base64 答案, 是否解出)``。"""
    target = bytes.fromhex(difficulty)
    diff_len = len(difficulty) // 2
    seed_bytes = seed.encode()
    head = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode()
    mid = ("," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ",").encode()
    tail = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode()
    for i in range(limit):
        encoded = base64.b64encode(head + str(i).encode() + mid + str(i >> 1).encode() + tail)
        if hashlib.sha3_512(seed_bytes + encoded).digest()[:diff_len] <= target:
            return encoded.decode(), True
    fallback = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + base64.b64encode(f'"{seed}"'.encode()).decode()
    return fallback, False


# --------------------------------------------------------------------- 会话


class _Session:
    def __init__(self, token: str, proxy: str = "", timeout: int = 60):
        self.token = token.strip()
        # 用户没填代理时自动用系统代理，否则 curl_cffi 直连会被墙掉。
        self.proxy = netproxy.resolve(proxy)
        self.timeout = timeout
        self.device_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.proxies = netproxy.proxy_dict(proxy)

    def headers(self, path: str, accept: str = "*/*", extra: dict | None = None) -> dict:
        headers = {
            "authorization": f"Bearer {self.token}",
            "accept": accept,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "oai-language": "zh-CN",
            "oai-device-id": self.device_id,
            "oai-session-id": self.session_id,
            "origin": BASE,
            "referer": BASE + "/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": USER_AGENT,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        }
        if extra:
            headers.update(extra)
        return headers

    def _client(self):
        """优先 curl_cffi，回落 requests。两者接口一致。"""
        try:
            from curl_cffi import requests as curl_requests

            return curl_requests, True
        except ImportError:
            import requests

            return requests, False

    def post(self, path: str, body: dict, extra: dict | None = None, stream: bool = False, timeout=None):
        client, is_curl = self._client()
        kwargs = {
            "headers": self.headers(path, extra=extra),
            "data": json.dumps(body).encode(),
            "timeout": timeout or self.timeout,
            "proxies": self.proxies,
            "stream": stream,
        }
        if is_curl:
            kwargs["impersonate"] = "chrome"
        return client.post(BASE + path, **kwargs)

    def get(self, path: str, accept: str = "*/*", target_path: str | None = None, timeout=None):
        client, is_curl = self._client()
        kwargs = {
            "headers": self.headers(target_path or path, accept=accept),
            "timeout": timeout or self.timeout,
            "proxies": self.proxies,
        }
        if is_curl:
            kwargs["impersonate"] = "chrome"
        return client.get(BASE + path, **kwargs)


# --------------------------------------------------------------------- 流程


def token_problem(access_token: str) -> str:
    """体检 access_token，返回问题描述；没问题返回空串。

    ★ 为什么必须在发请求**之前**自己查一遍（2026-10-11 实测钉死）：

    OpenAI 对「结构坏 / 签名不对 / 已过期 / 已被吊销」**统一返回同一个 401**：

        HTTP 401
        {"error":{"message":"Could not parse your authentication token.
                             Please try signing in again.",
                  "code":"unauthorized_unknown","param":null},"status":401}

    四种完全不同的成因给同一句话 —— **光看状态码永远分不出该怎么修**。
    所以这里把能自己判断的先判断掉，报错才 actionable。

    空 token 更阴：sentinel 会返回 **200**，只是 persona 从
    ``chatgpt-freeaccount`` 变成 ``chatgpt-noauth``，要等到后面
    ``conversation/prepare`` 才炸，报错离现场很远。
    """
    token = str(access_token or "").strip()
    if not token:
        return "这个账号没有 access_token（是空的）—— 请重新登录取一次 token"
    parts = token.split(".")
    if len(parts) != 3:
        return ("access_token 不是合法的 JWT（%d 段，应为 3 段）——"
                "多半是复制时被截断了" % len(parts))

    def _b64(seg: str) -> bytes:
        return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))

    try:
        header = json.loads(_b64(parts[0]))
        payload = json.loads(_b64(parts[1]))
    except Exception as exc:  # noqa: BLE001
        return "access_token 的 header/payload 解不开（%s）—— 内容被改过或截断了" % (
            type(exc).__name__)
    if not isinstance(payload, dict):
        return "access_token 的 payload 不是 JSON 对象"

    exp = payload.get("exp")
    if exp:
        try:
            exp_dt = datetime.fromtimestamp(float(exp), timezone.utc)
            if exp_dt < datetime.now(timezone.utc):
                return "access_token 已过期（%s UTC）—— 请重新登录换一个" % (
                    exp_dt.strftime("%Y-%m-%d %H:%M"))
        except Exception:  # noqa: BLE001
            pass
    if not header.get("alg"):
        return "access_token 的 header 里没有 alg —— 不像正常签发的 token"
    return ""


def _get_sentinel(session: _Session) -> tuple[dict, str]:
    problem = token_problem(session.token)
    if problem:
        raise ImageGenError(problem)

    path = "/backend-api/sentinel/chat-requirements"
    legacy, _ = solve_pow(format(random.random()), "0fffff", _pow_config())
    response = session.post(path, {"p": "gAAAAAC" + legacy})

    # ★★ 一定要把响应体带出来。原来只报 `HTTP {code}`，
    #    而 401 的 body 里才有唯一有用的那句话（见 token_problem 的说明）。
    body = (response.text or "").strip().replace("\n", " ")[:300]
    if response.status_code != 200:
        if response.status_code == 401:
            # 走到这里说明 token_problem 已经放行了 ⇒ 结构好、没过期，
            # 但服务端就是不要它。**基本只有一个解释：账号已被吊销/封禁。**
            raise ImageGenError(
                "这个号已经被封了（服务端拒绝 token，HTTP 401）：%s"
                "　→ 它会自动被标记为失效，不会再进交付包。" % (body or "(空响应)"),
                revoked=True)
        raise ImageGenError(
            "sentinel 握手失败 HTTP %d：%s" % (response.status_code, body or "(空响应)"))

    try:
        reqs = response.json()
    except Exception as exc:  # noqa: BLE001
        raise ImageGenError("sentinel 返回的不是 JSON（HTTP 200）：%s：%s"
                            % (type(exc).__name__, body)) from exc

    # ★ 空/无效 token 时这里会变成 chatgpt-noauth —— 必须当场拦住，
    #   否则会带着"未登录"身份一路走到 prepare 才报一句看不懂的错。
    persona = str(reqs.get("persona") or "")
    if persona.endswith("noauth"):
        raise ImageGenError(
            "sentinel 返回未登录身份（persona=%s）—— 这个 access_token 没有被接受，"
            "请重新登录取一个新的" % persona)

    proof = ""
    pow_info = reqs.get("proofofwork") or {}
    if pow_info.get("required"):
        answer, solved = solve_pow(pow_info["seed"], pow_info["difficulty"], _pow_config())
        if not solved:
            raise ImageGenError("PoW 求解失败")
        proof = "gAAAAAB" + answer
    return reqs, proof


def _collect_assets(conversation: dict) -> tuple[list[str], list[dict]]:
    """从会话 mapping 里找图片资产，返回 ``(文件 id, 元信息)``。"""
    file_ids: list[str] = []
    assets: list[dict] = []
    for node in (conversation.get("mapping") or {}).values():
        message = (node or {}).get("message") or {}
        content = message.get("content") or {}
        for part in (content.get("parts") or []):
            blob = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False)
            for hit in re.findall(r"(?:file-service|sediment)://([A-Za-z0-9_\-]+)", blob):
                if hit in file_ids or hit == "file_upload":
                    continue
                file_ids.append(hit)
                if isinstance(part, dict):
                    generation = ((part.get("metadata") or {}).get("generation")) or {}
                    assets.append({
                        "width": part.get("width") or generation.get("width"),
                        "height": part.get("height") or generation.get("height"),
                        "orientation": generation.get("orientation"),
                        "gen_size": generation.get("gen_size"),
                    })
    return file_ids, assets


def _download(session: _Session, conversation_id: str, file_id: str) -> bytes:
    meta_path = f"/backend-api/files/download/{file_id}"
    response = session.get(meta_path + f"?conversation_id={conversation_id}&inline=false")
    if response.status_code != 200:
        raise ImageGenError(f"取下载地址失败 HTTP {response.status_code}")
    url = (response.json() or {}).get("download_url") or ""
    if not url:
        raise ImageGenError("没有拿到 download_url")

    client, is_curl = session._client()
    kwargs = {
        "headers": session.headers("/backend-api/estuary/content",
                                   accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
        "timeout": 180,
        "proxies": session.proxies,
    }
    if is_curl:
        kwargs["impersonate"] = "chrome"
    image = client.get(url, **kwargs)
    if image.status_code != 200:
        raise ImageGenError(f"图片下载失败 HTTP {image.status_code}")
    return image.content


def _detect_suffix(data: bytes) -> str:
    """按文件头判断图片格式，别把 webp 存成 .png 让用户打不开。"""
    if data[:8].startswith(b"\x89PNG"):
        return ".png"
    if data[:3].startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


def generate(
    access_token: str,
    prompt: str,
    *,
    proxy: str = "",
    model: str = "",
    on_event=None,
    timeout: int = 240,
) -> dict:
    """跑一次完整生图，图片保存到 ``data/images/``。

    返回 ``{"ok": True, "path": ..., "url": ..., "width": ..., "height": ...}``。
    失败抛 :class:`ImageGenError`。

    ``model`` 留空用 ``gpt-5-5``；传 ``auto`` 让服务端路由（能观察到更新的模型）。
    """
    emit = on_event or (lambda stage, detail="": None)
    text = str(prompt or "").strip()
    if not text:
        raise ImageGenError("提示词不能为空")

    session = _Session(access_token, proxy=proxy, timeout=60)
    emit("sentinel", "正在通过验证")
    reqs, proof = _get_sentinel(session)

    extra = {"OpenAI-Sentinel-Chat-Requirements-Token": reqs.get("token", "")}
    if proof:
        extra["OpenAI-Sentinel-Proof-Token"] = proof
    model_slug = (model or IMAGE_MODEL).strip() or IMAGE_MODEL

    emit("prepare", "正在准备生成")
    parent_id = str(uuid.uuid4())
    prepare_path = "/backend-api/f/conversation/prepare"
    prepare = session.post(prepare_path, {
        "action": "next", "fork_from_shared_post": False, "parent_message_id": parent_id,
        "model": model_slug, "client_prepare_state": "success",
        "timezone_offset_min": -480, "timezone": "Asia/Shanghai",
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [SYSTEM_HINT],
        "partial_query": {
            "id": str(uuid.uuid4()), "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [text]},
        },
        "supports_buffering": True, "supported_encodings": ["v1"],
        "client_contextual_info": {"app_name": "chatgpt.com"},
    }, extra=extra)
    if prepare.status_code != 200:
        detail = str(getattr(prepare, "text", "") or "")[:200]
        raise ImageGenError(f"准备阶段失败 HTTP {prepare.status_code} {detail}")
    conduit = (prepare.json() or {}).get("conduit_token") or ""
    if not conduit:
        raise ImageGenError("准备阶段没有返回 conduit_token")

    emit("generating", "正在生成图片")
    gen_extra = dict(extra)
    gen_extra.update({"X-Conduit-Token": conduit, "X-Oai-Turn-Trace-Id": str(uuid.uuid4()),
                      "accept": "text/event-stream"})
    response = session.post("/backend-api/f/conversation", {
        "action": "next",
        "messages": [{
            "id": str(uuid.uuid4()), "author": {"role": "user"}, "create_time": time.time(),
            "content": {"content_type": "text", "parts": [text]},
            "metadata": {
                "developer_mode_connector_ids": [], "selected_github_repos": [],
                "selected_all_github_repos": False, "system_hints": [SYSTEM_HINT],
                "serialization_metadata": {"custom_symbol_offsets": []},
            },
        }],
        "parent_message_id": parent_id, "model": model_slug, "client_prepare_state": "sent",
        "timezone_offset_min": -480, "timezone": "Asia/Shanghai",
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True, "system_hints": [SYSTEM_HINT],
        "supports_buffering": True, "supported_encodings": ["v1"],
        "paragen_cot_summary_display_override": "allow", "force_parallel_switch": "auto",
        "client_contextual_info": {
            "is_dark_mode": False, "time_since_loaded": 1200, "page_height": 1072,
            "page_width": 1724, "pixel_ratio": 1.2, "screen_height": 1440,
            "screen_width": 2560, "app_name": "chatgpt.com",
        },
    }, extra=gen_extra, stream=True, timeout=timeout)
    if response.status_code != 200:
        detail = str(getattr(response, "text", "") or "")[:200]
        raise ImageGenError(f"生成请求失败 HTTP {response.status_code} {detail}")

    conversation_id = None
    observed: dict = {}
    deadline = time.time() + timeout
    for raw in response.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if conversation_id is None:
            candidate = parsed.get("conversation_id")
            if not candidate:
                value = parsed.get("v")
                if isinstance(value, dict):
                    candidate = value.get("conversation_id")
            if candidate:
                conversation_id = candidate
        if parsed.get("type") == "server_ste_metadata":
            meta = parsed.get("metadata") or {}
            for key in ("model_slug", "plan_type", "turn_use_case"):
                if meta.get(key):
                    observed[key] = meta[key]
        if time.time() > deadline:
            break
    response.close()

    if not conversation_id:
        raise ImageGenError("没有拿到 conversation_id", sent_unknown=True)

    emit("downloading", "正在取回图片")
    time.sleep(2)
    detail = session.get(f"/backend-api/conversation/{conversation_id}", accept="application/json")
    if detail.status_code != 200:
        raise ImageGenError(f"读取会话失败 HTTP {detail.status_code}", sent_unknown=True)

    file_ids, assets = _collect_assets(detail.json())
    if not file_ids:
        raise ImageGenError("会话里没有图片（可能还在生成，稍后重试）", sent_unknown=True)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results: list[dict] = []
    for index, file_id in enumerate(file_ids):
        data = _download(session, conversation_id, file_id)
        # 不能假定就是 PNG：上游换模型时可能直接给 webp/jpeg，按魔数落地更稳。
        name = f"{stamp}-{index + 1}{_detect_suffix(data)}"
        path = IMAGES_DIR / name
        path.write_bytes(data)
        asset = assets[index] if index < len(assets) else {}
        results.append({
            "file": name,
            "path": str(path),
            "size": len(data),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "orientation": asset.get("orientation"),
        })

    emit("done", "生成完成")
    payload = {
        "ok": True,
        "conversation_id": conversation_id,
        "images": results,
        "model_slug": observed.get("model_slug", ""),
        "plan_type": observed.get("plan_type", ""),
        "prompt": text,
        "model_requested": model_slug,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # 旁边落一份元数据，账号池页面不用读对话就能显示「这条图是用什么提示词生成的」。
    try:
        (IMAGES_DIR / f"{stamp}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.warning("[生图] 元数据写入失败，不影响图片本身")
    return payload
