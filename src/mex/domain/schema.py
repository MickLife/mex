"""schema.yaml 的加载、解析与校验（架构文档 ADR-6、§3.4）。

schema.yaml 定义画像的合法字段清单，是全部写入路径（手动 add、LLM 抽取、
import 恢复）的字段白名单。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from loguru import logger


class SchemaError(Exception):
    """schema.yaml 解析失败（文件缺失或结构非法）。"""


_TIME_RE = r"^\d{4}(-\d{2}(-\d{2})?)?$"
"""时间格式（format: time）：``YYYY``、``YYYY-MM`` 或 ``YYYY-MM-DD``。"""

_OPEN_END = {"至今", "今", "现在"}
"""time 格式的开放结束标记（结束时间未定时的合法写法，如工作至今）。"""


@dataclass(frozen=True)
class FieldSpec:
    """结构化字段（fields 声明）中单个 JSON 键的约束。

    Attributes:
        description: 该键的含义（展示名 / LLM 提示语）。
        required: True 时该键必须出现在每条记录中（缺失即校验失败）。
        format: 取值约束；``time`` 要求值为 ``YYYY``/``YYYY-MM``/``YYYY-MM-DD``；
            None 表示自由文本（不校验格式）。
    """

    description: str = ""
    required: bool = False
    format: str | None = None


@dataclass(frozen=True)
class SubTopicSpec:
    """单个画像字段（如 ``work.company``）。

    Attributes:
        name: 字段名。
        description: 字段含义（profile 展示名 / LLM 提示语）。
        unique: True 时该槽位只存一条有效记录；False 时允许多条独立记录（§3.5）。
        fields: 非空时为「结构化 JSON 字段」：每条记录 content 存一个 JSON 对象，
            键为 fields 的键、值类型统一为字符串、键序即展示/提示顺序，
            每个键的约束（必选/格式）由 FieldSpec 描述；None 表示普通纯文本字段。
    """

    name: str
    description: str = ""
    unique: bool = True  # False 时该槽位允许多条独立记录（§3.5，可多条字段）
    fields: dict[str, FieldSpec] | None = None


@dataclass(frozen=True)
class TopicSpec:
    """画像领域（如 ``work``），含其下全部字段。"""

    name: str
    description: str = ""
    sub_topics: dict[str, SubTopicSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class Schema:
    """加载完成的 schema：topic 名 → 定义。"""

    topics: dict[str, TopicSpec]

    def topic_names(self) -> list[str]:
        """全部领域名，按定义顺序。"""
        return list(self.topics.keys())

    def sub_topic_names(self, topic: str) -> list[str]:
        """某领域的全部字段名；领域不存在返回空列表。"""
        t = self.topics.get(topic)
        return list(t.sub_topics.keys()) if t else []

    def is_valid_slot(self, topic: str, sub_topic: str | None) -> bool:
        """``(topic, sub_topic)`` 是否为合法槽位；sub_topic 为空表示领域级槽位（topic 存在即合法）。"""
        t = self.topics.get(topic)
        return bool(t and (not sub_topic or sub_topic in t.sub_topics))

    def is_unique(self, topic: str, sub_topic: str) -> bool:
        """该槽位是否为唯一槽位（一个槽位只存一条有效记录）。

        可多条字段（``unique: false``）返回 False，允许多条独立记录共存。
        槽位不存在时返回 True（保守按唯一处理；实际由上层 :meth:`is_valid_slot` 先拦截）。
        """
        t = self.topics.get(topic)
        s = t.sub_topics.get(sub_topic) if t else None
        if s is None:
            return True
        return s.unique

    def is_structured(self, topic: str, sub_topic: str) -> bool:
        """该槽位是否为结构化 JSON 字段（content 存 JSON 对象）。

        结构化字段：`fields` 声明了键集；非结构化字段 content 为纯文本字符串。
        槽位不存在时返回 False（无字段声明即非结构化）。
        """
        t = self.topics.get(topic)
        s = t.sub_topics.get(sub_topic) if t else None
        return bool(s is not None and s.fields)

    def json_template(self, topic: str, sub_topic: str) -> str | None:
        """生成结构化字段的 JSON 模板文本（键 → 含义 + 约束标注，供 LLM 参考）。

        Returns:
            单行 JSON 对象模板；非结构化字段返回 None。约束标注：必选键值前置
            ``[必填]``，时间键值前置 ``[YYYY/YYYY-MM]``。
        """
        t = self.topics.get(topic)
        s = t.sub_topics.get(sub_topic) if t else None
        if s is None or not s.fields:
            return None
        template = {k: _field_hint(v) for k, v in s.fields.items()}
        return json.dumps(template, ensure_ascii=False)

    def has_topic(self, topic: str) -> bool:
        """领域是否存在（画像外记录只校验 topic 不校验 sub_topic 时使用）。"""
        return topic in self.topics

    def field_guide(self) -> str:
        """生成 LLM 提示词用的字段清单文本（架构 §7.1 第 2 部分）。"""
        lines: list[str] = []
        for topic in self.topics.values():
            header = topic.name if not topic.description else f"{topic.name} {topic.description}"
            lines.append(header)
            for sub in topic.sub_topics.values():
                line = f"  - {sub.name}"
                if sub.description:
                    line += f": {sub.description}"
                if not sub.unique:
                    line += "（可多条）"
                if sub.fields:
                    hint_template = json.dumps(
                        {k: _field_hint(v) for k, v in sub.fields.items()},
                        ensure_ascii=False,
                    )
                    line += f"，content 为 JSON 对象 {hint_template}"
                lines.append(line)
        return "\n".join(lines)

    def validate_structured_content(
        self,
        topic: str,
        sub_topic: str,
        content: str,
    ) -> tuple[bool, list[str]]:
        """校验结构化字段的一条 content（JSON 字符串）是否符合字段约束。

        校验项：① 必须是合法 JSON 对象；② 键必须在 fields 声明内；③ 必选键不得缺失；
        ④ 声明了 format 的键值须匹配格式。

        Args:
            topic: 领域名。
            sub_topic: 结构化字段名。
            content: 记录的 JSON 字符串。

        Returns:
            (是否通过, 违规原因列表)；非结构化字段或槽位不存在返回 (False, [提示])，
            调用方应在调用前用 :meth:`is_structured` 把关。
        """
        t = self.topics.get(topic)
        s = t.sub_topics.get(sub_topic) if t else None
        if s is None or not s.fields:
            return False, [f"Field '{topic}.{sub_topic}' is not a structured field"]
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return False, [f"Field '{topic}.{sub_topic}' content is not valid JSON"]
        if not isinstance(data, dict):
            return False, [f"Field '{topic}.{sub_topic}' content must be a JSON object (not array/text)"]

        errors: list[str] = []
        known = set(s.fields)
        unknown = set(data) - known
        if unknown:
            errors.append(f"Unknown keys: {sorted(unknown)}")
        for key, spec in s.fields.items():
            if spec.required and key not in data:
                errors.append(f"Missing required key: {key} ({spec.description})")
            value = data.get(key)
            if spec.format == "time" and value is not None and str(value).strip() != "":
                text = str(value).strip()
                if text not in _OPEN_END and not re.fullmatch(_TIME_RE, text):
                    errors.append(
                        f"Key '{key}' has invalid time format (expected YYYY/YYYY-MM/YYYY-MM-DD or '至今'): {value!r}",
                    )
        return (not errors), errors


def load_schema(path: str) -> Schema:
    """读取并解析 schema.yaml。

    Args:
        path: schema.yaml 的绝对或相对路径。

    Returns:
        解析后的 Schema。

    Raises:
        SchemaError: 文件不可读或结构非法（错误信息含路径与出错位置）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        raise SchemaError(f"Failed to read {path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("topics"), dict):
        raise SchemaError(f"{path}: top level must be a 'topics' dict")

    topics: dict[str, TopicSpec] = {}
    for topic_name, topic_data in data["topics"].items():
        topics[topic_name] = _parse_topic(path, topic_name, topic_data)
    return Schema(topics=topics)


def _parse_topic(path: str, topic_name: str, topic_data: Any) -> TopicSpec:
    """解析单个 topic 节点，容忍 dict / None / 字符串三种形态。"""
    if topic_data is None:
        topic_data = {}
    if not isinstance(topic_data, dict):
        raise SchemaError(f"{path}: topic '{topic_name}' must be a dict or omitted")

    description = str(topic_data.get("description", ""))
    subs_raw = topic_data.get("sub_topics")
    if subs_raw is None:
        subs_raw = {}
    if not isinstance(subs_raw, dict):
        raise SchemaError(f"{path}: sub_topics of topic '{topic_name}' must be a dict")

    sub_topics: dict[str, SubTopicSpec] = {}
    for sub_name, sub_data in subs_raw.items():
        raw: Any = {} if sub_data is None else sub_data
        if isinstance(raw, str):
            sub_topics[sub_name] = SubTopicSpec(name=sub_name, description=raw)
        elif isinstance(raw, dict):
            multiple = bool(raw.get("multiple", False))
            has_unique = "unique" in raw
            unique = bool(raw["unique"]) if has_unique else not multiple
            if multiple and not has_unique:
                logger.warning(
                    "schema field '{}.{}' uses deprecated 'multiple: true', "
                    "treated as 'unique: false'; please use 'unique: false' instead.",
                    topic_name,
                    sub_name,
                )
            fields = _parse_fields(path, topic_name, sub_name, raw)
            sub_topics[sub_name] = SubTopicSpec(
                name=sub_name,
                description=str(raw.get("description", "")),
                unique=unique,
                fields=fields,
            )
        else:
            raise SchemaError(f"{path}: sub_topic '{topic_name}.{sub_name}' must be a dict or string")
    return TopicSpec(name=topic_name, description=description, sub_topics=sub_topics)


def _parse_fields(path: str, topic_name: str, sub_name: str, raw: dict) -> dict[str, FieldSpec] | None:
    """解析结构化字段声明 ``fields``：键名 → FieldSpec（必选/格式约束）。

    两种形态兼容：
    - 字符串：``summary: 内容概要`` → 纯含义，可选项、自由格式；
    - 字典：``start: {description: 开始年份, format: time, required: true}``。

    Returns:
        键序保持声明顺序的字段字典；未声明 ``fields`` 返回 None（非结构化字段）。
    """
    raw_fields = raw.get("fields")
    if raw_fields is None:
        return None
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise SchemaError(
            f"{path}: fields of structured field '{topic_name}.{sub_name}' must be a non-empty dict",
        )
    result: dict[str, FieldSpec] = {}
    for key, value in raw_fields.items():
        spec = _parse_field_spec(path, topic_name, sub_name, key, value)
        result[str(key)] = spec
    return result


def _parse_field_spec(
    path: str,
    topic_name: str,
    sub_name: str,
    key: object,
    value: Any,
) -> FieldSpec:
    """解析 fields 中单个键的约束声明（字符串或字典两种形态）。"""
    if isinstance(value, str):
        return FieldSpec(description=value)
    if isinstance(value, dict):
        fmt = value.get("format")
        if fmt is not None and fmt not in ("time",):
            raise SchemaError(
                f"{path}: key '{key}' of field '{topic_name}.{sub_name}' has invalid format"
                f" ({fmt!r}, allowed: time)",
            )
        return FieldSpec(
            description=str(value.get("description", "")),
            required=bool(value.get("required", False)),
            format=str(fmt) if fmt is not None else None,
        )
    raise SchemaError(
        f"{path}: key '{key}' of field '{topic_name}.{sub_name}' must be a string or dict",
    )


def _field_hint(spec: FieldSpec) -> str:
    """字段约束 → 模板提示文本（供 json_template / field_guide）。"""
    hint = spec.description
    if spec.format == "time":
        hint = f"[YYYY/YYYY-MM]{hint}"
    if spec.required:
        hint = f"[必填]{hint}"
    return hint
