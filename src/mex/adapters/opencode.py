"""OpenCode 会话存储解析（架构文档 §8.2）。

OpenCode 会话格式未完全固定，采用尽力解析：逐行 ``json.loads``，递归查找含
``role`` + ``content`` 字段的对象；无法解析的行跳过不抛异常。
"""

from __future__ import annotations

import json

from mex.adapters import _extract_text, _normalize_role, _render_turns


def parse_opencode_jsonl(text: str) -> str:
    """解析 OpenCode 会话为"用户：/助手："交替文本。

    Args:
        text: 会话存储原始文本。

    Returns:
        对话文本（损坏行跳过）。
    """
    turns: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        turn = _find_turn(data)
        if turn is not None:
            turns.append(turn)
    return _render_turns(turns)


def _find_turn(data: object) -> tuple[str, str] | None:
    """递归查找第一个含 role+content 的 ``(归一角色, 文本)``。"""
    if isinstance(data, dict):
        role = _normalize_role(data.get("role"))
        content = data.get("content")
        if role is not None and content is not None:
            body = _extract_text(content)
            if body is not None:
                return role, body
        for value in data.values():
            found = _find_turn(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_turn(item)
            if found is not None:
                return found
    return None
