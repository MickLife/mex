"""M6 审查队列 CLI 测试（mex review 命令）。"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from mex.cli import app
from mex.cli import review as review_cli  # noqa: F401  导入即注册 review 命令
from mex.domain.memory import Confidence, Memory
from mex.store import history, memories
from mex.store.connection import connect, init_db, transaction

runner = CliRunner()


@pytest.fixture
def mex_env(tmp_path, monkeypatch):
    """初始化临时数据目录并把 MEX_HOME 指向它。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    init_db(tmp_path / "mex.db")
    return tmp_path


@pytest.fixture
def conn(mex_env):
    c = connect(mex_env / "mex.db")
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


class TestReviewList:
    def test_empty_queue(self, mex_env):
        assert (mex_env / "mex.db").exists()
        result = runner.invoke(app, ["review", "list"])
        assert result.exit_code == 0
        assert "No AI-inferred memories pending review" in result.stdout

    def test_empty_queue_json(self, mex_env):
        assert (mex_env / "mex.db").exists()
        result = runner.invoke(app, ["review", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_shows_pending_with_confidence_and_evidence(self, conn):
        insert(conn, make_memory())
        insert(
            conn,
            make_memory(id="m-user", sub_topic="employer", is_ai_inferred=False, confidence=Confidence.EXPLICIT),
        )

        result = runner.invoke(app, ["review", "list"])

        assert result.exit_code == 0
        assert "m-1" in result.stdout
        assert "speculated" in result.stdout
        assert "用户提到过华为" in result.stdout
        assert "m-user" not in result.stdout

    def test_pending_json(self, conn):
        insert(conn, make_memory())

        result = runner.invoke(app, ["review", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["id"] == "m-1"
        assert data[0]["confidence"] == "speculated"
        assert data[0]["is_ai_inferred"] is True
        assert data[0]["evidence"] == "用户提到过华为"


class TestReviewApprove:
    def test_success(self, conn):
        insert(conn, make_memory())

        result = runner.invoke(app, ["review", "approve", "m-1"])

        assert result.exit_code == 0
        assert "Approved" in result.stdout
        assert "华为" in result.stdout
        got = memories.get(conn, "m-1")
        assert got is not None and got.confidence is Confidence.CONFIRMED
        assert got.is_ai_inferred is True
        records = history.list_for_memory(conn, "m-1")
        assert records and records[0]["event"] == "approve"

    def test_json(self, conn):
        insert(conn, make_memory())

        result = runner.invoke(app, ["review", "approve", "m-1", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["id"] == "m-1"
        assert data["confidence"] == "confirmed"
        assert data["is_ai_inferred"] is True

    def test_missing_id_exit_1(self, mex_env):
        assert (mex_env / "mex.db").exists()
        result = runner.invoke(app, ["review", "approve", "no-such-id"])
        assert result.exit_code == 1
        assert "does not exist" in result.stderr

    def test_no_id_arg_exit_1(self, mex_env):
        assert (mex_env / "mex.db").exists()
        result = runner.invoke(app, ["review", "approve"])
        assert result.exit_code == 1
        assert "id" in result.stderr

    def test_confirmed_entry_exit_1(self, conn):
        insert(conn, make_memory(confidence=Confidence.CONFIRMED))
        result = runner.invoke(app, ["review", "approve", "m-1"])
        assert result.exit_code == 1
        assert "not in the pending review queue" in result.stderr

    def test_user_entry_exit_1(self, conn):
        insert(conn, make_memory(is_ai_inferred=False))
        result = runner.invoke(app, ["review", "approve", "m-1"])
        assert result.exit_code == 1
        assert "not in the pending review queue" in result.stderr

    def test_forgotten_entry_exit_1(self, conn):
        insert(conn, make_memory())
        with transaction(conn):
            memories.forget(conn, "m-1", "已删")
        result = runner.invoke(app, ["review", "approve", "m-1"])
        assert result.exit_code == 1
        assert "is already deleted" in result.stderr


class TestReviewDecline:
    def test_success_with_reason(self, conn):
        insert(conn, make_memory())

        result = runner.invoke(app, ["review", "decline", "m-1", "--reason", "与事实不符"])

        assert result.exit_code == 0
        assert "Declined" in result.stdout
        got = memories.get(conn, "m-1", include_forgotten=True)
        assert got is not None and got.is_forgotten()
        assert got.forgotten_reason == "与事实不符"
        records = history.list_for_memory(conn, "m-1")
        assert records and records[0]["event"] == "forget"

    def test_without_reason(self, conn):
        insert(conn, make_memory())

        result = runner.invoke(app, ["review", "decline", "m-1"])

        assert result.exit_code == 0
        got = memories.get(conn, "m-1", include_forgotten=True)
        assert got is not None and got.forgotten_reason == ""

    def test_json(self, conn):
        insert(conn, make_memory())

        result = runner.invoke(app, ["review", "decline", "m-1", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["id"] == "m-1"
        assert data["forgotten_at"] is not None
        assert data["is_ai_inferred"] is True

    def test_missing_id_exit_1(self, mex_env):
        assert (mex_env / "mex.db").exists()
        result = runner.invoke(app, ["review", "decline", "no-such-id"])
        assert result.exit_code == 1
        assert "does not exist" in result.stderr

    def test_confirmed_entry_exit_1(self, conn):
        insert(conn, make_memory(confidence=Confidence.CONFIRMED))
        result = runner.invoke(app, ["review", "decline", "m-1"])
        assert result.exit_code == 1
        assert "not in the pending review queue" in result.stderr

    def test_decline_overwritten_shows_previous_hint(self, conn):
        """decline 一条通过覆盖旧值产生的待审查记忆时，提示旧值可恢复。"""
        # 先插入旧值（AI 推断、uncertain，在审查队列）
        insert(conn, make_memory(id="m1", content="上海", confidence=Confidence.UNCERTAIN))
        # 模拟 extract 覆盖：update_content + history update 记录
        with transaction(conn):
            memories.update_content(conn, "m1", "深圳", Confidence.UNCERTAIN)
            history.record(conn, "m1", "update", old_content="上海", new_content="深圳", actor="ai")
        # m1 仍是 uncertain + is_ai_inferred，在审查队列里
        result = runner.invoke(app, ["review", "decline", "m1"])
        assert result.exit_code == 0
        assert "Declined" in result.stdout
        assert "上海" in result.stdout  # 旧值提示
        assert "re-write" in result.stdout

    def test_decline_new_entry_no_previous_hint(self, conn):
        """decline 一条新增（非覆盖）的待审查记忆时，不提示旧值。"""
        insert(conn, make_memory(content="华为"))
        result = runner.invoke(app, ["review", "decline", "m-1"])
        assert result.exit_code == 0
        assert "Declined" in result.stdout
        assert "恢复" not in result.stdout


class TestReviewMisc:
    def test_unknown_action_exit_1(self, mex_env):
        assert (mex_env / "mex.db").exists()
        result = runner.invoke(app, ["review", "foo"])
        assert result.exit_code == 1
        assert "Unknown subcommand" in result.stderr

    def test_uninitialized_exit_1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEX_HOME", str(tmp_path))
        result = runner.invoke(app, ["review", "list"])
        assert result.exit_code == 1
        assert "is not initialized" in result.stderr
