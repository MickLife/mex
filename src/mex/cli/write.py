"""M3：初始化与手动写入命令（init/add/update/forget/restore/list）。

依据架构文档 §5.1 命令清单与 §5.2 全局约定；依赖 domain（M1）、store（M2）。
依赖方向 cli → services/store/domain；全部写操作与 history 记录在同一事务内完成。
"""

from __future__ import annotations

import difflib
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import uuid4

import typer
from loguru import logger

from mex.cli import app, run
from mex.cli.common import ExternalError, UserError, ensure_initialized, print_json, render_memory_entries
from mex.config import get_mex_home, load_config
from mex.domain.memory import Confidence, Memory, now_iso_utc, parse_expires
from mex.domain.rules import validate
from mex.domain.schema import SchemaError, load_schema
from mex.services.search import SearchFilters, search
from mex.store import history, memories
from mex.store.connection import connect, init_db, transaction

if TYPE_CHECKING:
    import sqlite3

    from mex.config import Config
    from mex.domain.schema import Schema

_BATCH_LIMIT = 10000  # forget 批量/--dry-run 的筛选上限（个人量级足够）


@app.command("init", help="Initialize the data directory (create db, schema.yaml and config.yaml)")
@run
def init_db_cmd(
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """初始化数据目录：建库并生成默认 schema.yaml / config.yaml（幂等）。"""
    mex_home = get_mex_home()
    home = Path(mex_home)
    db_path = home / "mex.db"
    schema_path = home / "schema.yaml"
    config_path = home / "config.yaml"
    if not db_path.exists():
        home.mkdir(parents=True, exist_ok=True)
        _copy_template("schema.default.yaml", schema_path)
        _copy_template("config.default.yaml", config_path)
        config_path.chmod(0o600)  # 默认收紧权限（后续 mex config llm 写入 key 时保护）
        init_db(db_path)
        logger.info("Initialized data directory: {}", mex_home)
    _print_init_result(mex_home, db_path, schema_path, config_path, json_out)


def _print_init_result(mex_home: str, db_path: Path, schema_path: Path, config_path: Path, json_out: bool) -> None:
    """输出 init 结果（文本路径清单或 JSON）。"""
    if json_out:
        print_json(
            {
                "mex_home": mex_home,
                "db_path": str(db_path),
                "schema_path": str(schema_path),
                "config_path": str(config_path),
            },
        )
        return
    typer.echo(f"Initialized at {mex_home}")
    typer.echo(f"  mex.db: {db_path}")
    typer.echo(f"  schema.yaml: {schema_path}")
    typer.echo(f"  config.yaml: {config_path}")


def _copy_template(name: str, dest: Path) -> None:
    """从打包模板复制文件（已存在时不覆盖，保护用户编辑）。"""
    if dest.exists():
        return
    source = resources.files("mex.templates").joinpath(name)
    dest.write_bytes(source.read_bytes())


@app.command("add", help="Add a memory (default confidence: confirmed)")
@run
def add_cmd(  # noqa: PLR0913, PLR0917 - Typer 命令参数即 CLI 契约，不可合并
    topic: Annotated[
        str | None,
        typer.Option("--topic", help="Topic (required for profile slots, i.e. non-empty sub-topic)"),
    ] = None,
    sub_topic: Annotated[
        str | None,
        typer.Option(
            "--sub-topic",
            help="Sub-topic (non-empty = profile slot, must be valid; omitted = out-of-profile record)",
        ),
    ] = None,
    content: Annotated[str, typer.Option("--content", help="Memory content")] = ...,
    confidence: Annotated[Confidence, typer.Option("--confidence", help="Confidence level")] = Confidence.CONFIRMED,
    expires: Annotated[str | None, typer.Option(
        "--expires", help="Expiration (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS local time)",
    )] = None,
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """手动添加一条记忆（默认 confirmed，画像槽位命中唯一事实约束）。"""
    cfg = _load_ready_config()
    schema = _load_schema_or_raise(cfg.schema_path)
    errors = validate(
        topic=topic,
        sub_topic=sub_topic,
        content=content,
        confidence=confidence,
        schema=schema,
        allow_confirmed=True,
    )
    if errors:
        raise UserError(_format_schema_errors(errors, schema, topic, sub_topic))
    if topic and sub_topic and schema.is_structured(topic, sub_topic):
        ok, struct_errors = schema.validate_structured_content(topic, sub_topic, content)
        if not ok:
            raise UserError(f"Structured field {topic}.{sub_topic} validation failed: {'; '.join(struct_errors)}")
    expires_at = _parse_expires_or_raise(expires)
    conn = connect(cfg.db_path)
    try:
        if sub_topic:
            _check_slot_free(conn, topic or "", sub_topic, schema)
        now = now_iso_utc()
        m = Memory(
            id=uuid4().hex,
            topic=topic,
            sub_topic=sub_topic,
            content=content,
            is_ai_inferred=False,
            confidence=confidence,
            evidence=None,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        with transaction(conn):
            memories.insert(conn, m)
            history.record(conn, m.id, "add", old_content=None, new_content=m.content, actor="user")
    finally:
        conn.close()
    _emit_memory_result(f"Added {m.id}: {m.content}", m, json_out)


def _parse_expires_or_raise(expires: str | None) -> str | None:
    """解析 ``--expires`` 为 UTC ISO；None 返回 None；非法格式报 UserError。"""
    if expires is None:
        return None
    try:
        return parse_expires(expires)
    except ValueError as exc:
        raise UserError(str(exc)) from exc


def _check_slot_free(
    conn: sqlite3.Connection, topic: str, sub_topic: str, schema: Schema,
) -> None:
    """槽位占用检查：唯一画像槽位已有有效条目时报错并建议 mex update；可多条槽位放行（add 即追加）。

    - 可多条字段（schema ``unique: false``）：add 即追加新记录，不做占用检查；
    - 唯一字段（``unique: true``，默认）：已有有效条目时报错，引导用 ``mex update``。
    """
    if not schema.is_unique(topic, sub_topic):
        return
    existing = memories.find_by_slot(conn, topic, sub_topic)
    if existing is not None:
        slot = f"{topic}.{sub_topic}"
        raise UserError(
            f"Slot {slot} already has content: '{existing.content}'. "
            f"Use `mex update {existing.id}` to modify it.",
        )


@app.command("update", help="Update content, confidence or expiration of a memory")
@run
def update_cmd(  # noqa: PLR0913, PLR0917 - Typer 命令参数即 CLI 契约，不可合并
    memory_id: Annotated[str, typer.Argument(help="Target memory id")],
    content: Annotated[str | None, typer.Option("--content", help="New content")] = None,
    confidence: Annotated[Confidence | None, typer.Option("--confidence", help="New confidence level")] = None,
    expires: Annotated[str | None, typer.Option(
        "--expires", help="Set expiration (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS local time)",
    )] = None,
    clear_expires: Annotated[bool, typer.Option("--clear-expires", help="Clear expiration (keep forever)")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """更新内容/置信度/过期时间（槽位不可变；换槽位用 add + forget）。"""
    cfg = _load_ready_config()
    if content is None and confidence is None and expires is None and not clear_expires:
        raise UserError("Provide at least one of --content / --confidence / --expires / --clear-expires")
    conn = connect(cfg.db_path)
    new_expires: str | None = None
    try:
        old = memories.get(conn, memory_id, include_forgotten=True)
        if old is None:
            raise UserError(f"Memory {memory_id} does not exist")
        if old.is_forgotten():
            raise UserError(f"Memory {memory_id} is already deleted. Use `mex restore {memory_id}` to recover it.")
        new_content = content if content is not None else old.content
        new_confidence = confidence if confidence is not None else old.confidence
        if not new_content.strip():
            raise UserError("content must not be empty")
        if clear_expires:
            new_expires = None
        elif expires is not None:
            new_expires = _parse_expires_or_raise(expires)
        else:
            new_expires = old.expires_at  # 未改则保持原值
        with transaction(conn):
            updated = memories.update_content(conn, memory_id, new_content, new_confidence)
            memories.set_expiration(conn, memory_id, new_expires)
            history.record(conn, memory_id, "update", old_content=old.content, new_content=new_content, actor="user")
    finally:
        conn.close()
    if updated is None:
        raise ExternalError(f"Failed to update memory {memory_id}, please retry")
    _emit_memory_result(f"Updated {updated.id}: {updated.content}", updated, json_out)


@app.command("forget", help="Delete memories (soft delete by default; single id or batch filters)")
@run
def forget_cmd(  # noqa: PLR0913, PLR0917 - Typer 命令参数即 CLI 契约，不可合并
    memory_id: Annotated[
        str | None,
        typer.Argument(help="Target memory id (mutually exclusive with batch filters)"),
    ] = None,
    topic: Annotated[str | None, typer.Option("--topic", help="Filter: topic")] = None,
    keyword: Annotated[str | None, typer.Option("--keyword", help="Filter: keyword")] = None,
    since: Annotated[str | None, typer.Option("--since", help="Filter: created after (YYYY-MM-DD)")] = None,
    until: Annotated[str | None, typer.Option("--until", help="Filter: created before (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option("--reason", help="Forget reason")] = None,
    hard: Annotated[bool, typer.Option("--hard", help="Permanently delete (requires confirmation or --yes)")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview affected entries without executing")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation for permanent deletion")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """删除记忆：默认软删除，支持 id 单条或条件批量。"""
    cfg = _load_ready_config()
    conn = connect(cfg.db_path)
    try:
        filters = SearchFilters(
            topic=topic,
            keyword=keyword,
            since=since,
            until=until,
            limit=_BATCH_LIMIT,
        )
        targets = _forget_targets(conn, memory_id, filters, hard)
        if dry_run:
            _emit_dry_run(targets, json_out)
            return
        _execute_deletion(conn, targets, hard=hard, yes=yes, reason=reason)
    finally:
        conn.close()
    _emit_deleted(targets, hard, json_out)


def _forget_targets(
    conn: sqlite3.Connection,
    memory_id: str | None,
    filters: SearchFilters,
    hard: bool,
) -> list[Memory]:
    """解析 forget 目标：单条走 id，批量走 search 筛选（仅有效条目）。"""
    if memory_id is not None:
        return [_resolve_single(conn, memory_id, hard)]
    if not any((filters.topic, filters.keyword, filters.since, filters.until)):
        raise UserError("Provide an id or at least one filter (--topic/--keyword/--since/--until)")
    try:
        targets = search(conn, filters)
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    if not targets:
        raise UserError("No matching entries")
    return targets


def _resolve_single(conn: sqlite3.Connection, memory_id: str, hard: bool) -> Memory:
    """解析单条 forget 目标：软删目标必须是有效条目。"""
    m = memories.get(conn, memory_id, include_forgotten=True)
    if m is None:
        raise UserError(f"Memory {memory_id} does not exist")
    if m.is_forgotten() and not hard:
        raise UserError(f"Memory {memory_id} is already deleted. Use `mex restore {memory_id}` to recover it.")
    return m


def _execute_deletion(
    conn: sqlite3.Connection,
    targets: list[Memory],
    *,
    hard: bool,
    yes: bool,
    reason: str | None,
) -> None:
    """执行删除：软删除写 reason + history；硬删除需确认后物理删除 + history。"""
    if hard:
        if not yes:
            _confirm_hard_delete()
        with transaction(conn):
            for m in targets:
                memories.hard_delete(conn, m.id)
                history.record(conn, m.id, "delete", old_content=m.content, new_content=None, actor="user")
        return
    with transaction(conn):
        for m in targets:
            memories.forget(conn, m.id, reason or "")
            history.record(conn, m.id, "forget", old_content=m.content, new_content=None, actor="user")


def _confirm_hard_delete() -> None:
    """物理删除确认：从 stdin 读一行，非 y（或无可读输入）时取消。

    非交互场景（无输入流）需显式 ``--yes``；测试可通过注入 input 模拟交互。
    """
    try:
        line = sys.stdin.readline()
    except (ValueError, OSError):
        line = ""
    typer.echo("Permanent deletion cannot be undone, type y to confirm: ", nl=False)
    if line.strip().lower() != "y":
        raise UserError("Deletion cancelled; use --yes to skip confirmation")


@app.command("restore", help="Restore a soft-deleted memory")
@run
def restore_cmd(
    memory_id: Annotated[str, typer.Argument(help="Target memory id")],
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """恢复软删条目。"""
    cfg = _load_ready_config()
    conn = connect(cfg.db_path)
    try:
        with transaction(conn):
            restored = memories.restore(conn, memory_id)
            if restored is None:
                existing = memories.get(conn, memory_id, include_forgotten=True)
                if existing is None:
                    raise UserError(f"Memory {memory_id} does not exist")
                raise UserError(f"Memory {memory_id} is not in deleted state")
            history.record(conn, memory_id, "restore", old_content=None, new_content=restored.content, actor="user")
    finally:
        conn.close()
    _emit_memory_result(f"Restored {restored.id}: {restored.content}", restored, json_out)


@app.command("list", help="List memories (excludes deleted by default)")
@run
def list_cmd(
    topic: Annotated[str | None, typer.Option("--topic", help="Filter by topic")] = None,
    sub_topic: Annotated[str | None, typer.Option("--sub-topic", help="Filter by sub-topic (requires --topic)")] = None,
    include_forgotten: Annotated[bool, typer.Option("--include-forgotten", help="Include deleted entries")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """列出记忆（默认不含已删除条目）。"""
    if sub_topic is not None and topic is None:
        raise UserError(
            "--sub-topic requires --topic "
            "(a sub-topic belongs to a topic, e.g. --topic work --sub-topic award)",
        )
    cfg = _load_ready_config()
    conn = connect(cfg.db_path)
    try:
        items = memories.list_all(conn, topic=topic, sub_topic=sub_topic, include_forgotten=include_forgotten)
    finally:
        conn.close()
    if json_out:
        print_json([m.to_dict() for m in items])
        return
    schema = _load_schema_or_raise(cfg.schema_path)
    table = render_memory_entries(items, schema)
    if table:
        typer.echo(table)
        return
    typer.echo(_empty_list_hint(topic, sub_topic))


def _empty_list_hint(topic: str | None, sub_topic: str | None = None) -> str:
    """空记忆列表的提示：区分「整库为空」与「筛选后无匹配」。

    Args:
        topic: 用户传入的领域筛选；None 表示未按领域筛选。
        sub_topic: 用户传入的字段筛选（配合 topic）；None 表示未按字段筛选。

    Returns:
        面向用户的空列表提示文案。
    """
    if topic is not None and sub_topic is not None:
        return (
            f"No memories match (topic {topic}, sub-topic {sub_topic}). "
            f"Drop --sub-topic and run `mex list --topic {topic}` to retry."
        )
    if topic is not None:
        return f"No memories match (topic {topic}). Drop --topic and run `mex list` to retry."
    return "No memories yet. Use `mex add` to add manually, or `mex extract` to extract from a conversation."


def _load_ready_config() -> Config:
    """校验已初始化并返回配置快照（未 init 时抛 UserError 退出码 1）。"""
    mex_home = get_mex_home()
    ensure_initialized(mex_home)
    return load_config(mex_home)


def _load_schema_or_raise(schema_path: str) -> Schema:
    """加载 schema.yaml；缺失/非法时给出建议（重跑 mex init）。"""
    try:
        return load_schema(schema_path)
    except SchemaError as exc:
        raise UserError(f"Failed to read schema.yaml: {exc}. If the file is missing, run `mex init` first.") from exc


def _format_schema_errors(errors: list[str], schema: Schema, topic: str | None, sub_topic: str | None) -> str:
    """把校验错误加工成含"建议操作"的提示（架构 §5.2、ADR-6 例 2/3）。"""
    messages: list[str] = []
    for err in errors:
        if err.startswith("Slot ") and "is not defined in schema.yaml" in err:
            messages.append(_slot_not_found_message(schema, topic, sub_topic))
        elif err.startswith("Topic ") and "is not defined in schema.yaml" in err:
            messages.append(_topic_not_found_message(schema, topic))
        else:
            messages.append(err)
    return "; ".join(messages)


def _topic_not_found_message(schema: Schema, topic: str | None) -> str:
    """topic 不在 schema 时的报错：附带相近领域或已有领域清单。"""
    names = schema.topic_names()
    close = difflib.get_close_matches(topic or "", names, n=1, cutoff=0.4)
    hint = f"Close existing topic: '{close[0]}'." if close else f"Existing topics: {', '.join(names)}."
    return (
        f"Topic '{topic}' is not defined in schema.yaml. {hint} "
        "If you need a new topic, edit schema.yaml first."
    )


def _slot_not_found_message(schema: Schema, topic: str | None, sub_topic: str | None) -> str:
    """槽位不在 schema 时的报错：附带该领域合法字段清单。"""
    legal = schema.sub_topic_names(topic or "")
    hint = f"Valid sub-topics under {topic}: {', '.join(legal)}." if legal else f"Topic {topic} does not exist."
    return (
        f"Slot {topic}.{sub_topic} is not defined in schema.yaml. {hint} "
        "If you need a new field, edit schema.yaml first."
    )


def _emit_memory_result(text: str, m: Memory, json_out: bool) -> None:
    """输出单条记忆操作结果（文本摘要或 JSON）。"""
    if json_out:
        print_json(m.to_dict())
    else:
        typer.echo(text)


def _emit_deleted(targets: list[Memory], hard: bool, json_out: bool) -> None:
    """输出 forget 执行结果。"""
    if json_out:
        print_json([m.to_dict() for m in targets])
        return
    verb = "Permanently deleted" if hard else "Deleted"
    if len(targets) == 1:
        typer.echo(f"{verb} {targets[0].id}: {targets[0].content}")
    else:
        typer.echo(f"{verb} {len(targets)} memories")


def _emit_dry_run(targets: list[Memory], json_out: bool) -> None:
    """输出 forget --dry-run 预览结果（不执行）。"""
    if json_out:
        print_json([m.to_dict() for m in targets])
        return
    typer.echo(f"[dry-run] will affect {len(targets)} memories:")
    table = render_memory_entries(targets)
    if table:
        typer.echo(table)
