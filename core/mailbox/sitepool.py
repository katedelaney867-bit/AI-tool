# -*- coding: utf-8 -*-
"""本站号池邮箱来源。

把 :class:`~core.mailbox.base.MailboxProvider` 打到自己站内的号池接口上，
不再直连外部邮箱上游。用户只需要一把站内发的「工具密钥」。

与旧的直连 provider 的关系：接口完全一样（status / targets / allocate / wait_for_otp），
所以 `registration.py`、`browser_flow.py` 一行都不用改 —— 它们只认这个抽象。
"""
from __future__ import annotations

import logging
import time

from ..sitepool import (
    SitePoolAuthError,
    SitePoolClient,
    SitePoolError,
    SitePoolTimeout,
)
from .base import Inbox, MailboxError, MailboxProvider

logger = logging.getLogger(__name__)


def _split_filter(raw: str) -> list[str]:
    return [part.strip().lower().lstrip("@") for part in str(raw or "").split(",") if part.strip()]


def _money(value) -> str:
    try:
        return f"¥{float(value):.2f}"
    except (TypeError, ValueError):
        return "¥?"


class SitePoolProvider(MailboxProvider):
    """本站号池的邮箱资源。"""

    name = "sitepool"
    label = "本站号池"

    #: 拉商品目录失败时的重试次数（见 _catalog 的说明）。
    CATALOG_TRIES = 2

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        service_mode: str = "code",
        supply_policy: str = "",
        timeout: int = 30,
    ) -> None:
        # service_mode / supply_policy 是旧的直连 provider 的概念，本站用不到；
        # 这里保留参数是为了让 mailbox.build(...) 的调用方不用分支。
        self._client = SitePoolClient(base_url, mail_key=api_key, timeout=timeout)
        self._suffix_by_sku: dict[str, str] = {}
        self._catalog_cache: list[dict] | None = None

    # ------------------------------------------------------------- 目录

    def _catalog(self) -> list[dict]:
        """站内在售的邮箱商品。一次运行内只拉一次，避免每个任务都打一次站内接口。"""
        if self._catalog_cache is None:
            # ★ 2026-10-12 加重试：站内走一次网络抖动，原来会直接掉进
            #   resolve_target 的「拿配置值当 SKU」兜底，把一次抖动伪装成
            #   「商品已下架」（实测：23:44 报下架、23:47 同一单成功）。
            last: MailboxError | None = None
            for attempt in range(1, self.CATALOG_TRIES + 1):
                try:
                    self._catalog_cache = self._client.mail_targets()
                    break
                except SitePoolError as exc:
                    last = MailboxError(str(exc))
                    logger.warning("[号池] 拉邮箱商品列表失败（%s/%s）：%s",
                                   attempt, self.CATALOG_TRIES, exc)
                    if attempt < self.CATALOG_TRIES:
                        time.sleep(1.5)
            if self._catalog_cache is None:
                raise last or MailboxError("拉不到站内邮箱商品列表")
        return self._catalog_cache

    def resolve_target(self, configured: str = "") -> dict:
        """决定这一单买哪个 SKU。

        本站号池在售的邮箱商品通常只有一个，「选目标」对用户是个纯负担，
        所以配置里没写（或写的那个已经下架）时自动落到唯一在售的那个。
        真有多个在售时不做猜测，直接报错让人显式指定 —— 猜错就是花错钱。

        目录拉不到时（站内抖动）不硬拦：配置里写了什么就用什么，
        让下单那一步去报真正的原因。
        """
        want = str(configured or "").strip()
        try:
            rows = [r for r in self._catalog() if str(r.get("sku") or "").strip()]
        except MailboxError:
            # ★★ 2026-10-12 实测踩到的坑：这里原来只判 `if want` 就拿它当 SKU 去下单。
            #   但**老配置里留的是旧的目标格式** `项目ID:后缀`（如 `2:outlook.com`），
            #   那不是本站 SKU。于是「站内拉目录抖一下」会被伪装成
            #   「该资源已下架，请重新加载列表再选」—— 报错指向商品，真因是网络抖动，
            #   排查方向直接跑偏（我为此绕了一遍号池源码）。
            #   ⇒ 只在**看起来像本站 SKU**（不带冒号）时才敢兜底，
            #     否则老老实实报「拉不到列表」，让人重试。
            if want and ":" not in want:
                return {"sku": want, "suffix": self._suffix_by_sku.get(want, "")}
            raise MailboxError(
                "拉不到站内邮箱商品列表（站内抖动）。配置里的目标 "
                f"{want!r} 不是本站的 SKU（本站 SKU 形如 mail_microsoft_code），"
                "不敢拿它下单 —— 请稍等几秒重试；若反复出现，到「自建号池」页确认商品还在售"
            ) from None

        for row in rows:
            sku = str(row.get("sku") or "").strip()
            self._suffix_by_sku[sku] = str(row.get("suffix") or "").strip()

        if want and any(str(r.get("sku") or "").strip() == want for r in rows):
            return {"sku": want, "suffix": self._suffix_by_sku.get(want, "")}
        if len(rows) == 1:
            sku = str(rows[0].get("sku") or "").strip()
            return {"sku": sku, "suffix": self._suffix_by_sku.get(sku, "")}
        if not rows:
            raise MailboxError("站内没有在售的邮箱商品，请到「自建号池」页确认")
        names = "、".join(
            (self._suffix_by_sku.get(str(r.get("sku") or "").strip(), "")
             or str(r.get("sku") or "").strip())
            for r in rows[:5]
        )
        raise MailboxError(f"站内有多个在售邮箱商品（{names}），请在设置里指定「邮箱目标」")

    # ------------------------------------------------------------- 状态

    def status(self) -> dict:
        """给 GUI 顶部展示用：站内余额 + 在售邮箱数量。"""
        try:
            balance = self._client.balance()
        except SitePoolError as exc:
            raise MailboxError(str(exc)) from exc
        out = {
            "name": self.label,
            "balance": balance.get("balance"),
            "currency": balance.get("currency") or "CNY",
        }
        try:
            out["products"] = len(self._client.mail_targets())
        except SitePoolError:
            pass
        return out

    # ------------------------------------------------------------- 目标

    def targets(self, *, project_filter: str = "", suffix_filter: str = "") -> list[dict]:
        """把本站的在售邮箱商品拍平成 GUI 下拉。

        ``id`` 就是站内的 SKU，:meth:`allocate` 会把它原样传回去。

        ★ 关于过滤：``project_filter`` 是旧的直连 provider 的「项目」概念，本站没有，直接忽略；
        ``suffix_filter`` 只有在**确实筛得到东西**时才生效 ——
        否则用户从旧版本切过来时，配置里那条历史 ``mailbox_suffix=outlook.com``
        会把别的后缀全隐藏掉，看起来像「没有可用目标」。
        """
        rows = self._catalog()

        wanted = _split_filter(suffix_filter)
        if wanted:
            filtered = [row for row in rows if str(row.get("suffix") or "").lower() in wanted]
            if filtered:
                rows = filtered

        out: list[dict] = []
        for row in rows:
            sku = str(row.get("sku") or "").strip()
            if not sku:
                continue
            suffix = str(row.get("suffix") or "").strip()
            self._suffix_by_sku[sku] = suffix
            title = str(row.get("title") or "").strip()
            price = row.get("price")
            stock = row.get("stock")
            window = row.get("code_window_seconds")
            bits = [suffix or title or sku]
            if price is not None:
                bits.append(f"{_money(price)}/次")
            if isinstance(window, (int, float)) and window:
                bits.append(f"窗口 {int(window // 60)} 分钟")
            if isinstance(stock, (int, float)) and stock > 0:
                bits.append(f"现货 {int(stock)}")
            out.append({
                "id": sku,
                "sku": sku,
                "project_id": 0,
                "project_name": self.label,
                "suffix": suffix,
                "product_type": str(row.get("kind") or "mailbox"),
                "available": int(stock or 0),
                "price": price,
                "listed": True,
                "label": "  ·  ".join(bits),
            })
        return out

    # ------------------------------------------------------------- 下单

    def allocate(self, *, target_id: str, idempotency_key: str) -> Inbox:
        # target_id 允许为空：本站只有一个在售邮箱商品时由 resolve_target 自动定。
        picked = self.resolve_target(target_id)
        sku = picked["sku"]
        try:
            order = self._client.mail_allocate(sku, idempotency_key)
        except SitePoolError as exc:
            raise MailboxError(str(exc)) from exc

        email = str(order.get("email") or "").strip()
        order_id = str(order.get("order_id") or "").strip()
        if not email or not order_id:
            raise MailboxError("本站没有返回可用的邮箱，请重试")
        logger.info("[号池] 下单成功 order=%s sku=%s email=%s", order_id, sku, email)
        return Inbox(
            email=email,
            # 本站不需要 service_token：取码凭订单号，且订单号只有自己看得到
            service_token="",
            order_no=order_id,
            project_id=0,
            suffix=picked.get("suffix") or self._suffix_by_sku.get(sku, ""),
            provider=self.name,
            extra={"status": order.get("status"), "sku": order.get("sku") or sku},
        )

    # ------------------------------------------------------------- 取码

    def wait_for_otp(self, inbox: Inbox, *, after_ts: float, timeout: int = 180) -> str:
        """轮询本站取验证码。

        ``after_ts`` 是关键参数，原样透传给服务端：用户点过「重新发送」之后，
        调用方会把「上一次失败的时刻」传进来，服务端据此只认更新的邮件。
        忽略它的话会在失效的旧码上反复重试。
        """
        if not inbox.order_no:
            raise MailboxError("邮箱订单号缺失，请重新下单")
        try:
            return self._client.mail_wait_code(
                inbox.order_no, after=float(after_ts or 0), timeout=timeout
            )
        except SitePoolTimeout as exc:
            raise MailboxError(str(exc)) from exc
        except SitePoolAuthError as exc:
            raise MailboxError(str(exc)) from exc
        except SitePoolError as exc:
            raise MailboxError(str(exc)) from exc


# 供 GUI / 调试用：不带过滤器时本站在售的邮箱
def describe_targets(base_url: str, api_key: str) -> list[dict]:
    provider = SitePoolProvider(api_key=api_key, base_url=base_url)
    return provider.targets()


__all__ = ["SitePoolProvider", "describe_targets"]
