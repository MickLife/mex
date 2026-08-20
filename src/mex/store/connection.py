"""SQLite 连接、建表与事务封装。

连接约定（架构文档 §4.3）：
- 每条 CLI 命令一个连接，命令结束关闭，无连接池；
- WAL 模式 + busy_timeout 5000ms（多进程写冲突自动等待）；
- 写操作统一走 :func:`transaction`（BEGIN IMMEDIATE），保证原子。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from typing import TYPE_CHECKING, Iterator

from mex.domain.memory import Confidence, Memory

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 5


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开数据库连接（不建表）。

    设置 row_factory、busy_timeout（5000ms）、外键约束。

    Args:
        db_path: 数据库文件路径。

    Returns:
        已配置的连接。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _load_schema_sql() -> str:
    """读取建表 SQL 文本（``schema.sql``，打包后同样可用）。"""
    return resources.files("mex.store").joinpath("schema.sql").read_text(encoding="utf-8")


def init_db(db_path: str | Path) -> None:
    """创建全部表与索引（从 ``schema.sql`` 加载），设置结构版本与 WAL 模式。

    幂等：全部 DDL 为 ``IF NOT EXISTS``，可重复执行。

    Args:
        db_path: 数据库文件路径。
    """
    conn = connect(db_path)
    try:
        conn.executescript(_load_schema_sql())
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


def migrate(db_path: str | Path) -> None:
    """按结构版本执行迁移脚本（架构文档 §3.6）。

    v4→v5：memories 加 expires_at 列（TTL 自动遗忘）。
    v3→v4：移除 memories.layer 列（投影模型不再需要预分层）。
    v3：槽位唯一性从数据库唯一索引上移到 schema.yaml 的 ``unique`` 字段声明 +
    写入路径校验。迁移删除旧唯一索引（保险式清理），不迁移任何数据。

    Args:
        db_path: 数据库文件路径。

    Raises:
        RuntimeError: 数据库版本高于当前代码期望版本（说明数据来自更新的 meX）。
    """
    conn = connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is newer than the supported version {SCHEMA_VERSION}; "
                "please upgrade meX to use this database",
            )
        if version < 3:
            conn.execute("DROP INDEX IF EXISTS idx_unique_slot_v2")
            conn.execute("DROP INDEX IF EXISTS idx_unique_slot")
        if version < 4:
            conn.execute("ALTER TABLE memories DROP COLUMN layer")
        if version < 5:
            conn.execute("ALTER TABLE memories ADD COLUMN expires_at TEXT")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """写事务上下文：进入时 BEGIN IMMEDIATE，退出时提交，异常时回滚。

    Args:
        conn: 已打开的连接。

    Raises:
        RuntimeError: 在已有事务中嵌套调用时抛出（防止误用）。
    """
    if conn.in_transaction:
        raise RuntimeError("transaction() does not support nesting")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def memory_from_row(row: sqlite3.Row) -> Memory:
    """SQL 行 → 领域对象：枚举字符串转枚举、0/1 转 bool。

    Args:
        row: 查询结果行。

    Raises:
        ValueError: 行字段缺失或取值非法。
    """
    try:
        return Memory(
            id=row["id"],
            topic=row["topic"],
            sub_topic=row["sub_topic"],
            content=row["content"],
            is_ai_inferred=bool(row["is_ai_inferred"]),
            confidence=Confidence(row["confidence"]),
            evidence=row["evidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            forgotten_at=row["forgotten_at"],
            forgotten_reason=row["forgotten_reason"],
            expires_at=row["expires_at"],
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"Failed to construct Memory from SQL row: {exc}") from exc
