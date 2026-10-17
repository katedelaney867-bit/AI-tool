# -*- coding: utf-8 -*-
"""邮箱 provider 抽象。

目前只接本站号池一家，但接口按多来源设计：以后要加自建来源
临时邮箱或 Outlook 池，只需要新增一个实现类并注册，GUI 不用改。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class MailboxError(RuntimeError):
    """下单、取件或凭证失效。"""


@dataclass
class Inbox:
    """一次邮箱订单的取件上下文。

    service_token 是订单级凭证，必须和邮箱一起带着走 —— 不能只凭邮箱地址
    重新拼取件请求。
    """

    email: str
    service_token: str = ""
    order_no: str = ""
    project_id: int = 0
    suffix: str = ""
    provider: str = ""
    extra: dict = field(default_factory=dict)


class MailboxProvider(ABC):
    """邮箱来源必须实现的接口。"""

    name: str = ""
    label: str = ""

    @abstractmethod
    def status(self) -> dict:
        """账号状态：余额、额度、并发限制等，用于 GUI 顶部展示。"""

    @abstractmethod
    def targets(self, *, project_filter: str = "", suffix_filter: str = "") -> list[dict]:
        """可选目标列表，拍平成 GUI 里的一个下拉框。

        每项至少包含 ``id``（稳定标识）、``label``（显示文本）、
        ``available``（可用库存）、``price``（单价）字段。

        ``project_filter`` / ``suffix_filter`` 是逗号分隔的关键词，用来收窄结果。
        对号池这种上游，带上项目名能少发几十个请求，GUI 默认就带。
        两个参数都留空表示「全都要」（慢，但完整）。
        """

    @abstractmethod
    def allocate(self, *, target_id: str, idempotency_key: str) -> Inbox:
        """按目标下单一枚邮箱，等待交付凭证返回。"""

    @abstractmethod
    def wait_for_otp(self, inbox: Inbox, *, after_ts: float, timeout: int = 180) -> str:
        """轮询收件箱，返回 after_ts 之后最新的 OpenAI 验证码。"""
