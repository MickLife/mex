"""``mex clear`` 核心：清空记忆与历史数据（架构 §5.1 命令清单扩展）。

清空范围（仅记忆相关）：
- ``memories``：全部记忆（含有效与软删条目）。
- ``memories_fts``：全文检索索引（FTS5 外部内容表，须与 memories 同事务清空）。
- ``history``：全部变更审计记录。

明确保留：
- 文件 ``config.yaml`` / ``schema.yaml``（LLM 配置与架构，绝不触碰）。
- 表 ``llm_usage`` / ``extraction_state``（用量与抽取进度，不属于记忆）。

备份：清空前可复用 :func:`mex.services.backup.export_json` 生成全量 JSON 快照
（含软删条目，还原语义无损），由 :func:`backup_memory_data` 独立提供、落在
``<mex_home>/backups/``。备份在清空前单独执行并返回路径，供 CLI 在最终确认时提示。

安全说明：本模块所有 SQL 中的表名拼接均来自下方受控常量元组 ``CLEAR_TABLES``，
非外部输入，因此 ``# noqa: S608``（静态 SQL 注入告警）是安全的。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mex.services.backup import ExportReport, export_json
from mex.store.connection import transaction

if TYPE_CHECKING:
    import sqlite3

CLEAR_TABLES = ("memories_fts", "history", "memories")
"""清空顺序：先索引（外部内容表依赖 memories），再审计，最后主表。此元组为
受控内部常量，是唯一的表名来源——凡拼进 SQL 的表名必须来自这里。"""

_CLEAR_SET = frozenset(CLEAR_TABLES)

BACKUP_FILENAME = "mex-export-before-clear.json"
"""备份快照固定文件名（落在备份目录下）。"""


@dataclass(frozen=True)
class ClearReport:
    """一次清空的结果摘要。

    Attributes:
        memories_count: 清空前的记忆总条数（含软删）。
        forgotten_count: 其中软删条数（供展示）。
        history_count: 清空前的审计记录条数。
    """

    memories_count: int
    forgotten_count: int
    history_count: int


def backup_memory_data(conn: sqlite3.Connection, backup_dir: str | Path) -> ExportReport | None:
    """生成清空前的记忆快照（供最终确认前提示备份路径）。

    Args:
        conn: 数据库连接。
        backup_dir: 备份输出目录（不存在自动创建）。

    Returns:
        备份报告（含落盘路径）；导出失败返回 None（不抛出，由调用方决定是否提示）。
    """
    path = Path(backup_dir) / BACKUP_FILENAME
    try:
        return export_json(conn, str(path))
    except OSError:
        # 备份失败不阻断清空：导出只读，失败仅说明快照未生成，交由调用方决定。
        return None


def clear_memory_data(conn: sqlite3.Connection) -> ClearReport:
    """清空记忆与历史数据（返回清空前的数量摘要）。

    Args:
        conn: 数据库连接。

    Returns:
        清空前的数量摘要（记忆 / 软删 / 审计）。

    Raises:
        sqlite3.OperationalError: 连接写操作失败（事务自动回滚，无半清空状态）。
    """
    memories_n, forgotten_n, history_n = _snapshot_counts(conn)
    with transaction(conn):
        for table in CLEAR_TABLES:
            _assert_cleartable(table)
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - 表名来自受控 _CLEAR_SET
    return ClearReport(
        memories_count=memories_n,
        forgotten_count=forgotten_n,
        history_count=history_n,
    )


def _snapshot_counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """统计清空前的数量（总记忆 / 软删记忆 / 审计记录）。"""
    memories_n = _count(conn, "memories")
    forgotten_n = _count(conn, "memories", where="forgotten_at IS NOT NULL")
    history_n = _count(conn, "history")
    return memories_n, forgotten_n, history_n


def _assert_cleartable(table: str) -> None:
    """白名单守卫：非清空表集合内的表名一律拒绝（防未来误传外部表名）。"""
    if table not in _CLEAR_SET:
        raise ValueError(f"Invalid clear target table: {table}")


def _count(conn: sqlite3.Connection, table: str, *, where: str | None = None) -> int:
    """统计受控表行数（可选 WHERE 条件）。

    Args:
        conn: 数据库连接。
        table: 目标表名（须在 ``CLEAR_TABLES`` 白名单内，内部调用固定传字面量）。
        where: 可选 SQL WHERE 子句（由内部受控调用传入，非外部输入）。

    Returns:
        匹配行数。
    """
    _assert_cleartable(table)
    sql = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 - 表名经 _assert_cleartable 白名单
    if where:
        sql += f" WHERE {where}"  # noqa: S608 - 调用点为受控内部字面量
    row = conn.execute(sql).fetchone()
    return int(row["n"])
