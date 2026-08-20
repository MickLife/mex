"""CLI 通用件：错误类型、初始化校验、输出辅助。

供全部 CLI 模块（write/query/extract/review/backup/integrate）复用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from mex.domain.memory import Confidence
from mex.store.connection import migrate

if TYPE_CHECKING:
    from mex.domain.memory import Memory
    from mex.domain.schema import Schema


class UserError(Exception):
    """用户错误：参数非法、schema 校验失败、目标条目不存在 → 退出码 1。"""


class ExternalError(Exception):
    """外部错误：LLM 调用失败、数据库 IO 错误 → 退出码 2。"""


def ensure_initialized(mex_home: str) -> None:
    """校验 meX 已初始化（mex.db 存在），并执行结构迁移。

    Args:
        mex_home: 数据目录。

    Raises:
        UserError: 数据目录尚未初始化。
        RuntimeError: 数据库版本高于当前程序支持版本。
    """
    db_path = Path(mex_home) / "mex.db"
    if not db_path.exists():
        raise UserError(f"Data directory {mex_home} is not initialized. Run `mex init` first.")
    migrate(db_path)


def print_json(obj: object) -> None:
    """JSON 输出（ensure_ascii=False 保证中文可读），供 ``--json`` 全局选项使用。"""
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2))


def render_memory_entries(memories: list[Memory], schema: Schema | None = None) -> str:
    """记忆列表的块状条目格式（每条两行，内容完整不截断）。

    第一行元信息：序号、完整 id、``topic.sub_topic``、置信度标注、最后更新时间（本地）；
    第二行完整内容（缩进，超长自然折行）。置信度标注：``（已确认）``=confirmed、
    ``（用户明说）``=explicit、``（推断）``=inferred、``（待确认）``=speculated/uncertain；
    软删条目追加（已遗忘）。字段省略规则：无 sub_topic 显示 ``—``。
    传入 ``schema`` 时，结构化字段（schema 声明了 fields）的 JSON 对象 content
    渲染为 ``键：值`` 可读文本，与 profile 一致；未传 schema 或非结构化字段保持原样。

    Args:
        memories: 记忆列表。
        schema: 可选；用于把结构化字段的 JSON content 渲染为可读文本。

    Returns:
        格式化文本（多行），由调用方输出；空列表返回空字符串。
    """
    lines: list[str] = []
    for i, m in enumerate(memories, start=1):
        slot = _slot_text(m)
        confidence = _render_confidence(m)
        if m.is_forgotten():
            confidence += " [forgotten]"
        meta = f"{i}. {m.id} · {slot} · {confidence} · {_local_datetime(m.updated_at)}"
        lines.append(meta)
        lines.append(f"   {_display_content(m, schema)}")
    return "\n".join(lines)


def _display_content(m: Memory, schema: Schema | None) -> str:
    """条目 content 展示：结构化字段渲染为可读文本，其余原样。"""
    if schema is None or not m.topic or not m.sub_topic:
        return m.content
    if not schema.is_structured(m.topic, m.sub_topic):
        return m.content
    part = _render_structured(m.content, schema, m.topic, m.sub_topic)
    return part if part else m.content


def _render_structured(content: str, schema: Schema, topic: str, sub_topic: str) -> str:
    """结构化 JSON content 渲染为 ``键：值`` 顿号分隔文本；解析失败返回空串。"""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    spec = schema.topics[topic].sub_topics.get(sub_topic)
    if spec is None or not spec.fields:
        return ""
    parts: list[str] = []
    for key in spec.fields:
        value = data.get(key)
        if value is None or str(value).strip() == "":
            continue
        parts.append(f"{spec.fields[key].description}: {value}")
    return "; ".join(parts)


def _slot_text(m: Memory) -> str:
    """``topic.sub_topic`` 字段定位文本；缺失部分用 ``—``。"""
    if m.topic and m.sub_topic:
        return f"{m.topic}.{m.sub_topic}"
    if m.topic:
        return m.topic
    if m.sub_topic:
        return f"—.{m.sub_topic}"
    return "—"


def _render_confidence(m: Memory) -> str:
    """置信度标注（必然返回一个档位）。"""
    if m.confidence is Confidence.CONFIRMED:
        return "[confirmed]"
    if m.confidence is Confidence.EXPLICIT:
        return "[explicit]"
    if m.confidence is Confidence.INFERRED:
        return "[inferred]"
    if m.confidence is Confidence.SPECULATED:
        return "[speculated]"
    return "[uncertain]"


def _local_datetime(utc_iso: str) -> str:
    """UTC ISO → 本地日期时间（YYYY-MM-DD HH:MM）。"""
    dt = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")
