# -*- coding: utf-8 -*-
"""代理自动发现。

为什么需要这个：Clash / FlClash 这类工具默认只改 **Windows 系统代理**（注册表
``HKCU\\...\\Internet Settings``）。Python 的 ``requests`` 会通过
``urllib.request.getproxies()`` 读到它，但 curl_cffi 底下的 libcurl 不读注册表，
于是出现「额度查询通、生图超时」这种诡异差异。

对小白来说，手动再填一遍代理是纯负担。这里统一在开跑前把系统代理捞出来，
用户填了就用用户的，没填就用系统的，都没有才直连。

## 2026-10-11 修了三处（内测第一台 Win10 逼出来的）

**① 支持 PAC（AutoConfigURL）—— 这是「注册成功但生图失败」的真凶。**

Clash / FlClash 有「系统代理」和「自动配置(PAC)」两种模式，后者**只写
``AutoConfigURL``**。而 ``urllib.request.getproxies()`` 在 Windows 上
**只读 ``ProxyEnable`` / ``ProxyServer``，完全不看 ``AutoConfigURL``**。
结果三方行为不一致：

| 谁 | 读什么 | PAC 模式下 |
|---|---|---|
| Chromium（注册用的浏览器） | WinINET 全套，**含 PAC** | ✅ 能上网 |
| ``urllib.getproxies()`` | 只读 ProxyServer | ❌ 读不到 |
| curl_cffi / libcurl | 啥都不读，只认我们显式传的 ``proxies=`` | ❌ 直连被墙 |

实测症状：注册能过，一生图就
``curl: (28) Failed to connect to chatgpt.com:443``。
**注意报错里是目标域名而不是代理地址 —— 那就是"压根没走代理"的铁证。**
现在会把 PAC 文件抓下来，正则挖出第一条 ``PROXY host:port``。

**② 不再缓存「空」结果。**

原来读到空就永久缓存（``_CACHE["value"]``），而用户很可能是
**先开工具、后开代理客户端** —— 那整个进程生命周期都会直连，
表现成「我明明开了梯子，它还是连不上」。现在只有读到非空才缓存。
（PAC 抓取带 20 秒节流，避免每次请求都去拉一遍。）

**③ 删掉 ``_LOOPBACK_HOSTS`` 和那句误导性注释。**

原来留了个常量说「127.0.0.1 出现在系统代理里多半是抓包工具残留，不能当出口用」，
但它**从未被使用**。没被使用是对的 —— 对 Clash/FlClash 来说
``http://127.0.0.1:7890`` 恰恰就是**唯一正确的出口**（本地监听端口）。
照那句话去过滤 loopback，会把正常用户全判成"没有代理"。
**一个没被使用、注释还写反了的常量，迟早会有人照着它改坏代码。**
"""
from __future__ import annotations

import logging
import re
import time

#: ★ 单独 import 出来当模块级名字：`urllib.request.xxx` 这种深层属性
#: 在测试里没法直接替换（得连着 urllib 一起换），留别名才好打补丁。
from urllib.request import getproxies, urlopen

logger = logging.getLogger(__name__)

_CACHE: dict = {"value": None, "pac_at": 0.0}

#: PAC 文件里挖代理用的。不实现完整 PAC 求值（那是 JS），只取第一条指令。
#: 典型内容：``return "PROXY 127.0.0.1:7890; DIRECT";``
#: ⚠️ ``DIRECT`` 不是代理，正则里刻意不含它。
_PROXY_DIRECTIVE = re.compile(
    r"\b(PROXY|HTTPS?|SOCKS5?|SOCKS4)\s+([A-Za-z0-9.\-]+:\d{1,5})", re.I)

#: PAC 抓取的节流间隔（秒）。空结果不缓存，但不能每次请求都去拉一遍。
_PAC_RETRY_SECONDS = 20.0

_IE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _normalize(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def _read_reg(name: str) -> str:
    """读 HKCU 的 Internet Settings。读不到返回空串。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _IE_KEY) as key:
            return str(winreg.QueryValueEx(key, name)[0] or "")
    except Exception:  # noqa: BLE001
        return ""


def _from_pac(url: str) -> str:
    """把 PAC 文件抓下来，挖出第一条可用的代理指令。"""
    try:
        with urlopen(url, timeout=5) as resp:  # noqa: S310
            text = resp.read(200_000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        logger.info("[代理] PAC 读取失败：%s: %s", type(exc).__name__, exc)
        return ""
    match = _PROXY_DIRECTIVE.search(text)
    if not match:
        logger.info("[代理] PAC 里没找到 PROXY/HTTPS/SOCKS 指令，只有 DIRECT？")
        return ""
    scheme = "socks5" if match.group(1).upper().startswith("SOCKS") else "http"
    found = "%s://%s" % (scheme, match.group(2))
    logger.info("[代理] 从 PAC 取到代理 %s（PAC 源 %s）", found, url)
    return found


def system_proxy() -> str:
    """读操作系统的代理设置，读不到返回空串。

    ★ 只有读到**非空**才缓存（见模块文档 ②）。
    """
    cached = _CACHE.get("value")
    if cached:
        return str(cached)

    value = ""
    try:
        proxies = getproxies()
    except Exception:  # noqa: BLE001
        proxies = {}
    for scheme in ("https", "http"):
        candidate = _normalize(proxies.get(scheme, ""))
        if candidate:
            value = candidate
            break

    if not value:
        # getproxies() 不读 PAC，这里补上（带节流）
        pac = _read_reg("AutoConfigURL")
        now = time.time()
        if pac and (now - float(_CACHE.get("pac_at") or 0.0)) >= _PAC_RETRY_SECONDS:
            _CACHE["pac_at"] = now
            value = _from_pac(pac)

    if value:
        _CACHE["value"] = value
        logger.info("[代理] 自动使用系统代理 %s", value)
    else:
        logger.info(
            "[代理] 读不到系统代理（ProxyEnable/ProxyServer/AutoConfigURL 都没有）"
            "→ 将直连。若你在用 PAC/TUN 模式，请在设置里显式填一次代理地址。")
    return value


def resolve(explicit: str = "") -> str:
    """用户填了就用用户的；否则退回系统代理；都没有就是直连。"""
    text = _normalize(explicit)
    if text:
        return text
    return system_proxy()


def proxy_dict(explicit: str = "") -> dict | None:
    """给 requests / curl_cffi 用的 proxies 参数。"""
    url = resolve(explicit)
    return {"http": url, "https": url} if url else None


def refresh() -> str:
    """清缓存重读（用户在设置里改了代理之后调用）。"""
    _CACHE["value"] = None
    _CACHE["pac_at"] = 0.0
    return system_proxy()
