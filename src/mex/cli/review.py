"""mex review 命令：审查 AI 推断记忆（M6）。

用法：
    mex review list
    mex review approve <id>
    mex review decline <id> [--reason "..."]
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import typer

from mex import config
from mex.cli import app, run
from mex.cli.common import ExternalError, UserError, ensure_initialized, print_json
from mex.domain.memory import to_local_str
from mex.services import review as review_service
from mex.store import history as history_store, memories
from mex.store.connection import connect, migrate

if TYPE_CHECKING:
    from mex.domain.memory import Memory

_ACTIONS = ("list", "approve", "decline")


@app.command("review", help="Review AI-inferred memories: list / approve / decline")
@run
def review_cmd(
    action: str = typer.Argument(..., help="list / approve / decline"),
    memory_id: str | None = typer.Argument(None, help="Memory id (required for approve / decline)"),
    reason: str | None = typer.Option(None, "--reason", help="Reason for rejection (decline only)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """审查 AI 推断记忆：列出 / 批准 / 拒绝。"""
    if action not in _ACTIONS:
        raise UserError(
            f"Unknown subcommand '{action}'. Usage: mex review list | approve <id> | decline <id> [--reason ...]",
        )
    cfg = config.load_config(config.get_mex_home())
    ensure_initialized(cfg.mex_home)
    migrate(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        _dispatch(conn, action, memory_id, reason, json_output)
    except sqlite3.Error as exc:
        raise ExternalError(f"Database access failed: {exc}") from exc
    finally:
        conn.close()


def _dispatch(
    conn: sqlite3.Connection,
    action: str,
    memory_id: str | None,
    reason: str | None,
    json_output: bool,
) -> None:
    """按子命令分发到对应处理函数。"""
    if action == "list":
        _cmd_list(conn, json_output)
        return
    _require_id(memory_id, action)
    if action == "approve":
        _cmd_approve(conn, memory_id, json_output)
    else:
        _cmd_decline(conn, memory_id, reason, json_output)


def _cmd_list(conn: sqlite3.Connection, json_output: bool) -> None:
    """输出待审查列表（表格或 JSON）；空队列时给出提示（退出码 0）。"""
    pending = review_service.list_pending(conn)
    if not pending:
        if json_output:
            print_json([])
        else:
            typer.echo("No AI-inferred memories pending review")
        return
    if json_output:
        print_json([m.to_dict() for m in pending])
    else:
        typer.echo(_render_review_table(pending))


def _cmd_approve(conn: sqlite3.Connection, memory_id: str, json_output: bool) -> None:
    """批准一条待审查记忆并输出结果。"""
    updated = review_service.approve(conn, memory_id)
    if updated is None:
        _raise_queue_or_missing(conn, memory_id)
    if json_output:
        print_json(updated.to_dict())
    else:
        typer.echo(f"Approved {updated.id} ({_content_summary(updated.content)})")


def _cmd_decline(
    conn: sqlite3.Connection,
    memory_id: str,
    reason: str | None,
    json_output: bool,
) -> None:
    """拒绝（软删除）一条待审查记忆并输出结果。

    若该记忆是通过覆盖旧值产生的（history 有 update 事件），提示旧值可恢复。
    """
    previous = _find_overwritten_content(conn, memory_id)
    deleted = review_service.decline(conn, memory_id, reason)
    if deleted is None:
        _raise_queue_or_missing(conn, memory_id)
    if json_output:
        print_json(deleted.to_dict())
        return
    typer.echo(f"Declined {deleted.id} ({_content_summary(deleted.content)})")
    if previous:
        typer.echo(
            f"Note: this entry overwrote the old value '{_content_summary(previous)}'; "
            "use `mex add` to re-write it if needed.",
        )


def _find_overwritten_content(conn: sqlite3.Connection, memory_id: str) -> str | None:
    """查 history：该记忆是否通过覆盖旧值产生（有 update 事件的 old_content）。

    decline 场景下提示用户旧值可恢复用。
    """
    records = history_store.list_for_memory(conn, memory_id)
    for r in reversed(records):
        if r["event"] == "update" and r["old_content"]:
            return str(r["old_content"])
    return None


def _raise_queue_or_missing(conn: sqlite3.Connection, memory_id: str) -> None:
    """服务层返回 None 时区分原因：不存在 / 已删除 / 不在待审查队列。"""
    existing = memories.get(conn, memory_id, include_forgotten=True)
    if existing is None:
        raise UserError(f"Memory {memory_id} does not exist. Use `mex review list` to verify the id.")
    if existing.is_forgotten():
        raise UserError(f"Memory {memory_id} is already deleted.")
    raise UserError(f"Memory {memory_id} is not in the pending review queue.")


def _require_id(memory_id: str | None, action: str) -> None:
    """approve / decline 必须携带 id 参数。"""
    if not memory_id:
        raise UserError(f"`mex review {action}` requires an id. Usage: mex review {action} <id>")


def _content_summary(content: str) -> str:
    """内容摘要：去换行并截断（命令输出用）。"""
    return content.replace("\n", " ")[:50]


def _render_review_table(pending: list[Memory]) -> str:
    """待审查列表的块状条目格式：元信息行（含证据）+ 完整内容行。

    Args:
        pending: 待审查记忆列表。

    Returns:
        多行文本；空列表返回空字符串。
    """
    if not pending:
        return ""
    lines: list[str] = []
    for i, m in enumerate(pending, start=1):
        slot = f"{m.topic}.{m.sub_topic}" if m.topic else "—"
        created = to_local_str(m.created_at)
        meta = f"{i}. {m.id} · {slot} · confidence={m.confidence.value} · created={created}"
        lines.append(meta)
        lines.append(f"   {m.content}")
        evidence = (m.evidence or "(no evidence)").replace("\n", " ")
        lines.append(f"   evidence: {evidence}")
    return "\n".join(lines)
