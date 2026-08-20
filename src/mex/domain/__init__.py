"""领域层：全系统共用的数据结构、枚举、schema 与写入规则（M1）。

纯内存逻辑，无数据库 I/O；仅 ``load_schema`` 一处读取文件。
"""

from mex.domain.memory import (
    CONFIDENCE_WEIGHT,
    LLM_ALLOWED_CONFIDENCE,
    Confidence,
    Memory,
    now_iso_utc,
    to_local_str,
)
from mex.domain.rules import validate
from mex.domain.schema import Schema, SchemaError, SubTopicSpec, TopicSpec, load_schema

__all__ = [
    "CONFIDENCE_WEIGHT",
    "LLM_ALLOWED_CONFIDENCE",
    "Confidence",
    "Memory",
    "Schema",
    "SchemaError",
    "SubTopicSpec",
    "TopicSpec",
    "load_schema",
    "now_iso_utc",
    "to_local_str",
    "validate",
]
