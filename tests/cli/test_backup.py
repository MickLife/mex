"""M7 CLI 测试（mex export / import / stats 命令）。

全部通过 CliRunner 调用 app；每个测试方法都必须接收 mex_home fixture
（MEX_HOME 指向 tmp_path，硬性隔离要求，绝不触碰真实 ~/.mex）。
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from mex.cli import app
from mex.cli import backup as backup_cli  # noqa: F401 - import 触发命令注册
from mex.domain.memory import Confidence, Memory
from mex.llm import LLMResponse
from mex.store import memories
from mex.store.connection import connect, init_db, transaction

runner = CliRunner()

SCHEMA_YAML = """\
topics:
  basic_info:
    description: 基础信息
    sub_topics:
      location: { description: 现居地 }
  work:
    description: 工作与职业
    sub_topics:
      company: { description: 公司 }
      tech_stack: { description: 技术栈, multiple: true }
  finance:
    description: 财务
    sub_topics:
      risk_appetite: { description: 风险偏好 }
"""

CONFIG_WITH_LLM = (
    "llm:\n  base_url: 'https://api.example.com/v1'\n"
    "  api_key_env: 'DEEPSEEK_API_KEY'\n  model: 'deepseek-chat'\n"
)


@pytest.fixture
def mex_home(tmp_path, monkeypatch):
    """隔离数据目录：MEX_HOME 指向 tmp_path，写入 schema/config 并初始化。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    (tmp_path / "schema.yaml").write_text(SCHEMA_YAML, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(CONFIG_WITH_LLM, encoding="utf-8")
    init_db(tmp_path / "mex.db")
    return tmp_path


class FakeLLM:
    """CLI 测试替身：返回录制响应。"""

    model = "fake-model"

    def __init__(self, data: dict):
        self._data = data

    def chat_json(self, messages: list[dict]):
        return (
            self._data,
            LLMResponse(text=json.dumps(self._data, ensure_ascii=False), prompt_tokens=10, completion_tokens=5),
        )


def make_memory(**kwargs) -> Memory:
    """构造测试记忆：默认是显式陈述的画像条目。"""
    defaults = {
        "id": "m-1",
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "is_ai_inferred": False,
        "confidence": Confidence.EXPLICIT,
        "evidence": "用户提到",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


def insert_memory(home, m: Memory) -> None:
    """直接插入一条记忆。"""
    conn = connect(home / "mex.db")
    try:
        with transaction(conn):
            memories.insert(conn, m)
    finally:
        conn.close()


def write_export(home, memory_dicts: list[dict], *, name: str = "export.json") -> str:
    """在数据目录写一个导出 JSON 文件并返回路径。"""
    payload = {"version": 1, "exported_at": "2026-08-06T08:30:00Z", "memories": memory_dicts}
    p = home / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


class TestExport:
    def test_export_json_success(self, mex_home):
        insert_memory(mex_home, make_memory())
        result = runner.invoke(app, ["export", "--output", str(mex_home / "out.json")])
        assert result.exit_code == 0
        assert "Exported 1 memories" in result.stdout
        data = json.loads((mex_home / "out.json").read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["memories"][0]["id"] == "m-1"

    def test_export_markdown_success(self, mex_home):
        insert_memory(mex_home, make_memory())
        result = runner.invoke(app, ["export", "--format", "markdown", "--output", str(mex_home / "out.md")])
        assert result.exit_code == 0
        text = (mex_home / "out.md").read_text(encoding="utf-8")
        assert "## Profile" in text
        assert "- company: 华为" in text

    def test_export_missing_output_exit_1(self, mex_home):
        result = runner.invoke(app, ["export"])
        assert result.exit_code == 1
        assert "Provide an output path" in result.stderr

    def test_export_json_flag(self, mex_home):
        insert_memory(mex_home, make_memory())
        result = runner.invoke(app, ["export", "--json", "--output", str(mex_home / "out.json")])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["count"] == 1
        assert data["forgotten_count"] == 0

    def test_export_uninitialized_exit_1(self, mex_home, tmp_path, monkeypatch):
        monkeypatch.setenv("MEX_HOME", str(tmp_path / "nope"))
        result = runner.invoke(app, ["export", "--output", str(tmp_path / "out.json")])
        assert result.exit_code == 1
        assert "is not initialized" in result.stderr


class TestImportRestore:
    def test_roundtrip_restores_all(self, mex_home):
        insert_memory(mex_home, make_memory())
        insert_memory(mex_home, make_memory(
            id="m-2", topic="finance", sub_topic=None,
            content="基金下跌", is_ai_inferred=True, confidence=Confidence.SPECULATED,
        ))
        with connect(mex_home / "mex.db") as conn, transaction(conn):
            memories.forget(conn, "m-1", "已过时")
        out = mex_home / "out.json"
        assert runner.invoke(app, ["export", "--output", str(out)]).exit_code == 0

        conn = connect(mex_home / "mex.db")
        try:
            with transaction(conn):
                conn.execute("DELETE FROM memories")
        finally:
            conn.close()
        result = runner.invoke(app, ["import", str(out)])
        assert result.exit_code == 0
        assert "Imported 2, skipped 0, conflicts 0" in result.stdout
        conn = connect(mex_home / "mex.db")
        try:
            entries = memories.list_all(conn, include_forgotten=True)
            assert len(entries) == 2
            forgotten = [e for e in entries if e.is_forgotten()]
            assert len(forgotten) == 1
            assert forgotten[0].forgotten_reason == "已过时"
        finally:
            conn.close()

    def test_conflict_skip_reported_to_stderr_exit_0(self, mex_home):
        insert_memory(mex_home, make_memory(content="华为"))
        path = write_export(mex_home, [make_memory(content="腾讯").to_dict()], name="conflict.json")
        result = runner.invoke(app, ["import", path])
        assert result.exit_code == 0
        assert "skipped 1" in result.stdout
        assert "conflict" in result.stderr
        assert "same id different content" in result.stderr

    def test_overwrite_mode(self, mex_home):
        insert_memory(mex_home, make_memory(content="华为"))
        path = write_export(mex_home, [make_memory(content="腾讯").to_dict()], name="overwrite.json")
        result = runner.invoke(app, ["import", path, "--on-conflict", "overwrite"])
        assert result.exit_code == 0
        assert "Imported 1" in result.stdout
        conn = connect(mex_home / "mex.db")
        try:
            assert memories.get(conn, "m-1").content == "腾讯"
        finally:
            conn.close()

    def test_import_json_flag(self, mex_home):
        insert_memory(mex_home, make_memory(content="华为"))
        path = write_export(mex_home, [make_memory(content="腾讯").to_dict()], name="conflict.json")
        result = runner.invoke(app, ["import", path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["imported"] == 0
        assert data["skipped"] == 1
        assert data["conflicts"][0]["reason"] == "same id different content"

    def test_corrupt_json_exit_2(self, mex_home):
        bad = mex_home / "bad.json"
        bad.write_text("不是 JSON", encoding="utf-8")
        result = runner.invoke(app, ["import", str(bad)])
        assert result.exit_code == 2
        assert "JSON" in result.stderr

    def test_version_mismatch_exit_2(self, mex_home):
        payload = {"version": 99, "exported_at": "x", "memories": []}
        bad = mex_home / "v2.json"
        bad.write_text(json.dumps(payload), encoding="utf-8")
        result = runner.invoke(app, ["import", str(bad)])
        assert result.exit_code == 2
        assert "does not match the supported export version" in result.stderr


class TestImportExtract:
    def test_extract_mode_with_fake_llm(self, mex_home, monkeypatch):
        source = mex_home / "archive.md"
        source.write_text("用户：我在华为工作", encoding="utf-8")
        fake = FakeLLM({
            "memories": [
                {
                    "topic": "finance", "sub_topic": None,
                    "content": "今天完成了迁移", "is_ai_inferred": True,
                    "confidence": "inferred", "evidence": "用户提到",
                },
            ],
        })
        monkeypatch.setattr("mex.cli.backup._make_client", lambda _cfg, _key: fake)
        result = runner.invoke(app, ["import", str(source), "--mode", "extract"])
        assert result.exit_code == 0
        assert "Added 1" in result.stdout
        conn = connect(mex_home / "mex.db")
        try:
            events = [m for m in memories.list_all(conn) if m.sub_topic is None]
            assert len(events) == 1
            count = conn.execute("SELECT COUNT(*) FROM extraction_state").fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_extract_mode_llm_not_configured_exit_2(self, mex_home, tmp_path, monkeypatch):
        home = tmp_path / "nollm"
        home.mkdir()
        init_db(home / "mex.db")
        monkeypatch.setenv("MEX_HOME", str(home))
        source = home / "archive.md"
        source.write_text("对话文本", encoding="utf-8")
        result = runner.invoke(app, ["import", str(source), "--mode", "extract"])
        assert result.exit_code == 2
        assert "config.yaml" in result.stderr

    def test_extract_mode_missing_file_exit_2(self, mex_home, monkeypatch):
        fake = FakeLLM({"memories": []})
        monkeypatch.setattr("mex.cli.backup._make_client", lambda _cfg, _key: fake)
        result = runner.invoke(app, ["import", str(mex_home / "nope.md"), "--mode", "extract"])
        assert result.exit_code == 2
        assert "Failed to read file" in result.stderr


class TestStats:
    def test_stats_table(self, mex_home):
        insert_memory(mex_home, make_memory())
        insert_memory(mex_home, make_memory(
            id="m-2", topic="finance", sub_topic=None,
            content="基金下跌", is_ai_inferred=True, confidence=Confidence.SPECULATED,
        ))
        conn = connect(mex_home / "mex.db")
        try:
            conn.execute(
                "INSERT INTO llm_usage (id, purpose, model, prompt_tokens, completion_tokens, created_at) "
                "VALUES ('u1', 'extract', 'm', 100, 20, '2026-01-01T00:00:00Z')",
            )
            conn.execute(
                "INSERT INTO llm_usage (id, purpose, model, prompt_tokens, completion_tokens, created_at) "
                "VALUES ('u2', 'extract', 'm', 50, 10, '2026-01-02T00:00:00Z')",
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0
        assert "In-profile: 1" in result.stdout
        assert "Out-of-profile: 1" in result.stdout
        assert "Pending review: 1" in result.stdout
        assert "Database size:" in result.stdout
        assert "150 prompt / 30 completion tokens" in result.stdout
        assert "2 calls" in result.stdout

    def test_stats_json(self, mex_home):
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["profile"] == {"profile": 0, "outside": 0}
        assert data["pending_review"] == 0
        assert data["db_size_bytes"] > 0
        assert data["llm_usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def test_stats_uninitialized_exit_1(self, mex_home, tmp_path, monkeypatch):
        monkeypatch.setenv("MEX_HOME", str(tmp_path / "nope"))
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 1
        assert "is not initialized" in result.stderr
