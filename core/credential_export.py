# -*- coding: utf-8 -*-
"""把注册出来的账号导出成**可直接交付**的凭证文件。

## 格式

一行一个账号，三个字段，用**四个短横线**分隔：

    邮箱----密码----2FA密钥

例：

    someone@example.com----AbCdEfGh12345678----AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

★★ **示例必须用编的值，绝不许拿真实账号当例子。**
   2026-10-11 查出来：这里原来抄的是**一个真实交付过的账号**（邮箱 + 真密码 + 真 2FA
   密钥三件套），而这个文件**已经在每一个发出去的包里** —— 等于每卖一份就送一个别人的
   账号。密钥审计器当时没抓到，因为它只比对 config.json 里的三把接口密钥，
   **不比对账号库里的密码 / 2FA 密钥**（审计器已同步补上这条）。
   **文档里的示例也是代码，也走审计。**

**缺的字段写「无」，不写空。** 两个原因：

1. 空字段在 `----` 分隔里**读不出边界** —— `a------b` 到底是「密码为空」还是
   「多打了一个分隔符」，人眼很容易看错。
2. 买家一眼就知道这个号没密码（得走邮箱验证码），不用猜。

## ★ 2026-10-05 改版（老大要求）

原来的交付包是个 zip：`credentials.csv` + `otpauth/<邮箱>.png` 二维码 + `README.txt`。
**现在只要一个纯文本**，而且**不要 otpauth 链接**。所以：

- 不做 CSV（`COLUMNS` / `build_row` 已删）
- 不做二维码（`make_qr_png` / `otpauth_uri` 已删，连 `segno` 依赖也不再需要）
- 不做 README（`readme_text` 已删）

**编码用 UTF-8、不带 BOM。** 不带 BOM 是为了让第一行的邮箱不被一个不可见的
`\\ufeff` 弄脏（带 BOM 的文件用脚本 `split("----")` 会中招）；
「无」这个字现代记事本、VS Code、任何编辑器都正常显示。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: 注册时生成的密码必须长这样（见 browser_flow.generate_password：
#: `PASSWORD_ALPHABET = ascii_letters + digits`、`PASSWORD_LENGTH = 16`）。
#: 不符合的一律**不可信** —— 原样卖出去就是卖一个登不进去的号。
PASSWORD_OK = re.compile(r"^[A-Za-z0-9]{16}$")
#: TOTP 密钥：base32（security_setup 生成的是 32 位）。
TOTP_OK = re.compile(r"^[A-Z2-7]{16,64}$")


def deliverable(record) -> tuple[bool, str]:
    """这个号能不能交给买家？返回 ``(能, 原因)``，不能时原因是给运营看的人话。

    ★ 判定规则（2026-10-06 定）：

    1. **必须有密码。** 没密码的号只能靠邮箱验证码登录，而邮箱在号池那边、
       早就过期了 —— 买家拿在手里等于登不进去。这就是「只有邮箱」的旧号
       一律不进交付的原因。
    2. **密码格式必须可信。** 实测 `data/accounts.jsonl` 里混进过一条 14 位、
       含非字母数字的（比如 `abc!def`），不可能的产物 ⇒ 不可信。
       ⚠️ 这里**不写那条真实密码**：以前写了，等于把它印在每个包里。
    3. **2FA 是可选的，但绑了就必须交得出密钥。** 已绑却丢密钥 = 把买家锁在门外，
       比不交付更糟。所以「加固记录说绑了」但没存到密钥的，也判不可交付。
    """
    if str(getattr(record, "status", "") or "") != "success":
        return False, "注册未成功"

    # ★ 2026-10-11 加：**已判定失效的号绝不能进交付包。**
    #   实测踩到：老大用一个机房 IP 注册出来的号**当场就被封了**，而生图只报
    #   一句看不懂的 `sentinel 握手失败 HTTP 401`。回头查这里才发现 ——
    #   原来 deliverable() 只看 status/密码/2FA，**完全没看 liveness**，
    #   于是一个已经死掉的号照样会被导出成交付凭证卖出去，买家登不上就来找你退钱。
    #   「查活」和接口级判定（store.mark_dead / apply_quota）都会写这个字段。
    liveness = str(getattr(record, "liveness", "") or "").strip().lower()
    if liveness == "dead":
        note = str(getattr(record, "liveness_note", "") or "").strip()
        return False, ("查活判定已失效/被封" + ("（%s）" % note if note else ""))

    password = str(getattr(record, "password", "") or "").strip()
    if not password:
        return False, "没有密码（买家只能靠邮箱验证码，登不进去）"
    if not PASSWORD_OK.match(password):
        return False, f"密码格式不可信（[{password}]，不是 16 位字母数字）"

    totp = str(getattr(record, "totp_secret", "") or "").strip().upper()
    if totp and not TOTP_OK.match(totp):
        return False, f"已绑 2FA 但密钥格式异常（[{totp}]，买了也进不去）"
    if not totp:
        note = str(getattr(record, "security_note", "") or "")
        if "已绑定" in note and "2FA" in note:
            return False, "加固记录说已绑 2FA，但没存到密钥（买了也进不去）"

    return True, ""


def split_deliverable(records) -> tuple[list, list[tuple[object, str]]]:
    """拆成 ``(可交付, [(被跳过的记录, 原因), ...])``。

    交付包只装前者；后者仍然留在 `accounts.jsonl` 里给你自己用。
    """
    ok: list = []
    skipped: list[tuple[object, str]] = []
    for record in records:
        good, reason = deliverable(record)
        if good:
            ok.append(record)
        else:
            skipped.append((record, reason))
    return ok, skipped

#: 字段分隔符。老大指定的就是四个短横线。
SEP = "----"
#: 字段缺失时写这个，而不是留空。
MISSING = "无"


def build_line(record) -> str:
    """把一条 AccountRecord 变成一行交付文本。缺的字段写「无」。"""
    def _f(name: str) -> str:
        value = str(getattr(record, name, "") or "").strip()
        return value if value else MISSING

    return SEP.join([_f("email"), _f("password"), _f("totp_secret")])


def export_text(records) -> str:
    """把记录列表拼成整份交付文本（每条一行，结尾带换行）。"""
    return "".join(build_line(r) + "\n" for r in records)


def export(records, out_file: str, *, only_deliverable: bool = True) -> dict:
    """写成交付文本文件。返回 ``{"file", "rows", "skipped", "skip_reasons"}``。

    ★ ``only_deliverable`` 默认 True —— **交付包只装能自己登录的号**
    （判定见 `deliverable`）。只有邮箱、没有密码的旧号不进交付，
    但仍然留在 `accounts.jsonl` 里给你自己用；想连它们一起导，
    显式传 ``only_deliverable=False``。
    """
    if only_deliverable:
        picked, skipped = split_deliverable(records)
    else:
        picked, skipped = list(records), []

    with open(out_file, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(export_text(picked))

    skip_reasons: dict[str, int] = {}
    for _record, reason in skipped:
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    if skipped:
        logger.info("[交付] 跳过 %s 个不可交付的号：%s", len(skipped), skip_reasons)
    logger.info("[交付] 导出 %s 行 -> %s", len(picked), out_file)
    return {
        "file": out_file,
        "rows": len(picked),
        "skipped": len(skipped),
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: -kv[1])),
    }
