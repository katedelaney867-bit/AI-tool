# -*- coding: utf-8 -*-
"""运行配置：单一 JSON 文件，GUI 直接读写。

设计目标是把小白要填的东西压到最少。主界面只有 5 项：

    1. 工具密钥（本站号池）
    2. 邮箱目标（下拉，自动列出后缀/库存/单价）
    3. 注册数量
    4. 并发数
    5. 代理（选填）

其余全部有合理默认值，放在「高级」折叠区里。配置存在项目根目录的
``data/config.json``，不写注册表、不写用户目录，卸载就是删文件夹。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, fields

from .paths import data_dir

logger = logging.getLogger(__name__)

# 与 core/mailbox/ 各来源的校验保持一致，界面下拉不许出现别的值。
MAILBOX_MODES = {"purchase", "code"}
MAILBOX_POLICIES = {"public_only", "private_first"}
# 邮箱来源：**只有** sitepool = 本站号池（站内发密钥、站内计费）。
# 2026-10-18 把「直连外部邮箱上游」那条路整体删了：站内邮箱是加价卖的，
# 让用户看到上游是谁、卖多少钱等于给别人引流。
MAILBOX_PROVIDERS = {"sitepool"}
# 接码来源：sitepool = 本站号池；smsbower / grizzly = 直连上游；
# miaoic = 网页号源的**网页接口**（不是 SMS-Activate handler API，见 core/miaoic.py）
SMS_PROVIDERS = {"sitepool", "smsbower", "grizzly", "miaoic", "shortcode", "fivesim", "longterm"}
# 老配置里可能还写着旧的名字（拼起来的，不在源码里留明文）：读到就自动换成新名字
_LEGACY_SMS_PROVIDER = {"api" + "cc": "longterm"}

# 网页号源接口地址。★ 它**只有 HTTP，没有 TLS**（对方的现状，不是我们的选择），
# 所以填进去的 Token 是明文过公网的 —— 别拿它当账号，用按次充值的那把。
MIAOIC_DEFAULT_BASE = "http://miaoic.shop"

# 本站号池的出厂接口地址。★ 换域名只改这一行（core/sitepool.py 的 DEFAULT_BASE_URL
# 故意留空，不再有第二份默认值，免得两处漂移）。
# 界面上没有这一项：用户在站上买密钥，地址是产品定死的，不该让用户去填。
SITEPOOL_DEFAULT_BASE = "https://tonecho.xyz"

DATA_DIR = data_dir()
CONFIG_PATH = DATA_DIR / "config.json"

_LOCK = threading.RLock()


@dataclass
class Config:
    # ---- 主界面 5 项 ----
    # 默认走本站号池：用户在站内拿两把密钥就行，不用自己注册上游账号。
    mailbox_provider: str = "sitepool"
    # 号池接口地址是「产品内置」的，不是用户选项 —— 用户在站上买密钥，
    # 地址固定，界面上不给看也不给改（以前设置里有一格，纯属让用户犯迷糊）。
    # 出厂值见 SITEPOOL_DEFAULT_BASE；只有换域名时才需要改这一个常量。
    sitepool_base: str = SITEPOOL_DEFAULT_BASE
    # ★ 站内密钥只有一把。以前分成「邮箱密钥 / 号码密钥」两把，是多余的：
    #   号池的 /v1/pool/* 全组共用同一个中间件、同一套身份解析，
    #   一把主站 API Key 既能取邮箱也能取号码（详见 core/oauth.py）。
    #   两把并存还引出过一个静默 bug：号码那侧写的是 `sms_key or mail_key`，
    #   钥匙串里留着一把旧的号码密钥就会**优先**拿它去取号，而邮箱走的是新密钥 ——
    #   表现是邮箱全正常、只有取号 401，极难排查。合并成一把就没有这个面了。
    sitepool_tool_key: str = ""
    sitepool_phone_sku: str = ""
    mailbox_target_id: str = ""
    register_count: int = 1
    concurrency: int = 1
    proxy: str = ""

    # ---- 高级：浏览器 ----
    headless: bool = True
    cloak_license_key: str = ""
    page_load_timeout: int = 90
    humanize: bool = True
    geoip: bool = True

    # ---- 半自动模式（给「搞不到住宅代理」的小白）----
    # ★ 碰到 Cloudflare 挑战时**不退出浏览器**，而是把窗口留在屏幕上等人手动过验证，
    #   过完工具接着把邮箱 / 邮箱验证码 / 手机号 / 手机验证码全自动填完。
    #
    #   为什么这样反而解决了代理问题：
    #   - 出口变成**用户自己家的宽带 IP**（本来就是干净住宅，比买来的池子稳）；
    #   - 唯一无法自动化的环节（人机验证）交给**人** —— 人比任何代理池都强；
    #   - 于是「没有住宅代理」不再是小白的死门槛。
    #
    #   ⚠️ 与 `headless` 互斥：人要在屏幕上点验证，后台跑就没意义了。
    #      validate() 会在开了半自动时把 headless 强制拉回 False。
    semi_auto: bool = False
    #: 等人处理的最长秒数。超时才算这一轮失败（默认 10 分钟，够泡杯茶）。
    semi_auto_wait: int = 600

    # ---- 高级：邮箱 ----
    mailbox_service_mode: str = "purchase"
    mailbox_supply_policy: str = "public_only"
    otp_timeout: int = 180
    # 邮箱上游有 60+ 个项目，全量拉一遍要几十秒。默认只拉 chatgpt 项目的
    # outlook.com 后缀 —— 注册 ChatGPT 要的就是这个。
    # （走本站号池时这两个过滤项不生效，见 mailbox/sitepool.py 的说明）
    mailbox_project: str = "chatgpt"
    mailbox_suffix: str = "outlook.com"

    # ---- 高级：注册后加固（设密码 + 绑 2FA） ----
    # ★ 勾上之后：注册完就给这个号设一个随机登录密码，并绑定 TOTP 认证器 2FA，
    #   密码和 secret 都落进账号记录。
    #   为什么需要：OTP 注册出来的号**没有密码**，二次登录只能再拿邮箱，
    #   等于交付出去只有「一次性登录」能力。
    # ★ 2026-10-06 默认值从 False 翻成 **True**：
    #   对小白来说「装完直接跑」就是全部的操作，默认关着的话他跑出来的号**没密码**，
    #   交付给买家也登不进去 —— 实测存量 102 个号里只有 17 个有密码，就是这么来的。
    #   注意：翻默认值只影响**新装**；老用户 config.json 里存过的值照旧（不会被动改）。
    harden_after_register: bool = True

    # ---- 高级：ChatGPT 桌面端激活（可选） ----
    enable_activation: bool = False
    sms_provider: str = "sitepool"
    sms_api_key: str = ""
    # 走 miaoic 时，sms_api_key 里放的是它的 **Token**（32 位十六进制），不是 API Key。
    # 地址是产品内置的：它是别家的小站，换域名只改这一行。
    miaoic_base: str = MIAOIC_DEFAULT_BASE
    # 走 shortcode（「短效接码」token 访问页，如 longnotes.cn/m/<token>）时填这个完整链接。
    # 这类页面是转售商自封装的：一个 token 一个号码、换号次数有限，
    # 所以工具默认**复用页面上已经拿到的号**，不白烧次数。
    shortcode_url: str = ""
    sms_service: str = "dr"
    sms_country: str = "10"
    sms_max_price: str = ""
    # ---- 5SIM 专用（2026-10-16 加）----
    # 5SIM 的下单路径是 /v1/user/buy/activation/{国家}/{运营商}/{产品}，
    # 所以国家和运营商是**名字**（indonesia/colombia/philippines…），不是数字 ID。
    # operator 默认 any = 让平台挑最便宜的可用运营商；实测本账户 virtual34 报
    # no free phones，能买到的是 virtual53/58/66/51。
    fivesim_country: str = "indonesia"
    fivesim_operator: str = "any"
    fivesim_product: str = "openai"
    # ---- 长效号源（2026-10-17 加）----
    # token 放通用字段 sms_api_key（跟 miaoic 一样，那一格填的是 Token 不是 Key）；
    # 这里只放平台侧参数。
    #   prefixes：号段白名单，逗号分隔、按顺序轮着买 —— **唯一的质量手段**
    #            （买了不能退不能换，只能靠选号段）。空 = 让平台自己挑。
    #   expiry：0=随机 1=5-30天 2=10-30天 … 6=>80天（1 优惠 10%，6 加价 10%）
    longterm_app_id: int = 0
    longterm_prefixes: str = ""
    longterm_expiry: int = 0
    # ★ 只给「长效号能不能吃第二单」的实验用：填了号码就**不买新号**、直接复用
    #   本地号表 `data/longterm_numbers.json` 里那些号的接码链接（旧码会被忽略）。
    #   留空 = 正常产线（每次都买新号）。之所以做成配置项：工具是长驻进程，
    #   靠环境变量传不进已启动的服务。
    longterm_pin_numbers: str = ""
    sms_max_retries: int = 5
    sms_code_wait: int = 120
    oauth_timeout: int = 300

    # ---- 高级：查活 ----
    liveness_check: bool = True
    liveness_timeout: int = 30
    #: 测活（批量查活）的并发数。
    #  ★ 2026-10-11 从「跟注册并发共用」拆出来单独一项。原来 start_liveness 用的是
    #    ``config.concurrency``，也就是查活快慢被注册并发绑住 —— 想快点查完就得把
    #    注册并发也调大，而那会让注册更容易被拦。两者抢的根本不是同一份资源
    #    （查活是纯 HTTP，注册要开浏览器），没理由共用一个值。
    #  默认 1 = 保持拆出来之前的行为（老的默认注册并发也是 1），不制造意外。
    liveness_concurrency: int = 1

    # ---- 生图 ----
    image_timeout: int = 300

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        known = {item.name: item for item in fields(cls)}
        raw = raw or {}
        clean: dict = {}
        for key, value in raw.items():
            if key not in known:
                continue
            target = known[key]
            try:
                if target.type in ("int", int):
                    clean[key] = max(0, int(value))
                elif target.type in ("bool", bool):
                    clean[key] = _as_bool(value)
                else:
                    clean[key] = str(value)
            except (TypeError, ValueError):
                continue

        # ★ 旧配置迁移：站内密钥从「邮箱密钥 + 号码密钥」合并成一把「工具密钥」。
        #   老 config.json 里那两个键现在不在 dataclass 里，会被上面的过滤丢掉；
        #   不收进来，用户升级后会看到「请填写工具密钥」——而他明明填过。
        #
        #   两者都在时取哪把？取**邮箱密钥**（它是必填的那把、界面上的主字段）。
        #   不取号码密钥是有意的：老逻辑里号码那侧是 `sms_key or mail_key`，
        #   而当初之所以会留下「两把不一致」的状态，多半就是号码那把是旧的没清掉 ——
        #   继续让它赢等于把老 bug 原样搬过来。两把不同时打个日志，方便排查。
        if "sitepool_tool_key" not in raw:
            legacy_mail = str(raw.get("sitepool_mail_key", "") or "").strip()
            legacy_sms = str(raw.get("sitepool_sms_key", "") or "").strip()
            merged = legacy_mail or legacy_sms
            if merged:
                clean["sitepool_tool_key"] = merged
            if legacy_mail and legacy_sms and legacy_mail != legacy_sms:
                logger.warning(
                    "[配置] 检测到旧的邮箱密钥与号码密钥不一致，已统一保留「邮箱密钥」这把；"
                    "号码那把被丢弃。若取号报 401，请到设置里确认工具密钥。"
                )

        # ★ 老配置的向后兼容：老版本没有 mailbox_provider 这个字段。
        # 2026-10-18 起邮箱来源只有本站号池一种，所以直接补上默认值 ——
        # 老配置里那些指向外部上游的残留键在 from_dict 里会被忽略（不再认识它们）。
        if "mailbox_provider" not in raw:
            clean["mailbox_provider"] = "sitepool"
        legacy_sms = str(clean.get("sms_provider", "") or "").strip().lower()
        if legacy_sms in _LEGACY_SMS_PROVIDER:
            clean["sms_provider"] = _LEGACY_SMS_PROVIDER[legacy_sms]
        if "sms_provider" not in raw and clean.get("sms_api_key", "").strip():
            clean["sms_provider"] = "smsbower"

        # ★ 号池地址是产品内置值，不是用户选项：老配置里它是空串（以前界面上
        #   有一格让人填），空着会让用户点号池直接报「没配接口地址」。
        #   在这里补回出厂地址，保证 load() 出来的 Config 一定是可用的。
        if not str(clean.get("sitepool_base", "") or "").strip():
            clean["sitepool_base"] = SITEPOOL_DEFAULT_BASE
        # 同理：miaoic 的地址也不该因为老配置里没有这个键就变成空串。
        if not str(clean.get("miaoic_base", "") or "").strip():
            clean["miaoic_base"] = MIAOIC_DEFAULT_BASE

        return cls(**clean)

    def validate(self) -> list[str]:
        """返回面向用户的错误清单，空列表表示可以开跑。"""
        problems: list[str] = []
        # 枚举值直接纠正而不是报错：老版本界面写过 rent / prefer_private 这类
        # 后端不认的值，留着会让邮箱目标加载直接失败，用户完全看不出为什么。
        if self.mailbox_service_mode not in MAILBOX_MODES:
            self.mailbox_service_mode = "purchase"
        if self.mailbox_supply_policy not in MAILBOX_POLICIES:
            self.mailbox_supply_policy = "public_only"
        if self.mailbox_provider not in MAILBOX_PROVIDERS:
            self.mailbox_provider = "sitepool"
        if self.sms_provider not in SMS_PROVIDERS:
            self.sms_provider = "sitepool"

        # 半自动模式要在屏幕上点验证 —— 后台跑就没意义了。直接纠正而不是报错：
        # 让用户去两个地方分别改，只会把他堵在门口。
        if self.semi_auto and self.headless:
            self.headless = False
            logger.info("[配置] 开了「半自动模式」，已自动关闭「后台运行浏览器」"
                        "（人要在屏幕上完成人机验证）")
        if self.semi_auto_wait < 30:
            self.semi_auto_wait = 30

        # 老版本的 config.json 里 sitepool_base 是空串（那时候界面上有一格让它填）。
        # 空着会让用户点号池直接报错，所以这里补回出厂地址 —— 这个键对用户是隐藏的，
        # 不该因为历史遗留把小白堵在门口。
        if self.mailbox_provider == "sitepool" and not self.sitepool_base.strip():
            self.sitepool_base = SITEPOOL_DEFAULT_BASE

        # 密钥要求按来源分别检查 —— 只有本站号池这一条路，密钥也只要一把，
        # 还去要它只会把用户堵在门口。
        # ★ 站内密钥只有一把，所以只要**有一条链路**走本站号池，就要它：
        #   邮箱走号池、或者（邮箱不走但）号码走号池 + 开了手机验证。
        needs_tool_key = self.mailbox_provider == "sitepool" or (
            self.enable_activation and self.sms_provider == "sitepool"
        )
        if needs_tool_key and not self.sitepool_tool_key.strip():
            problems.append("请填写「工具密钥」（在站内「自建号池」页创建并复制）")

        # 本站号池只有一个在售邮箱商品，目标是自动定的，不该再拿「请选择邮箱目标」堵人。
        if self.mailbox_provider != "sitepool" and not self.mailbox_target_id.strip():
            problems.append("请选择邮箱目标")
        if self.register_count < 1:
            problems.append("注册数量至少为 1")
        if self.concurrency < 1:
            problems.append("并发数至少为 1")
        # 并发超过订单数没意义，还白白多开浏览器。
        if self.concurrency > self.register_count:
            self.concurrency = self.register_count

        # 测活并发：只做范围收敛，**不跟注册并发联动**（见字段上的说明）。
        # 0/负数拉回 1；过大夹到 8（和界面上限一致，免得填 999 把上游打出风控）。
        if self.liveness_concurrency < 1:
            self.liveness_concurrency = 1
        if self.liveness_concurrency > 8:
            self.liveness_concurrency = 8

        if self.enable_activation and self.sms_provider == "shortcode":
            # 这类页面的凭证就是那条完整链接本身，没有 API Key 那一格
            if not str(self.shortcode_url or "").strip():
                problems.append("走「短效接码」时，要填它的访问页链接（https://…/m/<token>）")
        elif self.enable_activation and self.sms_provider != "sitepool" and not self.sms_api_key.strip():
            # miaoic 里这一格填的是 Token，不是 API Key —— 提示语要跟着变，
            # 否则用户会去它站上找「API Key」而那里根本没有这东西。
            if self.sms_provider == "miaoic":
                problems.append("走 miaoic 时，「接码平台 API Key」这一格请填它的 Token（32 位十六进制）")
            else:
                problems.append("开启「ChatGPT 桌面端激活」后需要填写接码平台 API Key")
        return problems


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load() -> Config:
    with _LOCK:
        if not CONFIG_PATH.exists():
            return Config()
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[配置] 读取失败，使用默认值: %s", exc)
            return Config()
        return Config.from_dict(raw if isinstance(raw, dict) else {})


def save(config: Config) -> Config:
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(CONFIG_PATH)
        return config


def update(patch: dict) -> Config:
    """按字段更新并落盘。未知字段忽略，非法值保持原样。"""
    with _LOCK:
        current = load()
        merged = current.to_dict()
        known = {item.name for item in fields(Config)}
        for key, value in (patch or {}).items():
            if key in known:
                merged[key] = value
        config = Config.from_dict(merged)
        # 先纠正再存：validate() 会把非法枚举拉回默认值、把并发夹到合理范围。
        # 不这么做的话，坏值会被原样写进 config.json，下次读出来继续报错。
        config.validate()
        return save(config)
