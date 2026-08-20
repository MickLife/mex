"""memories 表 CRUD 与 FTS 索引同步。

所有函数接受连接参数、不自行开启事务（事务由调用方通过
``store.connection.transaction`` 管理）；写操作与 FTS 同步在同一事务内完成。

画像投影模型：sub_topic 非空 = 画像槽位（唯一性/覆盖语义由 schema 的 unique
声明决定，见 find_by_slot / list_by_slot 的调用方）；sub_topic 为空 = 画像外记录。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mex.domain.memory import Confidence, Memory, now_iso_utc

from mex.store import fts
from mex.store.connection import memory_from_row

if TYPE_CHECKING:
    import sqlite3

    from mex.domain.schema import Schema

# TTL 惰性过滤片段：绑定值 = 当前 UTC now。过期记录对"有效记录"查询不可见（数据保留）。
_EXPIRED_GUARD = "(expires_at IS NULL OR expires_at > ?)"


def _now_bound(now: str | None) -> str:
    """解析查询的当前时间（默认取系统当前 UTC；测试可注入固定 now）。"""
    return now if now is not None else now_iso_utc()


_INSERT_SQL = (
    "INSERT INTO memories (id, topic, sub_topic, content, is_ai_inferred, confidence, "
    "evidence, created_at, updated_at, forgotten_at, forgotten_reason, expires_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _memory_values(m: Memory) -> tuple:
    """Memory → SQL 绑定参数（枚举转字符串、bool 转 0/1）。"""
    return (
        m.id,
        m.topic,
        m.sub_topic,
        m.content,
        int(m.is_ai_inferred),
        m.confidence.value,
        m.evidence,
        m.created_at,
        m.updated_at,
        m.forgotten_at,
        m.forgotten_reason,
        m.expires_at,
    )


def insert(conn: sqlite3.Connection, m: Memory) -> None:
    """插入一条记忆并同步 FTS 索引。

    Raises:
        sqlite3.IntegrityError: 槽位唯一约束冲突（已有同画像槽位的有效条目）。
    """
    conn.execute(_INSERT_SQL, _memory_values(m))
    fts.sync_insert(conn, m.id, m.content)


def update_content(
    conn: sqlite3.Connection, memory_id: str, content: str, confidence: Confidence,
) -> Memory | None:
    """更新内容的 content / confidence / updated_at，并同步 FTS。

    槽位不可变（架构文档：换槽位用 add + forget）。

    Args:
        conn: 连接。
        memory_id: 目标记忆 id。
        content: 新内容。
        confidence: 新置信度。

    Returns:
        更新后的条目；id 不存在返回 None。
    """
    old = _select_one(conn, memory_id)
    if old is None:
        return None
    fts.sync_delete(conn, memory_id)  # 须在内容变更前删除旧索引（外部内容表特性）
    conn.execute(
        "UPDATE memories SET content = ?, confidence = ?, updated_at = ? WHERE id = ?",
        (content, confidence.value, now_iso_utc(), memory_id),
    )
    fts.sync_insert(conn, memory_id, content)
    return _select_one(conn, memory_id)


def forget(conn: sqlite3.Connection, memory_id: str, reason: str) -> Memory | None:
    """软删除：写 forgotten_at / forgotten_reason。

    Args:
        conn: 连接。
        memory_id: 目标记忆 id。
        reason: 遗忘原因（供审查追溯）。

    Returns:
        被软删的条目；不存在或已删返回 None。
    """
    old = _select_one(conn, memory_id)
    if old is None or old.is_forgotten():
        return None
    now = now_iso_utc()
    conn.execute(
        "UPDATE memories SET forgotten_at = ?, forgotten_reason = ?, updated_at = ? WHERE id = ?",
        (now, reason, now, memory_id),
    )
    return _select_one(conn, memory_id)


def hard_delete(conn: sqlite3.Connection, memory_id: str) -> Memory | None:
    """物理删除并移除 FTS 索引。

    Returns:
        被删除的条目（供 history 记录）；不存在返回 None。
    """
    old = _select_one(conn, memory_id)
    if old is None:
        return None
    fts.sync_delete(conn, memory_id)  # 行仍在时先删索引
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    return old


def restore(conn: sqlite3.Connection, memory_id: str) -> Memory | None:
    """恢复软删条目：清除 forgotten 标记。

    Returns:
        恢复后的条目；不存在或未处于软删状态返回 None。
    """
    old = _select_one(conn, memory_id)
    if old is None or not old.is_forgotten():
        return None
    conn.execute(
        "UPDATE memories SET forgotten_at = NULL, forgotten_reason = NULL, updated_at = ? WHERE id = ?",
        (now_iso_utc(), memory_id),
    )
    return _select_one(conn, memory_id)


def get(conn: sqlite3.Connection, memory_id: str, *, include_forgotten: bool = False) -> Memory | None:
    """按 id 查询单条记忆。"""
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return None
    m = memory_from_row(row)
    if m.is_forgotten() and not include_forgotten:
        return None
    return m


def find_by_slot(
    conn: sqlite3.Connection,
    topic: str,
    sub_topic: str | None,
    *,
    include_forgotten: bool = False,
    now: str | None = None,
) -> Memory | None:
    """按画像槽位查询单条（唯一槽位的读取方：先查再插）；sub_topic 为 None 时查询画像外记录。

    用 ``sub_topic IS ?`` 匹配：传 NULL 命中画像外记录（sub_topic 为空）。
    唯一槽位约定只有一条有效记录，故 LIMIT 1；可多条槽位请用 :func:`list_by_slot`。
    默认排除已过期记录（TTL 惰性过滤）。
    """
    sql = "SELECT * FROM memories WHERE topic = ? AND sub_topic IS ? AND " + _EXPIRED_GUARD + " LIMIT 1"  # noqa: S608 - 内插受控常量
    row = conn.execute(sql, (topic, sub_topic, _now_bound(now))).fetchone()
    if row is None:
        return None
    m = memory_from_row(row)
    if m.is_forgotten() and not include_forgotten:
        return None
    return m


def list_by_slot(
    conn: sqlite3.Connection,
    topic: str,
    sub_topic: str | None,
    *,
    include_forgotten: bool = False,
    now: str | None = None,
) -> list[Memory]:
    """按画像槽位查询全部有效记录（可多条槽位的读取方）。

    可多条槽位（schema ``unique: false``）允许同一 ``(topic, sub_topic)``
    存多条独立记录；本函数返回全部有效条目，按置信度权重降序、更新时间倒序排序
    （画像渲染取前 N 条时优先保留高置信、近更新的记录）。默认排除已过期记录。

    Args:
        conn: 连接。
        topic: 领域。
        sub_topic: 字段；None 表示领域级槽位。
        include_forgotten: 是否包含软删条目。
        now: 当前时间（UTC ISO，默认取系统当前；测试注入用）。

    Returns:
        该槽位的全部有效记录（无匹配返回空列表）。
    """
    sql = f"SELECT * FROM memories WHERE topic = ? AND sub_topic IS ? AND {_EXPIRED_GUARD}"  # noqa: S608 - 内插受控常量
    params: list[object] = [topic, sub_topic, _now_bound(now)]
    if not include_forgotten:
        sql += " AND forgotten_at IS NULL"
    sql += (
        " ORDER BY CASE confidence WHEN 'confirmed' THEN 1 WHEN 'explicit' THEN 2"
        " WHEN 'inferred' THEN 3 WHEN 'speculated' THEN 4 ELSE 5 END,"
        " updated_at DESC, id"
    )
    return [memory_from_row(r) for r in conn.execute(sql, params).fetchall()]


def list_all(
    conn: sqlite3.Connection,
    *,
    topic: str | None = None,
    sub_topic: str | None = None,
    include_forgotten: bool = False,
    now: str | None = None,
) -> list[Memory]:
    """列出记忆：created_at 倒序（新→旧），稳定排序（同时间按 id）。默认排除已过期记录。

    Args:
        conn: 连接。
        topic: 仅返回该领域。
        sub_topic: 仅返回该字段（须配合 topic；None 表示不按字段过滤）。
        include_forgotten: 是否包含软删条目。
        now: 当前时间（UTC ISO，默认取系统当前；测试注入用）。
    """
    sql = f"SELECT * FROM memories WHERE 1=1 AND {_EXPIRED_GUARD}"  # noqa: S608 - 内插受控常量
    params: list[object] = [_now_bound(now)]
    if topic is not None:
        sql += " AND topic = ?"
        params.append(topic)
    if sub_topic is not None:
        # IS 匹配兼容 NULL：配合 topic 过滤时可用于筛画像外记录（sub_topic 为空）
        sql += " AND sub_topic IS ?"
        params.append(sub_topic)
    if not include_forgotten:
        sql += " AND forgotten_at IS NULL"
    sql += " ORDER BY created_at DESC, id"
    return [memory_from_row(r) for r in conn.execute(sql, params).fetchall()]


def list_recent_outside(
    conn: sqlite3.Connection,
    *,
    since: str,
    limit: int,
    topics: list[str] | None = None,
    now: str | None = None,
) -> list[Memory]:
    """取最近期间内的画像外记录（``mex profile`` 近期动态小节的数据源）。

    只返回画像外记录（sub_topic 为空）、未软删、未过期，可按领域过滤，
    按 ``created_at`` 倒序（相同时间按 id 稳定排序）取前 limit 条。
    画像槽位（sub_topic 非空）不在此列。

    Args:
        conn: 连接。
        since: UTC ISO 时间边界（含），只返回 ``created_at >= since`` 的记录。
        limit: 返回条数上限。
        topics: 非空时只返回这些领域的记录。
        now: 当前时间（UTC ISO，默认取系统当前；测试注入用）。

    Returns:
        命中的画像外记录列表（按时间倒序）。
    """
    sql = (
        "SELECT * FROM memories WHERE sub_topic IS NULL AND forgotten_at IS NULL "  # noqa: S608 - 内插受控常量
        f"AND created_at >= ? AND {_EXPIRED_GUARD}"
    )
    params: list[object] = [since, _now_bound(now)]
    if topics:
        sql += f" AND topic IN ({','.join('?' * len(topics))})"
        params.extend(topics)
    sql += " ORDER BY created_at DESC, id LIMIT ?"
    params.append(limit)
    return [memory_from_row(r) for r in conn.execute(sql, params).fetchall()]


def list_forgotten(conn: sqlite3.Connection) -> list[Memory]:
    """列出全部软删条目（按遗忘时间倒序）。"""
    rows = conn.execute(
        "SELECT * FROM memories WHERE forgotten_at IS NOT NULL ORDER BY forgotten_at DESC, id",
    ).fetchall()
    return [memory_from_row(r) for r in rows]


def list_expired(conn: sqlite3.Connection, *, now: str | None = None) -> list[Memory]:
    """列出已过期但尚未软删的条目（``mex gc`` 的清理目标）。

    Returns:
        所有 ``expires_at <= now`` 且 ``forgotten_at IS NULL`` 的记录。
    """
    rows = conn.execute(
        "SELECT * FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ? AND forgotten_at IS NULL",
        (_now_bound(now),),
    ).fetchall()
    return [memory_from_row(r) for r in rows]


def set_expiration(
    conn: sqlite3.Connection, memory_id: str, expires_at: str | None,
) -> Memory | None:
    """设置/清除某条记忆的过期时间（``expires_at`` None 表示清除，恢复永久）。

    Returns:
        更新后的条目；id 不存在返回 None。
    """
    old = _select_one(conn, memory_id)
    if old is None:
        return None
    conn.execute(
        "UPDATE memories SET expires_at = ?, updated_at = ? WHERE id = ?",
        (expires_at, now_iso_utc(), memory_id),
    )
    return _select_one(conn, memory_id)


def count_by_shape(conn: sqlite3.Connection, *, now: str | None = None) -> dict[str, int]:
    """画像内/画像外有效条目数（排除已过期记录）。

    Returns:
        ``{"profile": n, "outside": n}``：profile 为画像槽位（sub_topic 非空），
        outside 为画像外记录（sub_topic 为空）。
    """
    sql = (
        "SELECT CASE WHEN sub_topic IS NULL OR sub_topic = '' THEN 'outside' ELSE 'profile' END AS shape, "  # noqa: S608 - 内插受控常量
        "COUNT(*) AS n FROM memories WHERE forgotten_at IS NULL AND " + _EXPIRED_GUARD + " GROUP BY shape"
    )
    rows = conn.execute(sql, (_now_bound(now),)).fetchall()
    counts = {r["shape"]: int(r["n"]) for r in rows}
    return {"profile": counts.get("profile", 0), "outside": counts.get("outside", 0)}


def count_pending_review(conn: sqlite3.Connection, *, now: str | None = None) -> int:
    """待审查条数：AI 推断、未删、非 confirmed、未过期。"""
    sql = (
        "SELECT COUNT(*) AS n FROM memories WHERE is_ai_inferred = 1 AND forgotten_at IS NULL "  # noqa: S608 - 内插受控常量
        "AND confidence != 'confirmed' AND " + _EXPIRED_GUARD
    )
    row = conn.execute(sql, (_now_bound(now),)).fetchone()
    return int(row["n"])


def list_pending_review(conn: sqlite3.Connection, *, now: str | None = None) -> list[Memory]:
    """待审查列表（created_at 倒序，排除已过期）。"""
    sql = (
        "SELECT * FROM memories WHERE is_ai_inferred = 1 AND forgotten_at IS NULL "  # noqa: S608 - 内插受控常量
        "AND confidence != 'confirmed' AND " + _EXPIRED_GUARD + " ORDER BY created_at DESC, id"
    )
    rows = conn.execute(sql, (_now_bound(now),)).fetchall()
    return [memory_from_row(r) for r in rows]


def list_dangling_slots(conn: sqlite3.Connection, schema: Schema) -> list[Memory]:
    """有效画像条目中与当前 schema 不一致的记忆（``mex doctor`` 用）。

    两类不一致：
    1. 槽位不在当前 schema（字段被移除/改名，`is_valid_slot` 失败）；
    2. 槽位在 schema 中但为结构化字段，content 不符合其结构化约束
       （如字段升级为 structured 后遗留的旧纯文本记录）。

    Args:
        conn: 连接。
        schema: 当前 schema。

    Returns:
        全部不一致的有效画像槽位条目。
    """
    result: list[Memory] = []
    for m in list_all(conn):
        if not m.topic or not m.sub_topic:
            continue
        if not schema.is_valid_slot(m.topic, m.sub_topic):
            result.append(m)
            continue
        if schema.is_structured(m.topic, m.sub_topic):
            ok, _ = schema.validate_structured_content(m.topic, m.sub_topic, m.content)
            if not ok:
                result.append(m)
    return result


def _select_one(conn: sqlite3.Connection, memory_id: str) -> Memory | None:
    """按 id 查询（不过滤软删状态），供写操作内部使用。"""
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return memory_from_row(row) if row is not None else None
