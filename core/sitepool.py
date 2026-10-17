# -*- coding: utf-8 -*-
"""本站号池客户端 —— 把取邮箱/取号码/取验证码打到自己站内，而不是直连上游。

为什么要这么改
--------------
原来小工具是拿着用户自己的上游 Key 直连的：
用户要自己去注册两个上游账号、自己充值、还要把密钥交给一个第三方小工具。
改成本站号池之后，用户只需要在**站内**拿一把「工具密钥」：

    工具密钥  -> 本站号池的邮箱资源 + 号码资源（同一把就够）

为什么一把够：号池的 /v1/pool/* 整组挂在同一个 API Key 中间件上，邮箱单和号码单
用的是同一套身份解析（handler.identity() 都从 API Key 取用户），所以不存在
「取邮箱的密钥」和「取号码的密钥」之分。以前界面上分两格纯属多余，
还引出一个静默 bug（号侧 `sms_key or mail_key`，旧号码密钥会优先命中）。

密钥由站内发放、站内计费（按次扣余额），小工具不再接触上游凭据。

对接的接口（服务端 backend/internal/server/routes/pool.go）
-----------------------------------------------------------
    GET  /v1/pool/catalog                     目录：价格 / 后缀 / 库存 / 窗口
    GET  /v1/pool/balance                     余额
    POST /v1/pool/mail/orders                 下邮箱单（必须带 Idempotency-Key）
    GET  /v1/pool/mail/orders/{id}/code       取邮箱验证码
    POST /v1/pool/phone/orders                下号码单（必须带 Idempotency-Key）
    GET  /v1/pool/phone/orders/{id}/code      取号码验证码

统一信封：成功 ``{"code":0,"message":"success","data":{...}}``；
失败走 HTTP 非 2xx，body 形如 ``{"code":409,"message":"...","reason":"ACCOUNT_POOL_..."}``。

★ 计费口径（写清楚免得误会）
----------------------------
**下单即扣款**。所以调用方必须把 Idempotency-Key 传稳（网络重发不会重复扣）。
临时失败本站会自动退款：拿不到货当场退，进程死了 worker 兜底退，
号码窗口到期没收到码也会退。小工具这边不需要自己做退款逻辑。
"""
from __future__ import annotations

import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

# 本站号池接口地址。
# ★ 这里故意留空、不放默认域名：出厂地址只有一处定义，在 core/settings.py 的
#   SITEPOOL_DEFAULT_BASE（换域名只改那一行）。两处都写默认值迟早会漂移。
#   地址对用户是隐藏的，界面上没有这一项 —— 用户在站上买密钥，不该让他去填域名。
DEFAULT_BASE_URL = ""

# 出错时给小白看的话术：reason -> 人话
_REASON_TEXT = {
    "ACCOUNT_POOL_PRODUCT_NOT_FOUND": "该资源已下架，请重新加载列表再选",
    "ACCOUNT_POOL_PRICE_NOT_SET": "该资源还没定价，请联系站长",
    "ACCOUNT_POOL_INSUFFICIENT_BALANCE": "站内余额不足，请先到充值站充值",
    "ACCOUNT_POOL_NOT_CONFIGURED": "号池服务暂未配置，请联系站长",
    "ACCOUNT_POOL_UPSTREAM_BUSY": "上游暂时没有可用资源，稍后重试",
    "ACCOUNT_POOL_NO_STOCK": "上游暂时缺货，稍后重试或换一个资源",
    "ACCOUNT_POOL_ORDER_NOT_READY": "订单还没交付完成，请稍后再试",
    "ACCOUNT_POOL_ORDER_EXPIRED": "该订单的取码窗口已过期",
    "ACCOUNT_POOL_ORDER_NOT_FOUND": "订单不存在",
    "ACCOUNT_POOL_IDEMPOTENCY_REQUIRED": "缺少 Idempotency-Key",
    "ACCOUNT_POOL_KIND_NOT_SUPPORTED": "该类型资源暂未开放",
    "ACCOUNT_POOL_INVALID_QUANTITY": "数量不合法",
    # ★ 2026-10-11 新增：号池的「使用资格」门禁。
    #   服务端要求「调号池的密钥必须绑定到工具分组」，没兑换过的用户拿不到这种密钥。
    #   这条一定要翻成人话，否则小白看到的只是服务端那句原文，不知道去哪办。
    "POOL_TOOL_GROUP_REQUIRED": (
        "这把密钥没有「工具使用资格」—— 本站要求调用号池的密钥必须绑定到指定的工具分组。"
        "请先在站内兑换使用资格，然后重新创建一把绑定该分组的密钥填进来"
    ),
}


class SitePoolError(RuntimeError):
    """本站号池通用错误。"""


class SitePoolAuthError(SitePoolError):
    """密钥无效 / 未登录。"""


class SitePoolLicenseRequired(SitePoolError):
    """这把密钥没有使用资格（没兑换 / 没绑到工具分组）。

    单独一个类型是给上层用的：`preflight.check_pool` 要把它显示成
    「去站内兑换资格」而不是「连不上」—— 两者对用户的下一步动作完全不同。
    """


class SitePoolNoStock(SitePoolError):
    """上游缺货。"""


class SitePoolNoBalance(SitePoolError):
    """站内余额不足。"""


class SitePoolTimeout(SitePoolError):
    """等待验证码超时。"""


def _humanize(reason: str, message: str, status_code: int) -> str:
    text = _REASON_TEXT.get(str(reason or "").strip())
    if text:
        return text
    if message:
        return str(message)
    return f"本站号池请求失败（HTTP {status_code}）"


class SitePoolClient:
    """本站号池的 HTTP 客户端。

    邮箱单和号码单共用同一把「工具密钥」。构造时仍可分别传 mail_key / sms_key，
    那只是给测试脚本留的口子；正常使用两者是同一个值。
    它是普通的主站 API Key，站内按次扣的是**用户余额**（不占 quota）。
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        mail_key: str = "",
        sms_key: str = "",
        timeout: int = 30,
    ) -> None:
        base = str(base_url or "").strip() or DEFAULT_BASE_URL
        if not base.lower().startswith(("http://", "https://")):
            base = "https://" + base
        # 用户常直接把文档页或带路径的地址粘进来，这里只保留 scheme+host(+可选前缀)
        self.base_url = base.rstrip("/")
        self.mail_key = str(mail_key or "").strip()
        self.sms_key = str(sms_key or "").strip()
        self.timeout = max(5, int(timeout))
        self._session = requests.Session()
        # ★★ 号池请求**绝不走任何代理**（trust_env=False 同时关掉环境变量和
        #   Windows 注册表里的系统代理）。
        #
        #   为什么必须关：requests 默认 trust_env=True，在 Windows 上会经
        #   urllib.request.getproxies() 读到注册表里的系统代理（Clash 的
        #   127.0.0.1:7890），号池请求于是也被塞进那个代理。而代理通常没把本站
        #   域名放进直连规则 → TLS 握手直接失败，表现为
        #   「连不上本站号池（POST /v1/pool/mail/orders）：SSLError」。
        #
        #   以前 base 是 127.0.0.1:18088（到改良机的本地隧道），系统代理的
        #   ProxyOverride 里有 127.* 会自动绕过，所以这个坑一直没暴露；
        #   2026-10-04 把 base 换成 https://tonecho.xyz 后立刻踩到。
        #
        #   业务上也不该走：那个代理是给 OpenAI 注册用的**计费**住宅代理，
        #   本站号池是自己的服务器，直连更快、也不烧代理流量。
        self._session.trust_env = False

    # ------------------------------------------------------------- HTTP

    def _request(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        if not api_key:
            raise SitePoolAuthError("还没填本站密钥，请先在设置里填好再试")
        request_headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
        if headers:
            request_headers.update(headers)
        url = self.base_url + (path if path.startswith("/") else f"/{path}")
        try:
            response = self._session.request(
                method.upper(), url,
                params=params, json=json_body, headers=request_headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SitePoolError(
                f"连不上本站号池（{method.upper()} {path}）：{type(exc).__name__}。"
                "检查一下「站点地址」填对没、以及本机网络。"
            ) from exc

        payload = None
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code in (401, 403):
            # ★ 2026-10-11：原来这里**把 reason 丢掉了**（只把 message 传给 _humanize），
            #   于是服务端新增的错误码翻不出人话，用户只看到一句原文。
            #   现在按 reason 分流：缺资格和密钥无效是完全不同的两件事，
            #   用户要做的动作也不同，不能都塞进同一句话。
            reason = str((payload or {}).get("reason") or "") if isinstance(payload, dict) else ""
            message = str((payload or {}).get("message") or "") if isinstance(payload, dict) else ""
            detail = _humanize(reason, message, response.status_code)
            if reason == "POOL_TOOL_GROUP_REQUIRED":
                raise SitePoolLicenseRequired(detail)
            if response.status_code == 401:
                raise SitePoolAuthError(f"本站密钥无效或已停用（{detail}）")
            raise SitePoolError(detail)

        if response.status_code >= 400:
            reason = str((payload or {}).get("reason") or "") if isinstance(payload, dict) else ""
            message = str((payload or {}).get("message") or "") if isinstance(payload, dict) else ""
            text = _humanize(reason, message, response.status_code)
            # ★ 2026-10-06：ACCOUNT_POOL_UPSTREAM_BUSY 以前落到下面那个通用 SitePoolError，
            #   于是 SitePoolSmsClient._translate 把它翻成兜底的 SmsBaseError —— 类型信息也丢了。
            #   后果（生产实测）：smsbower 502 期间号池一直回这个码，调用方按关键字
            #   （NO_NUMBERS / 暂无可用号码）判断「无号止损」，一个都不命中，
            #   于是对着一个「上游整体取不到号」的错误老老实实换号 12 次
            #   （约 70 秒 + 12 次下单，次次必然失败）。
            #   语义上它就是「上游取不到号」，与 NO_STOCK 同类，归到同一个异常。
            if reason in ("ACCOUNT_POOL_NO_STOCK", "ACCOUNT_POOL_UPSTREAM_BUSY"):
                raise SitePoolNoStock(text)
            if reason == "ACCOUNT_POOL_INSUFFICIENT_BALANCE":
                raise SitePoolNoBalance(text)
            raise SitePoolError(text)

        if not isinstance(payload, dict):
            raise SitePoolError(f"本站返回了看不懂的内容（{method.upper()} {path}）")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------- 目录 / 余额

    def balance(self) -> dict:
        return self._request("GET", "/v1/pool/balance", api_key=self.mail_key or self.sms_key)

    def mail_targets(self) -> list[dict]:
        data = self._request("GET", "/v1/pool/catalog", api_key=self.mail_key)
        return [item for item in (data.get("products") or []) if item.get("kind") == "mailbox"]

    def phone_targets(self) -> list[dict]:
        data = self._request("GET", "/v1/pool/catalog", api_key=self.sms_key)
        return [item for item in (data.get("products") or []) if item.get("kind") == "phone"]

    # ------------------------------------------------------------- 邮箱

    def mail_allocate(self, sku: str, idempotency_key: str) -> dict:
        """下一枚邮箱单，返回 ``{"order_id","email","status",...}``。

        ★ 服务端下单即扣款，所以 idempotency_key 必须稳定且唯一 ——
        同一次注册重试要用同一个 key，网络重发才不会重复扣。
        """
        sku = str(sku or "").strip()
        idem = str(idempotency_key or "").strip()
        if not sku:
            raise SitePoolError("还没选邮箱资源，点「加载邮箱目标」选一个")
        if not idem or len(idem) > 128:
            raise SitePoolError("内部幂等键无效")
        data = self._request(
            "POST", "/v1/pool/mail/orders",
            api_key=self.mail_key,
            json_body={"sku": sku, "quantity": 1},
            headers={"Idempotency-Key": idem},
        )
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        if not order.get("order_id") or not order.get("email"):
            raise SitePoolError("本站没有返回可用的邮箱，请重试")
        return order

    def mail_wait_code(
        self,
        order_id: str,
        *,
        after: float = 0.0,
        timeout: int = 180,
        poll_interval: float = 3.0,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询本站取邮箱验证码。

        ``after`` 是「只接受比这个 Unix 时间更新的邮件」，直接透传给服务端。

        ★ 这个参数必须传：用户点过「重新发送」之后 OpenAI 会再发一封，
        如果服务端不知道「只要新的」，它会把上一封里那个已经失效的码再给一次，
        注册流程就会在同一个错码上死循环。
        """
        order_id = str(order_id or "").strip()
        if not order_id:
            raise SitePoolError("邮箱订单号缺失")
        deadline = time.monotonic() + max(5, int(timeout))
        interval = max(2.0, float(poll_interval))
        rounds = 0
        last_error = "收件箱还没有新邮件"
        logger.info("[号池] 等待邮箱验证码 order=%s 最长 %ss after=%s", order_id, timeout, after or "-")

        while True:
            if stop_event is not None and stop_event.is_set():
                raise SitePoolError("用户已请求停止")
            rounds += 1
            params = {}
            if after and float(after) > 0:
                params["after"] = "%.3f" % float(after)
            try:
                data = self._request(
                    "GET", f"/v1/pool/mail/orders/{order_id}/code",
                    api_key=self.mail_key, params=params or None,
                )
                code = str(data.get("code") or "").strip()
                if code:
                    logger.info("[号池] 第 %s 轮收到邮箱验证码 order=%s", rounds, order_id)
                    return code
                # waiting=true 是正常状态，不是错误
                retry = data.get("retry_after_seconds")
                if isinstance(retry, (int, float)) and retry > 0:
                    interval = max(2.0, float(retry))
            except (SitePoolAuthError,) as exc:
                raise
            except SitePoolError as exc:
                last_error = str(exc)
                # 窗口过期是终态，不必死等到超时
                if "过期" in last_error or "不存在" in last_error:
                    raise
                logger.debug("[号池] 取邮箱验证码失败，继续重试: %s", last_error)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise SitePoolTimeout(f"等待邮箱验证码超时（{timeout}s）：{last_error}")

    # ------------------------------------------------------------- 号码

    def phone_allocate(self, sku: str, idempotency_key: str) -> dict:
        """下一枚号码单，返回 ``{"order_id","phone","status",...}``。"""
        sku = str(sku or "").strip()
        idem = str(idempotency_key or "").strip()
        if not sku:
            raise SitePoolError("还没选号码资源")
        if not idem or len(idem) > 128:
            raise SitePoolError("内部幂等键无效")
        data = self._request(
            "POST", "/v1/pool/phone/orders",
            api_key=self.sms_key,
            json_body={"sku": sku, "quantity": 1},
            headers={"Idempotency-Key": idem},
        )
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        if not order.get("order_id") or not order.get("phone"):
            raise SitePoolError("本站没有返回可用的号码，请重试")
        return order

    def phone_wait_code(
        self,
        order_id: str,
        *,
        timeout: int = 120,
        poll_interval: float = 5.0,
        stop_event: threading.Event | None = None,
    ) -> str:
        """轮询本站取号码验证码。"""
        order_id = str(order_id or "").strip()
        if not order_id:
            raise SitePoolError("号码订单号缺失")
        deadline = time.monotonic() + max(5, int(timeout))
        interval = max(2.0, float(poll_interval))
        rounds = 0
        last_error = "还没收到短信"
        logger.info("[号池] 等待短信验证码 order=%s 最长 %ss", order_id, timeout)

        while True:
            if stop_event is not None and stop_event.is_set():
                raise SitePoolError("用户已请求停止")
            rounds += 1
            try:
                data = self._request(
                    "GET", f"/v1/pool/phone/orders/{order_id}/code", api_key=self.sms_key
                )
                code = str(data.get("code") or "").strip()
                if code:
                    logger.info("[号池] 第 %s 轮收到短信验证码 order=%s", rounds, order_id)
                    return code
                retry = data.get("retry_after_seconds")
                if isinstance(retry, (int, float)) and retry > 0:
                    interval = max(2.0, float(retry))
            except SitePoolAuthError:
                raise
            except SitePoolError as exc:
                last_error = str(exc)
                if "过期" in last_error or "不存在" in last_error:
                    raise
                logger.debug("[号池] 取短信验证码失败，继续重试: %s", last_error)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise SitePoolTimeout(f"等待短信验证码超时（{timeout}s）：{last_error}")

    def phone_cancel(self, order_id: str) -> dict:
        """主动退掉一个还没取到码的号码单，返回退号/退款结论。

        ★ 站内是「先探测、再退号、后退款」：它会先问上游有没有码 ——
          上游已经有码就落库并释放号码（**不取消、不退款**，因为号码确实交付了），
          确认还在等码才退号 + 把 ¥0.40 退回余额。重复调用幂等。

        返回形如 ``{"order_id","status","cancelled","refunded","reason"[,"code"]}``。
        """
        order_id = str(order_id or "").strip()
        if not order_id:
            raise SitePoolError("号码订单号缺失")
        return self._request(
            "POST", f"/v1/pool/phone/orders/{order_id}/cancel", api_key=self.sms_key
        )


class SitePoolSmsClient:
    """把本站号码接口包装成和 :class:`core.sms.SmsClient` 一样的形状。

    这样 `core/oauth.py` 与上层流程都不用改，只是换了个对象。
    方法签名刻意和 SmsClient 保持一致：acquire / wait_code / set_status / complete / cancel。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        sku: str = "",
        timeout: int = 30,
    ) -> None:
        from .sms import SmsCodeTimeout, SmsError, SmsNoBalance, SmsNoNumbers

        self.label = "本站号池"
        self.provider = "sitepool"
        self._errors = {
            "base": SmsError,
            "timeout": SmsCodeTimeout,
            "nobalance": SmsNoBalance,
            "nonumbers": SmsNoNumbers,
        }
        self._client = SitePoolClient(base_url, mail_key="", sms_key=api_key, timeout=timeout)
        self.sku = str(sku or "").strip()

    # 号码商品没配就自动挑第一个在售的 —— 小白不应该被要求知道 SKU
    def _resolve_sku(self) -> str:
        if self.sku:
            return self.sku
        targets = self._client.phone_targets()
        if not targets:
            raise SitePoolError("本站还没有在售的号码资源，请联系站长")
        self.sku = str(targets[0].get("sku") or "").strip()
        return self.sku

    def _translate(self, exc: Exception) -> Exception:
        if isinstance(exc, SitePoolTimeout):
            return self._errors["timeout"](str(exc))
        if isinstance(exc, SitePoolNoBalance):
            return self._errors["nobalance"](str(exc))
        if isinstance(exc, SitePoolNoStock):
            return self._errors["nonumbers"](str(exc))
        return self._errors["base"](str(exc))

    def acquire(self) -> tuple[str, str]:
        """取一个号，返回 (订单号, 手机号)。手机号不带 + 前缀。"""
        try:
            sku = self._resolve_sku()
            order = self._client.phone_allocate(sku, idempotency_key=f"sms-{time.time_ns()}")
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        logger.info("[号池] 取号成功 sku=%s", sku)
        return str(order.get("order_id") or ""), str(order.get("phone") or "")

    def wait_code(
        self,
        activation_id: str,
        *,
        max_wait: int = 120,
        poll_interval: int = 5,
        stop_event: threading.Event | None = None,
    ) -> str:
        try:
            return self._client.phone_wait_code(
                activation_id,
                timeout=max_wait,
                poll_interval=max(2, int(poll_interval)),
                stop_event=stop_event,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    def set_status(self, activation_id: str, status: int) -> str:
        """本站不需要客户端报状态：拿到验证码时服务端已经自动释放号码了。"""
        logger.debug("[号池] set_status(%s) 在本站由服务端处理，忽略", status)
        return ""

    def complete(self, activation_id: str) -> None:
        """同上：服务端在返回验证码时已经把号码释放掉了。"""
        return

    def cancel(self, activation_id: str) -> None:
        """换号时主动退号：明确告诉站里「这个号不要了」。

        ★ 以前这里是空转（只打一条日志），代价不是钱而是**时间**：
          每换一次号就把号码和白付的 ¥0.40 攥到窗口结束（最长 20 分钟），
          号码还被占着不能给别人。2026-10-14 站内加了
          ``POST /v1/pool/phone/orders/:id/cancel``：先探测上游有没有码
          （有码就落库、不取消），确认没码才退号 + 全额退款。
          SMSBower 对没收到码的号码是全额退的，所以换号的成本从此只剩几秒。

        ★ 这里**永不抛异常**：它是在换号的重试循环里、已经失败之后调的，
          清理动作不该把原始失败原因盖掉（调用方要看的是「这个号为什么不行」）。
          即使这次没退掉，站内 worker 仍会在窗口内兜底退号退款。
        """
        order_id = str(activation_id or "").strip()
        if not order_id:
            return
        try:
            data = self._client.phone_cancel(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[号池] 主动退号失败（不影响换号，站内会在窗口内兜底退款）：%s", exc
            )
            return
        refunded = bool(data.get("refunded"))
        cancelled = bool(data.get("cancelled"))
        reason = str(data.get("reason") or "")
        if cancelled or refunded:
            logger.info("[号池] 已退号 order=%s 已退款=%s 说明=%s", order_id, refunded, reason)
        else:
            logger.info("[号池] 未退号 order=%s 说明=%s", order_id, reason)
