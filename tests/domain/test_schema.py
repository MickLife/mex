"""M1 领域层：schema.py 单测。"""

from importlib import resources

import pytest

from mex.domain.schema import Schema, SchemaError, SubTopicSpec, TopicSpec, load_schema

DEFAULT_TEMPLATE = str(resources.files("mex.templates").joinpath("schema.default.yaml"))
"""打包的默认 schema 模板（values 领域断言以真实模板为准）。"""

SAMPLE_YAML = """\
topics:
  basic_info:
    description: 基础信息
    sub_topics:
      name: { description: 姓名 }
      location: { description: 现居地 }
  work:
    description: 工作与职业
    sub_topics:
      company: { description: 公司 }
      tech_stack: { description: 技术栈, unique: false }
"""

# 旧格式（multiple: true）兼容用例：应自动当作 unique: false 并告警
LEGACY_MULTIPLE_YAML = """\
topics:
  work:
    description: 工作与职业
    sub_topics:
      tech_stack: { description: 技术栈, multiple: true }
"""


@pytest.fixture
def schema_path(tmp_path):
    p = tmp_path / "schema.yaml"
    p.write_text(SAMPLE_YAML, encoding="utf-8")
    return str(p)


class TestLoadSchema:
    def test_ok(self, schema_path):
        schema = load_schema(schema_path)
        assert schema.topic_names() == ["basic_info", "work"]
        assert schema.sub_topic_names("work") == ["company", "tech_stack"]
        assert schema.sub_topic_names("missing") == []

    def test_empty_topics(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text("topics: {}\n", encoding="utf-8")
        schema = load_schema(str(p))
        assert schema.topic_names() == []

    def test_missing_file(self, tmp_path):
        with pytest.raises(SchemaError, match="Failed to read"):
            load_schema(str(tmp_path / "nope.yaml"))

    def test_missing_topics_key(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text("foo: bar\n", encoding="utf-8")
        with pytest.raises(SchemaError, match="topics"):
            load_schema(str(p))

    def test_topics_not_dict(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text("topics: [a, b]\n", encoding="utf-8")
        with pytest.raises(SchemaError, match="topics"):
            load_schema(str(p))

    def test_sub_topics_not_dict(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text("topics:\n  work: { sub_topics: [a] }\n", encoding="utf-8")
        with pytest.raises(SchemaError, match="sub_topics"):
            load_schema(str(p))

    def test_topic_plain_string_form(self, tmp_path):
        p = tmp_path / "schema.yaml"
        content = (
            "topics:\n  pets:\n    description: 宠物\n"
            "    sub_topics:\n      dog_name: 狗的名字\n"
        )
        p.write_text(content, encoding="utf-8")
        schema = load_schema(str(p))
        assert schema.sub_topic_names("pets") == ["dog_name"]

    def test_sub_topic_none_form(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text("topics:\n  work:\n    sub_topics:\n      company:\n", encoding="utf-8")
        schema = load_schema(str(p))
        assert schema.sub_topic_names("work") == ["company"]


class TestSchemaQueries:
    def test_is_valid_slot(self, schema_path):
        schema = load_schema(schema_path)
        assert schema.is_valid_slot("work", "company")
        assert not schema.is_valid_slot("work", "employer")
        assert not schema.is_valid_slot("missing", "company")

    def test_is_unique(self, schema_path):
        schema = load_schema(schema_path)
        assert schema.is_unique("work", "company")
        assert not schema.is_unique("work", "tech_stack")
        # 槽位不存在时保守按唯一处理（由上层 schema 校验拦截）
        assert schema.is_unique("missing", "x")

    def test_legacy_multiple_treated_as_many(self, tmp_path):
        """旧格式 multiple: true 应自动当作 unique: false（兼容，并告警）。"""
        p = tmp_path / "schema.yaml"
        p.write_text(LEGACY_MULTIPLE_YAML, encoding="utf-8")
        schema = load_schema(str(p))
        assert not schema.is_unique("work", "tech_stack")

    def test_has_topic(self, schema_path):
        schema = load_schema(schema_path)
        assert schema.has_topic("work")
        assert not schema.has_topic("job")

    def test_field_guide_contains_description_and_unique(self, schema_path):
        guide = load_schema(schema_path).field_guide()
        assert "work 工作与职业" in guide
        assert "tech_stack: 技术栈（可多条）" in guide
        assert "name: 姓名" in guide


class TestDirectConstruction:
    def test_build_schema_programmatically(self):
        schema = Schema(
            topics={
                "work": TopicSpec(
                    name="work",
                    description="工作",
                    sub_topics={"company": SubTopicSpec(name="company", description="公司")},
                ),
            },
        )
        assert schema.is_valid_slot("work", "company")


STRUCTURED_YAML = """\
topics:
  work:
    description: 工作与职业
    sub_topics:
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
      tech_stack: { description: 技术栈, unique: false }
"""


class TestStructuredFields:
    """结构化 JSON 字段（fields 声明）的解析与查询。"""

    @pytest.fixture
    def structured_path(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text(STRUCTURED_YAML, encoding="utf-8")
        return str(p)

    def test_fields_parsed(self, structured_path):
        schema = load_schema(structured_path)
        spec = schema.topics["work"].sub_topics["experience"]
        assert list(spec.fields) == ["start", "end", "company"]
        start = spec.fields["start"]
        assert start.description == "开始时间"
        assert start.required is True
        assert start.format == "time"
        assert spec.fields["end"].format == "time" and spec.fields["end"].required
        assert spec.fields["company"].required and spec.fields["company"].format is None
        assert spec.unique is False
        # 未声明 fields 的字段为 None
        assert schema.topics["work"].sub_topics["tech_stack"].fields is None

    def test_is_structured(self, structured_path):
        schema = load_schema(structured_path)
        assert schema.is_structured("work", "experience")
        assert not schema.is_structured("work", "tech_stack")
        assert not schema.is_structured("work", "missing")

    def test_json_template(self, structured_path):
        schema = load_schema(structured_path)
        template = schema.json_template("work", "experience")
        assert '"start"' in template and "公司名" in template
        assert "[必填]" in template and "[YYYY/YYYY-MM]" in template
        assert schema.json_template("work", "tech_stack") is None

    def test_field_guide_marks_structured(self, structured_path):
        guide = load_schema(structured_path).field_guide()
        assert "content 为 JSON 对象" in guide
        assert "开始时间" in guide

    def test_fields_must_be_nonempty_dict(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text(
            "topics:\n  work:\n    sub_topics:\n      x: { fields: [] }\n",
            encoding="utf-8",
        )
        with pytest.raises(SchemaError, match="fields"):
            load_schema(str(p))

    def test_invalid_format_value_rejected(self, tmp_path):
        p = tmp_path / "schema.yaml"
        p.write_text(
            "topics:\n  work:\n    sub_topics:\n      x:\n        unique: false\n"
            "        fields:\n          t: { description: 时间, format: bogus }\n",
            encoding="utf-8",
        )
        with pytest.raises(SchemaError, match="format"):
            load_schema(str(p))

    def test_validate_structured_content(self, structured_path):
        schema = load_schema(structured_path)
        # 合法：时间格式正确 + 必选键齐全
        ok, errs = schema.validate_structured_content(
            "work", "experience",
            '{"start": "2019-07", "end": "至今", "company": "华为"}',
        )
        assert ok and errs == []
        # 未知键
        _, errs = schema.validate_structured_content(
            "work", "experience",
            '{"start": "2019", "end": "2020", "company": "X", "hobby": "y"}',
        )
        assert any("Unknown keys" in e for e in errs)
        # 必选缺失
        _, errs = schema.validate_structured_content(
            "work", "experience",
            '{"start": "2019"}',
        )
        assert any("end" in e for e in errs) and any("company" in e for e in errs)
        # 时间格式非法
        _, errs = schema.validate_structured_content(
            "work", "experience",
            '{"start": "19年", "end": "2020", "company": "X"}',
        )
        assert any("start" in e and "invalid time format" in e for e in errs)
        # 非 JSON / 非对象
        assert not schema.validate_structured_content("work", "experience", "不是JSON")[0]
        assert not schema.validate_structured_content("work", "experience", "[1,2]")[0]
        # 非结构化字段
        assert not schema.validate_structured_content("work", "tech_stack", "{}")[0]


VALUES_SUB_TOPICS = [
    "values",
    "principles",
    "life_goal",
    "personality",
    "risk_preference",
    "decision_style",
    "emotion_triggers",
    "bottom_line",
]


class TestDefaultTemplateValues:
    """默认模板 values 领域的 5 个新字段（心理与价值观图谱）。"""

    def test_values_topic_has_eight_sub_topics(self):
        """values 领域含 8 个 sub_topics（原有 3 + 新增 5），保持声明顺序。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.sub_topic_names("values") == VALUES_SUB_TOPICS

    def test_new_plain_fields_valid_and_unique(self):
        """personality/risk_preference/decision_style：合法槽位且默认唯一。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        for sub in ("personality", "risk_preference", "decision_style"):
            assert schema.is_valid_slot("values", sub), sub
            assert schema.is_unique("values", sub), sub

    def test_emotion_triggers_structured_and_many(self):
        """emotion_triggers：可多条 + 结构化（trigger/reaction 均必填）。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.is_valid_slot("values", "emotion_triggers")
        assert not schema.is_unique("values", "emotion_triggers")
        assert schema.is_structured("values", "emotion_triggers")
        spec = schema.topics["values"].sub_topics["emotion_triggers"]
        assert list(spec.fields) == ["trigger", "reaction"]
        assert spec.fields["trigger"].required and spec.fields["reaction"].required

    def test_bottom_line_many(self):
        """bottom_line：合法槽位且可多条。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.is_valid_slot("values", "bottom_line")
        assert not schema.is_unique("values", "bottom_line")

    def test_emotion_triggers_validation_ok_and_rejected(self):
        """结构化校验反例：合法 JSON 通过；缺必填键 / 含未知键被拒。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        ok, errs = schema.validate_structured_content(
            "values", "emotion_triggers",
            '{"trigger": "前领导", "reaction": "暴躁"}',
        )
        assert ok and errs == []
        _, errs = schema.validate_structured_content(
            "values", "emotion_triggers", '{"trigger": "前领导"}',
        )
        assert any("reaction" in e for e in errs)
        _, errs = schema.validate_structured_content(
            "values", "emotion_triggers",
            '{"trigger": "加班", "reaction": "焦虑", "mood": "差"}',
        )
        assert any("Unknown keys" in e for e in errs)

    def test_field_guide_contains_new_fields(self):
        """新字段自动进入 field_guide（LLM 提示词字段清单）。"""
        guide = load_schema(DEFAULT_TEMPLATE).field_guide()
        for name in ("personality", "risk_preference", "decision_style", "emotion_triggers", "bottom_line"):
            assert name in guide
        assert "性格倾向" in guide
        assert "触发话题或情境" in guide and "content 为 JSON 对象" in guide


class TestDefaultTemplateInteraction:
    """默认模板 interaction 领域的 4 个字段（交互偏好）。"""

    def test_interaction_topic_exists_with_four_fields(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.has_topic("interaction")
        assert schema.sub_topic_names("interaction") == [
            "communication_style",
            "info_density",
            "feedback_style",
            "behavior_habits",
        ]

    def test_new_fields_valid_and_unique(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        for sub in ("communication_style", "info_density", "feedback_style"):
            assert schema.is_valid_slot("interaction", sub), sub
            assert schema.is_unique("interaction", sub), sub

    def test_behavior_habits_many(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.is_valid_slot("interaction", "behavior_habits")
        assert not schema.is_unique("interaction", "behavior_habits")

    def test_field_guide_contains_interaction(self):
        guide = load_schema(DEFAULT_TEMPLATE).field_guide()
        assert "interaction 交互偏好" in guide
        for name in ("communication_style", "info_density", "feedback_style", "behavior_habits"):
            assert name in guide


class TestDefaultTemplateGoals:
    """默认模板 goals 领域的 short_term_goal 结构化字段。"""

    def test_goals_topic_exists_with_field(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.has_topic("goals")
        assert schema.sub_topic_names("goals") == ["short_term_goal"]

    def test_short_term_goal_many_structured(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.is_valid_slot("goals", "short_term_goal")
        assert not schema.is_unique("goals", "short_term_goal")
        assert schema.is_structured("goals", "short_term_goal")
        spec = schema.topics["goals"].sub_topics["short_term_goal"]
        assert list(spec.fields) == ["goal", "deadline", "domain"]
        assert spec.fields["goal"].required and spec.fields["deadline"].required
        assert spec.fields["deadline"].format == "time"
        assert not spec.fields["domain"].required and spec.fields["domain"].format is None

    def test_short_term_goal_validation_ok_and_rejected(self):
        """合法 JSON 通过；缺必填键 / 时间格式非法 / 含未知键被拒。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        ok, errs = schema.validate_structured_content(
            "goals", "short_term_goal",
            '{"goal": "通过 PMP 考试", "deadline": "2026-09", "domain": "职业"}',
        )
        assert ok and errs == []
        # 缺必填键
        _, errs = schema.validate_structured_content("goals", "short_term_goal", '{"goal": "减重"}')
        assert any("deadline" in e for e in errs)
        _, errs = schema.validate_structured_content("goals", "short_term_goal", '{"deadline": "2026-09"}')
        assert any("goal" in e for e in errs)
        # 时间格式非法（相对表述"下个月"）
        _, errs = schema.validate_structured_content(
            "goals", "short_term_goal", '{"goal": "a", "deadline": "下个月"}',
        )
        assert any("invalid time format" in e for e in errs)
        # 未知键
        _, errs = schema.validate_structured_content(
            "goals", "short_term_goal",
            '{"goal": "a", "deadline": "2026-09", "progress": "50%"}',
        )
        assert any("Unknown keys" in e for e in errs)


class TestDefaultTemplateBasicEduFields:
    """默认模板基础/家庭/工作/教育领域的补充字段与 job_search 结构化升级。"""

    def test_basic_info_new_fields_valid_unique(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        for sub in ("cultural_background", "language_habit", "life_stage"):
            assert schema.is_valid_slot("basic_info", sub), sub
            assert schema.is_unique("basic_info", sub), sub

    def test_family_relationships_structured_and_many(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.is_valid_slot("family", "relationships")
        assert not schema.is_unique("family", "relationships")
        assert schema.is_structured("family", "relationships")
        spec = schema.topics["family"].sub_topics["relationships"]
        assert spec.fields["relation"].required and spec.fields["quality"].required

    def test_family_relationships_validation(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        ok, errs = schema.validate_structured_content(
            "family", "relationships", '{"relation": "母亲", "quality": "关系紧张"}',
        )
        assert ok and errs == []
        _, errs = schema.validate_structured_content("family", "relationships", '{"relation": "母亲"}')
        assert any("quality" in e for e in errs)
        _, errs = schema.validate_structured_content(
            "family", "relationships", '{"relation": "a", "quality": "b", "closeness": "高"}',
        )
        assert any("Unknown keys" in e for e in errs)

    def test_work_soft_skill_many_and_edu_focus_unique(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.is_valid_slot("work", "soft_skill")
        assert not schema.is_unique("work", "soft_skill")
        assert schema.is_valid_slot("edu", "focus")
        assert schema.is_unique("edu", "focus")

    def test_experience_leave_reason_backward_compatible(self):
        """experience 加可选键 leave_reason：无该键的旧格式仍合法（非破坏）。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        ok, errs = schema.validate_structured_content(
            "work", "experience",
            '{"start": "2019", "end": "2023", "company": "华为", "position": "工程师"}',
        )
        assert ok and errs == []
        ok, errs = schema.validate_structured_content(
            "work", "experience",
            '{"start": "2019", "end": "2023", "company": "华为", "position": "工程师", '
            '"leave_reason": "追求更大平台"}',
        )
        assert ok and errs == []

    def test_job_search_structured_unique_all_keys_optional(self):
        """job_search 升级：唯一 + 结构化，6 个键全部可选。"""
        schema = load_schema(DEFAULT_TEMPLATE)
        assert schema.is_valid_slot("work", "job_search")
        assert schema.is_unique("work", "job_search")
        assert schema.is_structured("work", "job_search")
        spec = schema.topics["work"].sub_topics["job_search"]
        assert list(spec.fields) == [
            "status",
            "expected_industry",
            "expected_position",
            "expected_salary",
            "work_intensity",
            "forbidden_companies",
        ]
        assert all(not f.required for f in spec.fields.values())

    def test_job_search_validation_ok_and_rejected(self):
        schema = load_schema(DEFAULT_TEMPLATE)
        ok, errs = schema.validate_structured_content(
            "work", "job_search", '{"status": "主动求职", "expected_position": "算法工程师"}',
        )
        assert ok and errs == []
        # 升级遗留的旧纯文本记录：非 JSON 对象 → 校验失败（mex doctor 会报告）
        _, errs = schema.validate_structured_content("work", "job_search", "想去大厂做 AI")
        assert any("JSON" in e for e in errs)
        # 未知键
        _, errs = schema.validate_structured_content("work", "job_search", '{"status": "a", "salary_floor": "50w"}')
        assert any("Unknown keys" in e for e in errs)
