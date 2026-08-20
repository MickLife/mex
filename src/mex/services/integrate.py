"""M8 agent 集成服务：生成 skill 说明书与 README 文件（架构文档 §8、ADR-12）。

仅生成文件、不执行任何外部命令；幂等（重复执行覆盖旧文件，不报错）。

claude / opencode / workbuddy 产出 skill 说明书 + README.md（写入 agent 的 skill 发现目录）。
不再生成 hooks.md——OpenCode 原生不支持 SessionEnd hook（官方配置 schema 无 hooks 字段），
写路径由 skill 引导 agent 即时写承担，hook 能力留待扩展；
dsh 产出标准 DSH bundle 目录（index.js + panel.js + client.js + package.json + cordis.patch.yml + README.md）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from loguru import logger

from mex.cli.common import UserError
from mex.config import get_mex_home

AgentName = Literal["claude", "opencode", "workbuddy", "dsh"]
Scope = Literal["project", "global"]

# 各 agent 的生成物布局：(模板文件名, 目标相对路径)。
# claude/opencode 的 base 是配置根目录，skill 写入官方 skills/<name>/SKILL.md（skills 为复数，
# Claude Code / OpenCode 的官方发现路径）；workbuddy 的 base 本身就是 skill 根目录
# （~/.workbuddy/skills / ./.workbuddy/skills），skill 直接写入 mex/SKILL.md。
# README 一律写 base 根目录。hooks.md 不再生成（见模块 docstring）。
SKILL_FILE_LAYOUT: dict[AgentName, tuple[tuple[str, str], ...]] = {
    "claude": (("skill.md", "skills/mex/SKILL.md"), ("README.md", "README.md")),
    "opencode": (("skill.md", "skills/mex/SKILL.md"), ("README.md", "README.md")),
    "workbuddy": (("skill.md", "mex/SKILL.md"), ("README.md", "README.md")),
}

# DSH bundle 的组成文件（独立于 SKILL_FILE_LAYOUT 的清单）。
# index.js = Host half（工具注册）；panel.js = Host 面板逻辑（抽取 agent 触发）；
# client.js = Client half（浏览器 UI 面板）；package.json 声明 dsh.client 入口。
DSH_BUNDLE_FILES = ("index.js", "panel.js", "client.js", "package.json", "cordis.patch.yml", "README.md")


@dataclass(frozen=True)
class IntegrationResult:
    """集成结果：实际写入的文件绝对路径清单。"""

    agent: AgentName
    scope: Scope
    written_files: list[str]


# 目标根目录（claude/opencode/workbuddy）：global 用 ~ 前缀（Path.home() 展开）；project 为相对当前目录。
# dsh 的目标目录单独处理（见 _resolve_base_dir 分支），不在此表内。
AGENT_TARGETS: dict[AgentName, dict[Scope, str]] = {
    "claude": {"global": "~/.claude", "project": ".claude"},
    "opencode": {"global": "~/.config/opencode", "project": ".opencode"},
    "workbuddy": {"global": "~/.workbuddy/skills", "project": ".workbuddy/skills"},
}

_PLACEHOLDERS = (
    "{{MEX_HOME}}",
    "{{DB_PATH}}",
    "{{AGENT_DIR}}",
    "{{PROJECT_DIR}}",
)


def integrate(agent: AgentName, scope: Scope) -> IntegrationResult:
    """生成 agent 集成文件。

    claude/opencode/workbuddy：渲染 skill 说明书 + README 写入 agent 的 skill 发现目录；
    dsh：复制标准 DSH bundle 目录到目标位置。

    Args:
        agent: 目标 agent（claude / opencode / workbuddy / dsh）。
        scope: 作用域（project 写当前目录，global 写用户目录）。

    Returns:
        写入结果（文件绝对路径清单）。

    Raises:
        UserError: meX 尚未初始化（缺少 schema.yaml）。
    """
    _require_initialized()
    base_dir = _resolve_base_dir(agent, scope)
    base_dir.mkdir(parents=True, exist_ok=True)
    written = _generate_dsh_bundle(base_dir) if agent == "dsh" else _generate_skill_files(agent, base_dir)
    return IntegrationResult(agent=agent, scope=scope, written_files=written)


def _generate_skill_files(agent: AgentName, base_dir: Path) -> list[str]:
    """渲染 claude/opencode/workbuddy 的 skill 说明书 + README 写入 base_dir。

    目标相对路径取自 ``SKILL_FILE_LAYOUT[agent]``（claude/opencode 落到
    ``base_dir/skills/mex/SKILL.md``，workbuddy 落到 ``base_dir/mex/SKILL.md``），README 落根目录。

    Args:
        agent: 目标 agent（claude / opencode / workbuddy）。
        base_dir: agent 配置根目录。

    Returns:
        写入的文件绝对路径清单。
    """
    mex_home = get_mex_home()
    written: list[str] = []
    for template_name, rel_path in SKILL_FILE_LAYOUT[agent]:
        target = base_dir / rel_path
        content = _render_template(agent, template_name, base_dir, mex_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(str(target))
        logger.info("已生成 {}：{}", agent, target)
    return written


def _generate_dsh_bundle(base_dir: Path) -> list[str]:
    """复制 DSH bundle 目录（index.js/panel.js/client.js 等六件套）到 base_dir。"""
    written: list[str] = []
    for name in DSH_BUNDLE_FILES:
        source = resources.files("mex.integration").joinpath("dsh", name)
        target = base_dir / name
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(str(target))
        logger.info("已生成 dsh：{}", target)
    return written


def _require_initialized() -> None:
    """校验 meX 已初始化：MEX_HOME 下存在 schema.yaml（init 后才有默认 schema）。"""
    mex_home = get_mex_home()
    if not (Path(mex_home) / "schema.yaml").exists():
        raise UserError(f"Data directory {mex_home} is not initialized. Run `mex init` first.")


def _resolve_base_dir(agent: AgentName, scope: Scope) -> Path:
    """解析目标根目录：global 用 Path.home() 展开 ~ 前缀，project 相对当前目录。

    dsh 产出独立 bundle 目录（非 agent 配置文件目录）：
    project → ./mex-dsh-plugin；global → ~/.dsh/mex-dsh-plugin。

    Args:
        agent: 目标 agent。
        scope: 作用域。

    Returns:
        目标根目录（绝对路径）。
    """
    if agent == "dsh":
        raw = "~/.dsh/mex-dsh-plugin" if scope == "global" else "./mex-dsh-plugin"
    else:
        raw = AGENT_TARGETS[agent][scope]
    if raw.startswith("~/"):
        return Path.home() / raw[2:]
    return Path(os.getcwd()) / raw


def _render_template(agent: AgentName, name: str, base_dir: Path, mex_home: str) -> str:
    """读取模板并用简单占位符替换渲染（不引入模板引擎）。

    Args:
        agent: 目标 agent（决定模板子目录）。
        name: 模板文件名（skill.md / README.md）。
        base_dir: 目标根目录（渲染进 {{AGENT_DIR}}）。
        mex_home: 数据目录（渲染进 {{MEX_HOME}} / {{DB_PATH}}）。

    Returns:
        渲染后的模板文本。
    """
    template = (
        resources.files("mex.integration")
        .joinpath(agent, name)
        .read_text(encoding="utf-8")
    )
    values = {
        "{{MEX_HOME}}": mex_home,
        "{{DB_PATH}}": str(Path(mex_home) / "mex.db"),
        "{{AGENT_DIR}}": str(base_dir),
        "{{PROJECT_DIR}}": os.getcwd(),
    }
    for placeholder in _PLACEHOLDERS:
        template = template.replace(placeholder, values[placeholder])
    return template
