"""对话抽取核心流程（架构文档 §6.1）。

extract 与 import 共用此流程：LLM 调用与候选校验全部在事务外完成，只有批量写入
阶段开事务（§7.3 不变量：任何 LLM 层面的失败都不会留下半更新的数据库）。
候选落地由 ``services/apply`` 承载（含 retire 矛盾消解分支）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from mex.domain.memory import Confidence, Memory, now_iso_utc
from mex.domain.rules import validate
from mex.llm.prompts import build_extract_messages
from mex.services import apply as apply_service
from mex.services.apply import Action, Candidate, _VALID_ACTIONS  # noqa: A004
from mex.store import memories
from mex.store.connection import transaction

if TYPE_CHECKING:
    import sqlite3

    from mex.domain.schema import Schema
    from mex.llm.client import LLMClient


@dataclass(frozen=True)
class ExtractResult:
    """一次抽取的统计结果。"""

    added: int
    updated: int
    retired: int
    discarded: list[tuple[str, str]]  # [(内容摘要, 丢弃原因), ...]
    inferred_count: int
    prompt_tokens: int
    completion_tokens: int


def extract_dialogue(
    conn: sqlite3.Connection,
    *,
    text: str,
    source_file: str | None,
    client: LLMClient,
    schema: Schema,
) -> ExtractResult:
    """从对话/文档文本抽取记忆（架构 §6.1 主流程，对话与导入归一化）。

    Args:
        conn: 数据库连接。
        text: 完整对话文本（source_file 非空时内部做增量切片）。
        source_file: 会话文件绝对路径（增量状态记录用）；None 表示全量处理。
        client: LLM 客户端（可注入测试替身）。
        schema: 当前 schema（字段白名单）。

    Returns:
        抽取统计。

    Raises:
        LLMError: LLM 调用失败（此时数据库无任何变化）。
    """
    if source_file:
        offset = _get_last_position(conn, source_file)
        if offset > len(text):
            offset = 0
        text = text[offset:]
    else:
        offset = 0
    if not text:
        return ExtractResult(0, 0, 0, [], 0, 0, 0)

    current = _load_current_memories(conn)
    messages = build_extract_messages(schema, current, text)
    data, response = client.chat_json(messages)
    candidates, discarded = _validate_candidates(data, schema)
    candidates, deduped = _dedup_many_slots(candidates, current)
    discarded = discarded + deduped

    with transaction(conn):
        stats = apply_service.apply_candidates(
            conn, candidates, now_iso_utc(),
        )
        added, updated, retired, rejected, inferred = stats
        discarded = discarded + rejected
        if source_file:
            _set_last_position(conn, source_file, offset + len(text))
        _record_usage(conn, client.model, response.prompt_tokens, response.completion_tokens)

    return ExtractResult(
        added=added,
        updated=updated,
        retired=retired,
        discarded=discarded,
        inferred_count=inferred,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )


def _load_current_memories(conn: sqlite3.Connection) -> list[Memory]:
    """加载全部有效条目（个人量级小，直接全取，供去重与 LLM 上下文）。"""
    return memories.list_all(conn)


def _validate_candidates(data: dict, schema: Schema) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """逐条校验 LLM 候选（事务外）：返回 (通过列表, [(摘要, 原因)])。"""
    raw_list = data.get("memories") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return [], [("LLM 返回结构异常", "缺少 memories 数组")]
    candidates: list[Candidate] = []
    discarded: list[tuple[str, str]] = []
    for raw in raw_list:
        cand, reason = _parse_candidate(raw, schema)
        if cand is None:
            discarded.append((_summarize(raw), reason))
        else:
            candidates.append(cand)
    return candidates, discarded


def _parse_candidate(raw: object, schema: Schema) -> tuple[Candidate | None, str]:
    """单条候选校验：通过返回候选，失败返回 (None, 原因)。"""
    if not isinstance(raw, dict):
        return None, "候选不是 JSON 对象"
    fields = _extract_fields(raw, schema)
    if isinstance(fields, str):
        return None, fields
    topic, sub_topic, content, confidence, is_ai_inferred, evidence, unique, action = fields
    errors = validate(
        topic=topic, sub_topic=sub_topic, content=content,
        confidence=confidence, schema=schema, allow_confirmed=False, action=action,
    )
    if errors:
        return None, errors[0]
    return (
        Candidate(
            topic=topic, sub_topic=sub_topic, content=content,
            confidence=confidence, is_ai_inferred=is_ai_inferred, evidence=evidence,
            unique=unique, action=action,
        ),
        "",
    )


def _extract_fields(raw: dict, schema: Schema) -> tuple | str:
    """从原始候选提取并校验各字段；失败返回原因字符串，成功返回字段元组。"""
    confidence, reason = _parse_confidence(raw)
    if confidence is None:
        return reason
    topic, sub_topic = raw.get("topic") or None, raw.get("sub_topic") or None
    reason = _type_check_fields(raw, topic, sub_topic)
    if reason:
        return reason
    action, reason = _parse_action(raw)
    if reason:
        return reason
    content, reason = _serialize_content(raw["content"])
    if content is None:
        return reason
    if topic and sub_topic and schema.is_structured(topic, sub_topic):
        content, reason = _check_structured_content(content, schema, topic, sub_topic)
        if content is None:
            return reason
    # 唯一性只作用于画像槽位（sub_topic 非空）；画像外记录（sub_topic 为空）一律追加
    unique = bool(topic and sub_topic) and schema.is_unique(topic, sub_topic)
    return (topic, sub_topic, content, confidence, bool(raw["is_ai_inferred"]),
            _clean_evidence(raw.get("evidence")), unique, action)


def _parse_action(raw: dict) -> tuple[Action | None, str]:
    """解析 action 字段：缺失或非法返回 (None, 原因)。

    action 缺失时默认 \"new\"（向后兼容不输出 action 的旧 LLM 响应）。
    """
    action = raw.get("action", "new")
    if not isinstance(action, str):
        return None, "字段类型错误（action）"
    if action not in _VALID_ACTIONS:
        return None, f"action 非法（{action!r}），可选 new/update/uncertain_update/retire"
    return action, ""  # type: ignore[return-value]


def _type_check_fields(raw: dict, topic: object, sub_topic: object) -> str:
    """字段存在性与类型检查，返回空串表示通过。"""
    if "content" not in raw:
        return "字段缺失（content）"
    if topic is not None and not isinstance(topic, str):
        return "字段类型错误（topic）"
    if sub_topic is not None and not isinstance(sub_topic, str):
        return "字段类型错误（sub_topic）"
    if not isinstance(raw.get("is_ai_inferred"), bool):
        return "字段类型错误（is_ai_inferred）"
    return ""


def _parse_confidence(raw: dict) -> tuple[Confidence | None, str]:
    """解析 confidence 枚举，缺失或非法返回 (None, 原因)。"""
    if "confidence" not in raw:
        return None, "字段缺失（confidence）"
    try:
        return Confidence(raw["confidence"]), ""
    except ValueError:
        return None, f"confidence 非法（{raw['confidence']!r}）"


def _serialize_content(content: object) -> tuple[str | None, str]:
    """content 规范化：projection 模型下 content 永远是单值字符串。

    多个值由可多条槽位（``unique: false``）的独立记录承载，不再用 JSON 数组。
    """
    if not isinstance(content, str):
        return None, "字段类型错误（content）"
    return content, ""


def _check_structured_content(
    content: str,
    schema: Schema,
    topic: str,
    sub_topic: str,
) -> tuple[str | None, str]:
    """结构化字段候选校验：复用 schema 约束（JSON 合法 / 键白名单 / 必选 / 格式）。

    Args:
        content: LLM 输出的 content 字符串。
        schema: 当前 schema（含字段声明与约束）。
        topic: 目标领域（已有）。
        sub_topic: 目标字段（已有）。

    Returns:
        (规范化 content, 原因)：合法返回 (原样 content, "")；非法返回 (None, 原因)。
        合法时保持原样写入（含缩进/键序差异，渲染层按键名取值，不依赖格式）。
    """
    ok, errors = schema.validate_structured_content(topic, sub_topic, content)
    if ok:
        return content, ""
    return None, f"结构化字段 {topic}.{sub_topic}：{'；'.join(errors)}"


def _clean_evidence(evidence: object) -> str | None:
    """evidence 归一：None/字符串直用，其他类型转字符串。"""
    if evidence is None or isinstance(evidence, str):
        return evidence
    return str(evidence)


def _summarize(raw: object) -> str:
    """候选内容摘要（丢弃原因展示用，≤30 字符）。"""
    if isinstance(raw, dict) and isinstance(raw.get("content"), str):
        return raw["content"][:30]
    return str(raw)[:30]


def _dedup_many_slots(
    candidates: list[Candidate], current_memories: list[Memory],
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """可多条画像槽位去重：content 与已有记录或本批候选完全相同的，移入 discarded。

    唯一槽位与画像外记录不在此去重——唯一槽位的覆盖语义由
    :func:`services.apply.apply_candidates` 处理（同槽位后到候选覆盖先到），
    画像外记录天然多条且通常时敏、不强去重。

    Args:
        candidates: 通过校验的候选列表。
        current_memories: 已加载的现有条目，供查可多条槽位已有 content。

    Returns:
        (去重后保留的候选, 被去重的 [(内容摘要, "与已有记录或本批候选重复")])。
    """
    existing_by_slot: dict[tuple[str, str], set[str]] = {}
    for m in current_memories:
        if not m.sub_topic or not m.topic:
            continue
        existing_by_slot.setdefault((m.topic, m.sub_topic), set()).add(m.content)

    kept: list[Candidate] = []
    deduped: list[tuple[str, str]] = []
    batch_seen: dict[tuple[str, str], set[str]] = {}
    for cand in candidates:
        # retire 是已有记录的移除指令，不是待追加的新值，不参与内容去重
        if cand.action == "retire" or cand.unique or not cand.sub_topic or not cand.topic:
            kept.append(cand)
            continue
        slot = (cand.topic, cand.sub_topic)
        if cand.content in existing_by_slot.get(slot, set()) or cand.content in batch_seen.setdefault(slot, set()):
            deduped.append((cand.content[:30], "与已有记录或本批候选重复"))
            continue
        batch_seen.setdefault(slot, set()).add(cand.content)
        kept.append(cand)
    return kept, deduped


def _get_last_position(conn: sqlite3.Connection, source_file: str) -> int:
    """读取增量抽取位置（无记录返回 0）。"""
    row = conn.execute("SELECT last_position FROM extraction_state WHERE source_file = ?", (source_file,)).fetchone()
    return int(row["last_position"]) if row else 0


def _set_last_position(conn: sqlite3.Connection, source_file: str, position: int) -> None:
    """写入增量抽取位置（同事务内，处理成功才记录）。"""
    conn.execute(
        "INSERT INTO extraction_state (source_file, last_position, last_extracted_at) VALUES (?, ?, ?) "
        "ON CONFLICT(source_file) DO UPDATE SET last_position = excluded.last_position, "
        "last_extracted_at = excluded.last_extracted_at",
        (source_file, position, now_iso_utc()),
    )


def _record_usage(conn: sqlite3.Connection, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """记录 LLM 用量（§7.4，purpose=extract）。"""
    conn.execute(
        "INSERT INTO llm_usage (id, purpose, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?, 'extract', ?, ?, ?, ?)",
        (str(uuid4()), model, prompt_tokens, completion_tokens, now_iso_utc()),
    )
