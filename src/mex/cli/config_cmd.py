"""``mex config``：LLM 供应商与模型配置命令。

- ``mex config llm``：交互式配置（TTY 下引导选择供应商 → 地址 → key → 模型）
- ``mex config llm --provider deepseek --api-key xxx``：非交互参数模式（脚本友好）
- ``mex config show``：查看当前 LLM 配置（key 打码显示）
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from mex.cli import app, run
from mex.cli.common import UserError, ensure_initialized, print_json
from mex.config import get_mex_home, load_config, write_llm_config
from mex.llm.client import LLMError, fetch_models

LLM_PRESETS: dict[str, dict[str, object]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "o1-mini"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
    },
    "ollama": {
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "models": ["llama3.1", "qwen2.5"],
    },
    "moonshot": {
        "label": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k"],
    },
    "qwen": {
        "label": "Alibaba Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus"],
    },
    "custom": {"label": "Custom (manual URL)", "base_url": "", "models": []},
}

_PROVIDER_ORDER = ["openai", "deepseek", "ollama", "moonshot", "qwen", "custom"]


@app.command("config", help="Configure or view the LLM provider and model")
@run
def config_cmd(  # noqa: PLR0913, PLR0917 - Typer 命令参数即 CLI 契约，不可合并
    action: Annotated[str, typer.Argument(help="llm (configure LLM) or show (view current config)")],
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Provider preset name (openai/deepseek/ollama/moonshot/qwen/custom)"),
    ] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="OpenAI-compatible API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key (stored in config.yaml, mode 600)")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Model name")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
) -> None:
    """配置或查看 LLM 供应商与模型。"""
    if action == "show":
        _show()
        return
    if action != "llm":
        raise UserError("action must be 'llm' or 'show'")
    _configure_llm(provider=provider, base_url=base_url, api_key=api_key, model=model, json_out=json_out)


def _show() -> None:
    """展示当前配置（api_key 打码）+ 关键文件路径。"""
    cfg = _ready_config()
    masked = _mask_key(cfg.api_key())
    print_json(
        {
            "base_url": cfg.llm_base_url,
            "api_key": masked,
            "model": cfg.llm_model,
            "api_key_env": cfg.llm_api_key_env,
            "config_path": cfg.mex_home + "/config.yaml",
            "schema_path": cfg.schema_path,
            "db_path": cfg.db_path,
        },
    )


def _configure_llm(
    *,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    json_out: bool,
) -> None:
    """配置 LLM：交互模式（TTY 且未提供参数）或参数模式。"""
    mex_home = get_mex_home()
    ensure_initialized(mex_home)
    if _is_interactive() and not (provider or base_url or api_key or model):
        provider, base_url, api_key, model = _interactive_input()
    _validate_provider(provider)
    base_url = _resolve_base_url(provider, base_url)
    if not base_url:
        raise UserError("base_url is empty: provide --base-url or a valid --provider")
    api_key = api_key or None
    model = model or None
    write_llm_config(
        mex_home,
        base_url=base_url,
        api_key=api_key,
        api_key_env=None,
        model=model,
    )
    _emit_result(mex_home, base_url, model, api_key, json_out)


def _interactive_input() -> tuple[str, str, str, str]:
    """TTY 交互引导：选择供应商 → 地址 → key → 模型。

    模型列表优先**动态拉取**（GET /models）；拉取失败（离线/端点不支持）时
    回退到供应商预设列表，仍失败则手动输入。返回 (provider, base_url, api_key, model)。
    """
    typer.echo("Select the LLM provider:")
    for i, name in enumerate(_PROVIDER_ORDER, start=1):
        preset = LLM_PRESETS[name]
        typer.echo(f"  {i}. {preset['label']} ({preset['base_url'] or 'custom'})")
    raw = typer.prompt("Enter a number", default="1")
    try:
        provider = _PROVIDER_ORDER[int(raw) - 1]
    except (ValueError, IndexError) as exc:
        raise UserError(f"Invalid number: {raw}") from exc
    preset = LLM_PRESETS[provider]
    default_url = str(preset["base_url"])
    base_url = typer.prompt("API base URL (Enter for default)", default=default_url) or default_url
    typer.echo(
        "Note: the API key input is not echoed (normal anti-shoulder-surfing behavior). "
        "Paste it (macOS: Cmd+V or right-click paste) and press Enter.",
    )
    api_key = typer.prompt(
        "API key (paste and press Enter; Enter to skip, add later with --api-key)",
        hide_input=True,
        default="",
    )
    model = _pick_model(base_url, api_key, preset)
    return provider, base_url, api_key, model


def _pick_model(base_url: str, api_key: str, preset: dict[str, object]) -> str:
    """选择模型：动态拉取可用列表（成功则从中选），失败回退预设，再失败手动输入。"""
    if api_key:
        typer.echo("Fetching available models...")
        try:
            models = fetch_models(base_url, api_key)
            if models:
                return _select_from_list("Available models", models)
        except LLMError as exc:
            typer.echo(f"Fetch failed ({exc}), falling back to preset models.", err=True)
    fallback = list(preset["models"])
    if fallback:
        return _select_from_list("Preset models (offline fallback)", fallback)
    return typer.prompt("Model name", default="") or ""


def _select_from_list(title: str, models: list[str]) -> str:
    """从列表中交互选择：输入序号或直接输入模型名（回车跳过）。"""
    typer.echo(f"{title}:")
    for i, m in enumerate(models, start=1):
        typer.echo(f"  {i}. {m}")
    raw = typer.prompt("Enter a number or type a model name directly (Enter to skip)", default="")
    if raw.isdigit() and 1 <= int(raw) <= len(models):
        return models[int(raw) - 1]
    return raw


def _validate_provider(provider: str | None) -> None:
    """provider 参数合法性校验。"""
    if provider is not None and provider not in LLM_PRESETS:
        raise UserError(f"Unknown provider: {provider}. Available: {', '.join(_PROVIDER_ORDER)}")


def _resolve_base_url(provider: str | None, base_url: str | None) -> str:
    """地址解析：显式 base_url 优先，其次供应商 preset 默认值。"""
    if base_url:
        return base_url
    if provider:
        return str(LLM_PRESETS[provider]["base_url"])
    return ""


def _is_interactive() -> bool:
    """当前是否为交互终端（非交互时不引导，避免脚本卡死）。"""
    return sys.stdin.isatty()


def _mask_key(key: str | None) -> str | None:
    """key 打码显示：保留前 3 后 4，其余用 * 替代。"""
    if not key:
        return None
    if len(key) <= 7:
        return "*" * len(key)
    return f"{key[:3]}{'*' * (len(key) - 7)}{key[-4:]}"


def _emit_result(mex_home: str, base_url: str, model: str, api_key: str | None, json_out: bool) -> None:
    """输出配置结果（key 打码）。"""
    if json_out:
        print_json(
            {
                "base_url": base_url,
                "model": model,
                "api_key": _mask_key(api_key),
                "config_path": f"{mex_home}/config.yaml",
            },
        )
        return
    typer.echo(f"LLM config saved ({mex_home}/config.yaml, mode 600)")
    typer.echo(f"  base_url: {base_url}")
    typer.echo(f"  model: {model or '(not set)'}")
    typer.echo(f"  api_key: {_mask_key(api_key) or '(not set, use --api-key to add)'}")


def _ready_config():
    """已初始化校验 + 配置快照。"""
    mex_home = get_mex_home()
    ensure_initialized(mex_home)
    return load_config(mex_home)
