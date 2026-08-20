"""``mex integrate`` 命令：生成 agent 的 skill 说明书与 README（M8）。

用法：
    mex integrate claude|opencode|workbuddy|dsh [--scope project|global] [--json]
"""

from __future__ import annotations

from typing import cast

import typer

from mex.cli import app, run
from mex.cli.common import UserError, print_json
from mex.services.integrate import AGENT_TARGETS, AgentName, Scope, IntegrationResult, integrate

_SUPPORTED_AGENTS = "claude, opencode, workbuddy, dsh"
_SCOPES = ("project", "global")


@app.command("integrate", help="Generate agent integration files (skill guide + README)")
@run
def integrate_cmd(
    agent: str = typer.Argument(..., help="Target agent: claude / opencode / workbuddy / dsh"),
    scope: str = typer.Option("global", "--scope", help="project (current dir) / global (user dir, default)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """生成 agent 集成文件（skill 说明书 + README）。"""
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
        skill = "~/.claude/skills/mex/SKILL.md (global), ./.claude/skills/mex/SKILL.md (project)"
        return (
            f"Next steps: the skill is ready at {skill}. "
            "Start a new session for it to take effect, then ask the agent to run `mex profile` to verify."
        )
    if agent == "opencode":
        skill = "~/.config/opencode/skills/mex/SKILL.md (global), ./.opencode/skills/mex/SKILL.md (project)"
        return (
            f"Next steps: the skill is ready at {skill}. "
            "Start a new session for it to take effect, then ask the agent to run `mex profile` to verify."
        )
    if agent == "workbuddy":
        skill = "~/.workbuddy/skills/mex/SKILL.md (global), ./.workbuddy/skills/mex/SKILL.md (project)"
        return (
            f"Next steps: the skill is ready at {skill}. "
            "Start a new WorkBuddy session for it to take effect, then ask the agent to run `mex profile` to verify."
        )
    # dsh
    return (
        "Next steps: this is a DSH bundle directory. To install:\n"
        "  1) Install into your profile: dsh plugin --profile <profile> add ./mex-dsh-plugin\n"
        "  2) Or merge the insert lines in cordis.patch.yml into ~/.dsh/cordis.patch.yml\n"
        "Prerequisite: the mex command must be on PATH (or set MEX_BIN to its absolute path)."
    )
