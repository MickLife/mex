"""M2 存储层：history.py 单测。"""

import sqlite3

import pytest

from mex.domain.memory import Confidence, Memory
from mex.store import history, memories
from mex.store.connection import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "test.db")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def memory():
    return Memory(
        id="m-1",
        topic="work",
        sub_topic="company",
        content="华为",
        is_ai_inferred=False,
        confidence=Confidence.EXPLICIT,
        evidence=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


class TestRecord:
    def test_add_and_update(self, conn, memory):
        with transaction(conn):
            memories.insert(conn, memory)
            history.record(conn, "m-1", "add", old_content=None, new_content="华为", actor="user")
            history.record(
                conn,
                "m-1",
                "update",
                old_content="华为",
                new_content="中兴",
                actor="ai",
                evidence="来自对话",
            )
        records = history.list_for_memory(conn, "m-1")
        assert len(records) == 2
        assert records[0]["event"] == "add"
        assert records[1]["event"] == "update"
        assert records[1]["old_content"] == "华为"
        assert records[1]["new_content"] == "中兴"
        assert records[1]["actor"] == "ai"
        assert records[1]["evidence"] == "来自对话"

    def test_list_empty(self, conn):
        assert history.list_for_memory(conn, "missing") == []

    def test_order_by_time(self, conn, memory):
        with transaction(conn):
            memories.insert(conn, memory)
            history.record(conn, "m-1", "add", old_content=None, new_content="a", actor="user")
            history.record(conn, "m-1", "update", old_content="a", new_content="b", actor="user")
            history.record(conn, "m-1", "forget", old_content="b", new_content=None, actor="user")
        events = [r["event"] for r in history.list_for_memory(conn, "m-1")]
        assert events == ["add", "update", "forget"]

    def test_invalid_event_rejected(self, conn, memory):
        with transaction(conn):
            memories.insert(conn, memory)
            with pytest.raises(sqlite3.IntegrityError):
                history.record(conn, "m-1", "bogus", old_content=None, new_content="a", actor="user")
