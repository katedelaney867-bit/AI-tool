# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：**onedir**（一个文件夹 + 里面一个 exe）。

    .venv\\Scripts\\pyinstaller.exe --clean --noconfirm build.spec

产物：``dist/AI小工具/AI小工具.exe``（连同 ``_internal/`` 一起发）

## ★ 2026-10-06 为什么从 onefile 改成 onedir

原来是单文件（onefile）。对小白有三个实打实的坏处：

1. **每次启动都要把 ~70MB 解压到 `%TEMP%`** —— 首启要等十几秒，用户以为卡死。
2. **`%TEMP%` 正是杀软最盯的目录**。实测本机构建完第一次 `Start-Process`
   就被 `An Application Control policy has blocked this file` 拦了一次。
3. 单文件大 exe 是杀软误报的经典形态（360/火绒/Defender）。

onedir 之后：启动快（不用解压）、误报率显著低。代价是交付物从一个文件
变成一个文件夹 —— 用 zip 或安装包发即可（见 `打包发布.ps1`）。

## 数据放哪

`ui/` 会被放进 `_internal/`；`data/`、`logs/`、`browser/` 都在 **exe 旁边生成**
（见 `core/paths.py`）—— 所以整个 `AI小工具` 文件夹可以整体搬走，账号不丢。
"""
from PyInstaller.utils.hooks import collect_submodules

# 延迟导入的模块静态分析可能漏掉，这里显式收全。
hiddenimports = [
    "core.mailbox", "core.mailbox.base", "core.mailbox.otp",
    "core.oauth", "core.oauth_flow", "core.registration", "core.liveness",
    "core.engine", "core.sms", "core.store", "core.settings", "core.profile",
    "core.stopflag", "core.paths", "core.humanize", "core.cloak_driver",
    "core.browser_flow", "core.refresh", "core.jobs", "core.importer",
    "core.quota", "core.imagegen", "core.netproxy",
    # ★ 2026-10-06 新增的两个：都是「函数内 import」，漏了会在运行时才炸。
    "core.credential_export", "core.preflight",
]
for package in ("uvicorn", "cloakbrowser", "curl_cffi", "selenium", "playwright", "anyio"):
    hiddenimports += collect_submodules(package)

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("ui", "ui")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 明显用不到的重量级依赖排除掉，控制体积。
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # ← onedir 的关键：二进制不塞进 exe
    name="AI小工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 保留控制台窗口：启动横幅里有「界面地址」「数据目录」「浏览器内核」，
    # 也是「关闭本窗口即停止程序」的唯一入口。出问题时错误也看得见。
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AI小工具",
)
