"""M7 备份迁移服务测试（services/backup.py）。

覆盖：导出 JSON/Markdown 内容与往返、四类冲突 skip/overwrite、schema 外槽位
不可覆盖、版本不匹配与损坏 JSON 报错不落库、import extract 复用（fake LLM）、
stats 计数与聚合。全部使用 tmp_path 隔离（MEX_HOME 指向临时目录）。
"""

from __future__ import annotations

import json

import pytest

from mex.cli.common import ExternalError
from mex.domain.memory import Confidence, Memory
from mex.domain.schema import load_schema
from mex.llm import LLMResponse
from mex.services.backup import EXPORT_VERSION, export_json, export_markdown, import_extract, import_restore, stats
from mex.store import history, memories
from mex.store.connection import connect, init_db, transaction

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
      tech_stack: { description: 技术栈, unique: false }
  finance:
    description: 财务
    sub_topics:
      risk_appetite: { description: 风险偏好 }
"""


@pytest.fixture
def mex_home(tmp_path, monkeypatch):
    """隔离数据目录：MEX_HOME 指向 tmp_path 并初始化。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    init_db(tmp_path / "mex.db")
    (tmp_path / "schema.yaml").write_text(SCHEMA_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def conn(mex_home):
    c = connect(mex_home / "mex.db")
    yield c
    c.close()


class FakeLLM:
    """抽取测试替身：返回录制响应，不调用真实 LLM API。"""

    model = "fake-model"

    def __init__(self, data: dict):
        self._data = data
        self.calls: list[list[dict]] = []

    def chat_json(self, messages: list[dict]):
        self.calls.append(messages)
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


def insert(conn, m: Memory) -> None:
    """直接插入一条记忆。"""
    with transaction(conn):
        memories.insert(conn, m)


def write_export(path, memory_dicts: list[dict], *, version: int = EXPORT_VERSION) -> str:
    """写一个导出 JSON 文件并返回路径。"""
    payload = {"version": version, "exported_at": "2026-08-06T08:30:00Z", "memories": memory_dicts}
    p = path.parent / "export.json" if path.is_dir() else path
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


def memory_dict(m: Memory) -> dict:
    """Memory → 导出字典（同 to_dict）。"""
    return m.to_dict()


class TestExportJson:
    def test_export_parent_dir_auto_created(self, mex_home, conn):
        insert(conn, make_memory())
        report = export_json(conn, str(mex_home / "nested" / "dir" / "out.json"))
        assert report.count == 1
        assert report.forgotten_count == 0
        assert (mex_home / "nested" / "dir" / "out.json").exists()

    def test_export_json_structure(self, mex_home, conn):
        insert(conn, make_memory())
        out = export_json(conn, str(mex_home / "out.json"))
        data = json.loads((mex_home / "out.json").read_text(encoding="utf-8"))
        assert data["version"] == EXPORT_VERSION
        assert data["exported_at"].endswith("Z")
        assert len(data["memories"]) == 1
        entry = data["memories"][0]
        for key in (
            "id", "topic", "sub_topic", "content", "is_ai_inferred",
            "confidence", "evidence", "created_at", "updated_at", "forgotten_at", "forgotten_reason",
        ):
            assert key in entry
        assert "layer" not in entry
        assert out.path == str(mex_home / "out.json")

    def test_export_includes_forgotten_state(self, mex_home, conn):
        insert(conn, make_memory())
        with transaction(conn):
            memories.forget(conn, "m-1", "已删")
        report = export_json(conn, str(mex_home / "out.json"))
        assert report.count == 1
        assert report.forgotten_count == 1
        data = json.loads((mex_home / "out.json").read_text(encoding="utf-8"))
        entry = data["memories"][0]
        assert entry["forgotten_at"] is not None
        assert entry["forgotten_reason"] == "已删"

    def test_json_roundtrip_preserves_all_fields(self, mex_home, conn):
        entry_a = make_memory()
        entry_b = make_memory(
            id="m-2",
            topic="finance",
            sub_topic=None,
            content="基金单日下跌 3%",
            is_ai_inferred=True,
            confidence=Confidence.SPECULATED,
            evidence="用户提到今天跌得心慌",
            created_at="2026-08-05T10:00:00Z",
        )
        entry_c = make_memory(id="m-3", sub_topic="tech_stack", content='["Python", "Go"]')
        insert(conn, entry_a)
        insert(conn, entry_b)
        insert(conn, entry_c)
        with transaction(conn):
            memories.forget(conn, "m-3", "已过时")
        export_json(conn, str(mex_home / "out.json"))

        (mex_home / "restore").mkdir()
        init_db(mex_home / "restore" / "mex.db")
        conn2 = connect(mex_home / "restore" / "mex.db")
        try:
            report = import_restore(conn2, str(mex_home / "out.json"), on_conflict="skip")
            assert report.imported == 3
            assert report.skipped == 0
            assert report.conflicts == []
            got = memories.get(conn2, "m-1", include_forgotten=True)
            assert got is not None and got.to_dict() == entry_a.to_dict()
            got_b = memories.get(conn2, "m-2", include_forgotten=True)
            assert got_b is not None and got_b.to_dict() == entry_b.to_dict()
            got_c = memories.get(conn2, "m-3", include_forgotten=True)
            assert got_c is not None and got_c.is_forgotten()
            assert got_c.content == '["Python", "Go"]'
            assert got_c.forgotten_reason == "已过时"
            assert got_c.topic == "work" and got_c.sub_topic == "tech_stack"
        finally:
            conn2.close()


class TestExportMarkdown:
    def test_markdown_sections(self, mex_home, conn):
        insert(conn, make_memory())
        insert(conn, make_memory(id="m-2", sub_topic="tech_stack", content="Python, Go"))
        insert(conn, make_memory(
            id="m-3",
            topic="finance",
            sub_topic="risk_appetite",
            content="稳健偏保守",
        ))
        insert(conn, make_memory(
            id="m-4",
            topic="finance",
            sub_topic=None,
            content="基金单日下跌 3%",
            created_at="2026-08-05T10:00:00Z",
        ))
        insert(conn, make_memory(id="m-5", topic="basic_info", sub_topic="location", content="北京"))
        with transaction(conn):
            memories.forget(conn, "m-5", "信息过时")
        report = export_markdown(conn, str(mex_home / "out.md"))

        text = (mex_home / "out.md").read_text(encoding="utf-8")
        assert "# meX memory export (" in text
        assert "## Profile" in text
        assert "### work" in text
        assert "- company: 华为" in text
        assert "- tech_stack: Python, Go" in text
        assert "- risk_appetite: 稳健偏保守" in text
        assert "## Out-of-profile records (timeline)" in text
        assert "- [2026-08-05] 基金单日下跌 3%" in text
        assert "## Forgotten (1)" in text
        assert "[forgotten] basic_info.location: 北京" in text
        assert "forgotten on" in text
        assert report.count == 5
        assert report.forgotten_count == 1

    def test_markdown_newline_replaced_by_space(self, mex_home, conn):
        insert(conn, make_memory(content="第一行\n第二行"))
        export_markdown(conn, str(mex_home / "out.md"))
        text = (mex_home / "out.md").read_text(encoding="utf-8")
        assert "- company: 第一行 第二行" in text


class TestImportRestore:
    def test_import_new_entries_preserves_forgotten(self, mex_home, conn):
        path = write_export(mex_home, [
            memory_dict(make_memory()),
            memory_dict(make_memory(id="m-2", topic="finance", sub_topic=None, content="跌了")),
            memory_dict(make_memory(
                id="m-3", sub_topic="tech_stack", content='["Python"]',
                forgotten_at="2026-08-01T00:00:00Z", forgotten_reason="已过时",
            )),
        ])
        report = import_restore(conn, path, on_conflict="skip")
        assert report.imported == 3
        assert report.skipped == 0
        assert report.conflicts == []
        got = memories.get(conn, "m-3", include_forgotten=True)
        assert got is not None and got.is_forgotten()
        assert got.forgotten_at == "2026-08-01T00:00:00Z"
        assert got.forgotten_reason == "已过时"

    def test_same_id_identical_skipped(self, mex_home, conn):
        insert(conn, make_memory())
        path = write_export(mex_home, [memory_dict(make_memory())])
        report = import_restore(conn, path, on_conflict="skip")
        assert report.imported == 0
        assert report.skipped == 1
        assert report.conflicts == []

    def test_same_id_different_content_skip_default(self, mex_home, conn):
        insert(conn, make_memory())
        path = write_export(mex_home, [memory_dict(make_memory(content="腾讯"))])
        report = import_restore(conn, path, on_conflict="skip")
        assert report.imported == 0
        assert report.skipped == 1
        assert report.conflicts[0]["reason"] == "same id different content"
        assert report.conflicts[0]["id"] == "m-1"
        assert memories.get(conn, "m-1").content == "华为"

    def test_same_id_different_content_overwrite(self, mex_home, conn):
        insert(conn, make_memory())
        path = write_export(mex_home, [memory_dict(make_memory(content="腾讯", confidence=Confidence.INFERRED))])
        report = import_restore(conn, path, on_conflict="overwrite")
        assert report.imported == 1
        assert report.skipped == 0
        got = memories.get(conn, "m-1")
        assert got is not None and got.content == "腾讯"
        records = history.list_for_memory(conn, "m-1")
        assert records[-1]["event"] == "update"
        assert records[-1]["actor"] == "user"
        assert records[-1]["evidence"] == "import restore"
        assert records[-1]["old_content"] == "华为"

    def test_slot_conflict_skip_default(self, mex_home, conn):
        insert(conn, make_memory(id="m-1", content="华为"))
        path = write_export(mex_home, [memory_dict(make_memory(id="m-2", content="腾讯"))])
        report = import_restore(conn, path, on_conflict="skip")
        assert report.imported == 0
        assert report.skipped == 1
        assert report.conflicts[0]["reason"] == "slot conflict"
        assert memories.get(conn, "m-2") is None
        assert memories.get(conn, "m-1").content == "华为"

    def test_slot_conflict_overwrite_forgets_old(self, mex_home, conn):
        insert(conn, make_memory(id="m-1", content="华为"))
        path = write_export(mex_home, [memory_dict(make_memory(id="m-2", content="腾讯"))])
        report = import_restore(conn, path, on_conflict="overwrite")
        assert report.imported == 1
        old = memories.get(conn, "m-1", include_forgotten=True)
        assert old is not None and old.is_forgotten()
        assert old.forgotten_reason == "import overwrite"
        got = memories.get(conn, "m-2")
        assert got is not None and got.content == "腾讯"
        records = history.list_for_memory(conn, "m-1")
        assert records[-1]["event"] == "forget"
        assert records[-1]["actor"] == "user"

    def test_slot_not_in_schema_never_overwritten(self, mex_home, conn):
        path = write_export(mex_home, [
            memory_dict(make_memory(id="m-9", sub_topic="salary", content="100万")),
        ])
        report = import_restore(conn, path, on_conflict="overwrite")
        assert report.imported == 0
        assert report.skipped == 1
        assert report.conflicts[0]["reason"] == "slot not in schema"
        assert "schema.yaml" in report.conflicts[0]["detail"]
        assert memories.get(conn, "m-9", include_forgotten=True) is None

    def test_invalid_entry_does_not_break_others(self, mex_home, conn):
        bad = {"id": "m-bad", "topic": "work"}  # 缺 content 等字段
        good = memory_dict(make_memory())
        path = write_export(mex_home, [bad, good])
        report = import_restore(conn, path, on_conflict="skip")
        assert report.imported == 1
        assert report.skipped == 1
        assert report.conflicts[0]["reason"] == "invalid entry"
        assert report.conflicts[0]["id"] == "m-bad"
        assert memories.get(conn, "m-1") is not None
        assert memories.get(conn, "m-bad", include_forgotten=True) is None

    def test_version_mismatch_raises(self, mex_home, conn):
        path = write_export(mex_home, [memory_dict(make_memory())], version=2)
        with pytest.raises(ExternalError, match="does not match"):
            import_restore(conn, path, on_conflict="skip")
        assert memories.list_all(conn, include_forgotten=True) == []

    def test_missing_version_raises(self, mex_home, conn):
        path = mex_home / "bad.json"
        path.write_text(json.dumps({"memories": []}), encoding="utf-8")
        with pytest.raises(ExternalError, match="does not match"):
            import_restore(conn, str(path), on_conflict="skip")

    def test_corrupt_json_raises_no_write(self, mex_home, conn):
        path = mex_home / "bad.json"
        path.write_text("这不是 JSON", encoding="utf-8")
        with pytest.raises(ExternalError, match="JSON"):
            import_restore(conn, str(path), on_conflict="skip")
        assert memories.list_all(conn, include_forgotten=True) == []

    def test_missing_file_raises(self, mex_home, conn):
        with pytest.raises(ExternalError, match="JSON"):
            import_restore(conn, str(mex_home / "nope.json"), on_conflict="skip")


class TestImportExtract:
    def test_reuses_extract_dialogue_without_state(self, mex_home, conn):
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
        schema = load_schema(str(mex_home / "schema.yaml"))
        result = import_extract(
            conn, str(source), client=fake, schema=schema,
        )
        assert result.added == 1
        assert result.prompt_tokens == 10
        assert len(fake.calls) == 1
        outside = [m for m in memories.list_all(conn) if m.sub_topic is None]
        assert len(outside) == 1
        assert outside[0].content == "今天完成了迁移"
        assert outside[0].evidence == "用户提到"
        count = conn.execute("SELECT COUNT(*) FROM extraction_state").fetchone()[0]
        assert count == 0


class TestStats:
    def test_counts_and_aggregation(self, mex_home, conn):
        insert(conn, make_memory())
        insert(conn, make_memory(
            id="m-2", topic="finance", sub_topic="risk_appetite", content="稳健",
        ))
        insert(conn, make_memory(
            id="m-3", topic="finance", sub_topic=None,
            content="基金下跌", is_ai_inferred=True, confidence=Confidence.SPECULATED,
        ))
        insert(conn, make_memory(id="m-4", topic="basic_info", sub_topic="location", content="北京"))
        with transaction(conn):
            memories.forget(conn, "m-4", "已删")
        conn.execute(
            "INSERT INTO llm_usage (id, purpose, model, prompt_tokens, completion_tokens, created_at) "
            "VALUES ('u1', 'extract', 'm', 100, 20, '2026-01-01T00:00:00Z')",
        )
        conn.execute(
            "INSERT INTO llm_usage (id, purpose, model, prompt_tokens, completion_tokens, created_at) "
            "VALUES ('u2', 'extract', 'm', 50, 10, '2026-01-02T00:00:00Z')",
        )
        conn.commit()

        data = stats(conn, str(mex_home))
        assert data["profile"] == {"profile": 2, "outside": 1}
        assert data["pending_review"] == 1
        assert data["db_size_bytes"] > 0
        assert data["llm_usage"] == {"prompt_tokens": 150, "completion_tokens": 30, "calls": 2}

    def test_empty_db_stats(self, mex_home, conn):
        data = stats(conn, str(mex_home))
        assert data["profile"] == {"profile": 0, "outside": 0}
        assert data["pending_review"] == 0
        assert data["llm_usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
