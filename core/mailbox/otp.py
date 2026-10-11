# -*- coding: utf-8 -*-
"""从邮件里抽取 6 位 OTP。

提炼自 turb-gpt-free-register/core/otp_utils.py，去掉 Outlook 专有部分。
要求：多语言关键字识别（英/中/日/韩）、字段名容错、上下文优先
（一封邮件里有多个 6 位数时，选离「验证码/code/認証」最近的那个）。
"""
from __future__ import annotations

import re

# 判断邮件是否来自 OpenAI / ChatGPT 的关键字。
OPENAI_KEYWORDS = (
    "chatgpt", "openai",
    "verification code", "code is", "your code", "verify your email",   # 英文
    "代码", "验证码", "确认码",                                                # 中文
    "認証コード", "検証コード", "確認コード", "一時検証", "認証",                  # 日文
    "인증 코드", "확인 코드",                                                      # 韩文
)

# 用于在多个 6 位数中挑出真正验证码的上下文关键字。
OTP_CONTEXT_KEYWORDS = (
    "code", "verify", "verification",
    "代码", "验证", "确认",
    "コード", "認証", "検証", "確認",
    "코드", "인증",
)

_OTP_REGEX = re.compile(r"\b(\d{6})\b")


def _get_field(item: dict, *names: str) -> str:
    """按顺序尝试多个字段名，兼容不同邮件 API 的命名约定。支持点路径。"""
    for name in names:
        if "." in name:
            value = item
            for part in name.split("."):
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
            if isinstance(value, str) and value:
                return value
        else:
            value = item.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def looks_like_openai_email(item: dict) -> bool:
    """判断邮件是否来自 OpenAI / ChatGPT。"""
    sender = _get_field(item, "sendEmail", "from", "fromEmail", "from.emailAddress.address", "sender").lower()
    sender_name = _get_field(item, "sendName", "fromName", "from.emailAddress.name").lower()
    subject = _get_field(item, "subject").lower()
    text = _get_field(item, "text", "bodyPreview", "bodyText").lower()
    content = _get_field(item, "content", "body", "html", "body.content", "bodyHtml").lower()

    if "openai" in sender or "openai" in sender_name:
        return True
    return any(keyword in blob for blob in (subject, text, content) for keyword in OPENAI_KEYWORDS)


def extract_otp(item: dict) -> str | None:
    """从邮件中抽出 6 位 OTP。

    顺序：主题 → 纯文本 → HTML（去标签）。主题里恰好一个 6 位数时最可信。

    正文里有多个 6 位数时，取**离验证码关键字最近**的那个。原实现是
    「窗口内含关键字就返回第一个」，等于按出现顺序取；邮件里同时出现订单号、
    时间戳之类的 6 位数时会选错，这里改成真的算距离。
    """
    subject = _get_field(item, "subject")
    if subject:
        codes_in_subject = _OTP_REGEX.findall(subject)
        if len(codes_in_subject) == 1:
            return codes_in_subject[0]

    candidates = [
        _get_field(item, "text", "bodyPreview", "bodyText"),
        _get_field(item, "content", "html", "body", "body.content", "bodyHtml"),
    ]
    for body in candidates:
        if not body:
            continue
        # text 字段也可能含 HTML（转发邮件常见），统一去标签。
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        all_codes = _OTP_REGEX.findall(body)
        if not all_codes:
            continue

        body_lower = body.lower()
        keyword_positions = _find_all(body_lower, OTP_CONTEXT_KEYWORDS)
        if keyword_positions:
            best_code: str | None = None
            best_distance: int | None = None
            for code in all_codes:
                for index in _find_all(body_lower, (code,)):
                    distance = min(abs(index - position) for position in keyword_positions)
                    if best_distance is None or distance < best_distance:
                        best_distance, best_code = distance, code
            if best_code is not None:
                return best_code
        return all_codes[0]
    return None


def _find_all(haystack: str, needles: tuple[str, ...]) -> list[int]:
    """返回所有 needle 在 haystack 中出现的起始位置。"""
    positions: list[int] = []
    for needle in needles:
        target = needle.lower()
        start = 0
        while True:
            index = haystack.find(target, start)
            if index < 0:
                break
            positions.append(index)
            start = index + 1
    return positions
