"""M4 服务层：search 组合检索单测（04-query.md §4）。"""

from datetime import UTC, datetime

import pytest

from mex.domain.memory import Confidence, Memory
from mex.services.search import SearchFilters, search
from mex.store import memories
from mex.store.connection import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    """临时库连接（真实 SQLite，测试隔离）。"""
    init_db(tmp_path / "test.db")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def make_memory(**kwargs):
    """构造测试记忆（默认值可覆盖）。"""
    defaults = {
        "id": "m-1",
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "is_ai_inferred": False,
        "confidence": Confidence.EXPLICIT,
        "evidence": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


def seed(conn, *ms):
    """批量插入测试数据。"""
    with transaction(conn):
        for m in ms:
            memories.insert(conn, m)


@pytest.fixture
def dataset(conn):
    """混合数据集：画像槽位、画像外记录、软删、多值字段。"""
    seed(
        conn,
        make_memory(
            id="m1",
            topic="work",
            sub_topic="company",
            content="华为",
            created_at="2026-01-10T00:00:00Z",
        ),
        make_memory(
            id="m2",
            topic="work",
            sub_topic="position",
            content="无线部门工程师",
            created_at="2026-01-05T00:00:00Z",
        ),
        make_memory(
            id="m3",
            topic="finance",
            sub_topic=None,
            content="今天基金跌了 3%，有点焦虑",
            created_at="2026-01-20T00:00:00Z",
        ),
        make_memory(
            id="m4",
            topic="health",
            sub_topic="status",
            content="最近有点失眠睡不着",
            created_at="2026-01-15T00:00:00Z",
            forgotten_at="2026-01-16T00:00:00Z",
            forgotten_reason="测试",
        ),
        make_memory(
            id="m5",
            topic="work",
            sub_topic="tech_stack",
            content='["Python", "Go"]',
            created_at="2026-01-08T00:00:00Z",
        ),
    )
    return conn


class TestBasicFilters:
    def test_no_filter_excludes_forgotten_sorted_desc(self, dataset):
        results = search(dataset, SearchFilters())
        assert [m.id for m in results] == ["m3", "m1", "m5", "m2"]
        assert all(not m.is_forgotten() for m in results)

    def test_topic_filter(self, dataset):
        results = search(dataset, SearchFilters(topic="work"))
        assert [m.id for m in results] == ["m1", "m5", "m2"]

    def test_topic_filter_hits_outside_record(self, dataset):
        """画像外记录（sub_topic 为空）同样被领域检索命中。"""
        results = search(dataset, SearchFilters(topic="finance"))
        assert [m.id for m in results] == ["m3"]

    def test_sub_topic_filter(self, dataset):
        """topic + sub_topic 精确过滤画像槽位。"""
        results = search(dataset, SearchFilters(topic="work", sub_topic="tech_stack"))
        assert [m.id for m in results] == ["m5"]

    def test_sub_topic_filter_no_topic_matches_all_topics(self, dataset):
        """仅 sub_topic（无 topic）在服务层不报错：按字段全库匹配。"""
        results = search(dataset, SearchFilters(sub_topic="tech_stack"))
        assert [m.id for m in results] == ["m5"]

    def test_sub_topic_slot_missing_returns_empty(self, dataset):
        """字段无匹配返回空列表。"""
        results = search(dataset, SearchFilters(topic="work", sub_topic="salary"))
        assert results == []

    def test_include_forgotten(self, dataset):
        results = search(dataset, SearchFilters(include_forgotten=True))
        assert [m.id for m in results] == ["m3", "m4", "m1", "m5", "m2"]

    def test_limit(self, dataset):
        results = search(dataset, SearchFilters(limit=2))
        assert [m.id for m in results] == ["m3", "m1"]

    def test_same_timestamp_sorted_by_id(self, conn):
        seed(
            conn,
            make_memory(id="z-1", sub_topic="gender", content="男", created_at="2026-02-01T00:00:00Z"),
            make_memory(id="a-1", sub_topic="birth_year", content="1990", created_at="2026-02-01T00:00:00Z"),
        )
        results = search(conn, SearchFilters())
        assert [m.id for m in results] == ["a-1", "z-1"]

    def test_excludes_expired(self, conn):
        """TTL 惰性过滤：过期记录不进检索结果。"""
        seed(
            conn,
            make_memory(
                id="e1", topic="work", sub_topic="company", content="已过期",
                expires_at="2000-01-01T00:00:00Z",
            ),
            make_memory(
                id="e2", topic="work", sub_topic="company", content="未过期",
                expires_at="2099-01-01T00:00:00Z",
            ),
        )
        results = search(conn, SearchFilters())
        assert [m.id for m in results] == ["e2"]


class TestKeyword:
    def test_two_char_keyword_like_fallback(self, dataset):
        results = search(dataset, SearchFilters(keyword="华为"))
        assert [m.id for m in results] == ["m1"]

    def test_three_char_keyword_fts(self, dataset):
        results = search(dataset, SearchFilters(keyword="工程师"))
        assert [m.id for m in results] == ["m2"]

    def test_keyword_excludes_forgotten(self, dataset):
        results = search(dataset, SearchFilters(keyword="失眠睡"))
        assert results == []

    def test_keyword_with_include_forgotten(self, dataset):
        results = search(dataset, SearchFilters(keyword="失眠睡", include_forgotten=True))
        assert [m.id for m in results] == ["m4"]

    def test_keyword_plain(self, dataset):
        """关键词检索 + 无结构条件的普通场景。"""
        results = search(dataset, SearchFilters(keyword="工程师"))
        assert [m.id for m in results] == ["m2"]

    def test_keyword_plus_topic(self, dataset):
        results = search(dataset, SearchFilters(keyword="Python", topic="work"))
        assert [m.id for m in results] == ["m5"]

    def test_keyword_no_hit(self, dataset):
        assert search(dataset, SearchFilters(keyword="不存在的词")) == []


class TestSinceUntil:
    def _local_date(self, utc_iso: str) -> str:
        dt = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return dt.astimezone().strftime("%Y-%m-%d")

    @pytest.fixture
    def dated(self, conn):
        seed(
            conn,
            make_memory(
                id="e1",
                sub_topic="gender",
                content="早",
                created_at="2026-01-01T00:00:00Z",
            ),
            make_memory(
                id="e2",
                sub_topic="birth_year",
                content="中",
                created_at="2026-01-15T12:00:00Z",
            ),
            make_memory(
                id="e3",
                sub_topic="hometown",
                content="晚",
                created_at="2026-01-31T23:00:00Z",
            ),
        )
        return conn

    def test_since_includes_day(self, dated):
        since = self._local_date("2026-01-15T12:00:00Z")
        results = search(dated, SearchFilters(since=since))
        assert {m.id for m in results} == {"e2", "e3"}

    def test_until_includes_day(self, dated):
        until = self._local_date("2026-01-15T12:00:00Z")
        results = search(dated, SearchFilters(until=until))
        assert [m.id for m in results] == ["e2", "e1"]

    def test_since_and_until_same_day(self, dated):
        day = self._local_date("2026-01-15T12:00:00Z")
        results = search(dated, SearchFilters(since=day, until=day))
        assert [m.id for m in results] == ["e2"]

    def test_invalid_date_raises(self, dated):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            search(dated, SearchFilters(since="2026-13-45"))
