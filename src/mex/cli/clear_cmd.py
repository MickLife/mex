"""``mex clear`` 命令：三层确认（含备份执行）后清空记忆与历史数据。

删除前依次进行（默认交互模式，``--yes`` 可跳过）：
1. 数量确认：展示将被清空的各表数量，需输入 y 继续；
2. 备份确认：回答是否备份。选 y 时**立即执行备份**并打印落盘路径；
3. 最终二次确认：显示即将不可逆删除的总条数及备份状态，再次输入 y 才执行。

非交互场景（无 stdin）需 ``--yes``；测试可通过注入 stdin 模拟。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from mex.cli import app, run
from mex.cli.common import ExternalError, UserError, ensure_initialized, print_json
from mex.config import get_mex_home, load_config
from mex.services.clear import CLEAR_TABLES, ClearReport, backup_memory_data, clear_memory_data
from mex.store.connection import connect

if TYPE_CHECKING:
    import sqlite3

    from mex.services.backup import ExportReport

_BACKUP_SUBDIR = "backups"
_TERMINAL_YES = "y"


@app.command("clear", help="Clear all memories and history (keeps config and schema)")
@run
def clear_cmd(
    yes: Annotated[bool, typer.Option("--yes", help="Skip all confirmations (automation)")] = False,
    backup: Annotated[
        bool | None,
        typer.Option("--backup/--no-backup", help="Whether to back up: ask interactively by default"),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """清空全部记忆与历史数据（memories / 索引 / 审计），保留配置与架构。"""
    config = load_config(get_mex_home())
    ensure_initialized(config.mex_home)
    conn = connect(config.db_path)
    backup_report: ExportReport | None = None
    try:
        if _is_empty(conn):
            _print_no_data(json_out)
            return
        if not yes:
            _confirm_step_quantity(conn)
        use_backup = _resolve_backup(backup, yes)
        if use_backup:
            backup_report = _run_backup(conn, config.mex_home)
        if not yes:
            _confirm_step_final(conn, backup_report)
        report = clear_memory_data(conn)
    except OSError as exc:
        raise ExternalError(f"Clear failed: {exc}") from exc
    finally:
        conn.close()
    _print_report(report, backup_report, json_out)


def _is_empty(conn: sqlite3.Connection) -> bool:
    """判断是否有可清空的数据：memories 与 history 均为 0 才算空。"""
    counts = _table_counts(conn)
    return counts["memories"] == 0 and counts["history"] == 0


def _print_no_data(json_out: bool) -> None:
    """数据已为空时的提示输出（无需清空，不进入任何确认）。"""
    if json_out:
        print_json({"cleared_memories": 0, "cleared_history": 0, "note": "no data to clear"})
        return
    typer.echo("No data to clear.")


def _confirm_step_quantity(conn: sqlite3.Connection) -> None:
    """第 1 步数量确认：展示各表数量并要求输入 y。"""
    counts = _table_counts(conn)
    typer.echo("The following data will be cleared:")
    for name, n in counts.items():
        typer.echo(f"  - {name}: {n}")
    typer.echo("(config.yaml, schema.yaml and LLM usage are kept)")
    if not _asked_yes("Confirm to continue with these counts?"):
        raise UserError("Clear cancelled.")


def _resolve_backup(explicit: bool | None, yes: bool) -> bool:
    """第 2 步备份确认：解析最终是否备份。"""
    if explicit is not None:
        return explicit
    if yes:
        return True  # 自动化默认备份（更安全）
    return _asked_yes("Create a memory snapshot backup before clearing (backups/ directory)?")


def _run_backup(conn: sqlite3.Connection, mex_home: str) -> ExportReport | None:
    """执行备份并输出落盘路径；失败提示但返回 None（不阻断流程）。"""
    backup_dir = Path(mex_home) / _BACKUP_SUBDIR
    report = backup_memory_data(conn, backup_dir)
    if report is None:
        typer.echo("[notice] backup failed (no snapshot written), check that the disk is writable.")
        return None
    typer.echo(f"[backup done] memory snapshot created: {report.path}")
    return report


def _confirm_step_final(conn: sqlite3.Connection, backup_report: ExportReport | None) -> None:
    """第 3 步最终二次确认：显示即将删除的总条数与备份状态，再次输入 y。"""
    counts = _table_counts(conn)
    total = counts["memories"] + counts["history"]
    backup_text = "created" if backup_report else "not created"
    typer.echo("=== Final confirmation ===")
    typer.echo(
        f"This will irreversibly clear {counts['memories']} memories and {counts['history']} "
        f"history records ({total} in total).",
    )
    typer.echo(f"Backup status: {backup_text}.")
    if not _asked_yes("Confirm clearing again? This cannot be undone."):
        raise UserError("Clear cancelled.")


def _asked_yes(prompt: str) -> bool:
    """从 stdin 读一行：输入 y（忽略大小写）返回 True，其余返回 False。"""
    typer.echo(f"{prompt} [y/N] ", nl=False)
    try:
        line = sys.stdin.readline()
    except (ValueError, OSError):
        line = ""
    return line.strip().lower() == _TERMINAL_YES


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """统计将要清空各表的数量。

    表名遍历自 services 层受控常量 ``CLEAR_TABLES``（与清空时的删除目标完全一致），
    非外部输入，故 f-string 拼接是安全的。
    """
    result: dict[str, int] = {}
    for table in CLEAR_TABLES:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608 - 表名来自受控 CLEAR_TABLES
        result[table] = int(row["n"])
    return result


def _print_report(report: ClearReport, backup_report: ExportReport | None, json_out: bool) -> None:
    """输出清空结果（文本或 JSON），并在有备份时提示恢复命令。"""
    if json_out:
        print_json(
            {
                "cleared_memories": report.memories_count,
                "cleared_history": report.history_count,
                "backup": None if backup_report is None else backup_report.path,
            },
        )
        return
    typer.echo(
        f"Cleared {report.memories_count} memories (including {report.forgotten_count} soft-deleted) "
        f"and {report.history_count} audit records.",
    )
    if backup_report is not None:
        typer.echo(f"To restore memories, run: mex import {backup_report.path}")
