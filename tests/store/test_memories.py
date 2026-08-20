"""M2 存储层：memories.py 单测。"""


import pytest

from mex.domain.memory import Confidence, Memory
from mex.domain.schema import Schema, SubTopicSpec, TopicSpec
from mex.store import memories
from mex.store.connection import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "test.db")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def schema():
    return Schema(
        topics={
            "work": TopicSpec(
                name="work",
                sub_topics={"company": SubTopicSpec(name="company"), "employer": SubTopicSpec(name="employer")},
            ),
        },
    )


def make_memory(**kwargs):
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


class TestInsert:
    def test_insert_and_get(self, conn):
        m = make_memory()
        with transaction(conn):
            memories.insert(conn, m)
        assert memories.get(conn, "m-1") == m

    def test_duplicate_slot_allowed_no_db_constraint(self, conn):
        """v3 起无 DB 唯一索引：同槽位可多条共存（唯一性由业务层按 schema.unique 校验）。"""
        with transaction(conn):
            memories.insert(conn, make_memory())
            memories.insert(conn, make_memory(id="m-2", content="腾讯"))
        assert memories.get(conn, "m-1") is not None
        assert memories.get(conn, "m-2") is not None

    def test_outside_records_coexist(self, conn):
        """画像外记录（sub_topic 为空）天然多条，无任何唯一约束。"""
        with transaction(conn):
            memories.insert(conn, make_memory(topic="finance", sub_topic=None, content="配置重疾险"))
            memories.insert(conn, make_memory(id="m-2", topic="finance", sub_topic=None, content="配置医疗险"))
        assert memories.get(conn, "m-2") is not None

    def test_outside_record_coexists_with_profile_slot(self, conn):
        """画像外记录（sub_topic 空）与画像槽位互不冲突，可共存。"""
        with transaction(conn):
            memories.insert(conn, make_memory(topic="finance", sub_topic=None, content="配置重疾险"))
            memories.insert(conn, make_memory(id="m-2", topic="finance", sub_topic="income", content="月入2万"))
        assert memories.get(conn, "m-2") is not None

    def test_same_slot_after_forget_ok(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            memories.forget(conn, "m-1", "不再需要")
        with transaction(conn):
            memories.insert(conn, make_memory(id="m-2"))  # 软删后槽位释放
        assert memories.get(conn, "m-2") is not None


class TestFindBySlot:
    def test_profile_slot(self, conn):
        """find_by_slot 按 (topic, sub_topic) 匹配画像槽位。"""
        with transaction(conn):
            memories.insert(conn, make_memory(topic="finance", sub_topic=None, content="配置重疾险"))
            memories.insert(conn, make_memory(id="m-2", topic="finance", sub_topic="income", content="月入2万"))
        assert memories.find_by_slot(conn, "finance", None).id == "m-1"
        assert memories.find_by_slot(conn, "finance", "income").id == "m-2"


class TestListBySlot:
    def test_returns_all_ordered_by_confidence(self, conn):
        """可多条槽位：list_by_slot 返回全部有效记录，按置信度权重降序。"""
        with transaction(conn):
            memories.insert(conn, make_memory(id="a", confidence=Confidence.SPECULATED, content="A"))
            memories.insert(conn, make_memory(id="b", confidence=Confidence.EXPLICIT, content="B"))
            memories.insert(conn, make_memory(id="c", confidence=Confidence.INFERRED, content="C"))
        rows = memories.list_by_slot(conn, "work", "company")
        assert [m.id for m in rows] == ["b", "c", "a"]

    def test_excludes_forgotten(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory(id="a"))
            memories.forget(conn, "a", "x")
            memories.insert(conn, make_memory(id="b", content="腾讯"))
        rows = memories.list_by_slot(conn, "work", "company")
        assert [m.id for m in rows] == ["b"]

    def test_empty(self, conn):
        assert memories.list_by_slot(conn, "work", "company") == []

    def test_outside_record_slot(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory(id="a", topic="finance", sub_topic=None, content="x"))
            memories.insert(conn, make_memory(id="b", topic="finance", sub_topic=None, content="y"))
        rows = memories.list_by_slot(conn, "finance", None)
        assert {m.id for m in rows} == {"a", "b"}


class TestUpdateContent:
    def test_update(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            updated = memories.update_content(conn, "m-1", "中兴", Confidence.CONFIRMED)
        assert updated is not None
        assert updated.content == "中兴"
        assert updated.confidence is Confidence.CONFIRMED
        got = memories.get(conn, "m-1")
        assert got.content == "中兴"

    def test_update_missing_returns_none(self, conn):
        assert memories.update_content(conn, "nope", "x", Confidence.EXPLICIT) is None

    def test_slot_unchanged(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            memories.update_content(conn, "m-1", "中兴", Confidence.EXPLICIT)
        got = memories.get(conn, "m-1")
        assert got.topic == "work" and got.sub_topic == "company"


class TestDeleteAndRestore:
    def test_forget(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            gone = memories.forget(conn, "m-1", "原因")
        assert gone is not None
        assert gone.is_forgotten()
        assert gone.forgotten_reason == "原因"
        assert memories.get(conn, "m-1") is None
        assert memories.get(conn, "m-1", include_forgotten=True) is not None

    def test_forget_twice_returns_none(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            memories.forget(conn, "m-1", "x")
        assert memories.forget(conn, "m-1", "y") is None

    def test_restore(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            memories.forget(conn, "m-1", "x")
            restored = memories.restore(conn, "m-1")
        assert restored is not None
        assert not restored.is_forgotten()
        assert memories.get(conn, "m-1") is not None

    def test_restore_active_returns_none(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
        assert memories.restore(conn, "m-1") is None

    def test_hard_delete(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            gone = memories.hard_delete(conn, "m-1")
        assert gone is not None
        assert memories.get(conn, "m-1", include_forgotten=True) is None


class TestQueries:
    def test_find_by_slot(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
        hit = memories.find_by_slot(conn, "work", "company")
        assert hit is not None and hit.id == "m-1"
        assert memories.find_by_slot(conn, "work", "employer") is None

    def test_find_by_slot_ignores_forgotten(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory())
            memories.forget(conn, "m-1", "x")
        assert memories.find_by_slot(conn, "work", "company") is None
        assert memories.find_by_slot(conn, "work", "company", include_forgotten=True) is not None

    def test_list_all_filters(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory(id="a", topic="work", sub_topic="company"))
            memories.insert(conn, make_memory(id="b", topic="work", sub_topic="company", content="腾讯"))
            memories.insert(conn, make_memory(id="c", topic=None, sub_topic=None, content="随手记"))
        assert len(memories.list_all(conn)) == 3
        assert len(memories.list_all(conn, topic="work")) == 2

    def test_list_all_limit_and_offset(self, conn):
        """list_all SQL 层分页：limit 截断、offset 跳过、组合翻页、limit=0 表示全部。"""
        with transaction(conn):
            for i in range(5):
                memories.insert(
                    conn,
                    make_memory(
                        id=f"m-{i}",
                        topic=None,
                        sub_topic=None,
                        content=f"记录{i}",
                        created_at=f"2026-01-0{i + 1}T00:00:00Z",
                    ),
                )
        # created_at 倒序：m-4 最新在前
        assert [m.id for m in memories.list_all(conn, limit=2)] == ["m-4", "m-3"]
        assert [m.id for m in memories.list_all(conn, limit=2, offset=2)] == ["m-2", "m-1"]
        assert [m.id for m in memories.list_all(conn, offset=3)] == ["m-1", "m-0"]  # 只偏移不截断
        assert [m.id for m in memories.list_all(conn, limit=0)] == ["m-4", "m-3", "m-2", "m-1", "m-0"]
        assert memories.list_all(conn, limit=None) == memories.list_all(conn, limit=0)

    def test_count_all_matches_list_all_filters(self, conn):
        """count_all 与 list_all 同过滤条件统计总数，不受分页影响。"""
        with transaction(conn):
            memories.insert(conn, make_memory(id="a", topic="work", sub_topic="company"))
            memories.insert(conn, make_memory(id="b", topic="work", sub_topic="tech_stack", content="Python"))
            memories.insert(conn, make_memory(id="c", topic=None, sub_topic=None, content="随手记"))
            memories.forget(conn, "b", "x")
        assert memories.count_all(conn) == 2
        assert memories.count_all(conn, topic="work") == 1
        assert memories.count_all(conn, topic="work", include_forgotten=True) == 2
        assert len(memories.list_all(conn, limit=1)) == 1  # 分页只影响返回条数

    def test_list_forgotten(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory(id="a"))
            memories.insert(conn, make_memory(id="b", topic="work", sub_topic="company", content="腾讯"))
            memories.forget(conn, "b", "x")
        assert [m.id for m in memories.list_forgotten(conn)] == ["b"]

    def test_count_by_shape(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory(id="a", topic="work", sub_topic="company"))
            memories.insert(conn, make_memory(id="b", topic="work", sub_topic="tech_stack"))
            memories.insert(conn, make_memory(id="c", topic=None, sub_topic=None))
            memories.forget(conn, "c", "x")
        assert memories.count_by_shape(conn) == {"profile": 2, "outside": 0}

    def test_pending_review(self, conn):
        with transaction(conn):
            memories.insert(
                conn, make_memory(id="a", is_ai_inferred=True, confidence=Confidence.SPECULATED),
            )
            memories.insert(
                conn,
                make_memory(
                    id="b", is_ai_inferred=True, confidence=Confidence.CONFIRMED,
                    sub_topic="position",
                ),
            )
            memories.insert(
                conn,
                make_memory(
                    id="c",
                    is_ai_inferred=False,
                    confidence=Confidence.SPECULATED,
                    topic=None,
                    sub_topic=None,
                ),
            )
            memories.insert(
                conn,
                make_memory(
                    id="d",
                    is_ai_inferred=True,
                    confidence=Confidence.UNCERTAIN,
                    sub_topic="position",
                ),
            )
        assert memories.count_pending_review(conn) == 2
        assert {m.id for m in memories.list_pending_review(conn)} == {"a", "d"}

    def test_dangling_slots(self, conn):
        with transaction(conn):
            memories.insert(conn, make_memory(id="ok", topic="work", sub_topic="company"))
            memories.insert(conn, make_memory(id="bad", topic="work", sub_topic="salary"))
            memories.insert(conn, make_memory(id="evt", topic=None, sub_topic=None))
        dangling = memories.list_dangling_slots(conn, make_schema())
        assert [m.id for m in dangling] == ["bad"]


def make_schema():
    return Schema(
        topics={
            "work": TopicSpec(
                name="work",
                sub_topics={"company": SubTopicSpec(name="company")},
            ),
        },
    )


class TestExpiration:
    """TTL 惰性过滤：过期记录对有效记录查询不可见（数据保留）。"""

    NOW = "2026-08-10T00:00:00Z"

    def test_list_all_excludes_expired(self, conn):
        with transaction(conn):
            memories.insert(
                conn, make_memory(id="ok", topic="work", sub_topic="company", content="未过期"),
            )
            memories.insert(
                conn,
                make_memory(
                    id="exp", topic="work", sub_topic="company", content="已过期",
                    expires_at="2026-01-01T00:00:00Z",
                ),
            )
        rows = memories.list_all(conn, now=self.NOW)
        assert [m.id for m in rows] == ["ok"]
        # 数据仍在库（未 gc），仅查询不可见
        row = conn.execute("SELECT COUNT(*) AS n FROM memories WHERE id='exp'").fetchone()
        assert row["n"] == 1

    def test_find_by_slot_and_list_by_slot_exclude_expired(self, conn):
        with transaction(conn):
            memories.insert(
                conn,
                make_memory(
                    id="exp", topic="work", sub_topic="company",
                    content="已过期", expires_at="2026-01-01T00:00:00Z",
                ),
            )
        assert memories.find_by_slot(conn, "work", "company", now=self.NOW) is None
        assert memories.list_by_slot(conn, "work", "company", now=self.NOW) == []

    def test_pending_review_excludes_expired(self, conn):
        with transaction(conn):
            memories.insert(
                conn,
                make_memory(
                    id="a", topic="work", sub_topic="company", content="A",
                    is_ai_inferred=True, confidence=Confidence.SPECULATED,
                    expires_at="2026-01-01T00:00:00Z",
                ),
            )
            memories.insert(
                conn,
                make_memory(
                    id="b", topic="work", sub_topic="company", content="B",
                    is_ai_inferred=True, confidence=Confidence.SPECULATED,
                ),
            )
        assert memories.count_pending_review(conn, now=self.NOW) == 1
        assert [m.id for m in memories.list_pending_review(conn, now=self.NOW)] == ["b"]

    def test_not_expired_visible(self, conn):
        with transaction(conn):
            memories.insert(
                conn,
                make_memory(
                    id="future", topic="work", sub_topic="company",
                    content="未来过期", expires_at="2099-01-01T00:00:00Z",
                ),
            )
        assert memories.list_all(conn, now=self.NOW)[0].id == "future"

    def test_list_expired(self, conn):
        """list_expired 返回已过期未删记录；永不到期的不在其中。"""
        with transaction(conn):
            memories.insert(
                conn,
                make_memory(
                    id="e1", topic="work", sub_topic="company", content="旧",
                    expires_at="2026-01-01T00:00:00Z",
                ),
            )
            memories.insert(
                conn,
                make_memory(
                    id="e2", topic="work", sub_topic="company", content="保留",
                ),
            )
        expired = memories.list_expired(conn, now=self.NOW)
        assert [m.id for m in expired] == ["e1"]


class TestListRecentOutside:
    """最近画像外记录查询（近期动态小节的数据源）。"""

    def _seed(self, conn, *ms):
        with transaction(conn):
            for m in ms:
                memories.insert(conn, m)

    def test_window_order_and_limit(self, conn):
        self._seed(
            conn,
            make_memory(id="o1", topic="finance", sub_topic=None, content="最新", created_at="2026-08-09T00:00:00Z"),
            make_memory(id="o2", topic="finance", sub_topic=None, content="中间", created_at="2026-08-05T00:00:00Z"),
            make_memory(id="o3", topic="finance", sub_topic=None, content="最旧", created_at="2026-08-03T00:00:00Z"),
        )
        rows = memories.list_recent_outside(conn, since="2026-08-04T00:00:00Z", limit=10)
        assert [m.id for m in rows] == ["o1", "o2"]  # 窗口过滤（08-03 在窗外）+ created_at 倒序
        rows = memories.list_recent_outside(conn, since="2026-08-01T00:00:00Z", limit=2)
        assert [m.id for m in rows] == ["o1", "o2"]  # limit 截断

    def test_topic_filter(self, conn):
        self._seed(
            conn,
            make_memory(id="o1", topic="finance", sub_topic=None, content="理财", created_at="2026-08-09T00:00:00Z"),
            make_memory(id="o2", topic="work", sub_topic=None, content="工作", created_at="2026-08-09T01:00:00Z"),
        )
        rows = memories.list_recent_outside(conn, since="2026-08-01T00:00:00Z", limit=10, topics=["finance"])
        assert [m.id for m in rows] == ["o1"]

    def test_excludes_forgotten_and_profile_slots(self, conn):
        self._seed(
            conn,
            make_memory(id="o1", topic="finance", sub_topic=None, content="有效", created_at="2026-08-09T00:00:00Z"),
            make_memory(id="o2", topic="finance", sub_topic=None, content="已删", created_at="2026-08-09T01:00:00Z"),
            make_memory(
                id="o3", topic="work", sub_topic="company", content="画像槽位", created_at="2026-08-09T02:00:00Z",
            ),
        )
        with transaction(conn):
            memories.forget(conn, "o2", "x")
        rows = memories.list_recent_outside(conn, since="2026-08-01T00:00:00Z", limit=10)
        assert [m.id for m in rows] == ["o1"]
