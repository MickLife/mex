"""``mex integrate`` 命令：生成 agent hook 配置与 skill 说明书（M8）。

用法：
    mex integrate claude|opencode|dsh [--scope project|global] [--json]
"""

from __future__ import annotations

from typing import cast

import typer

from mex.cli import app, run
from mex.cli.common import UserError, print_json
from mex.services.integrate import AGENT_TARGETS, AgentName, Scope, IntegrationResult, integrate

_SUPPORTED_AGENTS = "claude, opencode, dsh"
_SCOPES = ("project", "global")


@app.command("integrate", help="Generate agent integration files (hook config + skill guide + README)")
@run
def integrate_cmd(
    agent: str = typer.Argument(..., help="Target agent: claude / opencode / dsh"),
    scope: str = typer.Option("global", "--scope", help="project (current dir) / global (user dir, default)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """生成 agent 集成文件（hook 配置 + skill 说明书 + README）。"""
    if agent not in AGENT_TARGETS and agent != "dsh":
        raise UserError(f"Unknown agent '{agent}'. Supported agents: {_SUPPORTED_AGENTS}")
    if scope not in _SCOPES:
        raise UserError(f"Unknown scope '{scope}'. Usage: --scope project|global")
    result = integrate(cast("AgentName", agent), cast("Scope", scope))
    if json_output:
        print_json({"agent": result.agent, "scope": result.scope, "written_files": result.written_files})
        return
    _print_human(result)


def _print_human(result: IntegrationResult) -> None:
    """文本输出：写入的文件路径清单 + 下一步说明。"""
    typer.echo(f"Generated {result.agent} ({result.scope}) integration files:")
    for path in result.written_files:
        typer.echo(f"  {path}")
    typer.echo(_next_steps(result.agent))


def _next_steps(agent: AgentName) -> str:
    """下一步说明：按 agent 类型给出不同的安装指引。"""
    if agent == "claude":
        hook = "~/.claude/settings.json, project ./.claude/settings.json"
        skill = "~/.claude/skills/mex/SKILL.md, project ./.claude/skills/mex/SKILL.md"
        return (
            f"Next steps: paste the config in hooks.md into the \"hooks\" field of {agent}'s config "
            f"({hook} for global); put the content of skill.md into the skill file ({skill} for global)."
        )
    if agent == "opencode":
        hook = "~/.config/opencode/opencode.json, project ./.opencode/opencode.json"
        skill = "~/.config/opencode/skill/mex/SKILL.md, project ./.opencode/skill/mex/SKILL.md"
        return (
            f"Next steps: paste the config in hooks.md into the \"hooks\" field of {agent}'s config "
            f"({hook} for global); put the content of skill.md into the skill file ({skill} for global)."
        )
    # dsh
    return (
        "Next steps: this is a DSH bundle directory. To install:\n"
        "  1) Install into your profile: dsh plugin --profile <profile> add ./mex-dsh-plugin\n"
        "  2) Or merge the insert lines in cordis.patch.yml into ~/.dsh/cordis.patch.yml\n"
        "Prerequisite: the mex command must be on PATH (or set MEX_BIN to its absolute path)."
    )
