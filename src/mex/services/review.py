"""审查队列业务服务：列出 / 批准 / 拒绝 AI 推断记忆。

语义依据架构文档 §6.3 与 ADR-5、ADR-8。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mex.domain.memory import Confidence, Memory
from mex.store import history, memories
from mex.store.connection import transaction

if TYPE_CHECKING:
    import sqlite3


def list_pending(conn: sqlite3.Connection) -> list[Memory]:
    """列出待审查记忆：AI 推断、未删除、置信度非 confirmed，创建时间倒序。

    Args:
        conn: 数据库连接。

    Returns:
        待审查记忆列表（最新在前）。
    """
    return memories.list_pending_review(conn)


def approve(conn: sqlite3.Connection, memory_id: str) -> Memory | None:
    """批准一条 AI 推断记忆：置信度提升为 confirmed 并写入审计记录。

    ``is_ai_inferred`` 保持不变（来源不可篡改，ADR-5）；置信度变为
    confirmed 后条目自然退出待审查队列。记忆更新与审计写入在同一事务内。

    Args:
        conn: 数据库连接。
        memory_id: 目标记忆 id。

    Returns:
        更新后的条目；不存在、已删除或不在待审查队列时返回 None。
    """
    with transaction(conn):
        current = memories.get(conn, memory_id)
        if not _is_in_queue(current):
            return None
        updated = memories.update_content(conn, memory_id, current.content, Confidence.CONFIRMED)
        history.record(
            conn,
            memory_id,
            "approve",
            old_content=current.content,
            new_content=current.content,
            actor="user",
        )
        logger.info("已批准记忆 {}（AI 推断 → confirmed）", memory_id)
        return updated


def decline(conn: sqlite3.Connection, memory_id: str, reason: str | None) -> Memory | None:
    """拒绝一条 AI 推断记忆：软删除（forgotten_at + forgotten_reason）并写入审计记录。

    与 M3 的 forget 语义一致（ADR-8），仅操作者（actor=user）与命令入口不同。
    软删除与审计写入在同一事务内。

    Args:
        conn: 数据库连接。
        memory_id: 目标记忆 id。
        reason: 拒绝原因（可为空，写入 forgotten_reason 供追溯）。

    Returns:
        被软删的条目；不存在、已删除或不在待审查队列时返回 None。
    """
    with transaction(conn):
        current = memories.get(conn, memory_id)
        if not _is_in_queue(current):
            return None
        deleted = memories.forget(conn, memory_id, reason or "")
        history.record(
            conn,
            memory_id,
            "forget",
            old_content=current.content,
            new_content=None,
            actor="user",
        )
        logger.info("已拒绝记忆 {}（软删除）", memory_id)
        return deleted


def _is_in_queue(memory: Memory | None) -> bool:
    """是否处于待审查队列：存在、未删除、AI 推断、置信度非 confirmed。"""
    if memory is None or memory.is_forgotten():
        return False
    return memory.is_ai_inferred and memory.confidence is not Confidence.CONFIRMED
