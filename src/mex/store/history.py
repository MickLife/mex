"""history 审计表：记录每次增删改/恢复/确认操作的前后内容与操作者。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mex.domain.memory import now_iso_utc

if TYPE_CHECKING:
    import sqlite3

Event = Literal["add", "update", "forget", "restore", "approve", "delete"]
Actor = Literal["user", "ai"]


def record(  # noqa: PLR0913 - 审计字段密集，打包参数会牺牲可读性
    conn: sqlite3.Connection,
    memory_id: str,
    event: Event,
    *,
    old_content: str | None,
    new_content: str | None,
    actor: Actor,
    evidence: str | None = None,
) -> None:
    """写入一条审计记录。

    Args:
        conn: 连接（调用方负责事务）。
        memory_id: 被操作的记忆 id。
        event: 操作类型。
        old_content: 修改前内容（add 时为 None）。
        new_content: 修改后内容（forget/delete 时为 None）。
        actor: 操作者：user = 人发起；ai = LLM 抽取写入。
        evidence: 来源证据（如来自哪段对话/哪个文件）。
    """
    conn.execute(
        "INSERT INTO history (id, memory_id, event, old_content, new_content, actor, evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid4()), memory_id, event, old_content, new_content, actor, evidence, now_iso_utc()),
    )


def list_for_memory(conn: sqlite3.Connection, memory_id: str) -> list[dict]:
    """某记忆的完整审计记录（按时间升序）。

    Returns:
        每项含 event/old_content/new_content/actor/evidence/created_at。
    """
    rows = conn.execute(
        "SELECT event, old_content, new_content, actor, evidence, created_at "
        "FROM history WHERE memory_id = ? ORDER BY rowid",
        (memory_id,),
    ).fetchall()
    return [dict(r) for r in rows]
