"""M1 领域层：memory.py 单测。"""

import re

import pytest

from mex.domain.memory import (
    CONFIDENCE_WEIGHT,
    LLM_ALLOWED_CONFIDENCE,
    Confidence,
    Memory,
    now_iso_utc,
    to_local_str,
)


def _make_memory(**kwargs):
    """构造一条合法的画像槽位记忆，便于测试覆盖各字段。"""
    defaults = {
        "id": "m-001",
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "is_ai_inferred": False,
        "confidence": Confidence.EXPLICIT,
        "evidence": None,
        "created_at": "2026-08-06T08:30:00Z",
        "updated_at": "2026-08-06T08:30:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


class TestConfidence:
    def test_confidence_values(self):
        assert Confidence.CONFIRMED.value == "confirmed"
        assert Confidence.EXPLICIT.value == "explicit"
        assert Confidence.INFERRED.value == "inferred"
        assert Confidence.SPECULATED.value == "speculated"
        assert Confidence.UNCERTAIN.value == "uncertain"

    def test_confidence_weight(self):
        assert CONFIDENCE_WEIGHT == {
            Confidence.CONFIRMED: 1.0,
            Confidence.EXPLICIT: 0.9,
            Confidence.INFERRED: 0.6,
            Confidence.SPECULATED: 0.4,
            Confidence.UNCERTAIN: 0.2,
        }

    def test_llm_allowed_confidence_excludes_confirmed(self):
        assert Confidence.CONFIRMED not in LLM_ALLOWED_CONFIDENCE
        assert len(LLM_ALLOWED_CONFIDENCE) == 4


class TestTime:
    def test_now_iso_utc_format(self):
        value = now_iso_utc()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)

    def test_to_local_str(self):
        result = to_local_str("2026-08-06T08:30:00Z")
        assert result.startswith("2026-08-06 ")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", result)


class TestMemoryConstruction:
    def test_ok_profile_slot(self):
        m = _make_memory()
        assert m.in_profile() is True
        assert m.slot_key() == ("work", "company")

    def test_ok_outside_record(self):
        m = _make_memory(topic="work", sub_topic=None)
        assert m.sub_topic is None
        assert m.in_profile() is False
        assert m.slot_key() is None

    def test_ok_outside_record_no_topic(self):
        m = _make_memory(topic=None, sub_topic=None)
        assert m.topic is None and m.sub_topic is None
        assert m.slot_key() is None

    def test_sub_topic_requires_topic(self):
        with pytest.raises(ValueError, match="topic"):
            _make_memory(topic=None, sub_topic="company")

    def test_is_forgotten(self):
        assert not _make_memory().is_forgotten()
        assert _make_memory(forgotten_at="2026-08-07T00:00:00Z").is_forgotten()


class TestDictRoundTrip:
    def test_to_dict_fields(self):
        m = _make_memory()
        d = m.to_dict()
        assert d["topic"] == "work"
        assert d["sub_topic"] == "company"
        assert d["confidence"] == "explicit"
        assert d["is_ai_inferred"] is False
        assert d["forgotten_at"] is None

    def test_roundtrip(self):
        m = _make_memory(
            id="m-007",
            is_ai_inferred=True,
            confidence=Confidence.SPECULATED,
            evidence="用户提到今天跌得有点心慌",
        )
        restored = Memory.from_dict(m.to_dict())
        assert restored == m

    def test_roundtrip_forgotten(self):
        m = _make_memory(forgotten_at="2026-08-07T00:00:00Z", forgotten_reason="测试")
        assert Memory.from_dict(m.to_dict()) == m

    def test_roundtrip_outside_record(self):
        m = _make_memory(topic="work", sub_topic=None)
        assert Memory.from_dict(m.to_dict()) == m

    def test_from_dict_missing_id(self):
        d = _make_memory().to_dict()
        del d["id"]
        with pytest.raises(ValueError, match="id"):
            Memory.from_dict(d)

    def test_from_dict_invalid_confidence(self):
        d = _make_memory().to_dict()
        d["confidence"] = "very_confident"
        with pytest.raises(ValueError):
            Memory.from_dict(d)

    def test_from_dict_invalid_bool(self):
        d = _make_memory().to_dict()
        d["is_ai_inferred"] = "yes"
        with pytest.raises(ValueError):
            Memory.from_dict(d)

    def test_from_dict_int_bool_forms(self):
        for raw in (0, 1):
            d = _make_memory().to_dict()
            d["is_ai_inferred"] = raw
            assert Memory.from_dict(d).is_ai_inferred is bool(raw)

    def test_from_dict_ignores_unknown_keys(self):
        d = _make_memory().to_dict()
        d["layer"] = "stable"  # 旧格式导出文件可能携带的冗余键，应被忽略
        assert Memory.from_dict(d) == _make_memory()
