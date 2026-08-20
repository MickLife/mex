"""M4 CLI：search / profile / history / doctor 命令端到端测试（CliRunner）。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import mex.cli.query as query_mod  # noqa: F401 - 导入即注册命令（cli/__init__.py 尚未统一引入）
from mex.cli import app
from mex.config import _write_config_yaml
from mex.domain.memory import Confidence, Memory
from mex.store import history as history_store
from mex.store import memories
from mex.store.connection import connect, init_db, transaction

runner = CliRunner()

_SCHEMA_YAML = """\
topics:
  basic_info:
    description: 基础信息
    sub_topics:
      name: { description: 姓名 }
  work:
    description: 工作与职业
    sub_topics:
      company: { description: 公司 }
      tech_stack: { description: 技术栈, unique: false }
      job_search:
        description: 求职状态与偏好
        fields:
          status: 求职状态
          expected_position: 期望岗位
  career:
    description: 职业发展
    sub_topics:
      current_goal: { description: 当前职业目标 }
"""


@pytest.fixture
def mex_home(tmp_path, monkeypatch):
    """临时 MEX_HOME：建库 + schema.yaml + 环境变量（绝不触碰 ~/.mex）。"""
    home = tmp_path / "mex"
    home.mkdir()
    init_db(home / "mex.db")
    (home / "schema.yaml").write_text(_SCHEMA_YAML, encoding="utf-8")
    monkeypatch.setenv("MEX_HOME", str(home))
    return home


@pytest.fixture
def uninitialized_home(tmp_path, monkeypatch):
    """未初始化数据目录（mex.db 缺失）。"""
    home = tmp_path / "mex"
    home.mkdir()
    monkeypatch.setenv("MEX_HOME", str(home))
    return home


def make_memory(**kwargs):
    """构造测试记忆。"""
    defaults = {
        "id": "m-1",
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "is_ai_inferred": False,
        "confidence": Confidence.CONFIRMED,
        "evidence": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


def seed(mex_home, *ms):
    """直连临时库批量插入（无需 M3 CLI）。"""
    conn = connect(mex_home / "mex.db")
    try:
        with transaction(conn):
            for m in ms:
                memories.insert(conn, m)
    finally:
        conn.close()


def seed_history(mex_home, memory_id: str):
    """写入该记忆的 add/update 两条审计记录。"""
    conn = connect(mex_home / "mex.db")
    try:
        with transaction(conn):
            history_store.record(
                conn, memory_id, "add", old_content=None, new_content="华为", actor="user",
            )
            history_store.record(
                conn, memory_id, "update", old_content="华为", new_content="中兴", actor="user",
            )
    finally:
        conn.close()


def _write_llm_config(mex_home, base_url: str, api_key: str, model: str) -> None:
    """向 config.yaml 写入 LLM 配置（doctor 连通性测试用）。"""
    _write_config_yaml(
        Path(mex_home) / "config.yaml",
        {"llm": {"base_url": base_url, "api_key": api_key, "api_key_env": "", "model": model}},
    )


class TestSearchCmd:
    def test_table_output(self, mex_home):
        seed(mex_home, make_memory())
        result = runner.invoke(app, ["search"])
        assert result.exit_code == 0
        assert "work" in result.stdout and "company" in result.stdout
        assert "华为" in result.stdout

    def test_filters(self, mex_home):
        seed(
            mex_home,
            make_memory(id="s1", topic="work", sub_topic="company", content="华为"),
            make_memory(
                id="s2",
                topic="finance",
                sub_topic=None,
                content="基金跌了",
            ),
        )
        result = runner.invoke(app, ["search", "--topic", "finance", "--keyword", "基金", "--limit", "1"])
        assert result.exit_code == 0
        assert "基金跌了" in result.stdout
        assert "华为" not in result.stdout

    def test_sub_topic_filter(self, mex_home):
        """--sub-topic 配合 --topic 精确过滤字段。"""
        seed(
            mex_home,
            make_memory(id="a1", topic="work", sub_topic="company", content="华为"),
            make_memory(id="a2", topic="work", sub_topic="tech_stack", content="Python"),
        )
        result = runner.invoke(app, ["search", "--topic", "work", "--sub-topic", "tech_stack"])
        assert result.exit_code == 0
        assert "Python" in result.stdout
        assert "华为" not in result.stdout

    def test_sub_topic_without_topic_exit_1(self, mex_home):
        """--sub-topic 未配合 --topic 时应报用户错误。"""
        seed(mex_home, make_memory(id="b1", topic="work", sub_topic="company", content="华为"))
        result = runner.invoke(app, ["search", "--sub-topic", "company"])
        assert result.exit_code == 1
        assert "--sub-topic requires --topic" in result.stderr

    def test_topic_filter_new_interaction_domain(self, mex_home):
        """新建领域（interaction）的领域筛选自动可用：search 按 topic 过滤，不依赖 schema。"""
        seed(
            mex_home,
            make_memory(id="i1", topic="interaction", sub_topic="communication_style", content="结论先行"),
            make_memory(id="i2", topic="work", sub_topic="company", content="华为"),
        )
        result = runner.invoke(app, ["search", "--topic", "interaction"])
        assert result.exit_code == 0
        assert "结论先行" in result.stdout
        assert "华为" not in result.stdout

    def test_keyword_hits_outside_record(self, mex_home):
        """画像外记录（sub_topic 为空）通过关键词检索命中。"""
        seed(
            mex_home,
            make_memory(id="s2", topic="finance", sub_topic=None, content="基金跌了，有点焦虑"),
        )
        result = runner.invoke(app, ["search", "--keyword", "基金"])
        assert result.exit_code == 0
        assert "基金跌了" in result.stdout

    def test_json_output(self, mex_home):
        seed(mex_home, make_memory())
        result = runner.invoke(app, ["search", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data[0]["topic"] == "work"
        assert data[0]["confidence"] == "confirmed"
        assert "layer" not in data[0]

    def test_no_match(self, mex_home):  # noqa: ARG002
        result = runner.invoke(app, ["search", "--keyword", "不存在的词"])
        assert result.exit_code == 0
        assert "(no matching memories)" in result.stdout

    def test_invalid_date_exit_1(self, mex_home):  # noqa: ARG002
        result = runner.invoke(app, ["search", "--since", "2026-13-01"])
        assert result.exit_code == 1
        assert "YYYY-MM-DD" in result.stderr

    def test_not_initialized_exit_1(self, uninitialized_home):  # noqa: ARG002
        result = runner.invoke(app, ["search"])
        assert result.exit_code == 1
        assert "init" in result.stderr


class TestGetCmd:
    def test_get_by_id_json(self, mex_home):
        """按 id 取出单条记忆，默认输出完整 JSON。"""
        seed(
            mex_home,
            make_memory(
                id="g-1",
                topic="work",
                sub_topic="company",
                content="华为",
                confidence=Confidence.CONFIRMED,
            ),
        )
        result = runner.invoke(app, ["get", "g-1"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["id"] == "g-1"
        assert data["topic"] == "work"
        assert data["sub_topic"] == "company"
        assert data["content"] == "华为"
        assert data["confidence"] == "confirmed"
        assert data["forgotten_at"] is None

    def test_get_shows_forgotten_status(self, mex_home):
        """软删条目也能按 id 取出（含 forgotten 字段）。"""
        seed(
            mex_home,
            make_memory(
                id="g-2",
                topic="work",
                sub_topic="company",
                content="华为",
                forgotten_at="2026-01-16T00:00:00Z",
                forgotten_reason="测试删除",
            ),
        )
        result = runner.invoke(app, ["get", "g-2"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["forgotten_at"] == "2026-01-16T00:00:00Z"
        assert data["forgotten_reason"] == "测试删除"

    def test_get_missing_id_exit_1(self, mex_home):  # noqa: ARG002
        """不存在的 id 报用户错误（退出码 1）。"""
        result = runner.invoke(app, ["get", "no-such-id"])
        assert result.exit_code == 1
        assert "does not exist" in result.stderr


class TestProfileCmd:
    def test_snapshot_output(self, mex_home):
        seed(
            mex_home,
            make_memory(id="p1", topic="basic_info", sub_topic="name", content="王某"),
            make_memory(
                id="p2",
                topic="work",
                sub_topic="tech_stack",
                content="Python",
            ),
        )
        result = runner.invoke(app, ["profile"])
        assert result.exit_code == 0
        assert "# 用户画像" in result.stdout
        assert "- 姓名：王某" in result.stdout
        assert "- 技术栈：Python" in result.stdout

    def test_outside_record_not_in_snapshot(self, mex_home):
        """画像外记录（sub_topic 为空）不进入 profile 快照。"""
        seed(
            mex_home,
            make_memory(id="p1", topic="basic_info", sub_topic="name", content="王某"),
            make_memory(id="p2", topic="finance", sub_topic=None, content="今天基金跌了3%"),
        )
        result = runner.invoke(app, ["profile"])
        assert result.exit_code == 0
        assert "王某" in result.stdout
        assert "基金" not in result.stdout

    def test_json_output(self, mex_home):
        seed(mex_home, make_memory())
        result = runner.invoke(app, ["profile", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "# 用户画像" in data["profile"]

    def test_topics_filter(self, mex_home):
        seed(
            mex_home,
            make_memory(id="p1", topic="basic_info", sub_topic="name", content="王某"),
            make_memory(id="p2", topic="work", sub_topic="company", content="华为"),
        )
        result = runner.invoke(app, ["profile", "--topics", "work"])
        assert result.exit_code == 0
        assert "华为" in result.stdout
        assert "王某" not in result.stdout

    def test_unknown_topic_exit_1(self, mex_home):  # noqa: ARG002
        result = runner.invoke(app, ["profile", "--topics", "nope"])
        assert result.exit_code == 1
        assert "Unknown topics" in result.stderr

    def test_max_tokens_truncates(self, mex_home):
        seed(
            mex_home,
            make_memory(
                id="p1",
                topic="work",
                sub_topic="company",
                content="这是一个用来撑大token预算的句子，" * 20,
                confidence=Confidence.UNCERTAIN,
            ),
            make_memory(
                id="p2",
                topic="basic_info",
                sub_topic="name",
                content="王某",
                confidence=Confidence.CONFIRMED,
            ),
        )
        result = runner.invoke(app, ["profile", "--max-tokens", "100"])
        assert result.exit_code == 0
        assert "王某" in result.stdout
        assert "这是一个用来撑大" not in result.stdout

    def test_recent_options_passthrough(self, mex_home):
        """--recent-days/--recent-limit 参数透传服务层（近期动态小节）。"""
        recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        seed(
            mex_home,
            make_memory(
                id="e1", topic="finance", sub_topic=None, content="正在准备明天终面",
                created_at=recent, updated_at=recent,
            ),
        )
        result = runner.invoke(app, ["profile", "--recent-days", "30", "--recent-limit", "10"])
        assert result.exit_code == 0
        assert "## 近期动态" in result.stdout
        assert "正在准备明天终面" in result.stdout

    def test_recent_limit_zero_disables(self, mex_home):
        """--recent-limit 0 关闭近期动态小节。"""
        recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        seed(
            mex_home,
            make_memory(
                id="e2", topic="finance", sub_topic=None, content="一条动态",
                created_at=recent, updated_at=recent,
            ),
        )
        result = runner.invoke(app, ["profile", "--recent-limit", "0"])
        assert result.exit_code == 0
        assert "## 近期动态" not in result.stdout
        assert "一条动态" not in result.stdout

    def test_not_initialized_exit_1(self, uninitialized_home):  # noqa: ARG002
        result = runner.invoke(app, ["profile"])
        assert result.exit_code == 1
        assert "init" in result.stderr


class TestHistoryCmd:
    def test_success(self, mex_home):
        seed(mex_home, make_memory(id="h1"))
        seed_history(mex_home, "h1")
        result = runner.invoke(app, ["history", "h1"])
        assert result.exit_code == 0
        assert "华为 → 中兴" in result.stdout
        assert "update" in result.stdout

    def test_json_output(self, mex_home):
        seed(mex_home, make_memory(id="h1"))
        seed_history(mex_home, "h1")
        result = runner.invoke(app, ["history", "h1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert [r["event"] for r in data] == ["add", "update"]

    def test_not_found_exit_1(self, mex_home):  # noqa: ARG002
        result = runner.invoke(app, ["history", "no-such-id"])
        assert result.exit_code == 1
        assert "does not exist" in result.stderr


class TestDoctorCmd:
    def test_consistent(self, mex_home):
        seed(mex_home, make_memory(topic="work", sub_topic="company", content="华为"))
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "data is consistent with the schema" in result.stdout

    def test_dangling_slots(self, mex_home):
        seed(
            mex_home,
            make_memory(topic="work", sub_topic="company", content="华为"),
            make_memory(
                id="d1",
                topic="work",
                sub_topic="removed_field",
                content="旧字段数据",
            ),
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "removed_field" in result.stdout
        assert "mex update" in result.stdout
        assert "mex forget" in result.stdout

    def test_structured_content_conflict_reported(self, mex_home):
        """字段升级为结构化后遗留的纯文本记录被 doctor 报告为不一致。"""
        seed(
            mex_home,
            make_memory(id="j1", topic="work", sub_topic="job_search", content="想去大厂做 AI"),
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "job_search" in result.stdout
        assert "mex update" in result.stdout
        assert "mex forget" in result.stdout

    def test_structured_content_compliant_not_reported(self, mex_home):
        """结构化 content 合规的记录不构成不一致。"""
        seed(
            mex_home,
            make_memory(id="j2", topic="work", sub_topic="job_search", content='{"status": "主动求职"}'),
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "data is consistent with the schema" in result.stdout

    def test_structured_conflict_json_output(self, mex_home):
        seed(mex_home, make_memory(id="j3", topic="work", sub_topic="job_search", content="旧文本"))
        result = runner.invoke(app, ["doctor", "--json"])
        data = json.loads(result.stdout)
        assert data["schema_ok"] is False
        assert [d["id"] for d in data["dangling"]] == ["j3"]

    def test_outside_record_not_dangling(self, mex_home):
        """画像外记录（sub_topic 为空）不构成 schema 漂移。"""
        seed(
            mex_home,
            make_memory(id="o1", topic="finance", sub_topic=None, content="随手记"),
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "data is consistent with the schema" in result.stdout

    def test_json_output(self, mex_home):
        seed(
            mex_home,
            make_memory(id="d1", topic="work", sub_topic="removed_field", content="旧字段数据"),
        )
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["schema_ok"] is False
        assert data["dangling"][0]["id"] == "d1"

    def test_llm_unconfigured(self, mex_home):
        """未配置 LLM 时报告未配置（不触发网络调用）。"""
        seed(mex_home, make_memory(topic="work", sub_topic="company", content="华为"))
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "[llm] LLM not configured" in result.stdout

    def test_llm_reachable(self, mex_home, monkeypatch):
        """LLM 配置完整且网络可达时报告模型数。"""
        seed(mex_home, make_memory(topic="work", sub_topic="company", content="华为"))
        _write_llm_config(mex_home, "https://api.x.com/v1", "sk-1", "m1")
        monkeypatch.setattr(query_mod, "fetch_models", lambda *a, **kw: ["m1", "m2"])  # noqa: ARG001
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "[llm] Network reachable" in result.stdout
        assert "(2 models)" in result.stdout

    def test_llm_unreachable(self, mex_home, monkeypatch):
        """LLM 网络不可达时报告原因。"""
        seed(mex_home, make_memory(topic="work", sub_topic="company", content="华为"))
        _write_llm_config(mex_home, "https://api.x.com/v1", "sk-1", "m1")
        monkeypatch.setattr(
            query_mod, "fetch_models", lambda *a, **kw: (_ for _ in ()).throw(query_mod.LLMError("other", "连接超时")),
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "[llm] Network unreachable" in result.stdout

    def test_not_initialized_exit_1(self, uninitialized_home):  # noqa: ARG002
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "init" in result.stderr
