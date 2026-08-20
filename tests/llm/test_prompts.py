"""M5 LLM 层：prompts.py 单测（各部分组装、置信度规则、输出契约）。"""

from importlib import resources

from mex.domain.memory import Confidence, Memory
from mex.domain.schema import Schema, SubTopicSpec, TopicSpec, load_schema
from mex.llm.prompts import build_extract_messages


def make_schema() -> Schema:
    return Schema(
        topics={
            "basic_info": TopicSpec(
                name="basic_info",
                description="基础信息",
                sub_topics={"name": SubTopicSpec(name="name", description="姓名")},
            ),
            "work": TopicSpec(
                name="work",
                description="工作与职业",
                sub_topics={
                    "company": SubTopicSpec(name="company", description="公司"),
                    "tech_stack": SubTopicSpec(name="tech_stack", description="技术栈", unique=False),
                },
            ),
        },
    )


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


def build_messages(**kwargs):
    defaults = {
        "schema": make_schema(),
        "current_memories": [],
        "dialogue": "对话文本",
    }
    defaults.update(kwargs)
    return build_extract_messages(**defaults)


class TestBuildExtractMessages:
    def test_two_messages_system_and_user(self):
        messages = build_messages(dialogue="今天聊了项目")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "今天聊了项目"

    def test_task_section_present(self):
        system = build_messages()[0]["content"]
        assert "记忆抽取器" in system
        assert "画像" in system
        assert "sub_topic" in system

    def test_field_guide_present(self):
        system = build_messages()[0]["content"]
        assert "画像字段清单" in system
        assert "work 工作与职业" in system
        assert "tech_stack: 技术栈（可多条）" in system
        assert "name: 姓名" in system

    def test_field_guide_includes_new_values_fields(self):
        """默认模板新增的 values 字段自动注入提示词字段清单。"""
        schema = load_schema(str(resources.files("mex.templates").joinpath("schema.default.yaml")))
        system = build_messages(schema=schema)[0]["content"]
        for name in ("personality", "risk_preference", "decision_style", "emotion_triggers", "bottom_line"):
            assert name in system
        assert "content 为 JSON 对象" in system  # emotion_triggers 结构化字段的结构化提示

    def test_confidence_rules_present_and_confirmed_forbidden(self):
        system = build_messages()[0]["content"]
        assert "explicit" in system and "inferred" in system
        assert "speculated" in system and "uncertain" in system
        assert "禁止" in system and "confirmed" in system
        assert "禁止使用 confirmed" in system

    def test_profile_section_renders_existing_memories(self):
        memories = [
            make_memory(),
            make_memory(id="m-2", topic="work", sub_topic="tech_stack", content="Python"),
            make_memory(id="m-3", topic="work", sub_topic=None, content="下周去深圳出差"),
        ]
        system = build_messages(current_memories=memories)[0]["content"]
        assert "当前已有记忆" in system
        assert "work.company: 华为" in system
        assert "work.tech_stack: Python" in system
        assert "（画像外）: 下周去深圳出差" in system

    def test_profile_section_empty(self):
        system = build_messages()[0]["content"]
        assert "暂无" in system

    def test_output_contract_present(self):
        system = build_messages()[0]["content"]
        assert "memories" in system
        assert "is_ai_inferred" in system
        assert "只输出一个 JSON 对象" in system
        assert "topic" in system and "sub_topic" in system
        assert "画像外必须为 null" in system

    def test_evidence_always_in_contract(self):
        """evidence 始终要求输出（无开关，默认开启并存储）。"""
        system = build_messages()[0]["content"]
        assert "evidence" in system
        assert "证据摘要，引用用户原话" in system


class TestUnifiedPrompt:
    """归一化后：dialogue 与 import 共用同一套提示词，无需 mode 参数。"""

    def test_role_mentions_both_input_types(self):
        system = build_messages()[0]["content"]
        assert "个人记忆抽取器" in system
        assert "对话" in system and "档案" in system

    def test_capture_rules_present(self):
        """import 的逐条捕获规则已并入统一提示词。"""
        system = build_messages()[0]["content"]
        assert "逐条写入可多条字段" in system
        assert "身份级" in system
        assert "档案文档" in system

    def test_quality_and_examples_present(self):
        system = build_messages()[0]["content"]
        assert "记忆质量标准" in system
        assert "specific" in system and "future-useful" in system
        assert "推断" in system and "is_ai_inferred" in system
        assert "时间" in system and "相对表述" in system
        assert "抽取示例" in system

    def test_many_field_dedup_example_present(self):
        system = build_messages()[0]["content"]
        assert "可多条字段去重新增" in system
        assert "Rust" in system

    def test_outside_record_example_present(self):
        """提示词包含画像外记录示例（sub_topic 为 null 的临时信息）。"""
        system = build_messages()[0]["content"]
        assert "画像外记录" in system
        assert '"sub_topic": null' in system
        assert "下周去深圳出差几天" in system

    def test_action_section_present(self):
        """提示词包含候选动作判定规则（action 字段：new/update/uncertain_update/retire）。"""
        system = build_messages()[0]["content"]
        assert "候选动作判定" in system
        assert "uncertain_update" in system
        assert "永久变更" in system
        assert "人工审查" in system
        # 输出契约里也要求 action 字段
        assert '"action": "new|update|uncertain_update|retire"' in system

    def test_retire_rule_and_example_present(self):
        """提示词含 retire 规则（仅可多条槽位）、输出契约与示例。"""
        system = build_messages()[0]["content"]
        assert '"retire"' in system
        assert "用户不再持有某个旧态度" in system
        assert "只用于可多条槽位" in system
        assert "唯一槽位的改变请用 update" in system
        # 示例：放弃旧态度 → retire
        assert "没那么看重自由了" in system
