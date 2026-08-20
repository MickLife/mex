"""记忆条目数据结构、枚举与时间工具。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Confidence(StrEnum):
    """置信度五档枚举（架构文档 §3.3）。"""

    CONFIRMED = "confirmed"  # 权重 1.0：经用户人工确认
    EXPLICIT = "explicit"  # 权重 0.9：用户原话明确陈述
    INFERRED = "inferred"  # 权重 0.6：证据链清晰的推导
    SPECULATED = "speculated"  # 权重 0.4：证据不充分的推测
    UNCERTAIN = "uncertain"  # 权重 0.2：几乎无直接证据的猜测


CONFIDENCE_WEIGHT: dict[Confidence, float] = {
    Confidence.CONFIRMED: 1.0,
    Confidence.EXPLICIT: 0.9,
    Confidence.INFERRED: 0.6,
    Confidence.SPECULATED: 0.4,
    Confidence.UNCERTAIN: 0.2,
}

LLM_ALLOWED_CONFIDENCE: tuple[Confidence, ...] = (
    Confidence.EXPLICIT,
    Confidence.INFERRED,
    Confidence.SPECULATED,
    Confidence.UNCERTAIN,
)

_UTC_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso_utc() -> str:
    """返回当前 UTC 时间的 ISO 8601 秒级字符串，如 ``2026-08-06T08:30:00Z``。"""
    return datetime.now(UTC).replace(microsecond=0).strftime(_UTC_ISO_FORMAT)


def to_local_str(utc_iso: str) -> str:
    """UTC ISO 字符串 → 本地时区可读显示（CLI 输出用）。"""
    dt = datetime.strptime(utc_iso, _UTC_ISO_FORMAT).replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_expires(text: str) -> str:
    """本地日期/日期时间 → 存库用的 UTC ISO 秒级字符串（``--expires`` 参数用）。

    接受 ``YYYY-MM-DD``（取该日结束边界 23:59:59 本地）或
    ``YYYY-MM-DD HH:MM:SS``（取该时刻），统一转 UTC ISO。

    Args:
        text: ``--expires`` 参数原样值。

    Returns:
        UTC ISO 秒级字符串。

    Raises:
        ValueError: 格式非法（不是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）。
    """
    text = text.strip()
    try:
        if " " in text:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone()
        else:
            dt = datetime.strptime(text, "%Y-%m-%d").astimezone().replace(hour=23, minute=59, second=59)
    except ValueError as exc:
        raise ValueError(
            f"--expires must be YYYY-MM-DD or YYYY-MM-DD HH:MM:SS (local time), got: {text!r}",
        ) from exc
    return dt.astimezone(UTC).replace(microsecond=0).strftime(_UTC_ISO_FORMAT)


@dataclass(frozen=True)
class Memory:
    """统一记忆条目（架构文档 §3.1）。

    画像槽位与画像外记录共用此结构：
    - 画像槽位条目：sub_topic 非空、topic 非空，且 ``(topic, sub_topic)``
      必须在 schema.yaml 定义内（合法性与投影由 rules.validate 与 Schema 把关）；
    - 画像外记录：sub_topic 为空，topic 可空，按时间索引，不参与画像投影。

    Raises:
        ValueError: 构造时 sub_topic 非空但 topic 为空时抛出。
    """

    id: str
    topic: str | None
    sub_topic: str | None
    content: str
    is_ai_inferred: bool
    confidence: Confidence
    evidence: str | None
    created_at: str
    updated_at: str
    forgotten_at: str | None = None
    forgotten_reason: str | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        """构造期校验：画像槽位条目（sub_topic 非空）必须提供 topic。"""
        if self.sub_topic and not self.topic:
            raise ValueError("Profile slot entries (non-empty sub_topic) must provide a topic")

    def is_forgotten(self) -> bool:
        """是否处于软删除状态。"""
        return self.forgotten_at is not None

    def is_expired_at(self, now: str) -> bool:
        """是否已过期（``now`` 为 UTC ISO；expires_at 为空永不过期）。"""
        return self.expires_at is not None and self.expires_at <= now

    def in_profile(self) -> bool:
        """是否为画像槽位条目（sub_topic 非空即视为画像内，槽位合法性由写入校验把关）。"""
        return bool(self.topic and self.sub_topic)

    def slot_key(self) -> tuple[str, str] | None:
        """画像槽位返回 ``(topic, sub_topic)`` 槽位键；画像外记录（sub_topic 为空）返回 None。"""
        if self.topic and self.sub_topic:
            return (self.topic, self.sub_topic)
        return None

    def to_dict(self) -> dict[str, Any]:
        """导出 / JSON 输出用：全部字段，枚举转为字符串。"""
        return {
            "id": self.id,
            "topic": self.topic,
            "sub_topic": self.sub_topic,
            "content": self.content,
            "is_ai_inferred": self.is_ai_inferred,
            "confidence": self.confidence.value,
            "evidence": self.evidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "forgotten_at": self.forgotten_at,
            "forgotten_reason": self.forgotten_reason,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Memory:
        """解析 :meth:`to_dict` 产物（import restore 用）。

        Raises:
            ValueError: 字段缺失或取值非法时抛出，指明出错字段。
        """
        try:
            return cls(
                id=d["id"],
                topic=d.get("topic"),
                sub_topic=d.get("sub_topic"),
                content=d["content"],
                is_ai_inferred=cls._parse_bool(d["is_ai_inferred"]),
                confidence=Confidence(d["confidence"]),
                evidence=d.get("evidence"),
                created_at=d["created_at"],
                updated_at=d["updated_at"],
                forgotten_at=d.get("forgotten_at"),
                forgotten_reason=d.get("forgotten_reason"),
                expires_at=d.get("expires_at"),
            )
        except KeyError as exc:
            raise ValueError(f"Missing memory field: {exc.args[0]}") from exc
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid memory field: {exc}") from exc

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        """容忍 bool / 0/1 两种存储形态。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError(f"Invalid value for is_ai_inferred: {value!r}")
