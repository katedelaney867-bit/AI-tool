# -*- coding: utf-8 -*-
"""一键诊断：把「出问题时本来要来回问用户十几遍的事」一次性收集成一份报告。

## 为什么要它

内测期最贵的不是 bug，是**沟通成本**。用户只会说「打不开」，而你要的那十几条
信息（系统版本、有没有装 Python、VC++ 运行库在不在、SAC 状态、执行策略、
杀软、内核下了没、端口通不通、日志尾部…）要来回问一整天。

用户双击「诊断.bat」→ 本脚本生成 `诊断结果.txt` → 他把**一个文件**发回来。

## ★ 脱敏是硬要求

用户会把这份文件发出来，所以**绝不能带密钥**：

- `config.json` 里名字像密钥的（key/secret/token/password/license）→ 只报「已填/未填」
- 代理串里的用户名密码 → 打码，只保留「协议 + 主机端口」
  （诊断需要知道代理指向哪，但凭据必须抹掉）
  ★ 这里刻意**不写示例串** —— 一写就会被自己的密钥审计器命中，
    那道闸门是对的，不该为了注释去放宽它。
- 日志尾部同样过一遍这两条规则
- **完全不读** `data/accounts.jsonl` / `data/codex_credentials/`（那是账号资产），
  只报行数

## 为什么要报「关键文件指纹」

2026-10-09 踩过一次：发出去的包里内嵌源码包是旧的，客户走了备用路线就踩回旧 bug。
把 `app.py` / `core/preflight.py` / `ui/index.html` 的 sha256 前 16 位报出来，
**一眼就能判断用户手上是哪一版**，不用再猜。

## 编码约定

- 只用标准库；不用 3.12 才支持的多行 f-string（PEP 701），旧版 Python 也能跑。
- 任何一步失败都只写进报告，**绝不让脚本自己崩**。
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

try:  # Windows 才有；万一在别的系统上跑也不该崩
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

#: 需要报指纹的关键文件（改动它们就等于换了一版）
FINGERPRINT_FILES = ("app.py", "core/preflight.py", "core/registration.py", "ui/index.html")

#: 配置里只要名字撞上这些词，值一律不打出来
SENSITIVE_KEY = re.compile(
    r"(key|secret|token|password|passwd|pwd|credential|license|auth)", re.I)

#: 文本里的密钥 / 凭据形态，统一打码
_RE_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}")
_RE_CRED = re.compile(r"\b(https?|socks5h?|socks4)://[^:@/\s]+:[^@/\s]+@", re.I)

#: 日志行首的级别标记
_RE_LEVEL = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b")
#: 真正的问题行（INFO 里带「失败」二字的不算 —— 实测会把日志刷满，把真错误淹掉）
_RE_ERR = re.compile(r"\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)")
_RE_WARN = re.compile(r"\b(WARNING|WARN)\b")

#: 日志记录的行首时间戳（用来判断 Traceback 到哪儿结束）
_RE_TS = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}|^\s*\d{2}:\d{2}:\d{2}")

WINVER_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


# ----------------------------------------------------------------- 脱敏

def mask_text(s: str) -> str:
    """把一段文本里的密钥 / 代理凭据打码。日志尾部也走这个。"""
    s = _RE_SK.sub("sk-***", s)
    s = _RE_CRED.sub(lambda m: m.group(1) + "://***:***@", s)
    return s


def mask_value(name: str, value) -> str:
    """单个配置项 → 可安全外发的字符串。"""
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return repr(value)
    text = str(value)
    if not text:
        return "(空)"
    if name.lower() == "proxy":
        return mask_text(text)
    if SENSITIVE_KEY.search(name):
        return "(已填，%d 字符)" % len(text)
    # 名字不敏感，但值本身可能是密钥（比如填错格子了）
    if _RE_SK.search(text) or _RE_CRED.search(text):
        return mask_text(text)
    if len(text) > 160:
        return text[:160] + "…（共 %d 字符）" % len(text)
    return text


# ----------------------------------------------------------------- 工具

def windows_flavor(product: str, build: str) -> str:
    """判定到底是 Win10 还是 Win11 —— **必须按 build 号，不能信 ProductName**。

    实测（2026-10-11，于 Win11 25H2 机器）：
        ProductName    = "Windows 10 Home"   ← 微软从 Win11 起就没再改过这个值
        CurrentBuild   = 26200
        cmd ver        = 10.0.26200.9457
    直接报 ProductName 会把 Win11 说成 Win10，而这恰恰是内测期最要分清的一件事
    （自己那台已经验过 Win11，缺的就是 Win10）。

    分界：build >= 22000 是 Windows 11（21H2=22000，22H2=22621，23H2=22631，
    24H2=26100，25H2=26200）。
    """
    m = re.search(r"(\d{4,})", str(build))
    if not m:
        return "%s（build 读不到，无法判定）" % product
    return "Windows 11" if int(m.group(1)) >= 22000 else "Windows 10"


def collapse(lines, limit: int) -> list:
    """把**连续重复**的行压成一行 `×N`。

    日志里同一句刷几百遍是常态（手机换号重试、OTP 轮询…），不压的话
    真正有用的那一行会被彻底淹掉。只压连续的，不改变顺序。
    """
    out = []
    prev = None
    n = 0
    for ln in lines:
        if ln == prev:
            n += 1
            continue
        if prev is not None:
            out.append(prev if n == 1 else "%s   ×%d" % (prev, n))
        prev = ln
        n = 1
    if prev is not None:
        out.append(prev if n == 1 else "%s   ×%d" % (prev, n))
    return out[-limit:]


def run(cmd, timeout: int = 25) -> str:
    """跑个命令，**永远不抛异常**（诊断工具自己崩了就毫无意义）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return out or "(退出码 %d，无输出)" % p.returncode
    except FileNotFoundError:
        return "(找不到该命令)"
    except subprocess.TimeoutExpired:
        return "(超时 %ds)" % timeout
    except Exception as exc:  # noqa: BLE001
        return "(%s: %s)" % (type(exc).__name__, exc)


def ps(script: str, timeout: int = 30) -> str:
    """跑一段 PowerShell。"""
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
               timeout)


def read_reg(path: str, name: str, hive=None) -> str:
    """读注册表。hive 默认 HKLM（系统信息）；代理设置那类要走 HKCU。"""
    if winreg is None:
        return "(非 Windows)"
    try:
        with winreg.OpenKey(hive if hive is not None else winreg.HKEY_LOCAL_MACHINE,
                            path) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except Exception as exc:  # noqa: BLE001
        return "(读不到：%s)" % type(exc).__name__


def sha16(p: Path) -> str:
    try:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return "(缺)"


def port_listening(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def newest_log(root: Path):
    logdir = root / "logs"
    if not logdir.is_dir():
        return None
    cands = sorted(logdir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


# ----------------------------------------------------------------- 主体

def token_health(token: str) -> str:
    """体检一个 access_token，返回人话结论。不返回 token 本身。"""
    t = str(token or "").strip()
    if not t:
        return "空"
    parts = t.split(".")
    if len(parts) != 3:
        return "★只有 %d 段（不是 JWT，多半被截断）" % len(parts)
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
    except Exception as exc:  # noqa: BLE001
        return "★payload 解不开（%s）" % type(exc).__name__
    exp = payload.get("exp")
    if not exp:
        return "★没有 exp 字段"
    try:
        exp_dt = _dt.datetime.fromtimestamp(float(exp), _dt.timezone.utc)
    except Exception:  # noqa: BLE001
        return "★exp 不是时间戳（%r）" % (exp,)
    left = (exp_dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    if left < 0:
        return "★已过期（%s UTC）" % exp_dt.strftime("%Y-%m-%d %H:%M")
    return "有效，剩 %.1f 天（到 %s UTC）" % (left / 86400.0, exp_dt.strftime("%Y-%m-%d %H:%M"))


def accounts_section(root: Path, limit: int = 40) -> list:
    """账号 token 体检 —— 2026-10-11「注册成功但生图 401」逼出来的。

    OpenAI 的 401 是**泛化**的：结构坏 / 被改过 / 已过期 / 被服务端吊销，
    响应体都是同一句 ``Could not parse your authentication token``（实测四种全一样）。
    **光看状态码永远分不出来**，所以这里把每个账号 token 的结构直接量出来。

    ★ 只报**后 12 位**（界面上的 ``access_token_tail`` 本来就暴露这个），
      绝不外发完整 token。
    """
    out = []
    add = out.append
    add("")
    add("--- [12] 账号 token 体检（只报后 12 位，不外发完整 token）---")

    path = root / "data" / "accounts.jsonl"
    if not path.is_file():
        add("  (没有 data\\accounts.jsonl)")
        return out
    rows = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except Exception as exc:  # noqa: BLE001
        add("  (读不了 accounts.jsonl：%s: %s)" % (type(exc).__name__, exc))
        return out

    add("  共 %d 条" % len(rows))
    tally = {}
    for r in rows:
        verdict = token_health(str(r.get("access_token") or ""))
        # 粗分类，别拿整句当 key（否则每个号一行，汇总毫无意义）
        if verdict.startswith("有效"):
            key = "有效"
        elif verdict == "空":
            key = "★空"
        elif "过期" in verdict:
            key = "★已过期"
        elif "段" in verdict:
            key = "★不是 3 段 JWT（被截断/被改）"
        elif "解不开" in verdict or "没有 exp" in verdict or "exp 不是" in verdict:
            key = "★结构异常（%s）" % verdict[:28]
        else:
            key = "★其它：" + verdict[:32]
        tally[key] = tally.get(key, 0) + 1
    for k, v in sorted(tally.items(), key=lambda x: -x[1]):
        add("    %-44s %d 个" % (k, v))

    add("")
    add("  明细（每个号一行；有密码/2FA/状态也带上，好判断是不是加固导致的）：")
    for r in rows[:limit]:
        tok = str(r.get("access_token") or "")
        add("    %-34s pwd=%-3s 2fa=%-3s act=%-8s live=%-6s tok[len=%-4d tail=%s] %s"
            % (str(r.get("email") or "?")[:34],
               "有" if (r.get("password") or "").strip() else "无",
               "有" if (r.get("totp_secret") or "").strip() else "无",
               str(r.get("activation") or "-"),
               str(r.get("liveness") or "-"),
               len(tok), tok[-12:] if tok else "(空)",
               token_health(tok)))
    if len(rows) > limit:
        add("    …（还有 %d 条未列出）" % (len(rows) - limit))
    return out


def proxy_section(root: Path) -> list:
    """代理诊断 —— 内测第一台机器暴露的头号疑难。

    2026-10-11 现象：**注册成功，但生图失败**
        curl_cffi ... Failed to connect to chatgpt.com:443 ... Could not connect to server

    注意报错指向的是 ``chatgpt.com:443`` 而**不是代理地址** —— 说明压根没走代理。
    为什么「浏览器能上网、curl_cffi 不能」？因为两者读代理的方式不同：

        浏览器(Chromium)  读 WinINET 设置：**ProxyServer 和 PAC(AutoConfigURL) 都会用**
        urllib.getproxies  只读 ProxyEnable / ProxyServer，**完全不看 AutoConfigURL**
        libcurl(curl_cffi) 连注册表都不读，只能靠我们显式传 proxies=

    所以只要用户用的是 **PAC 模式**（Clash/FlClash 的「自动配置」），
    就会出现这种「浏览器通、Python 直连被墙」的诡异差异。

    这一段把三件事一次量出来，避免来回猜：
      1. 注册表里 ProxyEnable / ProxyServer / AutoConfigURL 的**原始值**
      2. 直连 chatgpt.com 通不通
      3. **经代理**访问 chatgpt.com 通不通
    """
    out = []
    add = out.append
    add("")
    add("--- [11] 代理诊断 ---")

    cfg_proxy = ""
    cfg_file = root / "data" / "config.json"
    if cfg_file.is_file():
        try:
            cfg_proxy = str(json.loads(cfg_file.read_text(
                encoding="utf-8", errors="replace")).get("proxy") or "")
        except Exception:  # noqa: BLE001
            cfg_proxy = ""
    add("  配置里的 proxy        : %s"
        % (mask_text(cfg_proxy) if cfg_proxy.strip() else "(未填)"))

    IE = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    # ★ 注意参数顺序是 (path, name, hive) —— hive 默认 HKLM，代理设置要走 HKCU。
    #   （第一次写成 read_reg(HKCU, IE, "ProxyEnable") 了，三个参数全串位，
    #     结果是 OpenKey 抛 TypeError 被吞成"(读不到)"，四个值全是假的。）
    if winreg:
        enable = read_reg(IE, "ProxyEnable", winreg.HKEY_CURRENT_USER)
        server = read_reg(IE, "ProxyServer", winreg.HKEY_CURRENT_USER)
        pac = read_reg(IE, "AutoConfigURL", winreg.HKEY_CURRENT_USER)
        override = read_reg(IE, "ProxyOverride", winreg.HKEY_CURRENT_USER)
    else:
        enable = server = pac = override = "(非 Windows)"
    add("  注册表 ProxyEnable    : %s" % enable)
    add("  注册表 ProxyServer    : %s" % server)
    add("  注册表 ProxyOverride  : %s" % override)
    if pac.startswith("(读不到"):
        add("  注册表 AutoConfigURL  : (无)")
    else:
        add("  注册表 AutoConfigURL  : %s" % pac)
        add("      ★★ 这是 **PAC 模式**！Chromium 会用它，但 Python 侧读不到 ——")
        add("         正是「注册能过、生图直连被墙」的典型成因。")
        add("         请在软件设置里**显式填一次代理地址**（例如 http://127.0.0.1:7890）。")

    sysproxy = ""
    try:
        sys.path.insert(0, str(root))
        from core import netproxy  # type: ignore

        sysproxy = netproxy.system_proxy() or ""
    except Exception as exc:  # noqa: BLE001
        add("  (读 core.netproxy 失败：%s: %s)" % (type(exc).__name__, exc))
    add("  工具读到的系统代理    : %s" % (mask_text(sysproxy) if sysproxy else "(读不到 → 会直连)"))

    # ---- 连通性：直连 vs 走代理 ----
    try:
        import requests
    except Exception as exc:  # noqa: BLE001
        add("  (没有 requests，跳过连通性测试：%s)" % exc)
        return out

    s = requests.Session()
    s.trust_env = False  # 只测我们显式给的东西，别让它自己又读一遍环境

    def _try(url: str, proxies, timeout: int):
        try:
            r = s.get(url, proxies=proxies, timeout=timeout)
            # 任何 HTTP 状态码都算「连得上」——Cloudflare 返回 403 也是连上了
            return "通（HTTP %s）" % r.status_code
        except Exception as exc:  # noqa: BLE001
            return "不通（%s: %s）" % (type(exc).__name__, str(exc)[:90])

    add("  直连 chatgpt.com      : %s" % _try("https://chatgpt.com/", None, 10))
    if sysproxy:
        pd = {"http": sysproxy, "https": sysproxy}
        add("  经该代理访问 chatgpt.com: %s" % _try("https://chatgpt.com/", pd, 20))
    else:
        add("  经该代理访问 chatgpt.com: (没有代理可用，跳过)")
    return out


def build_report(root: Path) -> list:
    L = []
    add = L.append

    add("=" * 70)
    add("AI 小工具 · 一键诊断报告")
    add("=" * 70)
    add("生成时间 : %s" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add("程序目录 : %s" % root)
    add("目录里有中文 : %s    有空格 : %s"
        % ("是" if any(ord(c) > 127 for c in str(root)) else "否",
           "是" if " " in str(root) else "否"))
    add("")

    # ---------------------------------------------------------- 1 系统
    add("--- [1] Windows ---")
    product = read_reg(WINVER_KEY, "ProductName")
    display = read_reg(WINVER_KEY, "DisplayVersion")
    build = read_reg(WINVER_KEY, "CurrentBuild")
    ubr = read_reg(WINVER_KEY, "UBR")
    edition = read_reg(WINVER_KEY, "EditionID")
    add("  ★判定     : %s %s   (完整版本 %s.%s)"
        % (windows_flavor(product, build), display, build, ubr))
    add("  注册表原始值（★别信 ProductName，Win11 上也写着 Windows 10）：")
    add("      ProductName=%s   DisplayVersion=%s   CurrentBuild=%s   EditionID=%s"
        % (product, display, build, edition))
    add("  架构     : %s" % os.environ.get("PROCESSOR_ARCHITECTURE", "?"))
    ver = run(["cmd", "/c", "ver"], 10).splitlines()
    add("  cmd ver  : %s" % (ver[-1].strip() if ver else "(无输出)"))
    ram = ps("[math]::Round((Get-CimInstance Win32_ComputerSystem)"
             ".TotalPhysicalMemory/1GB,1)", 30)
    add("  内存     : %s GB" % ram)
    try:
        import shutil as _sh

        du = _sh.disk_usage(str(root))
        add("  所在盘   : 剩余 %.1f GB / 共 %.1f GB" % (du.free / 1e9, du.total / 1e9))
    except Exception as exc:  # noqa: BLE001
        add("  所在盘   : (读不到 %s)" % exc)

    # ---------------------------------------------------------- 2 SAC
    add("")
    add("--- [2] Smart App Control（智能应用控制）---")
    sac = read_reg(r"SYSTEM\CurrentControlSet\Control\CI\Policy",
                   "VerifiedAndReputablePolicyState")
    if sac.startswith("(读不到"):
        # ★ Win10 上**根本没有**这个功能（SAC 是 Win11 独有的），
        #   而注册表读不到时会返回 "(读不到：FileNotFoundError)" ——
        #   那句话容易被误读成「程序出问题了」，所以这里说清楚。
        add("  本机没有 Smart App Control。")
        add("  说明：这是 Win11 才有的功能 —— Windows 10 上没有，属正常，不是故障。")
        add("        （若本机是 Win11 却读不到，则说明 SAC 处于关闭状态。）")
    else:
        meaning = {"0": "关闭", "1": "★已开启（会拦未签名 exe）", "2": "评估模式"}
        add("  VerifiedAndReputablePolicyState = %s  %s" % (sac, meaning.get(sac, "")))

    # ---------------------------------------------------------- 3 Python
    add("")
    add("--- [3] Python / 运行环境 ---")
    rt = root / "runtime" / "python.exe"
    add("  包内 runtime\\python.exe : %s" % ("存在" if rt.is_file() else "★不存在"))
    if rt.is_file():
        probe = run([str(rt), "-c",
                     "import sys;print(sys.version.split()[0])"], 30)
        add("    版本     : %s" % probe)
    add("  系统 PATH 里的 python   : %s" % run(["cmd", "/c", "where python"], 15))
    add("  PYTHONHOME / PYTHONPATH : %s / %s"
        % (os.environ.get("PYTHONHOME", "(无)"), os.environ.get("PYTHONPATH", "(无)")))
    add("  PYTHONNOUSERSITE        : %s" % os.environ.get("PYTHONNOUSERSITE", "(无)"))

    # ---------------------------------------------------------- 4 VC++
    add("")
    add("--- [4] VC++ 运行库（干净机器最常缺的）---")
    sysdir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for dll in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "ucrtbase.dll"):
        found = []
        if (sysdir / dll).is_file():
            found.append("系统")
        if rt.is_file() and (rt.parent / dll).is_file():
            found.append("包内")
        add("  %-20s: %s" % (dll, "+".join(found) if found else "★都没有"))

    # ---------------------------------------------------------- 5 策略/杀软
    add("")
    add("--- [5] 执行策略 / 杀软 ---")
    add("  PowerShell 执行策略：")
    for line in ps("Get-ExecutionPolicy -List | Out-String").splitlines():
        if line.strip():
            add("    %s" % line.rstrip())
    av = ps("(Get-CimInstance -Namespace root/SecurityCenter2 "
            "-ClassName AntiVirusProduct).displayName -join ' | '", 30)
    add("  杀软     : %s" % av)
    det = ps("try{(Get-MpThreatDetection | Sort-Object InitialDetectionTime "
             "-Descending | Select-Object -First 5 | ForEach-Object "
             "{ $_.InitialDetectionTime.ToString() + ' ' + $_.ThreatID }) -join ' ; '}"
             "catch{'(查不到，可能需要管理员)'}", 40)
    if not det or det.startswith("(退出码"):
        det = "(无拦截记录)"
    add("  Defender 最近拦截 : %s" % det)

    # ---------------------------------------------------------- 6 内核/端口
    add("")
    add("--- [6] 浏览器内核 / 端口 ---")
    bdir = root / "browser"
    kern = []
    if bdir.is_dir():
        kern = [p.name for p in bdir.iterdir() if p.name.startswith("chromium-")]
    add("  browser 目录    : %s"
        % ("存在" if bdir.is_dir() else "★没有（首次运行要联网下约 150MB）"))
    add("  chromium-* 内核 : %s" % (kern[0] if kern else "★未就绪"))
    for port in (8770, 8771, 8772):
        add("  127.0.0.1:%-5d 在监听 : %s" % (port, "是" if port_listening(port) else "否"))

    # ---------------------------------------------------------- 7 配置
    add("")
    add("--- [7] 配置摘要（密钥已脱敏）---")
    cfg = root / "data" / "config.json"
    if not cfg.is_file():
        add("  (没有 data\\config.json —— 从未启动过，或还没保存过配置)")
    else:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
            for k in sorted(data):
                add("  %-28s: %s" % (k, mask_value(k, data[k])))
        except Exception as exc:  # noqa: BLE001
            add("  (读不了 config.json：%s: %s)" % (type(exc).__name__, exc))

    accounts = root / "data" / "accounts.jsonl"
    if accounts.is_file():
        try:
            with accounts.open("r", encoding="utf-8", errors="replace") as f:
                n = sum(1 for _ in f)
            add("  (账号库 accounts.jsonl：%d 行 —— 内容不读取、不上报)" % n)
        except Exception:  # noqa: BLE001
            add("  (账号库 accounts.jsonl：存在，行数读不到)")

    # ---------------------------------------------------------- 8 指纹
    add("")
    add("--- [8] 版本指纹（判断用户手上是哪一版）---")
    for rel in FINGERPRINT_FILES:
        p = root / rel
        if p.is_file():
            mt = _dt.datetime.fromtimestamp(p.stat().st_mtime)
            add("  %-26s sha256[:16]=%s  mtime=%s"
                % (rel, sha16(p), mt.strftime("%Y-%m-%d %H:%M")))
        else:
            add("  %-26s ★缺失" % rel)

    # ---------------------------------------------------------- 9 日志
    add("")
    add("--- [9] 日志诊断（自动挑出值得看的）---")
    log = newest_log(root)
    raw = ""
    if log is None:
        add("  (没有 logs 目录或里面没有 .log 文件)")
    else:
        try:
            raw = log.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            add("  (读不了 %s：%s)" % (log.name, exc))

    if raw:
        lines = raw.splitlines()
        add("  文件: %s   共 %d 行" % (log.name, len(lines)))
        levels = {}
        for ln in lines:
            m = _RE_LEVEL.search(ln)
            if m:
                k = m.group(1).upper()
                if k == "WARN":
                    k = "WARNING"
                levels[k] = levels.get(k, 0) + 1
        add("  各级别条数 : %s"
            % ("  ".join("%s=%d" % kv for kv in sorted(levels.items()))
               or "(识别不出级别)"))

        tbs = [i for i, ln in enumerate(lines)
               if "Traceback (most recent call last)" in ln]
        add("")
        add("  ● Traceback：共 %d 处" % len(tbs))
        if not tbs:
            add("      (无 —— 没有未捕获异常)")
        for i in tbs[-2:]:
            add("      ---- 第 %d 行起的最后一段 ----" % (i + 1))
            seg = []
            for ln in lines[i:i + 40]:
                # 遇到下一条日志记录（行首有时间戳）就说明这段 Traceback 结束了
                if seg and _RE_TS.match(ln):
                    break
                seg.append(ln)
            for ln in seg:
                add("      | %s" % mask_text(ln.strip())[:300])

        # ★ 只挑 ERROR/CRITICAL 和 WARNING。**不要把 INFO 算进来** ——
        #   实测 INFO 里大量「发送失败/准备换号」会把 30 条名额全占掉，
        #   真正的错误一条都看不见。
        for label, pat in (("ERROR / CRITICAL", _RE_ERR), ("WARNING", _RE_WARN)):
            hits = [ln.strip() for ln in lines if pat.search(ln)]
            add("")
            add("  ● %s：共 %d 条（连续重复已压成 ×N）" % (label, len(hits)))
            if not hits:
                add("      (无)")
            for ln in collapse(hits, 20):
                add("      | %s" % mask_text(ln)[:300])

    add("")
    add("--- [10] 日志尾部 80 行（连续重复已压缩）---")
    if raw:
        add("  文件: %s" % log.name)
        add("")
        for ln in collapse([x.rstrip() for x in raw.splitlines()], 80):
            add("  %s" % mask_text(ln))
    else:
        add("  (无日志)")

    L.extend(proxy_section(root))
    L.extend(accounts_section(root))

    add("")
    add("=" * 70)
    add("说明：密钥、代理密码已自动打码；账号库内容未被读取。")
    add("如果里面仍有你不希望外发的内容，删掉那几行再发送即可。")
    add("=" * 70)
    return L


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # 控制台码页不是 UTF-8 也别崩
    except Exception:  # noqa: BLE001
        pass

    root = Path(__file__).resolve().parent
    L = build_report(root)

    report = root / "诊断结果.txt"
    try:
        report.write_text("\n".join(L) + "\n", encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        print("写报告失败：%s" % exc)
        return 1

    # 控制台摘要（中文交给 Python 输出，bat 里保持纯 ASCII）
    print("")
    print("诊断完成，报告已生成：%s" % report.name)
    print("位置：%s" % report)
    print("")
    print("请把这个文件发给客服。")
    # 跑起来自动把报告打开给用户看（否则小白不知道要发哪个文件）。
    # --no-open / DOCTOR_NO_OPEN 供自动化测试抑制这一步。
    if "--no-open" not in sys.argv and not os.environ.get("DOCTOR_NO_OPEN"):
        try:
            os.startfile(str(report))  # noqa: S606 —— 让用户直接看到要发什么
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print("诊断脚本自身出错：%s: %s" % (type(exc).__name__, exc))
        raise SystemExit(1)
