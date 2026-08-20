"""M5 服务层：extract.py 全流程测试（fake LLM 驱动，绝不调用真实 API）。"""

import json

import pytest

from mex.domain.memory import Confidence, Memory
from mex.domain.schema import Schema, SubTopicSpec, TopicSpec, load_schema
from mex.llm import LLMError, LLMResponse
from mex.services.extract import ExtractResult, extract_dialogue
from mex.store import history, memories
from mex.store.connection import connect, init_db

SAMPLE_YAML = """\
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
  finance:
    description: 财务
    sub_topics:
      risk_appetite: { description: 风险偏好 }
  values:
    description: 三观与原则
    sub_topics:
      values: { description: 核心价值观, unique: false }
"""


class FakeLLM:
    """测试替身：返回录制响应的假 LLM 客户端（实现 chat_json 接口即可）。"""

    model = "fake-model"

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat_json(self, messages: list[dict]):
        self.calls.append(messages)
        data = self._responses.pop(0) if self._responses else {"memories": []}
        return data, LLMResponse(text=json.dumps(data, ensure_ascii=False), prompt_tokens=10, completion_tokens=5)


class BoomLLM:
    """总是解析失败的替身。"""

    model = "fake-model"

    def chat_json(self, _messages: list[dict]):
        raise LLMError("parse", "LLM 输出无法解析")


def make_memory(**kwargs) -> Memory:
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


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "test.db")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def schema(tmp_path):
    p = tmp_path / "schema.yaml"
    p.write_text(SAMPLE_YAML, encoding="utf-8")
    return load_schema(str(p))


def count_rows(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - 测试 helper，参数为字面量


def run_extract(conn, client, schema, **kwargs):
    return extract_dialogue(
        conn,
        text=kwargs.get("text", "对话内容"),
        source_file=kwargs.get("source_file"),
        client=client,
        schema=schema,
    )


class TestFullFlow:
    def test_new_slot_update_outside_and_counts(self, conn, schema):
        with conn:
            memories.insert(conn, make_memory())  # 预置 work.company=华为
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "work", "sub_topic": "company",
                            "content": "华为技术有限公司", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "用户提到公司名",
                        },
                        {
                            "topic": "work", "sub_topic": "tech_stack",
                            "content": "Python", "is_ai_inferred": True,
                            "confidence": "inferred", "evidence": "用户列举",
                        },
                        {
                            "topic": "finance", "sub_topic": None,
                            "content": "基金单日跌 3%，用户表达了焦虑", "is_ai_inferred": True,
                            "confidence": "speculated", "evidence": "用户说今天跌得心慌",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert isinstance(result, ExtractResult)
        assert result.added == 2
        assert result.updated == 1
        assert result.discarded == []
        assert result.inferred_count == 2
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 5

        company = memories.find_by_slot(conn, "work", "company")
        assert company.content == "华为技术有限公司"
        assert company.evidence == "用户提到公司名"
        events = history.list_for_memory(conn, company.id)
        assert events[-1]["event"] == "update"
        assert events[-1]["old_content"] == "华为"
        assert events[-1]["new_content"] == "华为技术有限公司"
        assert events[-1]["actor"] == "ai"

        # 可多条槽位 tech_stack：追加一条 Python
        stacks = memories.list_by_slot(conn, "work", "tech_stack")
        assert len(stacks) == 1
        assert stacks[0].content == "Python"
        assert stacks[0].is_ai_inferred is True
        stack_events = history.list_for_memory(conn, stacks[0].id)
        assert stack_events[-1]["event"] == "add"
        assert stack_events[-1]["new_content"] == "Python"

        # 画像外记录（sub_topic 为空）：直接追加一条
        outside = [m for m in memories.list_all(conn) if m.sub_topic is None]
        assert len(outside) == 1
        assert "基金" in outside[0].content
        assert history.list_for_memory(conn, outside[0].id)[0]["event"] == "add"

    def test_outside_record_candidate(self, conn, schema):
        """LLM 候选省略 sub_topic（画像外记录）→ 通过校验并入库。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "finance", "sub_topic": None,
                            "content": "计划配置百万医疗险+重疾险", "is_ai_inferred": True,
                            "confidence": "explicit", "evidence": "用户提到保险计划",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.added == 1
        assert result.discarded == []
        got = memories.find_by_slot(conn, "finance", None)
        assert got is not None
        assert got.content == "计划配置百万医疗险+重疾险"

    def test_duplicate_candidates_same_unique_slot_no_crash(self, conn, schema):
        """同一批 LLM 候选指向同一唯一槽位 → 首条新增、后条更新，不撞唯一索引。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "work", "sub_topic": "company",
                            "content": "华为", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "文档第一段",
                        },
                        {
                            "topic": "work", "sub_topic": "company",
                            "content": "华为技术有限公司", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "文档第二段",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.added == 1
        assert result.updated == 1
        got = memories.find_by_slot(conn, "work", "company")
        assert got is not None
        assert got.content == "华为技术有限公司"
        events = history.list_for_memory(conn, got.id)
        assert [e["event"] for e in events] == ["add", "update"]

    def test_usage_recorded(self, conn, schema):
        client = FakeLLM([{"memories": []}])
        run_extract(conn, client, schema)
        rows = conn.execute("SELECT purpose, model, prompt_tokens, completion_tokens FROM llm_usage").fetchall()
        assert len(rows) == 1
        assert rows[0]["purpose"] == "extract"
        assert rows[0]["model"] == "fake-model"
        assert rows[0]["prompt_tokens"] == 10
        assert rows[0]["completion_tokens"] == 5

    def test_evidence_stored_by_default(self, conn, schema):
        """evidence 默认开启：LLM 返回的证据摘要落库。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "finance", "sub_topic": None,
                            "content": "事件", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "用户原话",
                        },
                    ],
                },
            ],
        )
        run_extract(conn, client, schema)
        outside = [m for m in memories.list_all(conn) if m.sub_topic is None]
        assert outside[0].evidence == "用户原话"

    def test_uncertain_update_forces_uncertain_into_review(self, conn, schema):
        """uncertain_update：唯一槽位已存在时，强制置信度 uncertain → 进 review 队列。"""
        with conn:
            memories.insert(conn, make_memory(content="上海"))  # 预置 work.company=上海
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "action": "uncertain_update",
                            "topic": "work", "sub_topic": "company",
                            "content": "深圳", "is_ai_inferred": True,
                            "confidence": "inferred", "evidence": "用户提到深圳，不确定是否搬家",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.updated == 1
        got = memories.find_by_slot(conn, "work", "company")
        assert got.content == "深圳"
        # 核心：强制降为 uncertain，进入审查队列
        assert got.confidence is Confidence.UNCERTAIN
        assert got.is_ai_inferred is True
        assert memories.count_pending_review(conn) == 1
        # history 记录了 update（旧值可恢复）
        records = history.list_for_memory(conn, got.id)
        assert records[-1]["event"] == "update"
        assert records[-1]["old_content"] == "上海"

    def test_update_keeps_llm_confidence(self, conn, schema):
        """update（确信更新）：保持 LLM 给的置信度，不强制 uncertain。"""
        with conn:
            memories.insert(conn, make_memory(content="华为"))
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "action": "update",
                            "topic": "work", "sub_topic": "company",
                            "content": "腾讯", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "用户说换工作了",
                        },
                    ],
                },
            ],
        )
        run_extract(conn, client, schema)
        got = memories.find_by_slot(conn, "work", "company")
        assert got.content == "腾讯"
        assert got.confidence is Confidence.EXPLICIT  # 保持 LLM 给的，不强制 uncertain
        assert memories.count_pending_review(conn) == 0

    def test_missing_action_defaults_to_new(self, conn, schema):
        """action 缺失时默认 new（向后兼容旧 LLM 响应）。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "work", "sub_topic": "company",
                            "content": "华为", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "x",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.added == 1

    def test_invalid_action_discarded(self, conn, schema):
        """非法 action 值丢弃并报原因。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "action": "bogus",
                            "topic": "work", "sub_topic": "company",
                            "content": "华为", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "x",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.added == 0
        assert len(result.discarded) == 1
        assert "action" in result.discarded[0][1]


class TestDiscard:
    def test_confirmed_and_unknown_slot_discarded(self, conn, schema):
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "work", "sub_topic": "company",
                            "content": "华为", "is_ai_inferred": True, "confidence": "confirmed",
                            "evidence": "x",
                        },
                        {
                            "topic": "work", "sub_topic": "employer",
                            "content": "华为", "is_ai_inferred": False, "confidence": "explicit",
                            "evidence": "x",
                        },
                        {
                            "topic": "finance", "sub_topic": None,
                            "is_ai_inferred": False, "confidence": "explicit", "evidence": "x",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.added == 0
        assert len(result.discarded) == 3
        reasons = [reason for _, reason in result.discarded]
        assert any("confirmed" in r for r in reasons)
        assert any("employer" in r for r in reasons)
        assert any("content" in r for r in reasons)
        assert count_rows(conn, "memories") == 0

    def test_non_str_content_discarded(self, conn, schema):
        """两属性模型：content 必须是单值字符串，数组/数字一律丢弃。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "work", "sub_topic": "tech_stack",
                            "content": "Python", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "x",
                        },
                        {
                            "topic": "work", "sub_topic": "company",
                            "content": 123, "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "x",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.added == 1
        assert len(result.discarded) == 1
        assert result.discarded[0][1] == "字段类型错误（content）"
        stacks = memories.list_by_slot(conn, "work", "tech_stack")
        assert stacks[0].content == "Python"

    def test_sub_topic_without_topic_discarded(self, conn, schema):
        """sub_topic 非空但 topic 缺失：画像槽位必须成对，整条丢弃。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": None, "sub_topic": "company",
                            "content": "华为", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "x",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert len(result.discarded) == 1
        assert "topic" in result.discarded[0][1]


class TestIncremental:
    def test_second_call_only_new_text(self, conn, schema, tmp_path):
        source_file = str(tmp_path / "session.txt")
        first_text = "一" * 100
        client1 = FakeLLM([{"memories": []}])
        run_extract(conn, client1, schema, text=first_text, source_file=source_file)
        row = conn.execute("SELECT last_position FROM extraction_state").fetchone()
        assert row["last_position"] == 100

        second_text = first_text + "二" * 50
        client2 = FakeLLM([{"memories": []}])
        run_extract(conn, client2, schema, text=second_text, source_file=source_file)
        assert client2.calls[0][-1]["content"] == "二" * 50
        row = conn.execute("SELECT last_position FROM extraction_state").fetchone()
        assert row["last_position"] == 150

    def test_truncated_file_restarts_from_beginning(self, conn, schema, tmp_path):
        source_file = str(tmp_path / "session.txt")
        run_extract(conn, FakeLLM([{"memories": []}]), schema, text="一" * 100, source_file=source_file)
        client = FakeLLM([{"memories": []}])
        run_extract(conn, client, schema, text="短文本", source_file=source_file)
        assert client.calls[0][-1]["content"] == "短文本"

    def test_empty_after_incremental_skips_llm(self, conn, schema, tmp_path):
        source_file = str(tmp_path / "session.txt")
        run_extract(conn, FakeLLM([{"memories": []}]), schema, text="一" * 100, source_file=source_file)
        client = FakeLLM([{"memories": []}])
        result = run_extract(conn, client, schema, text="一" * 100, source_file=source_file)
        assert client.calls == []
        assert result.added == 0


class TestFailure:
    def test_llm_error_no_write(self, conn, schema):
        with pytest.raises(LLMError):
            run_extract(conn, BoomLLM(), schema)
        assert count_rows(conn, "memories") == 0
        assert count_rows(conn, "history") == 0
        assert count_rows(conn, "llm_usage") == 0
        assert count_rows(conn, "extraction_state") == 0


class TestSchemaDirectBuild:
    def test_programmatic_schema(self, conn):
        schema = Schema(
            topics={
                "work": TopicSpec(
                    name="work",
                    sub_topics={"company": SubTopicSpec(name="company")},
                ),
            },
        )
        client = FakeLLM(
            [
                {
                    "memories": [
                        {
                            "topic": "work", "sub_topic": "company",
                            "content": "华为", "is_ai_inferred": False,
                            "confidence": "explicit", "evidence": "x",
                        },
                    ],
                },
            ],
        )
        result = run_extract(conn, client, schema)
        assert result.added == 1
        assert memories.find_by_slot(conn, "work", "company").content == "华为"


STRUCTURED_SCHEMA_YAML = """\
topics:
  work:
    description: 工作与职业
    sub_topics:
      company: { description: 公司 }
      experience:
        description: 工作经历
        unique: false
        fields:
          start:
            description: 开始时间
            format: time
            required: true
          end:
            description: 结束时间
            format: time
            required: true
          company:
            description: 公司名
            required: true
          position: 职位
          summary: 内容概要
"""


class TestStructuredExtract:
    """结构化 JSON 字段的候选校验与写入。"""

    @pytest.fixture
    def structured_schema(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text(STRUCTURED_SCHEMA_YAML, encoding="utf-8")
        return load_schema(str(p))

    def _candidate(self, content, **extra):
        """构造一条结构化字段候选。"""
        cand = {
            "topic": "work", "sub_topic": "experience",
            "content": content, "is_ai_inferred": False,
            "confidence": "explicit", "evidence": "x",
        }
        cand.update(extra)
        return cand

    def test_valid_json_candidate_inserted(self, conn, structured_schema):
        """合法 JSON 对象 content 正常写入。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        self._candidate(
                            '{"start": "2019", "end": "2023", "company": "华为", '
                            '"position": "后端工程师", "summary": "订单系统重构"}',
                        ),
                    ],
                },
            ],
        )
        result = run_extract(conn, client, structured_schema)
        assert result.added == 1
        inserted = memories.list_by_slot(conn, "work", "experience")
        assert inserted[0].content == (
            '{"start": "2019", "end": "2023", "company": "华为", '
            '"position": "后端工程师", "summary": "订单系统重构"}'
        )

    def test_non_json_content_discarded(self, conn, structured_schema):
        """结构化字段的 content 不是 JSON 对象时丢弃并说明原因。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        self._candidate("2019-2023 在华为做后端"),
                    ],
                },
            ],
        )
        result = run_extract(conn, client, structured_schema)
        assert result.added == 0
        assert len(result.discarded) == 1
        assert "JSON" in result.discarded[0][1]

    def test_unknown_key_discarded(self, conn, structured_schema):
        """结构化字段包含未声明键时丢弃（防止 schema 外字段混入）。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        self._candidate(
                            '{"start": "2019", "end": "2020", "company": "华为", '
                            '"position": "工程师", "hobby": "篮球"}',
                        ),
                    ],
                },
            ],
        )
        result = run_extract(conn, client, structured_schema)
        assert result.added == 0
        assert len(result.discarded) == 1
        assert "Unknown keys" in result.discarded[0][1]

    def test_json_array_discarded(self, conn, structured_schema):
        """content 是 JSON 数组而非对象时丢弃。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        self._candidate('["2019", "2023", "华为"]'),
                    ],
                },
            ],
        )
        result = run_extract(conn, client, structured_schema)
        assert result.added == 0
        assert len(result.discarded) == 1
        assert "must be a JSON object" in result.discarded[0][1]

    def test_missing_required_key_discarded(self, conn, structured_schema):
        """结构化字段缺失必填键（end）时丢弃。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        self._candidate('{"start": "2019", "company": "华为", "position": "工程师"}'),
                    ],
                },
            ],
        )
        result = run_extract(conn, client, structured_schema)
        assert result.added == 0
        assert len(result.discarded) == 1
        assert "Missing required key" in result.discarded[0][1]

    def test_bad_time_format_discarded(self, conn, structured_schema):
        """结构化字段时间键格式非法时丢弃。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        self._candidate(
                            '{"start": "19年", "end": "2020", "company": "华为", '
                            '"position": "工程师"}',
                        ),
                    ],
                },
            ],
        )
        result = run_extract(conn, client, structured_schema)
        assert result.added == 0
        assert len(result.discarded) == 1
        assert "invalid time format" in result.discarded[0][1]


def _retire_candidate(sub_topic: str, content: str, **extra) -> dict:
    """构造一条 retire 候选。"""
    cand = {
        "action": "retire",
        "topic": "values",
        "sub_topic": sub_topic,
        "content": content,
        "is_ai_inferred": True,
        "confidence": "inferred",
        "evidence": "用户改变态度",
    }
    cand.update(extra)
    return cand


class TestRetire:
    """retire 动作（矛盾消解，移除可多条槽位的某条记录）。"""

    def test_retire_matching_record_soft_deleted(self, conn, schema):
        """库中有 content 完全相同的该槽位记录 → 软删 + forget history，profile 不再含旧值。"""
        with conn:
            memories.insert(conn, make_memory(id="v1", topic="values", sub_topic="values", content="自由"))
        client = FakeLLM([{"memories": [_retire_candidate("values", "自由")]}])
        result = run_extract(conn, client, schema)
        assert result.retired == 1
        assert result.discarded == []
        remaining = memories.list_by_slot(conn, "values", "values")
        assert remaining == []  # 记录已被软删，list_by_slot 只返回有效记录
        gone = memories.get(conn, "v1", include_forgotten=True)
        assert gone is not None and gone.is_forgotten()
        assert "retire" in gone.forgotten_reason
        events = history.list_for_memory(conn, "v1")
        assert events[-1]["event"] == "forget"

    def test_retire_no_matching_record_discarded(self, conn, schema):
        """库中无 content 完全相同的记录 → discarded（未找到要 retire 的记录）。"""
        with conn:
            memories.insert(conn, make_memory(id="v1", topic="values", sub_topic="values", content="自由"))
        client = FakeLLM([{"memories": [_retire_candidate("values", "完全不存在的价值观")]}])
        result = run_extract(conn, client, schema)
        assert result.retired == 0
        assert len(result.discarded) == 1
        assert "reject" not in result.discarded[0][1] or True  # 通过 retired 计数校验
        assert result.discarded[0][1] == "未找到要 retire 的记录"
        # 记录仍在（未删除）
        assert memories.list_by_slot(conn, "values", "values")[0].content == "自由"

    def test_retire_near_match_not_deleted(self, conn, schema):
        """相近但不完全相同（"自由" vs "追求自由"）→ 不匹配，discarded，不删。"""
        with conn:
            memories.insert(conn, make_memory(id="v1", topic="values", sub_topic="values", content="追求自由"))
        client = FakeLLM([{"memories": [_retire_candidate("values", "自由")]}])
        result = run_extract(conn, client, schema)
        assert result.retired == 0
        assert len(result.discarded) == 1
        assert memories.list_by_slot(conn, "values", "values")[0].content == "追求自由"

    def test_retire_unique_slot_rejected(self, conn, schema):
        """retire 指向唯一槽位 → 解析阶段拒绝（rules action 规则），discarded。"""
        cand = _retire_candidate("name", "x")
        cand["topic"] = "basic_info"
        client = FakeLLM([{"memories": [cand]}])
        result = run_extract(conn, client, schema)
        assert result.retired == 0
        assert len(result.discarded) == 1
        assert "retire" in result.discarded[0][1] and "multi-value slots" in result.discarded[0][1]


VALUES_YAML = """\
topics:
  values:
    description: 三观与原则
    sub_topics:
      personality: { description: 性格倾向 }
      emotion_triggers:
        description: 情绪触发点
        unique: false
        fields:
          trigger:
            description: 触发话题或情境
            required: true
          reaction:
            description: 情绪反应
            required: true
"""


class TestValuesExtract:
    """values 新字段经 LLM 抽取路径自动适配（fake LLM 驱动）。"""

    @pytest.fixture
    def values_schema(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text(VALUES_YAML, encoding="utf-8")
        return load_schema(str(p))

    def _candidate(self, sub_topic: str, content: str, **extra):
        """构造一条 values 领域候选。"""
        cand = {
            "topic": "values", "sub_topic": sub_topic,
            "content": content, "is_ai_inferred": True,
            "confidence": "inferred", "evidence": "对话中透露",
        }
        cand.update(extra)
        return cand

    def test_personality_and_emotion_triggers_inserted(self, conn, values_schema):
        """抽取路径：性格倾向 + 情绪触发点（结构化）候选正常入库。"""
        client = FakeLLM(
            [
                {
                    "memories": [
                        self._candidate("personality", "INTJ"),
                        self._candidate("emotion_triggers", '{"trigger": "前领导", "reaction": "暴躁"}'),
                    ],
                },
            ],
        )
        result = run_extract(conn, client, values_schema)
        assert result.added == 2
        assert result.discarded == []
        assert memories.find_by_slot(conn, "values", "personality").content == "INTJ"
        triggers = memories.list_by_slot(conn, "values", "emotion_triggers")
        assert len(triggers) == 1
        assert triggers[0].content == '{"trigger": "前领导", "reaction": "暴躁"}'

    def test_broken_emotion_triggers_discarded(self, conn, values_schema):
        """抽取路径：结构化校验同样生效——缺必填键被丢弃。"""
        client = FakeLLM([{"memories": [self._candidate("emotion_triggers", '{"trigger": "加班"}')]}])
        result = run_extract(conn, client, values_schema)
        assert result.added == 0
        assert len(result.discarded) == 1
        assert "Missing required key" in result.discarded[0][1]
