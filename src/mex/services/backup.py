"""备份与迁移服务（架构文档 §6.4、ADR-9、§7.4）。

实现 ``mex export`` / ``mex import`` / ``mex stats`` 的核心逻辑：

- 导出：JSON（机器可读，含全部字段与软删状态，还原语义完整）与
  Markdown（人类可读分节文档）；
- 恢复导入：不经过 LLM，逐条按架构 §6.4 冲突表处理，全部写入在单个事务内；
- 迁移导入：复用 ``extract_dialogue`` 走 LLM 抽取（source_file=None，不记录增量状态）；
- 统计：画像内/画像外计数、待审查数、库大小与 LLM 累计用量。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mex.cli.common import ExternalError
from mex.config import get_mex_home
from mex.domain.memory import Memory, now_iso_utc
from mex.domain.schema import SchemaError, load_schema
from mex.services.extract import extract_dialogue
from mex.store import history, memories
from mex.store.connection import transaction

if TYPE_CHECKING:
    import sqlite3

    from mex.domain.schema import Schema
    from mex.llm.client import LLMClient
    from mex.services.extract import ExtractResult

EXPORT_VERSION = 1
_FORGOTTEN_REASON = "import overwrite"


@dataclass(frozen=True)
class ExportReport:
    """一次导出的结果摘要。"""

    path: str
    count: int
    forgotten_count: int


@dataclass(frozen=True)
class ImportReport:
    """一次恢复导入的结果摘要。

    Attributes:
        imported: 成功写入的条数（新增 + 覆盖）。
        skipped: 跳过的条数（内容一致、冲突跳过、非法条目）。
        conflicts: 冲突清单，每项 ``{"reason": str, "id": str | None, "detail": str}``。
    """

    imported: int
    skipped: int
    conflicts: list[dict]


@dataclass
class _RestoreCounts:
    """import_restore 内部可变计数（ImportReport 为 frozen，无法原地累加）。"""

    imported: int = 0
    skipped: int = 0
    conflicts: list[dict] = field(default_factory=list)


def export_json(conn: sqlite3.Connection, output_path: str) -> ExportReport:
    """全量导出为 JSON（含软删条目，字段完整，还原语义无损）。

    Args:
        conn: 数据库连接。
        output_path: 输出文件路径（父目录不存在时自动创建）。

    Returns:
        导出摘要（路径 / 总条数 / 软删条数）。
    """
    all_memories = memories.list_all(conn, include_forgotten=True)
    payload = {
        "version": EXPORT_VERSION,
        "exported_at": now_iso_utc(),
        "memories": [m.to_dict() for m in all_memories],
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return ExportReport(str(path), len(all_memories), _count_forgotten(all_memories))


def export_markdown(conn: sqlite3.Connection, output_path: str) -> ExportReport:
    """导出为人类可读的 Markdown 分节文档（架构文档 §6.4 示例格式）。

    画像槽位条目（sub_topic 非空）按领域分节、槽位用 sub_topic 名展示、
    多值字段逗号拼接、内容换行替换为空格（不转义）；画像外记录（sub_topic 为空）
    按时间线分节；已遗忘条目单独成节并标注遗忘时间。

    Args:
        conn: 数据库连接。
        output_path: 输出文件路径（父目录不存在时自动创建）。

    Returns:
        导出摘要。
    """
    all_memories = memories.list_all(conn, include_forgotten=True)
    active = [m for m in all_memories if not m.is_forgotten()]
    forgotten = [m for m in all_memories if m.is_forgotten()]
    lines = [f"# meX memory export ({datetime.now(UTC).strftime('%Y-%m-%d')})"]
    lines.extend(_profile_section(active))
    lines.extend(_timeline_section(active))
    lines.extend(_forgotten_section(forgotten))
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ExportReport(str(path), len(all_memories), len(forgotten))


def import_restore(
    conn: sqlite3.Connection,
    import_path: str,
    *,
    on_conflict: Literal["skip", "overwrite"],
) -> ImportReport:
    """从导出 JSON 恢复（架构 §6.4 冲突表，逐条处理，全部写入在单个事务内）。

    冲突处理顺序：同 id 且内容一致 → 跳过；同 id 内容不同 → skip 跳过 / overwrite
    覆盖（旧值入 history）；槽位冲突 → skip 跳过 / overwrite 旧条目软删后插入新条目；
    槽位不在当前 schema → 一律跳过（schema 约束不可被导入绕过）。

    Args:
        conn: 数据库连接。
        import_path: 导出文件路径。
        on_conflict: 冲突处理策略：skip 跳过（默认，安全优先）；overwrite 覆盖。

    Returns:
        恢复摘要：新增 / 跳过 / 冲突清单。

    Raises:
        ExternalError: 文件不可读、不是合法 JSON 或 version 不匹配（不落任何数据）。
    """
    payload = _load_export_json(import_path)
    schema = _load_current_schema()
    counts = _RestoreCounts()
    with transaction(conn):
        for raw in payload["memories"]:
            try:
                entry = Memory.from_dict(raw)
            except ValueError as exc:
                counts.skipped += 1
                entry_id = raw.get("id") if isinstance(raw, dict) else None
                counts.conflicts.append({"reason": "invalid entry", "id": entry_id, "detail": str(exc)})
                continue
            _restore_one(conn, entry, schema, on_conflict, counts)
    return ImportReport(imported=counts.imported, skipped=counts.skipped, conflicts=counts.conflicts)


def import_extract(
    conn: sqlite3.Connection,
    import_path: str,
    *,
    client: LLMClient,
    schema: Schema,
) -> ExtractResult:
    """从文本/Markdown 文件抽取迁移（架构 §6.4：与 extract 相同流程，不记录增量状态）。

    Args:
        conn: 数据库连接。
        import_path: 待迁移文件路径。
        client: LLM 客户端（可注入测试替身）。
        schema: 当前 schema（字段白名单）。

    Returns:
        抽取统计（同 ``mex extract``）。

    Raises:
        LLMError: LLM 调用失败（数据库无任何变化）。
        OSError: 文件读取失败。
    """
    text = Path(import_path).read_text(encoding="utf-8")
    return extract_dialogue(
        conn,
        text=text,
        source_file=None,
        client=client,
        schema=schema,
    )


def stats(conn: sqlite3.Connection, mex_home: str) -> dict:
    """统计：画像内/画像外有效条目数、待审查数、数据库大小与 LLM 累计用量（架构 §7.4）。

    Args:
        conn: 数据库连接。
        mex_home: 数据目录（含 mex.db 与 schema.yaml）。

    Returns:
        ``{"profile": {"profile": n, "outside": n}, "pending_review": n,
        "db_size_bytes": n, "llm_usage": {"prompt_tokens": n, "completion_tokens": n, "calls": n}}``。
    """
    shapes = memories.count_by_shape(conn)
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        "COUNT(*) AS calls FROM llm_usage",
    ).fetchone()
    return {
        "profile": {
            "profile": shapes.get("profile", 0),
            "outside": shapes.get("outside", 0),
        },
        "pending_review": memories.count_pending_review(conn),
        "db_size_bytes": os.path.getsize(str(Path(mex_home) / "mex.db")),
        "llm_usage": {
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "calls": int(row["calls"]),
        },
    }


def _restore_one(
    conn: sqlite3.Connection,
    entry: Memory,
    schema: Schema,
    on_conflict: Literal["skip", "overwrite"],
    counts: _RestoreCounts,
) -> None:
    """单条导入：按冲突表决定写入或跳过（调用方负责事务）。"""
    existing = memories.get(conn, entry.id, include_forgotten=True)
    if existing is not None:
        if existing.content == entry.content:
            counts.skipped += 1
        elif on_conflict == "overwrite":
            _overwrite_same_id(conn, existing, entry)
            counts.imported += 1
        else:
            counts.skipped += 1
            counts.conflicts.append(
                {
                    "reason": "same id different content",
                    "id": entry.id,
                    "detail": f"Entry with same id but different content (existing: {existing.content[:30]}), skipped",
                },
            )
        return
    slot = entry.slot_key()
    if slot is not None and not schema.is_valid_slot(slot[0], slot[1]):
        counts.skipped += 1
        counts.conflicts.append(
            {
                "reason": "slot not in schema",
                "id": entry.id,
                "detail": (
                    f"Slot {_slot_label(entry)} is not in the current schema; edit schema.yaml first "
                    "(import cannot bypass schema constraints)"
                ),
            },
        )
        return
    if slot is not None and schema.is_unique(slot[0], slot[1]):
        # 唯一槽位：查槽位冲突；可多条槽位（unique: false）直接 insert，不查冲突
        holder = memories.find_by_slot(conn, slot[0], slot[1])
        if holder is not None:
            if on_conflict == "overwrite":
                _forget_holder(conn, holder)
                memories.insert(conn, entry)
                counts.imported += 1
            else:
                counts.skipped += 1
                counts.conflicts.append(
                    {
                        "reason": "slot conflict",
                        "id": entry.id,
                        "detail": (
                            f"Slot {_slot_label(entry)} is already occupied by an existing entry "
                            f"(id={holder.id}), skipped"
                        ),
                    },
                )
            return
    memories.insert(conn, entry)
    counts.imported += 1


def _overwrite_same_id(conn: sqlite3.Connection, existing: Memory, entry: Memory) -> None:
    """覆盖同 id 条目：更新内容并同步软删状态，旧值写入 history（槽位不变）。"""
    memories.update_content(conn, existing.id, entry.content, entry.confidence)
    conn.execute(
        "UPDATE memories SET evidence = ?, forgotten_at = ?, forgotten_reason = ?, updated_at = ? WHERE id = ?",
        (entry.evidence, entry.forgotten_at, entry.forgotten_reason, now_iso_utc(), existing.id),
    )
    history.record(
        conn,
        existing.id,
        "update",
        old_content=existing.content,
        new_content=entry.content,
        actor="user",
        evidence="import restore",
    )


def _forget_holder(conn: sqlite3.Connection, holder: Memory) -> None:
    """槽位冲突覆盖：旧条目软删除（reason='import overwrite' + forget 审计）。"""
    memories.forget(conn, holder.id, _FORGOTTEN_REASON)
    history.record(
        conn,
        holder.id,
        "forget",
        old_content=holder.content,
        new_content=None,
        actor="user",
        evidence="import restore",
    )


def _load_export_json(import_path: str) -> dict:
    """读取并校验导出文件：JSON 合法性 + version 字段匹配。"""
    try:
        raw = json.loads(Path(import_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalError(
            f"Import file is not valid JSON ({exc}). Make sure it was produced by `mex export --format json`.",
        ) from exc
    if not isinstance(raw, dict) or raw.get("version") != EXPORT_VERSION:
        version = raw.get("version") if isinstance(raw, dict) else "missing"
        raise ExternalError(
            f"Import file version {version} does not match the supported export version {EXPORT_VERSION}. "
            "Use an export produced by the same meX version.",
        )
    if not isinstance(raw.get("memories"), list):
        raise ExternalError(
            "Import file is missing the memories array. "
            "Make sure it was produced by `mex export --format json`.",
        )
    return raw


def _load_current_schema() -> Schema:
    """加载当前 schema（槽位白名单，冲突判定用）；加载失败视为外部错误。"""
    path = Path(get_mex_home()) / "schema.yaml"
    try:
        return load_schema(str(path))
    except SchemaError as exc:
        raise ExternalError(f"Failed to load {path}: {exc}") from exc


def _profile_section(items: list[Memory]) -> list[str]:
    """画像分节：只展示画像槽位条目（sub_topic 非空），按领域分节、槽位用 sub_topic 名展示。"""
    selected = [m for m in items if m.sub_topic]
    if not selected:
        return []
    lines = ["## Profile"]
    for topic in dict.fromkeys(m.topic for m in selected):
        lines.append(f"### {topic}")
        for m in [x for x in selected if x.topic == topic]:
            lines.append(f"- {m.sub_topic}: {_md_content(m)}")
    return lines


def _timeline_section(items: list[Memory]) -> list[str]:
    """画像外记录分节：``- [日期] 内容``，按时间倒序。"""
    outside = [m for m in items if not m.sub_topic]
    if not outside:
        return []
    lines = ["## Out-of-profile records (timeline)"]
    for m in outside:
        lines.append(f"- [{m.created_at[:10]}] {_md_content(m)}")
    return lines


def _slot_label(m: Memory) -> str:
    """槽位显示文本：画像槽位显示 ``topic.sub_topic``，画像外记录显示 topic（或 —）。"""
    if m.topic and m.sub_topic:
        return f"{m.topic}.{m.sub_topic}"
    return m.topic or "—"


def _forgotten_section(items: list[Memory]) -> list[str]:
    """已遗忘分节：单列一节并标注遗忘时间。"""
    if not items:
        return []
    lines = [f"## Forgotten ({len(items)})"]
    for m in items:
        display = (
            f"{_slot_label(m)}: {_md_content(m)}"
            if m.slot_key()
            else f"[{m.created_at[:10]}] {_md_content(m)}"
        )
        date = m.forgotten_at[:10] if m.forgotten_at else ""
        lines.append(f"- [forgotten] {display} (forgotten on {date})")
    return lines


def _md_content(m: Memory) -> str:
    """内容展示：换行替换为空格（其余原样，不转义）。

    两属性模型下 content 永远单值字符串，无需解析 JSON 数组。
    """
    return m.content.replace("\n", " ")


def _count_forgotten(items: list[Memory]) -> int:
    """统计软删条数。"""
    return sum(1 for m in items if m.is_forgotten())
