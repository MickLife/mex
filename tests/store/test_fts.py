"""M2 存储层：fts.py 单测。"""

import pytest

from mex.domain.memory import Confidence, Memory
from mex.store import fts, memories
from mex.store.connection import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "test.db")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def make_memory(**kwargs):
    defaults = {
        "id": "m-1",
        "topic": "finance",
        "sub_topic": None,
        "content": "最近失恋了，有点难过",
        "is_ai_inferred": False,
        "confidence": Confidence.EXPLICIT,
        "evidence": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


class TestKeywordSearch:
    def test_chinese_keyword(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
        results = fts.keyword_search(conn, "失恋", 10)
        assert len(results) == 1
        assert results[0].id == "m-1"

    def test_no_match(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
        assert fts.keyword_search(conn, "基金大涨", 10) == []

    def test_empty_and_bad_input(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
        assert fts.keyword_search(conn, "", 10) == []
        assert fts.keyword_search(conn, "   ", 10) == []
        assert fts.keyword_search(conn, "失恋", 0) == []

    def test_special_chars_no_error(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
        assert fts.keyword_search(conn, '" OR *', 10) == []

    def test_limit(self, conn):
        with transaction(conn):
            for i in range(5):
                memories.insert(conn, make_memory(id=f"m-{i}", content="记录一下今天的心情变化"))
        assert len(fts.keyword_search(conn, "心情", 3)) == 3


class TestIndexSync:
    def test_update_refreshes_index(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory(content="旧关键词甲乙丙"))
            memories.update_content(conn, "m-1", "新关键词子丑寅", Confidence.EXPLICIT)
        assert fts.keyword_search(conn, "甲乙丙", 10) == []
        assert len(fts.keyword_search(conn, "子丑寅", 10)) == 1

    def test_hard_delete_removes_index(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            memories.hard_delete(conn, "m-1")
        assert fts.keyword_search(conn, "失恋", 10) == []


class TestFtsEscape:
    def test_plain_word(self):
        assert fts.fts_escape("失恋") == '"失恋"'

    def test_embedded_quote(self):
        assert fts.fts_escape('say "hi"') == '"say ""hi"""'

    def test_double_quote_no_error(self):
        assert fts.fts_escape('" OR *') == '""" OR *"'
