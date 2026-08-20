"""M1 领域层：rules.py 单测（覆盖架构 ADR-6 例 1~6 与 §3.3）。"""

from mex.domain.memory import Confidence
from mex.domain.rules import validate
from mex.domain.schema import Schema, SubTopicSpec, TopicSpec


def _schema():
    """迷你 schema：work.company / work.tech_stack(可多条) / finance。"""
    return Schema(
        topics={
            "work": TopicSpec(
                name="work",
                description="工作",
                sub_topics={
                    "company": SubTopicSpec(name="company"),
                    "tech_stack": SubTopicSpec(name="tech_stack", unique=False),
                },
            ),
            "finance": TopicSpec(name="finance", description="财务"),
        },
    )


def _validate(**kwargs):
    defaults = {
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "confidence": Confidence.EXPLICIT,
        "schema": _schema(),
        "allow_confirmed": True,
    }
    defaults.update(kwargs)
    return validate(**defaults)


class TestPassCases:
    def test_profile_slot_ok(self):
        assert _validate() == []

    def test_many_slot_ok(self):
        assert _validate(topic="work", sub_topic="tech_stack", content="Python") == []

    def test_outside_record_no_sub_topic(self):
        """画像外记录：sub_topic 省略合法。"""
        assert _validate(topic="finance", sub_topic=None, content="计划配置重疾险") == []

    def test_outside_record_with_topic_ok(self):
        assert _validate(topic="finance", sub_topic=None, content="今天基金跌了") == []

    def test_outside_record_without_topic_ok(self):
        assert _validate(topic=None, sub_topic=None, content="随手记一笔") == []

    def test_user_confirmed_allowed(self):
        assert _validate(confidence=Confidence.CONFIRMED) == []


class TestRejectCases:
    def test_topic_not_in_schema(self):
        errors = _validate(topic="job", sub_topic="company")
        assert any("job" in e for e in errors)

    def test_sub_topic_not_in_schema(self):
        errors = _validate(sub_topic="employer")
        assert any("employer" in e for e in errors)

    def test_sub_topic_requires_topic(self):
        errors = _validate(topic=None, sub_topic="company")
        assert any("topic" in e for e in errors)

    def test_outside_record_topic_not_in_schema(self):
        errors = _validate(topic="hobby", sub_topic=None)
        assert any("hobby" in e for e in errors)

    def test_empty_content(self):
        errors = _validate(content="   ")
        assert any("content" in e for e in errors)

    def test_invalid_confidence(self):
        errors = _validate(confidence="bogus")  # type: ignore[arg-type]
        assert any("confidence" in e for e in errors)


class TestLLMConfidenceGate:
    def test_llm_confirmed_rejected(self):
        errors = _validate(confidence=Confidence.CONFIRMED, allow_confirmed=False)
        assert any("confirmed" in e for e in errors)

    def test_llm_lower_tiers_allowed(self):
        for conf in (Confidence.EXPLICIT, Confidence.INFERRED, Confidence.SPECULATED, Confidence.UNCERTAIN):
            assert _validate(confidence=conf, allow_confirmed=False) == []


class TestRetireAction:
    """retire 只作用于可多条槽位。"""

    def test_retire_many_slot_ok(self):
        assert _validate(topic="work", sub_topic="tech_stack", content="Python", action="retire") == []

    def test_retire_unique_slot_rejected(self):
        errors = _validate(topic="work", sub_topic="company", content="华为", action="retire")
        assert any("retire" in e and "multi-value slots" in e for e in errors)

    def test_retire_missing_slot_info_rejected(self):
        errors = _validate(topic="work", sub_topic=None, content="x", action="retire")
        assert any("topic" in e and "sub_topic" in e for e in errors)
        errors = _validate(topic=None, sub_topic="tech_stack", content="x", action="retire")
        assert any("topic" in e and "sub_topic" in e for e in errors)

    def test_other_actions_unchanged(self):
        for action in ("new", "update", "uncertain_update", None):
            assert _validate(action=action) == []
