# -*- coding: utf-8 -*-
"""开跑前的自检：把小白最常踩的四个坑提前暴露出来。

## 为什么要有它

小白最怕的不是报错，是**不知道为什么不动**。实测最常卡住的就是这四件事：

    ① 工具密钥没填 / 填错
    ② 站内余额不够
    ③ 代理配错（或 Clash 没开）
    ④ 浏览器内核还没下载完 / 下载失败

这四件事如果不在开跑前查，表现全都一样 ——「点了开始注册，半天没反应」，
小白只能来问你。

## 两档

``deep=False``：只做**本地快检**（毫秒级）。启动时自动跑，把最硬的坑先报出来。
``deep=True`` ：联网 + 起浏览器。用户点「开始检查」时跑。

## 一条重要边界

**内核没下载完时，deep 检查不会去启动浏览器** —— 因为 CloakBrowser 一启动就会
开始下那 150MB，用户会以为程序卡死。这种情况只报「内核未就绪」，
并引导他走「下载内核」按钮（有明确提示、能看见进度）。
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 浏览器启动测试的最长等待。**注意它只在 pool.shutdown(wait=False) 的前提下才有意义**
#: （见 check_browser_launch 里的注释：用 `with` 会把请求挂死，超时形同虚设）。
#: 90 秒是留足"首次运行被杀软扫描拖慢"的余量。
BROWSER_LAUNCH_TIMEOUT = 90


def _r(check_id: str, name: str, ok: bool, detail: str, hint: str = "",
       *, fatal: bool = False, skipped: bool = False) -> dict:
    return {"id": check_id, "name": name, "ok": bool(ok), "detail": detail,
            "hint": hint, "fatal": bool(fatal), "skipped": bool(skipped)}


# ------------------------------------------------------------------ ① 密钥

def check_key(config) -> dict:
    # 2026-10-18：邮箱来源只剩本站号池一种，这条检查不再分叉。
    # （以前还有一条「直连外部邮箱上游」的分支，已随上游一起删掉。）
    key = str(getattr(config, "sitepool_tool_key", "") or "").strip()
    if not key:
        return _r("key", "工具密钥", False, "还没填",
                  "到站内「自建号池」页创建一把密钥，复制后粘到上面那一格",
                  fatal=True)
    if len(key) < 20:
        return _r("key", "工具密钥", False, f"看起来太短（{len(key)} 字符）",
                  "整把复制，别漏字符", fatal=True)
    return _r("key", "工具密钥", True, f"已填（{len(key)} 字符）", "")


# ------------------------------------------------------------- ② 浏览器内核

def kernel_cache_dir() -> Path:
    """内核缓存目录。

    ★ 这里主动调一次 `apply_runtime_env()`：它是幂等的（设过就早退），
      但**不能假设调用方一定设过** —— 从 tools/ 里单独跑自检、或者别的入口进来时，
      少了这一步就会指到空的程序目录，误报「内核没下载」。
    """
    from core import paths

    paths.apply_runtime_env()
    raw = os.environ.get("CLOAKBROWSER_CACHE_DIR", "").strip()
    return Path(raw) if raw else paths.browser_dir()


def kernel_present() -> tuple[bool, str]:
    """内核在不在。返回 (是否就绪, 人话描述)。"""
    cache = kernel_cache_dir()
    try:
        names = [p.name for p in cache.iterdir() if p.name.startswith("chromium-")]
    except Exception:  # noqa: BLE001
        names = []
    if names:
        return True, f"已就绪（{names[0]}）"
    return False, f"还没下载（目录 {cache} 是空的）"


def check_kernel(config) -> dict:
    ready, detail = kernel_present()
    cache = kernel_cache_dir()
    if ready:
        # ★ 把**实际用的目录**报出来：这样一眼能看出用的是「打包内置的那份」
        #   还是「家目录里以前下的那份」——离线包有没有生效，看这行就知道。
        return _r("kernel", "浏览器内核", True, f"{detail} · {cache}", "")
    size_mb = 150
    return _r("kernel", "浏览器内核", False, detail,
              f"首次使用要联网下载约 {size_mb}MB（CloakBrowser 的 stealth Chromium）。"
              "点上面那行右边的「下载内核」即可；网络慢的话请耐心等，"
              "下载过程中界面不会卡死。")


# ------------------------------------------------------------------ ③ 号池

def check_pool(config) -> dict:
    """号池能不能连上、余额够不够。"""
    provider = str(getattr(config, "mailbox_provider", "sitepool") or "sitepool").lower()
    if provider != "sitepool":
        return _r("pool", "号池余额", True, "当前用的是直连上游，跳过", "", skipped=True)

    from core import sitepool

    base = str(getattr(config, "sitepool_base", "") or "").strip()
    key = str(getattr(config, "sitepool_tool_key", "") or "").strip()
    if not (base and key):
        return _r("pool", "号池余额", False, "密钥没填，无法查询", "先填工具密钥")

    try:
        client = sitepool.SitePoolClient(base, mail_key=key, timeout=20)
        data = client.balance()
    except sitepool.SitePoolLicenseRequired as exc:
        # ★ 「没资格」和「连不上」是两件事，用户的下一步动作完全不同，
        #   不能都报成「连不上」—— 那会把人带去查网络，查半天查不出来。
        #   而且这里必须 fatal：没资格的话注册第一步（下邮箱单）就会失败，
        #   让他点「开始注册」只是白等。
        return _r("pool", "号池余额", False, str(exc),
                  "到站内兑换使用资格，再重新创建一把绑定该分组的「工具密钥」",
                  fatal=True)
    except sitepool.SitePoolAuthError as exc:
        # 密钥无效同理：这是配置问题，不是网络问题。
        return _r("pool", "号池余额", False, str(exc),
                  "到站内「自建号池」页重新复制一把工具密钥", fatal=True)
    except Exception as exc:  # noqa: BLE001
        return _r("pool", "号池余额", False, f"连不上：{exc}",
                  "检查本机网络；如果开了代理，确认代理能访问本站")

    balance = data.get("balance")
    try:
        amount = float(balance)
    except (TypeError, ValueError):
        return _r("pool", "号池余额", False, f"返回看不懂：{data}",
                  "本站接口异常，稍后再试或联系站长")

    if amount <= 0:
        return _r("pool", "号池余额", False, f"¥{amount:.2f}",
                  "余额为 0，先到站内充值再跑", fatal=True)
    return _r("pool", "号池余额", True, f"¥{amount:.2f}", "")


# ------------------------------------------------------------------ ④ 代理

def check_proxy(config) -> dict:
    """出口 IP 通不通、落在哪个国家。"""
    import requests

    from core import netproxy

    explicit = str(getattr(config, "proxy", "") or "").strip()
    resolved = netproxy.resolve(explicit)
    proxies = netproxy.proxy_dict(explicit)

    session = requests.Session()
    # ★ 必须关掉 trust_env：否则 requests 会自己再去读一遍系统代理，
    #   和我们这里显式传的 proxies 打架（这个坑 2026-10-04 在号池请求上踩过）。
    session.trust_env = False

    payload = None
    last_error = ""
    for url in ("http://ip-api.com/json/?fields=query,country,city,isp,hosting",
                "https://api.ipify.org?format=json"):
        try:
            resp = session.get(url, proxies=proxies, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            continue

    if payload is None:
        where = f"（{resolved}）" if resolved else "（直连）"
        return _r("proxy", "代理出口", False, f"连不出去{where}：{last_error}",
                  "如果填了代理，确认它还能用；如果用的是本机 Clash，确认它开着")

    ip = str(payload.get("ip") or payload.get("query") or "?")
    parts = [ip]
    if payload.get("country"):
        parts.append(str(payload["country"]))
    if payload.get("city"):
        parts.append(str(payload["city"]))
    if payload.get("isp"):
        parts.append(str(payload["isp"]))
    detail = " · ".join(parts)

    hint = ""
    if payload.get("hosting") is True:
        # ★ 2026-10-18 改口：原来这里写的是「机房 IP，注册大概率过不去」并把这一项
        #   判成**不通过**（红叉）。这是**错的**，被自己的实测反复推翻：
        #     ・同一条机房出口一天注册成功 30+ 个号（成功率 70%~86%）；
        #     ・真正决定成败的是**号码质量/号段**和**时段**，不是出口是不是机房；
        #     ・把这项判红，等于让用机场/Clash 的人（绝大多数用户）一进来就看到
        #       「过不去」，然后不敢跑 —— 这是误导，不是保护。
        #   所以：**只提示，不判红**。出口确实被拦时，注册流程自己会在日志里说。
        hint = ("检测到这是机房/云厂商出口（hosting=true）。这不代表一定注册不了 —— "
                "用机场或 Clash 的人多半是这个类型。可以先跑一单试试；"
                "如果连续整单都在登录页被人机验证挡住，再换一条家宽/住宅出口。")
    elif not resolved:
        hint = "当前是直连（没走代理）。国内直连容易被拦，注册失败时优先加一条代理。"

    # ★ hosting 只是**信息**，不是失败：只有「连不出去」才算这一项不通过。
    return _r("proxy", "代理出口", True, detail, hint)


# -------------------------------------------------------- ⑤ 真启动一次浏览器

def check_browser_launch(config) -> dict:
    """真起一次 CloakBrowser 再关掉 —— 这是「能不能跑」最硬的验证。

    内核没就绪时**直接跳过**：一启动就会开始下 150MB，用户会以为卡死。
    """
    ready, detail = kernel_present()
    if not ready:
        return _r("browser", "浏览器启动", False, "内核未就绪，跳过启动测试",
                  "先点「下载内核」", skipped=True)

    from core import cloak_driver

    proxy = str(getattr(config, "proxy", "") or "").strip() or None
    license_key = str(getattr(config, "cloak_license_key", "") or "").strip()

    def _launch() -> tuple[bool, str]:
        driver = None
        try:
            driver, _ = cloak_driver.build_cloak_driver(
                proxy, headless=True, humanize=False,
                # geoip 关掉：这一步只验「能不能起来」，不验地理。
                # 开着它要多查一次地理库，慢且可能超时，会把结论弄脏。
                geoip=False, license_key=license_key,
            )
            return True, "启动成功并已关闭"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass

    started = time.time()
    logger.info("[自检] 开始浏览器启动测试（最长 %s 秒）", BROWSER_LAUNCH_TIMEOUT)
    # ★★ 这里**绝对不能**写成 `with ThreadPoolExecutor(...) as pool`。
    #   上下文管理器退出时会调 `shutdown(wait=True)` —— 也就是**死等那个线程结束**。
    #   于是 `future.result(timeout=...)` 超时后我们虽然想返回，却会被 `__exit__`
    #   卡住：**整个 HTTP 请求永久挂起、前端一直显示「检查中」**，那个超时形同虚设。
    #   （2026-10-09 实测踩到：首次启动被杀软扫描新文件拖慢，深检接口再没返回过。）
    #   正确做法：自己建 pool，finally 里 `shutdown(wait=False)` ——
    #   宁可泄漏一个后台线程，也绝不能把请求挂死。
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_launch)
        try:
            ok, detail2 = future.result(timeout=BROWSER_LAUNCH_TIMEOUT)
        except concurrent.futures.TimeoutError:
            logger.warning("[自检] 浏览器启动超过 %s 秒仍未返回，放弃等待（后台线程已放弃回收）",
                           BROWSER_LAUNCH_TIMEOUT)
            return _r("browser", "浏览器启动", False,
                      f"启动超过 {BROWSER_LAUNCH_TIMEOUT} 秒没返回",
                      "常见原因：① **首次运行被杀软/Defender 扫描新文件拖慢** —— "
                      "过几分钟再点一次通常就快了；② 安全软件拦了浏览器；"
                      "③ 内核损坏（删掉 browser 目录重新下载）。"
                      "也可以先直接开跑，注册流程里会再试。")
    finally:
        pool.shutdown(wait=False)

    took = time.time() - started
    logger.info("[自检] 浏览器启动测试结束：ok=%s 耗时 %.1f 秒", ok, took)
    return _r("browser", "浏览器启动", ok, f"{detail2}（{took:.1f} 秒）",
              "" if ok else "常见原因：杀软拦了浏览器、内存不够、或内核损坏（删掉重下）")


# ------------------------------------------------------------------ 汇总

#: 快检包含哪些（启动时自动跑，毫秒级）
FAST_CHECKS = ("key", "kernel")
#: 深检包含哪些（用户点按钮时跑）
DEEP_CHECKS = ("key", "kernel", "pool", "proxy", "browser")


def run(config, *, deep: bool = False) -> dict:
    """跑一遍自检。**任何单项失败都不允许把整个接口打挂。**"""
    names = DEEP_CHECKS if deep else FAST_CHECKS
    checks: list[dict] = []
    for name in names:
        try:
            if name == "key":
                checks.append(check_key(config))
            elif name == "kernel":
                checks.append(check_kernel(config))
            elif name == "pool":
                checks.append(check_pool(config))
            elif name == "proxy":
                checks.append(check_proxy(config))
            elif name == "browser":
                checks.append(check_browser_launch(config))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[自检] %s 抛异常：%s", name, exc)
            checks.append(_r(name, name, False, f"检查自身出错：{type(exc).__name__}: {exc}",
                             "这是程序问题，请把这条发给开发者"))

    blocking = [c for c in checks
                if not c["ok"] and not c["skipped"] and c["fatal"]]
    failing = [c for c in checks if not c["ok"] and not c["skipped"]]
    if blocking:
        summary = "有必须先解决的问题：" + "、".join(c["name"] for c in blocking)
    elif failing:
        summary = "能跑，但有需要注意的地方：" + "、".join(c["name"] for c in failing)
    else:
        summary = "全部通过，可以开跑"
    return {
        "ok": not blocking,
        "deep": bool(deep),
        "summary": summary,
        "checks": checks,
    }
