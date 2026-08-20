"""``mex gc`` 命令：软删已过期（TTL）的记忆（清理性维护）。

惰性过滤已让过期记录对查询不可见（数据保留）；gc 只是把这些过期记录
真正软删（写 ``forgotten_at``，可 restore），释放"有效记录"集合，符合
"后悔药"原则。支持 ``--dry-run`` 预览不执行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from mex.cli import app, run
from mex.cli.common import print_json
from mex.config import get_mex_home, load_config
from mex.store import history, memories
from mex.store.connection import connect, transaction

if TYPE_CHECKING:
    import sqlite3

    from mex.domain.memory import Memory

_TTL_REASON = "TTL expired"


@app.command("gc", help="Soft-delete expired (TTL) memories")
@run
def gc_cmd(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview expired records to be soft-deleted without executing"),
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """清理已到期的记忆：按 TTL 惰性过滤已不可见的记录，逐个软删（可恢复）。"""
    config = load_config(get_mex_home())
    conn = connect(config.db_path)
    try:
        targets = memories.list_expired(conn)
        if not targets:
            _emit_empty(dry_run, json_out)
            return
        if dry_run:
            _emit_dry_run(targets, json_out)
            return
        with transaction(conn):
            _soft_delete_expired(conn, targets)
    finally:
        conn.close()
    _emit_done(targets, json_out)


def _soft_delete_expired(conn: sqlite3.Connection, targets: list[Memory]) -> None:
    """逐条软删过期记录并写 forget 事件（TTL 过期原因）。"""
    for m in targets:
        memories.forget(conn, m.id, _TTL_REASON)
        history.record(conn, m.id, "forget", old_content=m.content, new_content=None, actor="user")


def _emit_empty(dry_run: bool, json_out: bool) -> None:
    """无过期记录时的输出。"""
    if json_out:
        print_json({"expired": 0, "dry_run": dry_run, "note": "no expired records"})
        return
    typer.echo("No expired records.")


def _emit_dry_run(targets: list[Memory], json_out: bool) -> None:
    """--dry-run 预览：列出将软删的记录。"""
    if json_out:
        print_json({"expired": len(targets), "dry_run": True, "records": [m.to_dict() for m in targets]})
        return
    typer.echo(f"[dry-run] will soft-delete {len(targets)} expired records:")
    for m in targets:
        typer.echo(f"  {m.id} · {m.topic or '—'}.{m.sub_topic or '—'} · {m.content}")


def _emit_done(targets: list[Memory], json_out: bool) -> None:
    """执行完成输出。"""
    if json_out:
        print_json({"expired": len(targets), "dry_run": False})
        return
    typer.echo(f"Soft-deleted {len(targets)} expired records (reason=TTL expired, use `mex restore <id>` to recover).")
