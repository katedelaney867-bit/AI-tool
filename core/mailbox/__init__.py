# -*- coding: utf-8 -*-
"""邮箱来源 provider。

两家：Remail（直连上游）和 sitepool（本站号池）。接口按多来源设计：
新增来源只需实现 :class:`~core.mailbox.base.MailboxProvider`，然后在下面登记。
"""
from __future__ import annotations

from .base import Inbox, MailboxError, MailboxProvider
from .remail import RemailProvider
from .sitepool import SitePoolProvider

PROVIDERS: dict[str, type[MailboxProvider]] = {
    SitePoolProvider.name: SitePoolProvider,
    RemailProvider.name: RemailProvider,
}


def build(name: str, **kwargs) -> MailboxProvider:
    """按名字构造 provider。

    默认走本站号池：站内发密钥、站内计费，用户不用碰上游账号。
    只有显式选 remail 才会去直连上游。
    """
    key = str(name or "").strip().lower() or SitePoolProvider.name
    if key not in PROVIDERS:
        raise MailboxError(f"未知的邮箱来源: {name}")
    return PROVIDERS[key](**kwargs)


def build_from_config(config) -> MailboxProvider:
    """按配置构造 provider —— **GUI 端点和后台引擎共用这一条路径**。

    之前 `app.py` 和 `engine.py` 各写了一份构造逻辑，而且是各自硬编码 "remail"。
    两处漂移过一次就够受了：加载目标能走本站、真注册还去打 Remail，
    用户看到的现象是「列表能加载、一开跑就报 Key 错」。所以收敛到这里。
    """
    provider = str(getattr(config, "mailbox_provider", "") or "").strip().lower() or SitePoolProvider.name
    if provider not in PROVIDERS:
        raise MailboxError(f"未知的邮箱来源: {provider}")

    if provider == SitePoolProvider.name:
        from ..sitepool import DEFAULT_BASE_URL

        api_key = str(getattr(config, "sitepool_tool_key", "") or "").strip()
        if not api_key:
            raise MailboxError(
                "还没填「工具密钥」：到站内「自建号池」页点一下「创建并复制」，粘到设置里再试"
            )
        base_url = str(getattr(config, "sitepool_base", "") or "").strip() or DEFAULT_BASE_URL
        if not base_url:
            raise MailboxError(
                "本站号池没配接口地址：请在 config.json 里填 sitepool_base，"
                "或者把邮箱来源换成直连上游"
            )
    else:
        api_key = str(getattr(config, "remail_api_key", "") or "").strip()
        if not api_key:
            raise MailboxError("还没填 Remail API Key")
        base_url = str(getattr(config, "remail_api_base", "") or "").strip()

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
    "RemailProvider",
    "SitePoolProvider",
    "available",
    "build",
    "build_from_config",
]
