"""M4 查询命令：search / profile / history / doctor（架构文档 §5.1、§6.2）。

查询路径零 LLM 调用；每命令一条短命连接（架构文档 §4.3）。
"""

from __future__ import annotations

import sqlite3
from functools import wraps
from typing import TYPE_CHECKING

import typer

from mex import config
from mex.cli import app, run
from mex.cli.common import (
    ExternalError,
    UserError,
    ensure_initialized,
    print_json,
    render_memory_entries,
)
from mex.domain.memory import to_local_str
from mex.domain.schema import SchemaError, load_schema
from mex.llm.client import LLMError, fetch_models
from mex.services import profile as profile_service
from mex.services import search as search_service
from mex.store import connection, history as history_store, memories

if TYPE_CHECKING:
    from mex.config import Config


def _guard(fn):
    """把 sqlite3 错误转为 ExternalError（数据库 IO 错误 → 退出码 2）。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            raise ExternalError(f"Database read/write failed: {exc}. Check the database file for integrity.") from exc

    return wrapper


@app.command("search", help="Search memories by topic, sub-topic, keyword or date range")
@run
@_guard
def search(  # noqa: PLR0913, PLR0917 - CLI 命令参数多属接口形态
    topic: str | None = typer.Option(None, "--topic", help="Filter by topic"),
    sub_topic: str | None = typer.Option(None, "--sub-topic", help="Filter by sub-topic (requires --topic)"),
    keyword: str | None = typer.Option(None, "--keyword", help="Keyword search"),
    since: str | None = typer.Option(None, "--since", help="Local date YYYY-MM-DD (inclusive)"),
    until: str | None = typer.Option(None, "--until", help="Local date YYYY-MM-DD (inclusive)"),
    limit: int = typer.Option(50, "--limit", min=1, max=1000, help="Maximum number of results"),
    include_forgotten: bool = typer.Option(False, "--include-forgotten", help="Include soft-deleted entries"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """多条件检索记忆（画像槽位与画像外记录统一检索）。"""
    cfg, conn = _connect()
    try:
        try:
            if sub_topic is not None and topic is None:
                raise ValueError(
                    "--sub-topic requires --topic "
                    "(a sub-topic belongs to a topic, e.g. --topic work --sub-topic award)",
                )
            filters = search_service.SearchFilters(
                topic=topic,
                sub_topic=sub_topic,
                keyword=keyword,
                since=since,
                until=until,
                limit=limit,
                include_forgotten=include_forgotten,
            )
            results = search_service.search(conn, filters)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        if json_output:
            print_json([m.to_dict() for m in results])
        else:
            schema = _load_schema(cfg)
            typer.echo(render_memory_entries(results, schema) or "(no matching memories)")
    finally:
        conn.close()


@app.command("profile", help="Output the profile snapshot text for agent injection")
@run
@_guard
def profile(
    max_tokens: int | None = typer.Option(
        None, "--max-tokens", min=1, help="Token budget for output (default: from config)",
    ),
    topics: str | None = typer.Option(None, "--topics", help="Only include these topics, comma-separated"),
    recent_days: int = typer.Option(7, "--recent-days", min=1, help="Recent activity window in days"),
    recent_limit: int = typer.Option(
        5, "--recent-limit", min=0, help="Max recent activity entries (0=disable this section)",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """输出画像快照文本，供 agent 注入。"""
    cfg, conn = _connect()
    try:
        schema = _load_schema(cfg)
        topic_list = _parse_topics(topics, schema)
        limit = max_tokens if max_tokens is not None else cfg.profile_max_tokens
        snapshot = profile_service.generate_profile(
            conn,
            schema,
            max_tokens=limit,
            topics=topic_list,
            recent_days=recent_days,
            recent_limit=recent_limit,
        )
        if json_output:
            print_json({"profile": snapshot})
        else:
            typer.echo(snapshot)
    finally:
        conn.close()


@app.command("get", help="Show a single memory by id (JSON output)")
@run
@_guard
def get(memory_id: str = typer.Argument(..., help="Memory id")) -> None:
    """按 id 查看单条记忆的完整内容（恒定 JSON 输出，含软删/过期状态）。"""
    cfg, conn = _connect()
    try:
        memory = memories.get(conn, memory_id, include_forgotten=True)
        if memory is None:
            raise UserError(
                f"Memory {memory_id} does not exist. Use `mex list --include-forgotten` to verify the id.",
            )
        print_json(memory.to_dict())
    finally:
        conn.close()


@app.command("history", help="Show the change history of a memory")
@run
@_guard
def history(
    memory_id: str = typer.Argument(..., help="Memory id"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """查看单条记忆的变更历史。"""
    cfg, conn = _connect()
    try:
        if memories.get(conn, memory_id, include_forgotten=True) is None:
            raise UserError(
                f"Memory {memory_id} does not exist. Use `mex list --include-forgotten` to verify the id.",
            )
        records = history_store.list_for_memory(conn, memory_id)
        if json_output:
            print_json(records)
        else:
            _render_history(records)
    finally:
        conn.close()


@app.command("doctor", help="Diagnose schema consistency and LLM connectivity")
@run
@_guard
def doctor(
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """诊断：数据与 schema 一致性 + LLM 配置与网络连通性（退出码恒为 0）。"""
    cfg, conn = _connect()
    try:
        schema = _load_schema(cfg)
        dangling = memories.list_dangling_slots(conn, schema)
        llm_report = _llm_health(cfg)
        if json_output:
            print_json(
                {
                    "schema_ok": not dangling,
                    "dangling": [m.to_dict() for m in dangling],
                    "llm": llm_report,
                },
            )
            return
        _print_schema_report(dangling)
        _print_llm_report(llm_report)
    finally:
        conn.close()


def _print_schema_report(dangling: list) -> None:
    """输出 schema 一致性检查结果。"""
    if not dangling:
        typer.echo("[schema] data is consistent with the schema")
        return
    typer.echo(
        f"[schema] found {len(dangling)} active profile entries inconsistent with the schema "
        "(invalid slot or structured content):",
    )
    typer.echo(render_memory_entries(dangling))
    typer.echo(
        "Suggestion: use `mex update <id>` to migrate to a new field/structured format, "
        "or `mex forget <id>` to delete.",
    )


def _llm_health(cfg: Config) -> dict:
    """LLM 健康检查：配置完整性 + /models 网络连通性。

    Returns:
        {"configured": bool, "reachable": bool | None, "models": int | None, "message": str}
        reachable 为 None 表示未配置跳过检查。
    """
    if not cfg.llm_base_url or not cfg.api_key() or not cfg.llm_model:
        return {
            "configured": False,
            "reachable": None,
            "models": None,
            "message": "LLM not configured (run `mex config llm`)",
        }
    try:
        models = fetch_models(cfg.llm_base_url, cfg.api_key() or "")
        return {
            "configured": True,
            "reachable": True,
            "models": len(models),
            "message": "Network reachable, model list available",
        }
    except LLMError as exc:
        return {"configured": True, "reachable": False, "models": None, "message": f"Network unreachable: {exc}"}


def _print_llm_report(report: dict) -> None:
    """输出 LLM 健康检查结果。"""
    if not report["configured"]:
        typer.echo(f"[llm] {report['message']}")
        return
    if report["reachable"]:
        typer.echo(f"[llm] {report['message']} ({report['models']} models)")
        return
    typer.echo(f"[llm] {report['message']}")


def _connect() -> tuple[Config, sqlite3.Connection]:
    """打开连接：加载配置、校验初始化（mex.db 必须存在）。"""
    cfg = config.load_config(config.get_mex_home())
    ensure_initialized(cfg.mex_home)
    return cfg, connection.connect(cfg.db_path)


def _load_schema(cfg: Config):
    """加载 schema.yaml；解析失败视为用户配置错误（退出码 1）。"""
    try:
        return load_schema(cfg.schema_path)
    except SchemaError as exc:
        raise UserError(f"Failed to parse schema.yaml: {exc}. Check the content of {cfg.schema_path}.") from exc


def _parse_topics(topics: str | None, schema) -> list[str]:
    """--topics 逗号分隔 → 去重后的列表；未知 topic 报错（避免静默忽略）。"""
    if not topics:
        return []
    names = [t.strip() for t in topics.split(",") if t.strip()]
    unknown = [t for t in names if not schema.has_topic(t)]
    if unknown:
        raise UserError(
            f"Unknown topics: {', '.join(unknown)}. Available topics: {', '.join(schema.topic_names())}.",
        )
    return names


def _render_history(records: list[dict]) -> None:
    """人类可读历史：时间（本地）、事件、actor、旧内容 → 新内容。"""
    if not records:
        typer.echo("(no history records for this memory)")
        return
    for r in records:
        change = f"{r['old_content'] or '(none)'} → {r['new_content'] or '(none)'}"
        typer.echo(f"{to_local_str(r['created_at'])}  {r['event']:<7} {r['actor']:<4} {change}")
