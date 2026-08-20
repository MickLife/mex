"""FTS5 全文索引：外部内容表的同步维护与关键词检索。

``memories_fts`` 是外部内容表（content='memories'），写入后必须与主表
在同一事务内手动同步，否则检索查不到新内容（架构文档 §3.1 的明确要求）。
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from mex.store.connection import memory_from_row

if TYPE_CHECKING:
    from mex.domain.memory import Memory


def _rowid_of(conn: sqlite3.Connection, memory_id: str) -> int | None:
    """memories 表的 rowid（FTS 外部内容表以 rowid 关联）。"""
    row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return int(row["rowid"]) if row is not None else None


def sync_insert(conn: sqlite3.Connection, memory_id: str, content: str) -> None:
    """新增记忆时写入索引。"""
    rowid = _rowid_of(conn, memory_id)
    if rowid is not None:
        conn.execute("INSERT INTO memories_fts (rowid, content) VALUES (?, ?)", (rowid, content))


def sync_delete(conn: sqlite3.Connection, memory_id: str) -> None:
    """移除索引（须在主表内容变更**之前**调用，外部内容表按当前内容验证删除）。"""
    conn.execute(
        "DELETE FROM memories_fts WHERE rowid IN (SELECT rowid FROM memories WHERE id = ?)",
        (memory_id,),
    )


def keyword_search(conn: sqlite3.Connection, keyword: str, limit: int) -> list[Memory]:
    """按关键词检索记忆。

    trigram 分词器要求查询串 ≥3 字符（3 字连续片段匹配）；不足 3 字的短词
    （如两字中文词"失恋"）回退为 LIKE 子串匹配，保证短词可用。

    Args:
        conn: 连接。
        keyword: 检索关键词（内部自动转义）。
        limit: 返回条数上限。

    Returns:
        命中的记忆。检索失败返回空列表而非抛出（查询路径不因脏输入报错）。
    """
    if not keyword or limit <= 0:
        return []
    try:
        if len(keyword) < 3:
            pattern = _like_escape(keyword)
            rows = conn.execute(
                "SELECT * FROM memories WHERE content LIKE ? ESCAPE '\\' "
                "AND forgotten_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (f"%{pattern}%", limit),
            ).fetchall()
            return [memory_from_row(r) for r in rows]
        rows = conn.execute(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
            "WHERE memories_fts MATCH ? LIMIT ?",
            (fts_escape(keyword), limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [memory_from_row(r) for r in rows]


def _like_escape(keyword: str) -> str:
    """转义 LIKE 通配符（配合 ESCAPE '\\' 使用）。"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fts_escape(keyword: str) -> str:
    """fts5 查询转义：把整个关键词作为字面短语匹配，防止特殊字符破坏 MATCH 语法。

    fts5 语法中双引号内的内容按字面处理，内部双引号翻倍转义。
    """
    return '"' + keyword.replace('"', '""') + '"'
