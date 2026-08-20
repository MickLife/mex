"""``mex extract`` 命令：从对话文本抽取个人记忆（架构文档 §6.1）。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from mex.adapters import Format, read_dialogue
from mex.cli import app, run
from mex.cli.common import ExternalError, UserError, ensure_initialized, print_json
from mex.config import Config, get_mex_home, load_config
from mex.domain.schema import load_schema
from mex.llm.client import LLMClient, LLMConfig, LLMError
from mex.services.extract import ExtractResult, extract_dialogue
from mex.store.connection import connect


@app.command("extract", help="Extract memories from conversation text (calls LLM)")
@run
def extract_cmd(
    text: Annotated[str | None, typer.Argument(help="Conversation text (mutually exclusive with --file)")] = None,
    file: Annotated[
        str | None,
        typer.Option("--file", help="Session file path (mutually exclusive with text; use --from for format)"),
    ] = None,
    source_format: Annotated[
        Format, typer.Option("--from", help="Session file format: plain/claude/opencode"),
    ] = "plain",
    json_output: Annotated[bool, typer.Option("--json", help="Output the extraction summary as JSON")] = False,
) -> None:
    """从对话文本抽取记忆并写入数据库（调用 LLM）。

    两种输入方式：`mex extract "对话文本"`（直接传文本）、
    `mex extract --file <文件> [--from claude|opencode]`（传文件路径）。
    """
    if (text is None) == (file is None):
        raise UserError('Provide exactly one input: `mex extract "conversation text"` or `mex extract --file <file>`')
    config = load_config(get_mex_home())
    ensure_initialized(config.mex_home)
    api_key = config.api_key()
    if not config.llm_base_url or not api_key or not config.llm_model:
        raise ExternalError(_llm_config_hint(config.mex_home))

    if file is not None:
        source_file = str(Path(file).resolve())
        if source_format == "plain":
            text = Path(file).read_text(encoding="utf-8")
        else:
            text = read_dialogue(file, source_format)
    else:
        source_file = None

    schema = load_schema(config.schema_path)
    conn = connect(config.db_path)
    try:
        typer.echo(
            f"Extracting memories (model {config.llm_model}, network connected, waiting for generation)...",
            err=True,
        )
        result = extract_dialogue(
            conn,
            text=text,
            source_file=source_file,
            client=_make_client(config, api_key),
            schema=schema,
        )
    except LLMError as exc:
        raise ExternalError(f"LLM call failed ({exc.kind}): {exc}") from exc
    finally:
        conn.close()
    _print_summary(result, config.llm_model, json_output)


def _make_client(config: Config, api_key: str) -> LLMClient:
    """按配置构造 LLM 客户端（测试可替换此工厂注入替身）。

    使用流式输出：LLM 等待期间通过 ``on_chunk`` 给出实时反馈，
    便于区分"网络正常、模型正在生成"与"请求卡死/超时"。
    """
    started = {"n": 0}

    def _on_chunk(_fragment: str) -> None:
        if started["n"] == 0:
            started["n"] = 1
            typer.echo("Model started generating...", err=True)

    return LLMClient(
        LLMConfig(
            base_url=config.llm_base_url or "",
            api_key=api_key,
            model=config.llm_model or "",
            timeout_seconds=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
            retry_base_seconds=config.llm_retry_base_seconds,
        ),
        stream=True,
        on_chunk=_on_chunk,
    )


def _llm_config_hint(mex_home: str) -> str:
    """未配置 LLM 时的配置指引。"""
    return (
        f"LLM is not configured. Run `mex config llm` to set up the provider, API key and model, "
        f"or edit the llm section of {mex_home}/config.yaml (base_url/api_key/api_key_env/model)."
    )


def _print_summary(result: ExtractResult, model: str, json_output: bool) -> None:
    """输出抽取摘要（架构 §6.1 第 8 步），支持 --json。"""
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
