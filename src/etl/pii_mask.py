"""PII 脱敏工具（身份证号 / 手机号）。

掩码规则：保留可识别的前缀与后缀、中间以 * 替代——既便于人工核对，又不可还原。

    身份证号  510101190001010000 -> 510***********0000   （前 3 后 4）
    手机号     13900000000        -> 138****0000          （前 3 后 4）

示例值均为**虚构**（生日 1900-01-01、尾号全 0、138-0000-0000 测试段）：源码与文档里不得
出现真实号码，本文件只描述规则形态（旧示例的身份证校验位是合法的，无法排除来自真实记录，
故一并替换）。

供 s25（报告内命中值脱敏）与 s26（产出物脱敏副本）共用。
"""

from __future__ import annotations

import re

ID_PAT = re.compile(r"\b\d{17}[\dXx]\b")
PHONE_PAT = re.compile(r"\b1[3-9]\d{9}\b")


def mask_id(v: str) -> str:
    """身份证号脱敏：前 3 后 4，中间全 *。"""
    return v[:3] + "*" * (len(v) - 7) + v[-4:]


def mask_phone(v: str) -> str:
    """手机号脱敏：前 3 后 4。"""
    return v[:3] + "****" + v[-4:]


def mask_text(text: str) -> tuple[str, int]:
    """对整段文本脱敏，返回 (脱敏后文本, 脱敏处数)。"""
    text, a = ID_PAT.subn(lambda m: mask_id(m.group(0)), text)
    text, b = PHONE_PAT.subn(lambda m: mask_phone(m.group(0)), text)
    return text, a + b
