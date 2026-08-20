"""M2 存储层：connection.py 单测。"""


import pytest

from mex.domain.memory import Confidence
from mex.store.connection import connect, init_db, memory_from_row, migrate, transaction

_INSERT_SQL = (
    "INSERT INTO memories (id, topic, sub_topic, content, is_ai_inferred, "
    "confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def _insert_raw(conn, mid, content="华为", is_ai=0, confidence="explicit"):  # noqa: PLR0913, PLR0917 - 测试辅助
    """直接 SQL 插入最小字段（测试用）。"""
    conn.execute(_INSERT_SQL, (mid, "work", "company", content, is_ai, confidence,
                               "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path):
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


class TestInitDb:
    def test_idempotent(self, db_path):
        init_db(db_path)
        init_db(db_path)

    def test_user_version(self, db_path):
        init_db(db_path)
        c = connect(db_path)
        try:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 5
        finally:
            c.close()

    def test_no_layer_column(self, db_path):
        """v5 表结构不含 layer 列。"""
        init_db(db_path)
        c = connect(db_path)
        try:
            cols = {r["name"] for r in c.execute("PRAGMA table_info('memories')")}
            assert "layer" not in cols
        finally:
            c.close()

    def test_has_expires_at_column(self, db_path):
        """v5 新库含 expires_at 列（TTL 自动遗忘）。"""
        init_db(db_path)
        c = connect(db_path)
        try:
            cols = {r["name"] for r in c.execute("PRAGMA table_info('memories')")}
            assert "expires_at" in cols
        finally:
            c.close()

    def test_wal_enabled(self, db_path):
        init_db(db_path)
        c = connect(db_path)
        try:
            assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            c.close()

    def test_tables_exist(self, db_path):
        init_db(db_path)
        c = connect(db_path)
        try:
            names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"memories", "history", "llm_usage", "extraction_state", "memories_fts"} <= names
        finally:
            c.close()

    def test_busy_timeout_set(self, db_path):
        init_db(db_path)
        c = connect(db_path)
        try:
            assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            c.close()


class TestMigrate:
    def test_version_ok(self, db_path):
        init_db(db_path)
        migrate(db_path)

    def test_v3_to_v5_drops_layer_and_adds_expires(self, db_path):
        """v3 库迁移到 v5：layer 列移除，expires_at 列新增，数据无损。"""
        conn = connect(db_path)
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, layer TEXT NOT NULL, topic TEXT, sub_topic TEXT,
                content TEXT NOT NULL, is_ai_inferred INTEGER NOT NULL, confidence TEXT NOT NULL,
                evidence TEXT, embedding BLOB, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                forgotten_at TEXT, forgotten_reason TEXT
            );
            INSERT INTO memories (id, layer, topic, sub_topic, content, is_ai_inferred, confidence,
                created_at, updated_at)
                VALUES ('a', 'stable', 'work', 'company', '华为', 0, 'explicit',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """,
        )
        conn.execute("PRAGMA user_version=3")
        conn.commit()
        conn.close()

        migrate(db_path)
        c = connect(db_path)
        try:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 5
            cols = {r["name"] for r in c.execute("PRAGMA table_info('memories')")}
            assert "layer" not in cols
            assert "expires_at" in cols
            # 迁移后数据完整保留
            row = c.execute("SELECT * FROM memories WHERE id='a'").fetchone()
            assert row["topic"] == "work" and row["sub_topic"] == "company"
            assert row["expires_at"] is None
        finally:
            c.close()

    def test_v2_to_v5_full_migration(self, db_path):
        """v2 库迁移到 v5：旧唯一索引删除 + layer 移除 + expires_at 新增。"""
        conn = connect(db_path)
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, layer TEXT NOT NULL, topic TEXT, sub_topic TEXT,
                content TEXT NOT NULL, is_ai_inferred INTEGER NOT NULL, confidence TEXT NOT NULL,
                evidence TEXT, embedding BLOB, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                forgotten_at TEXT, forgotten_reason TEXT
            );
            CREATE UNIQUE INDEX idx_unique_slot_v2 ON memories(layer, topic, COALESCE(sub_topic, ''))
                WHERE forgotten_at IS NULL AND layer IN ('stable', 'state');
            """,
        )
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()

        migrate(db_path)
        c = connect(db_path)
        try:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 5
            idx = {
                r["name"]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memories'")
            }
            assert "idx_unique_slot_v2" not in idx
            cols = {r["name"] for r in c.execute("PRAGMA table_info('memories')")}
            assert "layer" not in cols
            assert "expires_at" in cols
        finally:
            c.close()

    def test_v4_to_v5_adds_expires_column(self, db_path):
        """v4 库迁移到 v5：仅新增 expires_at 列，数据无损。"""
        conn = connect(db_path)
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, topic TEXT, sub_topic TEXT, content TEXT NOT NULL,
                is_ai_inferred INTEGER NOT NULL, confidence TEXT NOT NULL, evidence TEXT,
                embedding BLOB, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                forgotten_at TEXT, forgotten_reason TEXT
            );
            INSERT INTO memories (id, topic, sub_topic, content, is_ai_inferred, confidence,
                created_at, updated_at)
                VALUES ('v4r', 'work', 'company', '腾讯', 0, 'explicit',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """,
        )
        conn.execute("PRAGMA user_version=4")
        conn.commit()
        conn.close()

        migrate(db_path)
        c = connect(db_path)
        try:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 5
            cols = {r["name"] for r in c.execute("PRAGMA table_info('memories')")}
            assert "expires_at" in cols
            row = c.execute("SELECT * FROM memories WHERE id='v4r'").fetchone()
            assert row["content"] == "腾讯" and row["expires_at"] is None
        finally:
            c.close()

    def test_newer_version_raises(self, db_path):
        init_db(db_path)
        c = connect(db_path)
        try:
            c.execute("PRAGMA user_version=99")
            c.commit()
        finally:
            c.close()
        with pytest.raises(RuntimeError, match="99"):
            migrate(db_path)


class TestTransaction:
    def test_commit(self, conn):
        with transaction(conn):
            _insert_raw(conn, "t1")
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='t1'").fetchone()[0] == 1

    def test_rollback(self, conn):
        with pytest.raises(ValueError), transaction(conn):
            _insert_raw(conn, "t2")
            raise ValueError("boom")
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='t2'").fetchone()[0] == 0

    def test_nested_raises(self, conn):
        with pytest.raises(RuntimeError, match="does not support nesting"), transaction(conn), transaction(conn):
            pass


class TestMemoryFromRow:
    def test_full_row(self, conn):
        _insert_raw(conn, "m1", is_ai=1, confidence="speculated")
        conn.execute(
            "UPDATE memories SET evidence='证据', forgotten_at=NULL, forgotten_reason=NULL "
            "WHERE id='m1'",
        )
        conn.commit()
        row = conn.execute("SELECT * FROM memories WHERE id='m1'").fetchone()
        m = memory_from_row(row)
        assert m.topic == "work"
        assert m.sub_topic == "company"
        assert m.confidence is Confidence.SPECULATED
        assert m.is_ai_inferred is True
        assert m.content == "华为"
        assert m.evidence == "证据"
        assert m.expires_at is None

    def test_invalid_confidence_raises(self):
        """检查 memory_from_row 对非法枚举字符串的防御。"""

        class _FakeRow:
            """最小 Row 替身：支持 dict 风格访问。"""

            def __init__(self, data: dict):
                self._data = data

            def __getitem__(self, key: str):
                return self._data[key]

        row = _FakeRow(
            {
                "id": "bad",
                "topic": "work",
                "sub_topic": "company",
                "content": "x",
                "is_ai_inferred": 0,
                "confidence": "bogus",
                "evidence": None,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "forgotten_at": None,
                "forgotten_reason": None,
                "expires_at": None,
            },
        )
        with pytest.raises(ValueError, match="Memory"):
            memory_from_row(row)
