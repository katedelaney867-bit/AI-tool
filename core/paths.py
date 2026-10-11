# -*- coding: utf-8 -*-
"""路径解析：区分「打包进 exe 的只读资源」和「用户数据」。

开发运行：

    项目根/
      ├── app.py
      ├── ui/            <- 资源
      ├── data/          <- 用户数据
      └── logs/

PyInstaller 单文件运行：

    GPT注册助手.exe
    data/                <- 用户数据，在 exe 旁边
    logs/
    _MEIPASS/ui/         <- 资源，临时解压目录，退出即删

把 data/ 放在 exe 旁边，用户换 exe 不会丢账号；资源走 _MEIPASS，因为只读。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """只读资源（ui/ 等）所在目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """程序根目录：打包后是 exe 所在目录，开发时是项目根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """用户数据目录（账号、配置、凭证）。"""
    return data_root() / "data"


def logs_dir() -> Path:
    """运行日志目录。"""
    return data_root() / "logs"


def resource(*parts: str) -> Path:
    return resource_dir().joinpath(*parts)


def data(*parts: str) -> Path:
    return data_dir().joinpath(*parts)


def logs(*parts: str) -> Path:
    return logs_dir().joinpath(*parts)


def browser_dir() -> Path:
    """CloakBrowser 浏览器内核的缓存目录（放在程序目录旁边）。"""
    return data_root() / "browser"


def legacy_cache_dir() -> Path:
    """历史位置：cloakbrowser 默认的 ``~/.cloakbrowser``。

    单独抽成函数是为了**可测** —— 优先级逻辑要能在离线测试里被钉死
    （见 tools/test_preflight.py）。
    """
    return Path.home() / ".cloakbrowser"


def _looks_populated(path: Path) -> bool:
    """这个目录里是不是已经躺着下好的 chromium-*。"""
    try:
        return any(item.name.startswith("chromium-") for item in path.iterdir())
    except Exception:  # noqa: BLE001
        return False


def apply_runtime_env() -> str:
    """把 CloakBrowser 的内核缓存指到「程序目录旁边」，让整个文件夹可以整体搬走。

    ★ 必须在**任何 cloakbrowser 调用之前**执行。
      已核对过源码：`cloakbrowser.config.get_cache_dir()` 是**惰性**读
      `CLOAKBROWSER_CACHE_DIR` 的，所以只要在第一次 launch 之前设好就行。

    ★ 但不无脑覆盖：老用户机器上 `~/.cloakbrowser` 里可能已经躺着一份几百 MB 的内核，
      直接改目录 = 让他白等一次重新下载。所以按这个优先级挑：

        1. 用户自己设了 `CLOAKBROWSER_CACHE_DIR`  → 听用户的
        2. 程序目录旁边已经有内核（离线包预置）  → 用它
        3. `~/.cloakbrowser` 已经有内核           → 继续用它（不重下）
        4. 都没有                                  → 用程序目录（便于预置与整包搬走）

    返回最终选定的目录。
    """
    existing = os.environ.get("CLOAKBROWSER_CACHE_DIR", "").strip()
    if existing:
        return existing

    program = browser_dir()
    if _looks_populated(program):
        chosen = program
    else:
        legacy = legacy_cache_dir()
        chosen = legacy if _looks_populated(legacy) else program

    os.environ["CLOAKBROWSER_CACHE_DIR"] = str(chosen)

    # ★ 关掉 cloakbrowser 的自动更新检查（2026-10-06）。
    #   已核对源码：那两个检查都是 **fire-and-forget 守护线程**
    #   （download.py 的 `_maybe_trigger_update_check`），缓存命中时直接
    #   `return` 本机二进制，**不会被网络拖住启动**。
    #   但仍然要关掉，两个理由：
    #     1. 离线 / 被墙的机器上它们白跑一趟，还会打出「Update available」
    #        之类的提示，客户会以为程序在偷偷下东西；
    #     2. 我们要的就是**固定内核版本** —— 内核换了、指纹行为可能就变，
    #        注册成功率会莫名波动，而且极难归因。
    #   ★ 关掉它**不影响首次下载**：`ensure_binary()` 在缺二进制时照下不误，
    #     这个开关只关掉后台"看看有没有新版"。
    os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")

    return str(chosen)
