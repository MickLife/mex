"""纯文本直通适配器：``mex extract --file <file>`` 的默认输入。"""

from __future__ import annotations


def parse_plain(text: str) -> str:
    """plain 直通：不做任何转换，原样返回。"""
    return text
