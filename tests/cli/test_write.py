"""M3 CLI 端到端测试：init/add/update/forget/restore/list（03-write.md §3 契约）。

数据目录一律走 MEX_HOME → pytest tmp_path，绝不触碰真实 ~/.mex。
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

import mex.cli.query as query_mod  # noqa: F401 - 导入即注册 M4 命令（profile 端到端测试用）
from mex.cli import app
from mex.cli import write  # noqa: F401 - 导入即注册 M3 命令
from mex.domain.schema import load_schema
from mex.store.connection import connect

runner = CliRunner()


@pytest.fixture()
def mex_home(tmp_path, monkeypatch) -> str:
    """MEX_HOME 指向临时目录。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    return str(tmp_path)


def _invoke(*args: str, **kwargs):
    return runner.invoke(app, list(args), **kwargs)


def _init() -> None:
    assert os.environ.get("MEX_HOME"), "测试必须在 MEX_HOME 隔离下运行"
    assert _invoke("init").exit_code == 0


def _add(**opts) -> dict:
    """执行 add --json 并返回解析后的记忆字典。"""
    args = ["add", "--json"]
    for key, value in opts.items():
        args.extend([f"--{key.replace('_', '-')}", str(value)])
    result = _invoke(*args)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def _seed(**opts) -> dict:
    """init 后 add 一条记忆并返回记忆字典。"""
    _init()
    return _add(**opts)


def _query(mex_home: str, sql: str, params: tuple = ()) -> list[dict]:
    conn = connect(Path(mex_home) / "mex.db")
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _history_events(mex_home: str) -> list[dict]:
    """全量 history 记录（按写入顺序）。"""
    return _query(mex_home, "SELECT memory_id, event, old_content, new_content, actor FROM history ORDER BY rowid")


class TestInit:
    def test_creates_files(self, mex_home):
        _init()
        home = Path(mex_home)
        assert (home / "mex.db").exists()
        assert (home / "schema.yaml").exists()
        assert (home / "config.yaml").exists()
        assert "work:" in (home / "schema.yaml").read_text(encoding="utf-8")

    def test_config_permission_600(self, mex_home):
        _init()
        mode = stat.S_IMODE(os.stat(Path(mex_home) / "config.yaml").st_mode)
        assert mode == 0o600

    def test_idempotent(self, mex_home):
        _init()
        result = _invoke("init")
        assert result.exit_code == 0 and "Initialized at" in result.stdout

    def test_json_output(self, mex_home):
        data = json.loads(_invoke("init", "--json").stdout)
        assert data["mex_home"] == mex_home
        assert data["db_path"].endswith("mex.db")


class TestAdd:
    def test_success_default_confirmed(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        assert data["content"] == "华为" and data["confidence"] == "confirmed"
        assert data["is_ai_inferred"] is False
        assert _query(mex_home, "SELECT topic, confidence FROM memories") == [
            {"topic": "work", "confidence": "confirmed"},
        ]

    @pytest.mark.parametrize("level", ["explicit", "inferred", "speculated", "uncertain"])
    def test_confidence_options(self, mex_home, level):
        _seed(topic="work", sub_topic="company", content="x", confidence=level)
        assert _query(mex_home, "SELECT confidence FROM memories")[0]["confidence"] == level

    def test_outside_record(self, mex_home):
        data = _seed(topic="finance", content="今天基金跌了3%")
        assert data["sub_topic"] is None

    def test_outside_record_without_topic(self, mex_home):
        data = _seed(content="随手记一条")
        assert data["topic"] is None and data["sub_topic"] is None

    def test_empty_content_rejected(self, mex_home):
        _init()
        result = _invoke("add", "--topic", "work", "--sub-topic", "company", "--content", "")
        assert result.exit_code == 1 and "must not be empty" in result.stderr

    def test_topic_not_in_schema(self, mex_home):
        _init()
        result = _invoke("add", "--topic", "job", "--sub-topic", "company", "--content", "x")
        assert result.exit_code == 1 and "schema.yaml" in result.stderr

    def test_subtopic_not_in_schema(self, mex_home):
        _init()
        result = _invoke("add", "--topic", "work", "--sub-topic", "employer", "--content", "x")
        assert result.exit_code == 1
        assert "Valid sub-topics under" in result.stderr and "company" in result.stderr

    def test_subtopic_without_topic_rejected(self, mex_home):
        """画像槽位必须 topic 与 sub_topic 成对：只有 sub_topic 时报错。"""
        _init()
        result = _invoke("add", "--sub-topic", "company", "--content", "x")
        assert result.exit_code == 1
        assert "topic" in result.stderr

    def test_outside_record_with_unknown_topic_rejected(self, mex_home):
        """画像外记录填了 topic 也必须在 schema 内（领域检索可靠）。"""
        _init()
        result = _invoke("add", "--topic", "hobby", "--content", "x")
        assert result.exit_code == 1
        assert "schema.yaml" in result.stderr

    def test_slot_collision(self, mex_home):
        """唯一槽位（position，默认 unique）碰撞报错；可多条槽位（company）不碰撞另测。"""
        first = _seed(topic="work", sub_topic="position", content="工程师")
        result = _invoke("add", "--topic", "work", "--sub-topic", "position", "--content", "总监")
        assert result.exit_code == 1
        assert "already has content" in result.stderr and f"mex update {first['id']}" in result.stderr

    def test_many_slot_add_appends(self, mex_home):
        """可多条槽位（tech_stack，unique: false）：连续 add 多条不报碰撞，各自独立。"""
        _init()
        _add(topic="work", sub_topic="tech_stack", content="Python")
        second = _add(topic="work", sub_topic="tech_stack", content="Go")
        third = _add(topic="work", sub_topic="tech_stack", content="Rust")
        assert second["id"] != third["id"]
        rows = _query(mex_home, "SELECT content FROM memories WHERE forgotten_at IS NULL ORDER BY content")
        assert [r["content"] for r in rows] == ["Go", "Python", "Rust"]

    def test_history_recorded(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        assert _history_events(mex_home) == [
            {"memory_id": data["id"], "event": "add", "old_content": None, "new_content": "华为", "actor": "user"},
        ]

    def test_outside_records_append_multiple(self, mex_home):
        """画像外记录（sub_topic 为空）天然多条，可重复添加、互不覆盖。"""
        _init()
        _add(topic="finance", content="计划配置百万医疗险")
        second = _add(topic="finance", content="计划配置重疾险")
        assert second["id"] is not None
        rows = _query(mex_home, "SELECT content FROM memories ORDER BY rowid")
        assert len(rows) == 2


class TestUpdate:
    def test_update_content_and_confidence(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("update", data["id"], "--content", "新内容", "--confidence", "inferred")
        assert result.exit_code == 0 and "新内容" in result.stdout
        assert _query(mex_home, "SELECT content, confidence FROM memories WHERE id = ?", (data["id"],)) == [
            {"content": "新内容", "confidence": "inferred"},
        ]
        assert _history_events(mex_home)[-1] == {
            "memory_id": data["id"],
            "event": "update",
            "old_content": "华为",
            "new_content": "新内容",
            "actor": "user",
        }

    def test_slot_immutable(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        _invoke("update", data["id"], "--content", "新内容")
        assert _query(mex_home, "SELECT topic, sub_topic FROM memories WHERE id = ?", (data["id"],)) == [
            {"topic": "work", "sub_topic": "company"},
        ]

    def test_not_found(self, mex_home):
        _init()
        result = _invoke("update", "no-such-id", "--content", "x")
        assert result.exit_code == 1 and "does not exist" in result.stderr

    def test_no_options(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("update", data["id"])
        assert result.exit_code == 1 and "Provide at least one of" in result.stderr

    def test_forgotten_entry(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        assert _invoke("forget", data["id"]).exit_code == 0
        result = _invoke("update", data["id"], "--content", "x")
        assert result.exit_code == 1 and "is already deleted" in result.stderr


class TestForget:
    def test_soft_delete_single(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("forget", data["id"], "--reason", "过时了")
        assert result.exit_code == 0 and "Deleted" in result.stdout
        row = _query(mex_home, "SELECT forgotten_at, forgotten_reason FROM memories WHERE id = ?", (data["id"],))[0]
        assert row["forgotten_at"] is not None and row["forgotten_reason"] == "过时了"
        assert _history_events(mex_home)[-1]["event"] == "forget"

    def test_not_found(self, mex_home):
        _init()
        result = _invoke("forget", "no-such-id")
        assert result.exit_code == 1 and "does not exist" in result.stderr

    def test_already_forgotten(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        assert _invoke("forget", data["id"]).exit_code == 0
        result = _invoke("forget", data["id"])
        assert result.exit_code == 1 and "is already deleted" in result.stderr

    def test_no_target_error(self, mex_home):
        _init()
        result = _invoke("forget")
        assert result.exit_code == 1 and "at least one filter" in result.stderr

    def test_no_match(self, mex_home):
        _init()
        result = _invoke("forget", "--topic", "nope")
        assert result.exit_code == 1 and "No matching entries" in result.stderr

    def test_dry_run_no_effect(self, mex_home):
        _init()
        _add(topic="work", sub_topic="company", content="华为")
        _add(topic="work", sub_topic="position", content="工程师")
        result = _invoke("forget", "--topic", "work", "--dry-run")
        assert result.exit_code == 0 and "[dry-run]" in result.stdout and "华为" in result.stdout
        assert _query(mex_home, "SELECT COUNT(*) AS n FROM memories WHERE forgotten_at IS NULL")[0]["n"] == 2

    def test_hard_requires_yes(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("forget", data["id"], "--hard")
        assert result.exit_code == 1 and "--yes" in result.stderr
        assert _query(mex_home, "SELECT COUNT(*) AS n FROM memories WHERE id = ?", (data["id"],))[0]["n"] == 1

    def test_hard_with_yes(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("forget", data["id"], "--hard", "--yes")
        assert result.exit_code == 0 and "Permanently deleted" in result.stdout
        assert _query(mex_home, "SELECT COUNT(*) AS n FROM memories WHERE id = ?", (data["id"],))[0]["n"] == 0
        assert _history_events(mex_home)[-1] == {
            "memory_id": data["id"],
            "event": "delete",
            "old_content": "华为",
            "new_content": None,
            "actor": "user",
        }

    def test_hard_interactive_yes(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        assert _invoke("forget", data["id"], "--hard", input="y\n").exit_code == 0
        assert _query(mex_home, "SELECT COUNT(*) AS n FROM memories WHERE id = ?", (data["id"],))[0]["n"] == 0

    def test_hard_interactive_no(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("forget", data["id"], "--hard", input="n\n")
        assert result.exit_code == 1 and "Deletion cancelled" in result.stderr
        assert _query(mex_home, "SELECT COUNT(*) AS n FROM memories WHERE id = ?", (data["id"],))[0]["n"] == 1

    def test_batch_by_topic(self, mex_home):
        _init()
        _add(topic="work", sub_topic="company", content="华为")
        _add(topic="work", sub_topic="position", content="工程师")
        _add(topic="finance", content="今天基金跌了3%")
        result = _invoke("forget", "--topic", "work")
        assert result.exit_code == 0 and "Deleted 2 memories" in result.stdout
        assert _query(mex_home, "SELECT COUNT(*) AS n FROM memories WHERE forgotten_at IS NULL")[0]["n"] == 1

    def test_batch_by_keyword(self, mex_home):
        _init()
        _add(topic="work", sub_topic="company", content="华为")
        _add(topic="finance", content="今天基金跌了3%")
        assert _invoke("forget", "--keyword", "基金").exit_code == 0
        assert _query(mex_home, "SELECT topic FROM memories WHERE forgotten_at IS NULL") == [{"topic": "work"}]

    def test_batch_by_since_until(self, mex_home):
        _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("forget", "--since", "2025-01-01", "--until", "2025-12-31", "--dry-run")
        assert result.exit_code == 1 and "No matching entries" in result.stderr
        result = _invoke("forget", "--since", "2025-01-01", "--dry-run")
        assert result.exit_code == 0 and "华为" in result.stdout

    def test_invalid_date(self, mex_home):
        _init()
        result = _invoke("forget", "--since", "not-a-date", "--dry-run")
        assert result.exit_code == 1 and "YYYY-MM-DD" in result.stderr


class TestRestore:
    def test_roundtrip(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        assert _invoke("forget", data["id"]).exit_code == 0
        result = _invoke("restore", data["id"])
        assert result.exit_code == 0 and "Restored" in result.stdout
        assert _query(mex_home, "SELECT forgotten_at FROM memories WHERE id = ?", (data["id"],))[0][
            "forgotten_at"
        ] is None
        assert _history_events(mex_home)[-1]["event"] == "restore"

    def test_not_forgotten(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        result = _invoke("restore", data["id"])
        assert result.exit_code == 1 and "is not in deleted state" in result.stderr
        assert _query(mex_home, "SELECT forgotten_at FROM memories WHERE id = ?", (data["id"],))[0][
            "forgotten_at"
        ] is None

    def test_not_found(self, mex_home):
        _init()
        result = _invoke("restore", "no-such-id")
        assert result.exit_code == 1 and "does not exist" in result.stderr


class TestList:
    def test_empty_library_shows_hint(self, mex_home):
        """空库时 list 打印提示，而非静默无输出。"""
        _init()
        result = _invoke("list")
        assert result.exit_code == 0
        assert "No memories yet" in result.stdout

    def test_filter_no_match_shows_hint(self, mex_home):
        """按筛选无匹配时提示条件，而非静默无输出。"""
        _init()
        result = _invoke("list", "--topic", "finance")
        assert result.exit_code == 0
        assert "No memories match" in result.stdout
        assert "(topic finance)" in result.stdout

    def test_empty_with_json_remains_empty_array(self, mex_home):
        """--json 模式不受影响，空库仍返回 []。"""
        _init()
        data = json.loads(_invoke("list", "--json").stdout)
        assert data == []

    def test_table_output(self, mex_home):
        _init()
        _add(topic="work", sub_topic="company", content="华为")
        _add(topic="finance", content="今天基金跌了3%")
        result = _invoke("list")
        assert result.exit_code == 0
        assert "work" in result.stdout and "company" in result.stdout and "华为" in result.stdout
        assert "今天基金跌了3%" in result.stdout

    def test_topic_filter(self, mex_home):
        _init()
        _add(topic="work", sub_topic="company", content="华为")
        _add(topic="finance", content="今天基金跌了3%")
        result = _invoke("list", "--topic", "work")
        assert "华为" in result.stdout and "今天基金跌了3%" not in result.stdout

    def test_sub_topic_filter(self, mex_home):
        """--sub-topic 配合 --topic 精确过滤字段。"""
        _init()
        _add(topic="work", sub_topic="company", content="华为")
        _add(topic="work", sub_topic="tech_stack", content="Python")
        result = _invoke("list", "--topic", "work", "--sub-topic", "tech_stack")
        assert result.exit_code == 0
        assert "Python" in result.stdout and "华为" not in result.stdout

    def test_sub_topic_without_topic_exit_1(self, mex_home):
        """--sub-topic 未配合 --topic 时应报用户错误。"""
        _init()
        result = _invoke("list", "--sub-topic", "company")
        assert result.exit_code == 1
        assert "--sub-topic requires --topic" in result.stderr

    def test_sub_topic_no_match_shows_hint(self, mex_home):
        """按 topic+sub_topic 无匹配时提示具体条件。"""
        _init()
        _add(topic="work", sub_topic="company", content="华为")
        result = _invoke("list", "--topic", "work", "--sub-topic", "salary")
        assert result.exit_code == 0
        assert "(topic work, sub-topic salary)" in result.stdout

    def test_topic_filter_no_match(self, mex_home):
        _seed(topic="work", sub_topic="company", content="华为")
        assert json.loads(_invoke("list", "--topic", "nope", "--json").stdout) == []

    def test_json_output(self, mex_home):
        _seed(topic="work", sub_topic="company", content="华为")
        data = json.loads(_invoke("list", "--json").stdout)
        assert len(data) == 1 and data[0]["content"] == "华为"
        assert "layer" not in data[0]

    def test_include_forgotten(self, mex_home):
        data = _seed(topic="work", sub_topic="company", content="华为")
        assert _invoke("forget", data["id"]).exit_code == 0
        assert "华为" not in _invoke("list").stdout
        result = _invoke("list", "--include-forgotten")
        assert "华为" in result.stdout and "[forgotten]" in result.stdout

    def test_structured_json_rendered_readable_in_list(self, mex_home):
        """结构化字段的 JSON content 在 list 中渲染为可读文本（与 profile 一致）。"""
        _init()
        _add(
            topic="work", sub_topic="experience",
            content='{"start": "2021", "end": "2022", "company": "腾讯", "position": "实习生"}',
        )
        result = _invoke("list")
        assert result.exit_code == 0
        assert '"start"' not in result.stdout  # 不展示原始 JSON 键
        assert "公司名: 腾讯" in result.stdout
        assert "职位: 实习生" in result.stdout
        # --json 保持原始 JSON 字符串
        data = json.loads(_invoke("list", "--json").stdout)
        assert '"start"' in data[0]["content"]

    # ---- 分页：默认 10 条 / --limit / --offset / --all ----

    def _seed_many(self, mex_home: str, n: int) -> None:
        """插入 n 条画像外记录，固定 created_at 递增（记录1 最旧 → 记录n 最新）。"""
        _init()
        for i in range(1, n + 1):
            _add(topic="finance", content=f"记录{i}")
        conn = connect(Path(mex_home) / "mex.db")
        try:
            for i in range(1, n + 1):
                conn.execute(
                    "UPDATE memories SET created_at = ? WHERE content = ?",
                    (f"2026-01-{i:02d}T00:00:00Z", f"记录{i}"),
                )
            conn.commit()
        finally:
            conn.close()

    def test_default_limit_10(self, mex_home):
        """默认只打印最近 10 条，并提示剩余条数与翻页命令。"""
        self._seed_many(mex_home, 12)
        result = _invoke("list")
        assert result.exit_code == 0
        assert "记录12" in result.stdout and "记录3" in result.stdout  # 最新的 10 条
        # 最旧的 2 条被截断（用行匹配避免 "记录12" 含子串 "记录1" 误判）
        assert not re.search(r"^\s*记录[12]$", result.stdout, flags=re.M)
        assert "Showing 1-10 of 12" in result.stdout
        assert "--offset 10" in result.stdout

    def test_limit_option(self, mex_home):
        self._seed_many(mex_home, 5)
        result = _invoke("list", "--limit", "2")
        assert "记录5" in result.stdout and "记录4" in result.stdout
        assert "记录3" not in result.stdout
        assert "Showing 1-2 of 5" in result.stdout

    def test_offset_pagination(self, mex_home):
        """第 2 页：--offset 10 显示 11-12 条，序号从 11 连续编号。"""
        self._seed_many(mex_home, 12)
        result = _invoke("list", "--offset", "10")
        assert result.exit_code == 0
        assert re.search(r"^\s*记录2$", result.stdout, flags=re.M) is not None
        assert re.search(r"^\s*记录1$", result.stdout, flags=re.M) is not None
        assert re.search(r"^\s*记录3$", result.stdout, flags=re.M) is None  # 第 1 页的条目不在本页
        assert "Showing" not in result.stdout  # 已到最后一页，无翻页提示
        assert "11. " in result.stdout and "12. " in result.stdout

    def test_offset_with_limit(self, mex_home):
        """--limit 与 --offset 组合翻页：第 2 页 2 条，编号从 3 开始。"""
        self._seed_many(mex_home, 6)
        result = _invoke("list", "--limit", "2", "--offset", "2")
        assert "记录4" in result.stdout and "记录3" in result.stdout
        assert "记录5" not in result.stdout
        assert "Showing 3-4 of 6" in result.stdout

    def test_all_flag(self, mex_home):
        """--all 打印全部且不再提示翻页。"""
        self._seed_many(mex_home, 12)
        result = _invoke("list", "--all")
        assert result.exit_code == 0
        assert "记录1" in result.stdout and "记录12" in result.stdout
        assert "Showing" not in result.stdout

    def test_limit_zero_means_all(self, mex_home):
        """--limit 0 等价 --all。"""
        self._seed_many(mex_home, 12)
        result = _invoke("list", "--limit", "0")
        assert "记录1" in result.stdout and "记录12" in result.stdout
        assert "Showing" not in result.stdout

    def test_all_with_offset(self, mex_home):
        """--all 可与 --offset 组合（跳过最新的 N 条后打印剩余全部）。"""
        self._seed_many(mex_home, 5)
        result = _invoke("list", "--all", "--offset", "2")
        assert re.search(r"^\s*记录5$", result.stdout, flags=re.M) is None  # 最新的 2 条被跳过
        assert re.search(r"^\s*记录4$", result.stdout, flags=re.M) is None
        assert "记录3" in result.stdout and "记录1" in result.stdout
        assert "Showing" not in result.stdout

    def test_page_hint_absent_when_all_shown(self, mex_home):
        """不足一页时不打印翻页提示。"""
        self._seed_many(mex_home, 3)
        result = _invoke("list")
        assert "Showing" not in result.stdout
        assert "记录1" in result.stdout and "记录3" in result.stdout

    def test_json_defaults_to_all(self, mex_home):
        """--json 默认输出全部条目（程序消费不截断），按 created_at 倒序。"""
        self._seed_many(mex_home, 12)
        data = json.loads(_invoke("list", "--json").stdout)
        assert len(data) == 12
        assert data[0]["content"] == "记录12"

    def test_json_with_limit(self, mex_home):
        """--json 显式 --limit/--offset 仍生效。"""
        self._seed_many(mex_home, 5)
        data = json.loads(_invoke("list", "--json", "--limit", "2").stdout)
        assert [d["content"] for d in data] == ["记录5", "记录4"]

    def test_negative_limit_error(self, mex_home):
        _init()
        result = _invoke("list", "--limit", "-1")
        assert result.exit_code == 1
        assert "--limit must be >= 0" in result.stderr

    def test_negative_offset_error(self, mex_home):
        _init()
        result = _invoke("list", "--offset", "-1")
        assert result.exit_code == 1
        assert "--offset must be >= 0" in result.stderr

    def test_all_with_limit_conflict(self, mex_home):
        _init()
        result = _invoke("list", "--all", "--limit", "5")
        assert result.exit_code == 1
        assert "mutually exclusive" in result.stderr


class TestNotInitialized:
    @pytest.mark.parametrize(
        "args",
        [
            ["add", "--topic", "work", "--sub-topic", "company", "--content", "x"],
            ["update", "some-id", "--content", "x"],
            ["forget", "some-id"],
            ["restore", "some-id"],
            ["list"],
        ],
    )
    def test_command_requires_init(self, mex_home, args):
        result = _invoke(*args)
        assert result.exit_code == 1
        assert "init" in result.stderr and mex_home in result.stderr


class TestValuesFields:
    """values 领域新字段的 add 契约与 profile 渲染（心理与价值观图谱）。"""

    def test_init_schema_has_eight_values_subtopics(self, mex_home):
        """init 生成的 schema.yaml 的 values 领域含 8 个 sub_topics。"""
        _init()
        schema = load_schema(str(Path(mex_home) / "schema.yaml"))
        assert schema.sub_topic_names("values") == [
            "values",
            "principles",
            "life_goal",
            "personality",
            "risk_preference",
            "decision_style",
            "emotion_triggers",
            "bottom_line",
        ]

    def test_add_personality_ok(self, mex_home):
        """add values.personality 成功入库。"""
        data = _seed(topic="values", sub_topic="personality", content="INTJ")
        assert data["content"] == "INTJ"
        assert _query(mex_home, "SELECT topic, sub_topic, content FROM memories") == [
            {"topic": "values", "sub_topic": "personality", "content": "INTJ"},
        ]

    def test_profile_shows_new_fields(self, mex_home):
        """mex profile 输出"## 三观与原则"小节及新字段行。"""
        _seed(topic="values", sub_topic="personality", content="INTJ")
        _add(topic="values", sub_topic="risk_preference", content="稳健偏保守")
        result = _invoke("profile")
        assert result.exit_code == 0
        assert "## 三观与原则" in result.stdout
        assert "- 性格倾向：INTJ" in result.stdout
        assert "- 风险偏好：稳健偏保守" in result.stdout

    def test_add_unknown_subtopic_rejected(self, mex_home):
        """非法字段被拒，并列出 values 下合法字段。"""
        _init()
        result = _invoke("add", "--topic", "values", "--sub-topic", "mbti", "--content", "INTJ")
        assert result.exit_code == 1
        assert "Valid sub-topics under" in result.stderr
        for name in ("personality", "risk_preference", "decision_style", "emotion_triggers", "bottom_line"):
            assert name in result.stderr

    def test_emotion_triggers_ok_and_profile_readable(self, mex_home):
        """add 结构化字段成功，profile 渲染为可读文本（非原始 JSON）。"""
        _init()
        _add(
            topic="values", sub_topic="emotion_triggers",
            content='{"trigger": "前领导", "reaction": "暴躁"}',
        )
        result = _invoke("profile")
        assert result.exit_code == 0
        assert '"trigger"' not in result.stdout
        assert '"reaction"' not in result.stdout
        assert "- 情绪触发点：触发话题或情境：前领导；情绪反应：暴躁" in result.stdout

    def test_emotion_triggers_invalid_rejected(self, mex_home):
        """缺必填键被拒；含未知键被拒。"""
        _init()
        result = _invoke(
            "add", "--topic", "values", "--sub-topic", "emotion_triggers",
            "--content", '{"trigger": "前领导"}',
        )
        assert result.exit_code == 1 and "reaction" in result.stderr
        result = _invoke(
            "add", "--topic", "values", "--sub-topic", "emotion_triggers",
            "--content", '{"trigger": "加班", "reaction": "焦虑", "mood": "差"}',
        )
        assert result.exit_code == 1 and "Unknown keys" in result.stderr


class TestInteractionFields:
    """interaction 领域的 add 契约、profile 渲染与 search 检索（交互偏好）。"""

    def test_init_schema_has_interaction(self, mex_home):
        """init 生成的 schema.yaml 含 interaction 领域及 4 字段。"""
        _init()
        schema = load_schema(str(Path(mex_home) / "schema.yaml"))
        assert schema.has_topic("interaction")
        assert schema.sub_topic_names("interaction") == [
            "communication_style",
            "info_density",
            "feedback_style",
            "behavior_habits",
        ]

    def test_add_communication_style_and_profile(self, mex_home):
        """add 成功，profile 出现"## 交互偏好"小节及该行。"""
        _seed(topic="interaction", sub_topic="communication_style", content="结论先行，轻松幽默")
        result = _invoke("profile")
        assert result.exit_code == 0
        assert "## 交互偏好" in result.stdout
        assert "- 沟通风格偏好：结论先行，轻松幽默" in result.stdout

    def test_behavior_habits_append_many(self, mex_home):
        """behavior_habits 可追加多条且互相独立。"""
        _init()
        first = _add(topic="interaction", sub_topic="behavior_habits", content="严重拖延症")
        second = _add(topic="interaction", sub_topic="behavior_habits", content="夜猫子")
        assert first["id"] != second["id"]
        rows = _query(mex_home, "SELECT content FROM memories WHERE topic = 'interaction' ORDER BY rowid")
        assert [r["content"] for r in rows] == ["严重拖延症", "夜猫子"]

    def test_search_topic_interaction(self, mex_home):
        """mex search --topic interaction 筛出该领域记录（含画像槽位与画像外记录）。"""
        _init()
        _add(topic="interaction", sub_topic="communication_style", content="结论先行")
        _add(topic="interaction", content="今天别用敬语，直接用你")
        result = _invoke("search", "--topic", "interaction")
        assert result.exit_code == 0
        assert "结论先行" in result.stdout
        assert "今天别用敬语" in result.stdout

    def test_add_unknown_subtopic_rejected(self, mex_home):
        """非法字段被拒，并列出 interaction 下合法字段。"""
        _init()
        result = _invoke("add", "--topic", "interaction", "--sub-topic", "tone", "--content", "x")
        assert result.exit_code == 1
        assert "Valid sub-topics under" in result.stderr
        for name in ("communication_style", "info_density", "feedback_style", "behavior_habits"):
            assert name in result.stderr


class TestGoalsFields:
    """goals 领域短期目标（结构化可多条）的 add 契约与失败路径。"""

    def test_add_short_term_goal_and_profile(self, mex_home):
        """合法 JSON content 入库，profile 渲染为可读文本（非原始 JSON）。"""
        _seed(
            topic="goals", sub_topic="short_term_goal",
            content='{"goal": "通过 PMP 考试", "deadline": "2026-09", "domain": "职业"}',
        )
        result = _invoke("profile")
        assert result.exit_code == 0
        assert "## 目标与计划" in result.stdout
        assert '"goal"' not in result.stdout
        assert '"deadline"' not in result.stdout
        assert "- 短期目标：目标内容：通过 PMP 考试；截止时间：2026-09；所属领域：职业" in result.stdout

    def test_add_multiple_goals_append(self, mex_home):
        """可多条：连续 add 多条成功且互相独立。"""
        _init()
        first = _add(topic="goals", sub_topic="short_term_goal", content='{"goal": "A", "deadline": "2026-09"}')
        second = _add(topic="goals", sub_topic="short_term_goal", content='{"goal": "B", "deadline": "2026-10"}')
        assert first["id"] != second["id"]

    def test_missing_required_key_rejected(self, mex_home):
        """缺必填键（无 deadline）被拒。"""
        _init()
        result = _invoke(
            "add", "--topic", "goals", "--sub-topic", "short_term_goal",
            "--content", '{"goal": "减重"}',
        )
        assert result.exit_code == 1
        assert "deadline" in result.stderr

    def test_bad_time_format_rejected(self, mex_home):
        """deadline 写"下个月"（非时间格式）被拒。"""
        _init()
        result = _invoke(
            "add", "--topic", "goals", "--sub-topic", "short_term_goal",
            "--content", '{"goal": "a", "deadline": "下个月"}',
        )
        assert result.exit_code == 1
        assert "invalid time format" in result.stderr

    def test_unknown_key_rejected(self, mex_home):
        """含未知键 progress 被拒。"""
        _init()
        result = _invoke(
            "add", "--topic", "goals", "--sub-topic", "short_term_goal",
            "--content", '{"goal": "a", "deadline": "2026-09", "progress": "50%"}',
        )
        assert result.exit_code == 1
        assert "Unknown keys" in result.stderr


class TestBasicEduFields:
    """基础/教育/家庭/工作领域的补充字段 add 契约（含 job_search 结构化升级）。"""

    def test_add_basic_info_new_fields_ok(self, mex_home):
        """文化背景/语言习惯/人生阶段均可入库。"""
        _seed(topic="basic_info", sub_topic="life_stage", content="大三学生")
        _add(topic="basic_info", sub_topic="cultural_background", content="汉族，北方文化背景")
        _add(topic="basic_info", sub_topic="language_habit", content="日常中文，技术讨论中英混杂")
        rows = _query(mex_home, "SELECT sub_topic FROM memories WHERE topic = 'basic_info' ORDER BY rowid")
        assert [r["sub_topic"] for r in rows] == ["life_stage", "cultural_background", "language_habit"]

    def test_add_family_relationships_ok(self, mex_home):
        """核心人际关系及质量（结构化）入库。"""
        _seed(topic="family", sub_topic="relationships", content='{"relation": "母亲", "quality": "关系紧张"}')
        rows = _query(mex_home, "SELECT content FROM memories")
        assert rows[0]["content"] == '{"relation": "母亲", "quality": "关系紧张"}'

    def test_family_relationships_invalid_rejected(self, mex_home):
        """缺必填键被拒；含未知键被拒。"""
        _init()
        result = _invoke(
            "add", "--topic", "family", "--sub-topic", "relationships", "--content", '{"relation": "母亲"}',
        )
        assert result.exit_code == 1 and "quality" in result.stderr
        result = _invoke(
            "add", "--topic", "family", "--sub-topic", "relationships",
            "--content", '{"relation": "a", "quality": "b", "closeness": "高"}',
        )
        assert result.exit_code == 1 and "Unknown keys" in result.stderr

    def test_soft_skill_append_and_edu_focus(self, mex_home):
        """软技能可多条追加；教育聚焦方向入库。"""
        _init()
        _add(topic="work", sub_topic="soft_skill", content="跨部门沟通")
        second = _add(topic="work", sub_topic="soft_skill", content="公开演讲")
        assert second["id"] is not None
        _add(topic="edu", sub_topic="focus", content="自然语言处理")

    def test_experience_with_and_without_leave_reason(self, mex_home):
        """experience 带 leave_reason 合法；无该键的旧格式仍合法（非破坏）。"""
        _init()
        _add(
            topic="work", sub_topic="experience",
            content='{"start": "2019", "end": "2023", "company": "华为", "position": "工程师", '
            '"leave_reason": "追求更大平台"}',
        )
        _add(
            topic="work", sub_topic="experience",
            content='{"start": "2023", "end": "至今", "company": "腾讯", "position": "工程师"}',
        )

    def test_job_search_plain_text_rejected_json_ok(self, mex_home):
        """job_search 升级后：纯文本 content 被拒，JSON 通过。"""
        _init()
        result = _invoke("add", "--topic", "work", "--sub-topic", "job_search", "--content", "想去大厂做 AI")
        assert result.exit_code == 1 and "JSON" in result.stderr
        _add(topic="work", sub_topic="job_search", content='{"status": "主动求职", "expected_position": "算法工程师"}')

    def test_add_unknown_subtopic_rejected(self, mex_home):
        """非法字段被拒，并列出 basic_info 下合法字段。"""
        _init()
        result = _invoke("add", "--topic", "basic_info", "--sub-topic", "mbti", "--content", "INTJ")
        assert result.exit_code == 1
        assert "Valid sub-topics under" in result.stderr
        for name in ("cultural_background", "language_habit", "life_stage"):
            assert name in result.stderr


class TestExpiration:
    """TTL 过期：add/update 的 --expires 参数、过期对 list/search/profile 不可见。"""

    def _expired_row(self, mex_home, mid: str) -> dict:
        return _query(mex_home, "SELECT expires_at, forgotten_at FROM memories WHERE id = ?", (mid,))[0]

    def test_add_expires_sets_expires_at(self, mex_home):
        """--expires 本地日期 → expires_at 存 UTC ISO 边界（该日结束，本地转 UTC）。"""
        data = _seed(topic="finance", content="尴尬记录", expires="2026-08-20")
        row = _query(mex_home, "SELECT expires_at FROM memories WHERE id = ?", (data["id"],))[0]
        assert row["expires_at"] is not None and row["expires_at"] >= "2026-08-20"

    def test_add_expires_datetime_ok(self, mex_home):
        """--expires 接受 YYYY-MM-DD HH:MM:SS。"""
        _init()
        _add(topic="finance", content="x", expires="2026-08-20 10:30:00")
        row = _query(mex_home, "SELECT expires_at FROM memories")[0]
        assert row["expires_at"] is not None

    def test_add_expires_relative_rejected(self, mex_home):
        """相对时长"24h"被拒（只接绝对日期）。"""
        _init()
        result = _invoke("add", "--topic", "finance", "--content", "x", "--expires", "24h")
        assert result.exit_code == 1
        assert "expires" in result.stderr

    def test_update_expires_and_clear(self, mex_home):
        """--expires 改过期时间；--clear-expires 清除（expires_at=NULL）。"""
        data = _seed(topic="finance", content="x", expires="2026-08-20")
        assert self._expired_row(mex_home, data["id"])["expires_at"] is not None
        result = _invoke("update", data["id"], "--expires", "2026-09-01")
        assert result.exit_code == 0
        assert _query(mex_home, "SELECT expires_at FROM memories WHERE id = ?", (data["id"],))[0][
            "expires_at"
        ] >= "2026-09-01"
        result = _invoke("update", data["id"], "--clear-expires")
        assert result.exit_code == 0
        assert self._expired_row(mex_home, data["id"])["expires_at"] is None

    def test_expired_not_in_table_or_search(self, mex_home):
        """过期记录在 list/search 不可见（惰性过滤）。"""
        _init()
        _add(topic="finance", content="已过期记录", expires="2000-01-01")
        _add(topic="finance", content="未过期记录", expires="2099-01-01")
        result = _invoke("list")
        assert "已过期记录" not in result.stdout
        assert "未过期记录" in result.stdout
        result = _invoke("search", "--keyword", "过")
        assert "已过期记录" not in result.stdout
        assert "未过期记录" in result.stdout
