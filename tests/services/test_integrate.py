"""M8 集成服务测试（services/integrate.py，08-integrate.md §5）。

数据目录一律走 MEX_HOME → pytest tmp_path；涉及 HOME 展开的测试
monkeypatch Path.home()，绝不触碰真实用户目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mex.cli.common import UserError
from mex.services.integrate import AGENT_TARGETS, integrate

AGENTS = ("claude", "opencode", "workbuddy")
SCOPES = ("project", "global")


@pytest.fixture
def mex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """MEX_HOME 指向临时目录并写入 schema.yaml（模拟已 init）。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    (tmp_path / "schema.yaml").write_text("topics: []", encoding="utf-8")
    return tmp_path


def target_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, scope: str) -> Path:
    """把目标根目录重定向到 tmp_path 下，返回预期目标目录。

    global 用 monkeypatch Path.home()；project 用 monkeypatch chdir。
    """
    if scope == "global":
        monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))
        return tmp_path / AGENT_TARGETS[agent][scope].lstrip("~/")
    monkeypatch.chdir(tmp_path)
    return tmp_path / AGENT_TARGETS[agent][scope]


class TestIntegrate:
    # 各 agent 生成物：skill 说明书（claude/opencode 走官方 skills/mex/SKILL.md 发现路径，
    # workbuddy 的 base 本身就是 skill 根目录 → mex/SKILL.md）+ README。
    GENERATED_FILES = {
        "claude": ("skills/mex/SKILL.md", "README.md"),
        "opencode": ("skills/mex/SKILL.md", "README.md"),
        "workbuddy": ("mex/SKILL.md", "README.md"),
    }

    @pytest.mark.parametrize("agent", AGENTS)
    @pytest.mark.parametrize("scope", SCOPES)
    def test_generates_skill_and_readme(self, mex_home, tmp_path, monkeypatch, agent, scope):
        expected = target_dir(tmp_path, monkeypatch, agent, scope)

        result = integrate(agent, scope)

        assert result.agent == agent and result.scope == scope
        assert len(result.written_files) == 2
        for rel in self.GENERATED_FILES[agent]:
            assert (expected / rel).exists()
            assert str(expected / rel) in result.written_files

    @pytest.mark.parametrize("agent", AGENTS)
    @pytest.mark.parametrize("scope", SCOPES)
    def test_placeholders_replaced(self, mex_home, tmp_path, monkeypatch, agent, scope):
        expected = target_dir(tmp_path, monkeypatch, agent, scope)

        integrate(agent, scope)

        for rel in self.GENERATED_FILES[agent]:
            content = (expected / rel).read_text(encoding="utf-8")
            assert "{{" not in content, f"{rel} 仍含未替换占位符"
            assert str(mex_home) in content
            assert "mex.db" in content

    @pytest.mark.parametrize("agent", AGENTS)
    def test_idempotent(self, mex_home, tmp_path, monkeypatch, agent):
        expected = target_dir(tmp_path, monkeypatch, agent, "project")

        first = integrate(agent, "project")
        second = integrate(agent, "project")

        assert first.written_files == second.written_files
        for rel in self.GENERATED_FILES[agent]:
            assert (expected / rel).exists()
        skill_rel = self.GENERATED_FILES[agent][0]
        assert "{{" not in (expected / skill_rel).read_text(encoding="utf-8")

    def test_not_initialized_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEX_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)

        with pytest.raises(UserError, match="mex init"):
            integrate("claude", "project")

    def test_unknown_agent_raises(self, mex_home, tmp_path, monkeypatch):
        target_dir(tmp_path, monkeypatch, "claude", "global")
        with pytest.raises(KeyError):
            integrate("gemini", "global")  # type: ignore[arg-type]

    def test_unknown_scope_raises(self, mex_home, tmp_path, monkeypatch):
        target_dir(tmp_path, monkeypatch, "claude", "global")
        with pytest.raises(KeyError):
            integrate("claude", "user")  # type: ignore[arg-type]


class TestIntegrateDsh:
    """dsh 产出标准 DSH bundle 目录（六件套：Host/Client 双 half），形态与 claude/opencode 的三件 .md 不同。"""

    DSH_FILES = ("index.js", "panel.js", "client.js", "package.json", "cordis.patch.yml", "README.md")

    @pytest.fixture
    def mex_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """MEX_HOME 指向临时目录并写入 schema.yaml（模拟已 init）。"""
        monkeypatch.setenv("MEX_HOME", str(tmp_path))
        (tmp_path / "schema.yaml").write_text("topics: []", encoding="utf-8")
        return tmp_path

    @pytest.mark.parametrize("scope", SCOPES)
    def test_generates_bundle(self, mex_home, tmp_path, monkeypatch, scope):
        if scope == "global":
            monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))
            expected = tmp_path / ".dsh" / "mex-dsh-plugin"
        else:
            monkeypatch.chdir(tmp_path)
            expected = tmp_path / "mex-dsh-plugin"

        result = integrate("dsh", scope)

        assert result.agent == "dsh" and result.scope == scope
        assert len(result.written_files) == 6
        for name in self.DSH_FILES:
            assert (expected / name).exists()
            assert str(expected / name) in result.written_files

    def test_bundle_manifest_valid(self, mex_home, tmp_path, monkeypatch):
        """index.js 与 package.json 是标准 DSH bundle 的必备要素。"""
        monkeypatch.chdir(tmp_path)
        integrate("dsh", "project")

        pkg = (tmp_path / "mex-dsh-plugin" / "package.json").read_text(encoding="utf-8")
        assert "mex-dsh-plugin" in pkg
        assert '"type": "module"' in pkg
        assert '"client"' in pkg  # Client half 入口

        js = (tmp_path / "mex-dsh-plugin" / "index.js").read_text(encoding="utf-8")
        assert "@deepseek-ai/dsh-tools" in js
        assert "export function apply" in js
        assert "defineTool" in js
        assert "applyMexPanel" in js  # Host 面板逻辑已接入

        client = (tmp_path / "mex-dsh-plugin" / "client.js").read_text(encoding="utf-8")
        assert "__ModuleLoader__.load" in client  # Client half 打包格式
        assert "shell.overlay" in client  # 注册到浮动层
        assert "Add to MeX" in client  # 抽取按钮

        panel = (tmp_path / "mex-dsh-plugin" / "panel.js").read_text(encoding="utf-8")
        assert "agent/turn-stopping" in panel  # 每轮结束感知
        assert "/mex/panel-state" in panel
        assert "/mex/extract" in panel
        assert "subagents.start" in panel  # 触发新 agent 对话

    def test_idempotent(self, mex_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        first = integrate("dsh", "project")
        second = integrate("dsh", "project")
        assert first.written_files == second.written_files
