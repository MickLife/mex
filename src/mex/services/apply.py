"""候选写入应用（extract/import 共用）：事务内批量落地 LLM 候选。

按 action 分发：new（追加）、update/uncertain_update（覆盖唯一槽位）、
retire（软删可多条槽位中 content 完全相同的记录，矛盾消解范围 A）。
本模块只被 extract.py 调用，依赖方向保持 services 内部。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mex.domain.memory import Confidence, Memory
from mex.store import history, memories

if TYPE_CHECKING:
    import sqlite3

# 候选动作（LLM 输出的 action 字段）：决定写入行为，uncertain_update 强制进 review
Action = Literal["new", "update", "uncertain_update", "retire"]
_VALID_ACTIONS: tuple[str, ...] = ("new", "update", "uncertain_update", "retire")


@dataclass(frozen=True)
class Candidate:
    """通过校验的候选记忆。

    Attributes:
        unique: 槽位是否唯一（schema.is_unique）；画像外记录（sub_topic 为空）为 False。
        action: LLM 判定的写入动作（new/update/uncertain_update/retire）。
    """

    topic: str | None
    sub_topic: str | None
    content: str
    confidence: Confidence
    is_ai_inferred: bool
    evidence: str | None
    unique: bool
    action: Action


def apply_candidates(
    conn: sqlite3.Connection,
    candidates: list[Candidate],
    now: str,
) -> tuple[int, int, int, list[tuple[str, str]], int]:
    """事务内批量落地候选（架构 §6.1：写入阶段与 LLM 调用隔离）。

    Args:
        conn: 数据库连接（调用方已开启事务）。
        candidates: 通过校验的候选列表。
        now: 统一写入时间戳（ISO UTC）。

    Returns:
        (added, updated, retired, rejected, inferred_count)：rejected 为应用期
        拒绝的 [(内容摘要, 原因)]（retire 未找到目标记录）。
    """
    added = updated = retired = inferred = 0
    rejected: list[tuple[str, str]] = []
    occupied: dict[tuple[str, str | None], str] = {}  # 本批已占用的唯一槽位 → id
    for cand in candidates:
        evidence = cand.evidence
        if cand.action == "retire":
            if _apply_retire(conn, cand):
                retired += 1
            else:
                rejected.append((cand.content[:30], "未找到要 retire 的记录"))
            continue
        if not cand.unique:
            added += _apply_new(conn, cand, now, evidence)
        else:
            delta_added, delta_updated = _apply_unique(conn, cand, now, evidence, occupied)
            added += delta_added
            updated += delta_updated
        if cand.is_ai_inferred:
            inferred += 1
    return added, updated, retired, rejected, inferred


def _apply_new(
    conn: sqlite3.Connection,
    cand: Candidate,
    now: str,
    evidence: str | None,
) -> int:
    """new：画像外记录或可多条槽位，直接追加新记录（去重已前置完成）。"""
    m = _build_memory(cand, now, evidence)
    memories.insert(conn, m)
    history.record(conn, m.id, "add", old_content=None, new_content=m.content, actor="ai", evidence=evidence)
    return 1


def _apply_unique(
    conn: sqlite3.Connection,
    cand: Candidate,
    now: str,
    evidence: str | None,
    occupied: dict[tuple[str, str | None], str],
) -> tuple[int, int]:
    """update/uncertain_update：覆盖唯一槽位（本批先占优先，后到候选更新）。

    Returns:
        (加的新条数, 更新的条数)：insert 分支 (1, 0)，覆盖分支 (0, 1)。
    """
    slot = (cand.topic or "", cand.sub_topic)
    existing = memories.find_by_slot(conn, cand.topic or "", cand.sub_topic)
    target_id = existing.id if existing is not None else occupied.get(slot)
    if target_id is None:
        m = _build_memory(cand, now, evidence)
        memories.insert(conn, m)
        history.record(conn, m.id, "add", old_content=None, new_content=m.content, actor="ai", evidence=evidence)
        occupied[slot] = m.id
        return 1, 0
    cur = memories.get(conn, target_id)
    old = cur.content if cur else ""
    # uncertain_update：LLM 不确定是否永久变更 → 强制 uncertain 进 review
    is_uncertain = cand.action == "uncertain_update"
    eff_confidence = Confidence.UNCERTAIN if is_uncertain else cand.confidence
    memories.update_content(conn, target_id, cand.content, eff_confidence)
    if is_uncertain:
        _mark_ai_inferred(conn, target_id, evidence)
    elif evidence is not None:
        conn.execute("UPDATE memories SET evidence = ? WHERE id = ?", (evidence, target_id))
    history.record(conn, target_id, "update", old_content=old, new_content=cand.content, actor="ai", evidence=evidence)
    return 0, 1


def _apply_retire(conn: sqlite3.Connection, cand: Candidate) -> bool:
    """retire：软删可多条槽位中 content 完全相同的有效记录（矛盾消解）。

    匹配前提：唯一槽位/画像外记录已在解析校验阶段被拒（rules.validate action 规则）。
    找不到 content 完全相同的记录时不删（相近不匹配，用户可手动 ``mex forget`` 兜底）。

    Returns:
        True 匹配并软删成功；False 无完全相同记录（调用方记为 rejected）。
    """
    hits = memories.list_by_slot(conn, cand.topic or "", cand.sub_topic)
    target = next((m for m in hits if m.content == cand.content), None)
    if target is None:
        return False
    memories.forget(conn, target.id, "user changed stance, extract retire")
    history.record(conn, target.id, "forget", old_content=target.content, new_content=None, actor="ai")
    return True


def _build_memory(cand: Candidate, now: str, evidence: str | None) -> Memory:
    """候选 → 新 Memory（id 由 uuid4 生成）。"""
    return Memory(
        id=str(uuid4()),
        topic=cand.topic,
        sub_topic=cand.sub_topic,
        content=cand.content,
        is_ai_inferred=cand.is_ai_inferred,
        confidence=cand.confidence,
        evidence=evidence,
        created_at=now,
        updated_at=now,
    )


def _mark_ai_inferred(conn: sqlite3.Connection, memory_id: str, evidence: str | None) -> None:
    """uncertain_update 覆盖后：置 is_ai_inferred=1（满足审查队列入场条件）+ 写 evidence。"""
    if evidence is not None:
        conn.execute(
            "UPDATE memories SET is_ai_inferred = 1, evidence = ? WHERE id = ?",
            (evidence, memory_id),
        )
    else:
        conn.execute("UPDATE memories SET is_ai_inferred = 1 WHERE id = ?", (memory_id,))
