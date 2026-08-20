"""画像快照生成服务（架构文档 §6.2）。

只读路径，零 LLM 调用。输出纯文本，供 agent 直接嵌入系统提示词。
画像 = schema 槽位投影：只有 sub_topic 非空且 (topic, sub_topic) 落在
schema.yaml 定义内的条目进入画像主体；近期动态小节额外纳入最近 N 天内的
画像外记录（sub_topic 为空），按时间窗口承载"当前焦点"。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mex.domain.memory import CONFIDENCE_WEIGHT, Confidence, Memory
from mex.store import memories

if TYPE_CHECKING:
    import sqlite3

    from mex.domain.schema import Schema

_MAX_MANY_DISPLAY = 3  # 可多条槽位在画像快照中最多展示的条数（超出折叠为"共 X 条"提示）
_BODY_RATIO = 0.7  # 总预算中画像主体的占比（近期动态占 0.3）


def estimate_tokens(text: str) -> int:
    """估算 token：中文字符数 × 1.5 + 英文单词数 × 1.3，向上取整。

    仅用于截断决策，不引入 tokenizer 库（避免为估算值增加重依赖）。
    """
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    words = len(re.findall(r"[A-Za-z]+", text))
    return math.ceil(chinese * 1.5 + words * 1.3)


def generate_profile(  # noqa: PLR0913 - 服务函数签名即对外契约（预算/窗口/筛选参数语义独立），拆分反而降低可读性
    conn: sqlite3.Connection,
    schema: Schema,
    *,
    max_tokens: int = 500,
    topics: list[str] | None = None,
    recent_days: int = 7,
    recent_limit: int = 5,
    now: datetime | None = None,
) -> str:
    """生成画像快照文本：画像主体（schema 槽位投影）+ 近期动态小节。

    预算按总 token 拆分：画像主体 70% / 近期动态 30%（各自预留 20% 余量后截断）。
    主体超限时按置信度权重删低置信（``_drop_lowest``）；近期动态超限时按
    ``created_at`` 倒序保留最新（倒序列表尾部即最旧）。

    Args:
        conn: 已连接的 SQLite 连接。
        schema: 加载完成的 schema（topic 顺序决定输出顺序）。
        max_tokens: 输出 token 上限（主体 70% / 近期动态 30%）。
        topics: 只输出指定 topic 的条目（画像主体与近期动态同筛）；None 输出全部。
        recent_days: 近期动态窗口天数（默认 7）。
        recent_limit: 近期动态最多条数（默认 5；0 表示关闭近期动态小节）。
        now: 注入的当前时间（测试用固定值；None 取系统当前时间）。

    Returns:
        画像快照纯文本。
    """
    body_lines = _collect(conn, schema, topics)
    recent_lines = _collect_recent(conn, topics, recent_days, recent_limit, now)
    body_budget = max_tokens * 0.8 * _BODY_RATIO
    while body_lines and estimate_tokens(_render(schema, body_lines)) > body_budget:
        _drop_lowest(body_lines)
    recent_budget = max_tokens * 0.8 * (1 - _BODY_RATIO)
    while recent_lines and estimate_tokens(_recent_text(recent_lines)) > recent_budget:
        recent_lines.pop()  # 倒序列表尾部即最旧：删旧保留最新
    return _render(schema, body_lines, recent_lines)


@dataclass
class _Line:
    """单条画像行（含截断决策所需元数据）。"""

    topic: str
    sub_topic: str
    text: str
    weight: float


def _collect(
    conn: sqlite3.Connection,
    schema: Schema,
    topics: list[str] | None,
) -> list[_Line]:
    """收集画像槽位条目行：sub_topic 非空、槽位在 schema 内，且 topic 命中筛选。"""
    topic_set = set(topics) if topics else None
    lines: list[_Line] = []
    for m in memories.list_all(conn):
        if not m.sub_topic or not m.topic:
            continue  # 画像外记录（sub_topic 为空）不进入画像快照
        if m.topic not in schema.topics or not schema.is_valid_slot(m.topic, m.sub_topic):
            continue  # 槽位不在 schema 定义内，不进入画像快照
        if topic_set is not None and m.topic not in topic_set:
            continue
        lines.append(
            _Line(
                topic=m.topic,
                sub_topic=m.sub_topic,
                text=_format_line(m, schema),
                weight=CONFIDENCE_WEIGHT[m.confidence],
            ),
        )
    return lines


def _collect_recent(
    conn: sqlite3.Connection,
    topics: list[str] | None,
    recent_days: int,
    recent_limit: int,
    now: datetime | None,
) -> list[_Line]:
    """收集近期动态行：最近 ``recent_days`` 天内、最多 ``recent_limit`` 条画像外记录。

    只纳入画像外记录（sub_topic 为空）；画像槽位已在画像主体中，不重复收集。
    ``recent_limit`` 为 0 时返回空列表（关闭近期动态小节）。
    """
    if recent_limit <= 0:
        return []
    since = (now or datetime.now(UTC)) - timedelta(days=recent_days)
    items = memories.list_recent_outside(
        conn,
        since=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        limit=recent_limit,
        topics=topics,
    )
    return [
        _Line(
            topic=m.topic or "",
            sub_topic="",
            text=f"- {m.content}{_confidence_mark(m.confidence)}",
            weight=0.0,
        )
        for m in items
    ]


def _recent_text(lines: list[_Line]) -> str:
    """近期动态行的合计文本（token 估算用）。"""
    return "\n".join(line.text for line in lines)


def _format_line(m: Memory, schema: Schema) -> str:
    """单行格式：``- <字段显示名>：<内容>`` + 置信度标注。

    结构化字段（schema 声明了 fields）先把 content 的 JSON 对象按键序渲染成
    可读文本再展示，避免把原始 JSON 直接暴露给用户；解析失败时回退展示原文。
    """
    spec = schema.topics[m.topic].sub_topics.get(m.sub_topic) if m.sub_topic else None
    label = spec.description if spec else (m.sub_topic or "未分类")
    content = _render_content(spec, m.content) if spec and spec.fields else m.content
    return f"- {label}：{content}{_confidence_mark(m.confidence)}"


def _render_content(spec, content: str) -> str:
    """结构化字段：JSON 对象按 fields 键序渲染为 ``键：值`` 顿号分隔文本。

    Args:
        spec: 字段定义（此处必须声明了 fields，即结构化字段）。
        content: 待渲染的 content 原始文本（应为 JSON 对象字符串）。

    Returns:
        可读文本；JSON 非法或为纯文本时原样返回 content（容错）。
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return content
    if not isinstance(data, dict):
        return content
    parts: list[str] = []
    for key in spec.fields:
        value = data.get(key)
        if value is None or str(value).strip() == "":
            continue
        label = spec.fields[key].description
        parts.append(f"{label}：{value}")
    return "；".join(parts) if parts else content


def _confidence_mark(confidence: Confidence) -> str:
    """置信度标注：speculated/uncertain → 待确认；inferred → 推断；其余不标注。"""
    if confidence in (Confidence.SPECULATED, Confidence.UNCERTAIN):
        return "（待确认）"
    if confidence is Confidence.INFERRED:
        return "（推断）"
    return ""


def _render(schema: Schema, lines: list[_Line], recent_lines: list[_Line] | None = None) -> str:
    """组装快照文本：# 用户画像 → 按领域分节 →（可选）近期动态小节。"""
    parts = ["# 用户画像"]
    parts.extend(_render_section(schema, lines))
    if recent_lines:
        parts.append("## 近期动态")
        parts.extend(line.text for line in recent_lines)
    return "\n".join(parts)


def _render_section(schema: Schema, lines: list[_Line]) -> list[str]:
    """按 schema 的 topic 顺序输出 ``## <领域描述>`` 小节，空 topic 省略。

    同 topic 内按 sub_topic 分组：唯一槽位输出其行；可多条槽位输出前 3 行 +
    ``（共 X 条，mex search --topic <领域> 查看全部）`` 提示（X > 3 时）。
    """
    out: list[str] = []
    for topic_name in schema.topic_names():
        topic_lines = [line for line in lines if line.topic == topic_name]
        if not topic_lines:
            continue
        spec = schema.topics[topic_name]
        out.append(f"## {spec.description or spec.name}")
        out.extend(_render_topic_groups(schema, topic_name, topic_lines))
    return out


def _render_topic_groups(schema: Schema, topic_name: str, lines: list[_Line]) -> list[str]:
    """单 topic 内按 sub_topic 分组渲染，尊重 schema 字段定义顺序。"""
    sub_order = list(schema.topics[topic_name].sub_topics.keys())
    groups: dict[str, list[_Line]] = {}
    for line in lines:
        groups.setdefault(line.sub_topic, []).append(line)

    out: list[str] = []
    for sub_topic in sorted(groups, key=lambda st: _sub_sort_key(st, sub_order)):
        group = groups[sub_topic]
        unique = schema.is_unique(topic_name, sub_topic) if sub_topic else True
        if unique or len(group) <= _MAX_MANY_DISPLAY:
            out.extend(line.text for line in group)
        else:
            out.extend(line.text for line in group[:_MAX_MANY_DISPLAY])
            out.append(f"  （共 {len(group)} 条，mex search --topic {topic_name} 查看全部）")
    return out


def _sub_sort_key(sub_topic: str, sub_order: list[str]) -> tuple[int, str]:
    """字段排序键：schema 定义顺序优先，未定义字段排最后。"""
    try:
        return (sub_order.index(sub_topic), sub_topic)
    except ValueError:
        return (len(sub_order), sub_topic)


def _drop_lowest(lines: list[_Line]) -> None:
    """移除置信度最低的一行（同权重删最早一条，保持确定性）。

    画像投影模型下所有行同权竞争，截断只依据置信度与先后顺序。
    """
    idx = min(range(len(lines)), key=lambda i: (lines[i].weight, i))
    lines.pop(idx)
