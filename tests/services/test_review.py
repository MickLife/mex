"""M6 审查队列服务层测试（services/review.py）。"""

from __future__ import annotations

import pytest

from mex.domain.memory import Confidence, Memory
from mex.services import review as review_service
from mex.store import history, memories
from mex.store.connection import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "test.db")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def make_memory(**kwargs) -> Memory:
    """构造测试记忆：默认是待审查的画像条目（AI 推断 + speculated）。"""
    defaults = {
        "id": "m-1",
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "is_ai_inferred": True,
        "confidence": Confidence.SPECULATED,
        "evidence": "用户提到过华为",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


def insert(conn, m: Memory) -> None:
    """直接插入一条记忆（绕过审查业务）。"""
    with transaction(conn):
        memories.insert(conn, m)


def forget(conn, memory_id: str, reason: str) -> None:
    """软删除一条记忆（模拟 M3 forget）。"""
    with transaction(conn):
        memories.forget(conn, memory_id, reason)


class TestListPending:
    def test_filters(self, conn):
        insert(conn, make_memory(id="m-ai", created_at="2026-01-01T00:00:00Z"))
        insert(
            conn,
            make_memory(
                id="m-explicit",
                sub_topic="position",
                confidence=Confidence.EXPLICIT,
                created_at="2026-03-01T00:00:00Z",
            ),
        )
        insert(conn, make_memory(id="m-user", sub_topic="employer", is_ai_inferred=False))
        insert(conn, make_memory(id="m-confirmed", sub_topic="hometown", confidence=Confidence.CONFIRMED))
        insert(conn, make_memory(id="m-forgotten", sub_topic="stack", created_at="2026-04-01T00:00:00Z"))
        forget(conn, "m-forgotten", "不需要")

        pending = review_service.list_pending(conn)

        assert [m.id for m in pending] == ["m-explicit", "m-ai"]

    def test_order_by_created_at_desc(self, conn):
        insert(conn, make_memory(id="m-old", created_at="2026-01-01T00:00:00Z"))
        insert(conn, make_memory(id="m-new", sub_topic="position", created_at="2026-02-01T00:00:00Z"))
        assert [m.id for m in review_service.list_pending(conn)] == ["m-new", "m-old"]

    def test_empty(self, conn):
        assert review_service.list_pending(conn) == []


class TestApprove:
    def test_confirms_and_keeps_ai_flag(self, conn):
        insert(conn, make_memory())
        updated = review_service.approve(conn, "m-1")

        assert updated is not None
        assert updated.confidence is Confidence.CONFIRMED
        assert updated.is_ai_inferred is True
        assert updated.evidence == "用户提到过华为"
        assert updated.updated_at > updated.created_at
        assert review_service.list_pending(conn) == []

    def test_writes_history(self, conn):
        insert(conn, make_memory())
        review_service.approve(conn, "m-1")

        records = history.list_for_memory(conn, "m-1")
        assert len(records) == 1
        assert records[0]["event"] == "approve"
        assert records[0]["actor"] == "user"
        assert records[0]["old_content"] == "华为"
        assert records[0]["new_content"] == "华为"

    def test_user_memory_returns_none(self, conn):
        insert(conn, make_memory(is_ai_inferred=False))
        assert review_service.approve(conn, "m-1") is None
        assert history.list_for_memory(conn, "m-1") == []

    def test_confirmed_memory_returns_none(self, conn):
        insert(conn, make_memory(confidence=Confidence.CONFIRMED))
        assert review_service.approve(conn, "m-1") is None

    def test_forgotten_returns_none(self, conn):
        insert(conn, make_memory())
        forget(conn, "m-1", "先放着")
        assert review_service.approve(conn, "m-1") is None

    def test_missing_returns_none(self, conn):
        assert review_service.approve(conn, "no-such-id") is None

    def test_approve_twice_returns_none(self, conn):
        insert(conn, make_memory())
        assert review_service.approve(conn, "m-1") is not None
        assert review_service.approve(conn, "m-1") is None


class TestDecline:
    def test_soft_deletes_with_reason(self, conn):
        insert(conn, make_memory())
        deleted = review_service.decline(conn, "m-1", "推断错误")

        assert deleted is not None
        assert deleted.forgotten_at is not None
        assert deleted.forgotten_reason == "推断错误"
        assert memories.get(conn, "m-1") is None
        got = memories.get(conn, "m-1", include_forgotten=True)
        assert got is not None and got.is_forgotten()
        assert review_service.list_pending(conn) == []

    def test_without_reason_ok(self, conn):
        insert(conn, make_memory())
        deleted = review_service.decline(conn, "m-1", None)
        assert deleted is not None
        assert deleted.forgotten_reason == ""

    def test_writes_history(self, conn):
        insert(conn, make_memory())
        review_service.decline(conn, "m-1", "不准确")

        records = history.list_for_memory(conn, "m-1")
        assert len(records) == 1
        assert records[0]["event"] == "forget"
        assert records[0]["actor"] == "user"
        assert records[0]["old_content"] == "华为"
        assert records[0]["new_content"] is None

    def test_user_memory_returns_none(self, conn):
        insert(conn, make_memory(is_ai_inferred=False))
        assert review_service.decline(conn, "m-1", "原因") is None
        assert history.list_for_memory(conn, "m-1") == []

    def test_confirmed_memory_returns_none(self, conn):
        insert(conn, make_memory(confidence=Confidence.CONFIRMED))
        assert review_service.decline(conn, "m-1", "原因") is None

    def test_missing_returns_none(self, conn):
        assert review_service.decline(conn, "no-such-id", "原因") is None

    def test_forgotten_returns_none(self, conn):
        insert(conn, make_memory())
        forget(conn, "m-1", "已删")
        assert review_service.decline(conn, "m-1", "再删一次") is None
