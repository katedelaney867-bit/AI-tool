# -*- coding: utf-8 -*-
"""邮箱来源 provider。

目前只有一家：**本站号池**（站内发密钥、站内计费）。
接口按多来源设计：新增来源只需实现 :class:`~core.mailbox.base.MailboxProvider`，
然后在下面登记。

★ 2026-10-18：直连外部邮箱上游的那条路**从代码里整体删掉了**。
  原因不是技术问题，是定价问题 —— 站内的邮箱是加价卖的，
  让用户看到上游是谁、卖多少钱，等于白给别人引流。所以这一版只剩本站号池。
"""
from __future__ import annotations

from .base import Inbox, MailboxError, MailboxProvider
from .sitepool import SitePoolProvider

PROVIDERS: dict[str, type[MailboxProvider]] = {
    SitePoolProvider.name: SitePoolProvider,
}

DEFAULT_PROVIDER = SitePoolProvider.name


def build(name: str, **kwargs) -> MailboxProvider:
    """按名字构造 provider。默认（也是目前唯一）走本站号池。"""
    key = str(name or "").strip().lower() or DEFAULT_PROVIDER
    if key not in PROVIDERS:
        raise MailboxError(f"未知的邮箱来源: {name}")
    return PROVIDERS[key](**kwargs)


def build_from_config(config) -> MailboxProvider:
    """按配置构造 provider —— **GUI 端点和后台引擎共用这一条路径**。

    两个入口各写一份构造逻辑曾经漂移过：列表能加载、一开跑就报 Key 错。
    所以收敛到这里，只认 ``mailbox_provider`` 一个字段。
    """
    provider = str(getattr(config, "mailbox_provider", "") or "").strip().lower() or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        raise MailboxError(
            f"不支持的邮箱来源「{provider}」：本版本只支持本站号池（mailbox_provider=sitepool）"
        )

    from ..sitepool import DEFAULT_BASE_URL

    api_key = str(getattr(config, "sitepool_tool_key", "") or "").strip()
    if not api_key:
        raise MailboxError(
            "还没填「工具密钥」：到站内「自建号池」页点一下「创建并复制」，粘到设置里再试"
        )
    base_url = str(getattr(config, "sitepool_base", "") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        raise MailboxError("本站号池没配接口地址：请在 config.json 里填 sitepool_base")

    return build(
        provider,
        api_key=api_key,
        base_url=base_url,
        service_mode=getattr(config, "mailbox_service_mode", "purchase"),
        supply_policy=getattr(config, "mailbox_supply_policy", "public_only"),
    )


def available() -> list[dict]:
    """GUI 下拉用的来源清单。"""
    return [{"name": cls.name, "label": cls.label} for cls in PROVIDERS.values()]


__all__ = [
    "Inbox",
    "MailboxError",
    "MailboxProvider",
    "PROVIDERS",
    "SitePoolProvider",
    "available",
    "build",
    "build_from_config",
]
