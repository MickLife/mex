"""组合检索服务：结构化过滤 + FTS 关键词叠加（查询路径零 LLM 调用）。

策略（架构文档 §5.1 search 命令、§4.2）：
- keyword 为空：纯 SQL 结构化过滤；
- keyword 非空：``store/fts.keyword_search`` 得 id 集合 → 叠加结构化过滤。
- since/until：本地日期 → UTC 起始/结束边界（字符串比较，字典序即时间序）。
画像外记录与画像槽位条目同表同检索：不区分来源，统一按条件过滤。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from mex.domain.memory import now_iso_utc
from mex.store import fts
from mex.store.connection import memory_from_row

if TYPE_CHECKING:
    from mex.domain.memory import Memory


@dataclass(frozen=True)
class SearchFilters:
    """检索条件集合（全部可选，组合取交集）。

    Attributes:
        topic: 仅检索该领域。
        sub_topic: 仅检索该字段（须配合 topic 使用；None 表示不按字段过滤）。
        keyword: 关键词（中文按字匹配，短词自动 LIKE 回退）。
        since: 本地日期 ``YYYY-MM-DD``（含当天）。
        until: 本地日期 ``YYYY-MM-DD``（含当天）。
        limit: 返回条数上限。
        include_forgotten: 是否包含软删条目。
    """

    topic: str | None = None
    sub_topic: str | None = None
    keyword: str | None = None
    since: str | None = None
    until: str | None = None
    limit: int = 50
    include_forgotten: bool = False


def search(conn: object, filters: SearchFilters) -> list[Memory]:
    """组合检索，排序：created_at 倒序（新→旧），相同时按 id（确定性）。

    Args:
        conn: 已连接的 SQLite 连接。
        filters: 检索条件。

    Returns:
        命中的记忆列表（默认不含软删条目，并排除已过期记录）。

    Raises:
        ValueError: since/until 不是 ``YYYY-MM-DD`` 格式的本地日期。
    """
    sql = "SELECT * FROM memories WHERE 1=1 AND (expires_at IS NULL OR expires_at > ?)"
    params: list[object] = [now_iso_utc()]
    if filters.topic is not None:
        sql += " AND topic = ?"
        params.append(filters.topic)
    if filters.sub_topic is not None:
        # IS 匹配兼容 NULL：配合 topic 过滤时可用于筛画像外记录（sub_topic 为空）
        sql += " AND sub_topic IS ?"
        params.append(filters.sub_topic)
    if not filters.include_forgotten:
        sql += " AND forgotten_at IS NULL"
    if filters.since is not None:
        sql += " AND created_at >= ?"
        params.append(_to_utc_boundary(filters.since, end_of_day=False))
    if filters.until is not None:
        sql += " AND created_at <= ?"
        params.append(_to_utc_boundary(filters.until, end_of_day=True))
    if filters.keyword:
        hits = fts.keyword_search(conn, filters.keyword, filters.limit)
        if not hits:
            return []
        ids = [m.id for m in hits]
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        params.extend(ids)
    sql += " ORDER BY created_at DESC, id LIMIT ?"
    params.append(filters.limit)
    return [memory_from_row(r) for r in conn.execute(sql, params).fetchall()]


def _to_utc_boundary(date_str: str, *, end_of_day: bool) -> str:
    """本地日期 → UTC 边界字符串：起始取当日 00:00，结束取当日 23:59:59。

    Args:
        date_str: 本地日期 ``YYYY-MM-DD``。
        end_of_day: True 取当日结束边界，False 取当日起始边界。

    Returns:
        UTC ISO 8601 秒级字符串（created_at 字典序即时间序，可直接比较）。

    Raises:
        ValueError: 日期格式非法。
    """
    try:
        day = date.fromisoformat(date_str)
    except ValueError as exc:
        raise ValueError(f"--since/--until require a local date in YYYY-MM-DD format, got: {date_str!r}") from exc
    boundary = time.max if end_of_day else time.min
    local = datetime.combine(day, boundary).astimezone()
    return local.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
