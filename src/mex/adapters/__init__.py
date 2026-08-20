"""会话格式适配层：各 agent 会话 → 纯对话文本（架构文档 §8.2）。

统一输出格式：``用户：<文本>`` / ``助手：<文本>`` 逐行交替。新增一种 agent
支持 = 在 ``adapters/`` 加一个解析模块，不动其他代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal


def _normalize_role(role: object) -> str | None:
    """角色名归一：仅保留 user/assistant，其余（system 等）返回 None。"""
    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant"
    return None


def _extract_text(content: object) -> str | None:
    """content 文本提取：字符串直取；数组拼接各元素的 text 字段。

    Claude/OpenCode 的 content 可能是字符串或对象数组（如
    ``[{"type": "text", "text": "..."}]``），非文本元素跳过。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item
            for item in content
            if isinstance(item, str) or (isinstance(item, dict) and isinstance(item.get("text"), str))
        ]
        if not parts:
            return None
        return "".join(item if isinstance(item, str) else item["text"] for item in parts)
    return None


def _render_turns(turns: list[tuple[str, str]]) -> str:
    """``(role, text)`` 列表 → ``用户：…/助手：…`` 逐行文本。"""
    return "\n".join(f"{'用户' if role == 'user' else '助手'}：{text}" for role, text in turns)


# noqa: E402 - 循环依赖：子模块需要上方 helpers，须在 import 前定义
from mex.adapters.claude import parse_claude_jsonl  # noqa: E402
from mex.adapters.opencode import parse_opencode_jsonl  # noqa: E402
from mex.adapters.plain import parse_plain  # noqa: E402

Format = Literal["plain", "claude", "opencode"]


def read_dialogue(source: str, fmt: Format) -> str:
    """读取并转换对话文本。

    Args:
        source: ``plain`` 时为文本本身；``claude``/``opencode`` 时为会话文件路径。
        fmt: 会话格式。

    Returns:
        纯对话文本（claude/opencode 转为"用户：/助手："行；plain 原样返回）。
    """
    if fmt == "plain":
        return parse_plain(source)
    text = Path(source).read_text(encoding="utf-8")
    if fmt == "claude":
        return parse_claude_jsonl(text)
    return parse_opencode_jsonl(text)
