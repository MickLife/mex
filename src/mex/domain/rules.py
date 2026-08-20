"""写入规则校验：任何写入路径（手动 / LLM / import）都必须经过这里。

规则来源：架构文档 ADR-6 例 1~6 与 §3.3（置信度档位权限）。
画像投影模型：画像由 schema.yaml 定义——sub_topic 非空的条目是画像槽位，
必须落在 schema 声明的槽位内；sub_topic 为空的条目是画像外记录（topic 可空，
但填了必须合法，保证领域检索可靠）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mex.domain.memory import Confidence

if TYPE_CHECKING:
    from mex.domain.schema import Schema


def validate(  # noqa: PLR0913 - 数据校验函数参数密集，打包 dataclass 反而牺牲可读性
    *,
    topic: str | None,
    sub_topic: str | None,
    content: str,
    confidence: Confidence,
    schema: Schema,
    allow_confirmed: bool,
    action: str | None = None,
) -> list[str]:
    """校验一条记忆的写入合法性。

    Args:
        topic: 领域主题。
        sub_topic: 画像字段；非空时该条目为画像槽位，须在 schema 内。
        content: 记忆内容。
        confidence: 置信度档位。
        schema: 当前 schema。
        allow_confirmed: 是否允许 ``confirmed`` 档位（人工写入允许；LLM 来源禁止）。
        action: 可选写入动作（extract 专用）：``retire`` 只能指向可多条画像槽位。

    Returns:
        违规原因列表；空列表表示通过。
    """
    errors: list[str] = []

    if confidence not in Confidence:
        return [f"Invalid confidence: {confidence!r}"]
    if not content or not content.strip():
        errors.append("content must not be empty")
    if not allow_confirmed and confidence is Confidence.CONFIRMED:
        errors.append("LLM-sourced memories cannot use the 'confirmed' level")

    errors.extend(_validate_slot(topic, sub_topic, schema))
    errors.extend(_validate_retire(action, topic, sub_topic, schema))
    return errors


def _validate_slot(topic: str | None, sub_topic: str | None, schema: Schema) -> list[str]:
    """校验画像槽位合法性：sub_topic 非空须有 topic 且在 schema 内；画像外 topic 须合法。"""
    errors: list[str] = []
    if sub_topic:
        if not topic:
            errors.append("Profile slot entries (non-empty sub_topic) must provide a topic")
        elif not schema.is_valid_slot(topic, sub_topic):
            errors.append(f"Slot '{topic}.{sub_topic}' is not defined in schema.yaml")
    elif topic and not schema.has_topic(topic):
        errors.append(f"Topic '{topic}' is not defined in schema.yaml")
    return errors


def _validate_retire(action: str | None, topic: str | None, sub_topic: str | None, schema: Schema) -> list[str]:
    """提取校验 retire 动作：只指向可多条画像槽位（唯一槽位变更用 update）。"""
    if action != "retire":
        return []
    if not topic or not sub_topic:
        return ["retire requires topic and sub_topic (pointing to a specific record in a multi-value slot)"]
    if schema.is_unique(topic, sub_topic):
        return ["retire only applies to multi-value slots (use update for unique slots)"]
    return []
