"""存储层：全部 SQL 的唯一归属地（M2）。

- ``connection``：连接、建表、事务封装
- ``memories``：memories 表 CRUD（含 FTS 同步）
- ``history``：审计历史
- ``fts``：FTS5 索引同步与检索
"""

from mex.store.connection import SCHEMA_VERSION, connect, init_db, memory_from_row, migrate, transaction

__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "init_db",
    "memory_from_row",
    "migrate",
    "transaction",
]
