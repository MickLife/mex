"""Claude Code 会话 JSONL 解析（架构文档 §8.2）。

每行为 ``{"type": "user|assistant|system", "message": {"role": ..., "content": ...}}``。
content 支持字符串或数组（含 ``text`` 字段的对象）；无法解析的行跳过不抛异常。
"""

from __future__ import annotations

import json

from mex.adapters import _extract_text, _normalize_role, _render_turns


def parse_claude_jsonl(text: str) -> str:
    """解析 Claude Code 会话 JSONL 为"用户：/助手："交替文本。

    Args:
        text: JSONL 原始文本。

    Returns:
        对话文本（损坏行与 system 行跳过）。
    """
    turns: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            continue
        role = _normalize_role(message.get("role") or data.get("type"))
        body = _extract_text(message.get("content"))
        if role is not None and body is not None:
            turns.append((role, body))
    return _render_turns(turns)
