"""``mex export`` / ``mex import`` / ``mex stats`` 命令（架构 §5.1、ADR-9、§6.4）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

import typer

from mex.cli import app, run
from mex.cli.common import ExternalError, UserError, ensure_initialized, print_json
from mex.config import Config, get_mex_home, load_config
from mex.domain.schema import load_schema
from mex.llm.client import LLMClient, LLMConfig, LLMError
from mex.services.backup import (
    ExportReport,
    ImportReport,
    export_json,
    export_markdown,
    import_extract,
    import_restore,
    stats,
)
from mex.store.connection import connect

if TYPE_CHECKING:
    from mex.services.extract import ExtractResult


@app.command("export", help="Export all memories (including soft-deleted) to JSON or Markdown")
@run
def export_cmd(
    export_format: Annotated[
        Literal["json", "markdown"],
        typer.Option("--format", help="Export format: json (default) / markdown"),
    ] = "json",
    output: Annotated[str | None, typer.Option("--output", help="Output file path (required)")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output the export summary as JSON")] = False,
) -> None:
    """导出全部记忆（含软删）为 JSON 或 Markdown 文件。"""
    if not output:
        raise UserError("Provide an output path: `mex export --format json|markdown --output <path>`")
    config = load_config(get_mex_home())
    ensure_initialized(config.mex_home)
    conn = connect(config.db_path)
    try:
        report = export_json(conn, output) if export_format == "json" else export_markdown(conn, output)
    except OSError as exc:
        raise ExternalError(f"Export failed: {exc}") from exc
    finally:
        conn.close()
    _print_export_summary(report, json_output)


@app.command("import", help="Restore from an exported JSON, or extract-migrate from a text file")
@run
def import_cmd(
    file: Annotated[
        str,
        typer.Argument(help="Import file path: exported JSON for restore; any text/Markdown for extract"),
    ],
    mode: Annotated[
        Literal["restore", "extract"],
        typer.Option("--mode", help="Import mode: restore (default) / extract (LLM migration)"),
    ] = "restore",
    on_conflict: Annotated[
        Literal["skip", "overwrite"],
        typer.Option("--on-conflict", help="Conflict handling: skip (default) / overwrite"),
    ] = "skip",
    json_output: Annotated[bool, typer.Option("--json", help="Output the import summary as JSON")] = False,
) -> None:
    """从导出 JSON 恢复，或从文本文件抽取迁移。"""
    config = load_config(get_mex_home())
    ensure_initialized(config.mex_home)
    if mode == "restore":
        _run_import_restore(config, file, on_conflict, json_output)
    else:
        _run_import_extract(config, file, json_output)


@app.command("stats", help="Show statistics (profile/outside counts, pending review, db size, LLM usage)")
@run
def stats_cmd(
    json_output: Annotated[bool, typer.Option("--json", help="Output full statistics as JSON")] = False,
) -> None:
    """统计：画像内/画像外条目数、待审查数、库大小与 LLM 累计用量。"""
    config = load_config(get_mex_home())
    ensure_initialized(config.mex_home)
    conn = connect(config.db_path)
    try:
        data = stats(conn, config.mex_home)
    finally:
        conn.close()
    if json_output:
        print_json(data)
        return
    _print_stats_table(data)


def _run_import_restore(
    config: Config,
    file: str,
    on_conflict: Literal["skip", "overwrite"],
    json_output: bool,
) -> None:
    """restore 模式：恢复导出 JSON 并输出摘要（冲突为提示性质，退出码 0）。"""
    conn = connect(config.db_path)
    try:
        report = import_restore(conn, file, on_conflict=on_conflict)
    finally:
        conn.close()
    _print_restore_summary(report, json_output)


def _run_import_extract(config: Config, file: str, json_output: bool) -> None:
    """extract 模式：读文本走 LLM 抽取迁移（架构 §6.4：不记录增量状态）。"""
    api_key = config.api_key()
    if not config.llm_base_url or not api_key or not config.llm_model:
        raise ExternalError(_llm_config_hint(config.mex_home))
    schema = load_schema(config.schema_path)
    conn = connect(config.db_path)
    try:
        result = import_extract(
            conn,
            file,
            client=_make_client(config, api_key),
            schema=schema,
        )
    except LLMError as exc:
        raise ExternalError(f"LLM call failed ({exc.kind}): {exc}") from exc
    except OSError as exc:
        raise ExternalError(f"Failed to read file: {exc}") from exc
    finally:
        conn.close()
    _print_extract_summary(result, config.llm_model, json_output)


def _make_client(config: Config, api_key: str) -> LLMClient:
    """按配置构造 LLM 客户端（测试可替换此工厂注入替身）。"""
    return LLMClient(
        LLMConfig(
            base_url=config.llm_base_url or "",
            api_key=api_key,
            model=config.llm_model or "",
            timeout_seconds=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
            retry_base_seconds=config.llm_retry_base_seconds,
        ),
    )


def _llm_config_hint(mex_home: str) -> str:
    """未配置 LLM 时的配置指引。"""
    return (
        f"LLM is not configured. Run `mex config llm` to set up the provider, API key and model, "
        f"or edit the llm section of {mex_home}/config.yaml (base_url/api_key/api_key_env/model)."
    )


def _print_export_summary(report: ExportReport, json_output: bool) -> None:
    """输出导出摘要（支持 --json）。"""
    if json_output:
        print_json({"path": report.path, "count": report.count, "forgotten_count": report.forgotten_count})
        return
    typer.echo(f"Exported {report.count} memories (including {report.forgotten_count} soft-deleted) to {report.path}")


def _print_restore_summary(report: ImportReport, json_output: bool) -> None:
    """输出 restore 摘要：新增/跳过/冲突（冲突清单写 stderr，提示性质）。"""
    if json_output:
        print_json({"imported": report.imported, "skipped": report.skipped, "conflicts": report.conflicts})
        return
    typer.echo(f"Imported {report.imported}, skipped {report.skipped}, conflicts {len(report.conflicts)}")
    for conflict in report.conflicts:
        id_part = f" (id={conflict['id']})" if conflict.get("id") else ""
        typer.echo(f"  - conflict: {conflict['reason']}{id_part} {conflict.get('detail', '')}", err=True)


def _print_extract_summary(result: ExtractResult, model: str, json_output: bool) -> None:
    """输出 extract 迁移摘要（与 ``mex extract`` 相同格式，架构 §6.1 第 8 步）。"""
    if json_output:
        print_json(
            {
                "added": result.added,
                "updated": result.updated,
                "discarded": [{"content": c, "reason": r} for c, r in result.discarded],
                "inferred_count": result.inferred_count,
                "llm_usage": {
                    "model": model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            },
        )
        return
    typer.echo(f"Added {result.added}, updated {result.updated}, discarded {len(result.discarded)}")
    for content, reason in result.discarded:
        typer.echo(f"  - discarded: {content} ({reason})")
    typer.echo(f"{result.inferred_count} are AI-inferred, run `mex review` to check them")
    typer.echo(f"LLM usage: {result.prompt_tokens} prompt / {result.completion_tokens} completion tokens")


def _print_stats_table(data: dict) -> None:
    """stats 人类可读表格输出。"""
    shapes = data["profile"]
    usage = data["llm_usage"]
    typer.echo(f"In-profile: {shapes['profile']}")
    typer.echo(f"Out-of-profile: {shapes['outside']}")
    typer.echo(f"Pending review: {data['pending_review']}")
    typer.echo(f"Database size: {_format_bytes(data['db_size_bytes'])}")
    typer.echo(
        f"LLM usage: {usage['prompt_tokens']} prompt / {usage['completion_tokens']} completion tokens, "
        f"{usage['calls']} calls",
    )


def _format_bytes(size: int) -> str:
    """字节数 → 可读大小（B / KB / MB）。"""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"
