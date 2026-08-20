"""M8 集成 CLI 测试（mex integrate 命令，08-integrate.md §5）。

数据目录一律走 MEX_HOME → pytest tmp_path；HOME 展开用 monkeypatch
Path.home()，project scope 用 monkeypatch chdir，绝不触碰真实用户目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mex.cli import app
from mex.cli import integrate as integrate_cli  # noqa: F401  导入即注册命令
from mex.services.integrate import AGENT_TARGETS

runner = CliRunner()


@pytest.fixture
def mex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """MEX_HOME 指向临时目录并写入 schema.yaml（模拟已 init）。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    (tmp_path / "schema.yaml").write_text("topics: []", encoding="utf-8")
    return tmp_path


def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 Path.home() 重定向到 tmp_path（隔离 global scope 的写入）。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))


def expected_dir(tmp_path: Path, agent: str, scope: str) -> Path:
    """预期的目标根目录（重定向后的路径）。"""
    if scope == "global":
        return tmp_path / AGENT_TARGETS[agent][scope].lstrip("~/")
    return tmp_path / AGENT_TARGETS[agent][scope]


class TestIntegrateCli:
    @pytest.mark.parametrize("agent", ["claude", "opencode"])
    @pytest.mark.parametrize("scope", ["project", "global"])
    def test_success_outputs_paths(self, mex_home, tmp_path, monkeypatch, agent, scope):
        if scope == "global":
            fake_home(tmp_path, monkeypatch)
        else:
            monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["integrate", agent, "--scope", scope])

        assert result.exit_code == 0, result.stderr
        assert "hooks.md" in result.stdout and "skill.md" in result.stdout
        assert "Next steps" in result.stdout
        target = expected_dir(tmp_path, agent, scope)
        for name in ("hooks.md", "skill.md", "README.md"):
            assert (target / name).exists()
            assert str(target / name) in result.stdout

    def test_default_scope_is_global(self, mex_home, tmp_path, monkeypatch):
        fake_home(tmp_path, monkeypatch)

        result = runner.invoke(app, ["integrate", "claude"])

        assert result.exit_code == 0, result.stderr
        assert (tmp_path / ".claude" / "hooks.md").exists()

    def test_unknown_agent_exit_1(self, mex_home, tmp_path, monkeypatch):
        fake_home(tmp_path, monkeypatch)

        result = runner.invoke(app, ["integrate", "gemini"])

        assert result.exit_code == 1
        assert "Unknown agent" in result.stderr
        assert "claude" in result.stderr and "opencode" in result.stderr

    def test_unknown_scope_exit_1(self, mex_home, tmp_path, monkeypatch):
        fake_home(tmp_path, monkeypatch)

        result = runner.invoke(app, ["integrate", "claude", "--scope", "user"])

        assert result.exit_code == 1
        assert "Unknown scope" in result.stderr

    def test_json_output_structure(self, mex_home, tmp_path, monkeypatch):
        fake_home(tmp_path, monkeypatch)

        result = runner.invoke(app, ["integrate", "claude", "--json"])

        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["agent"] == "claude"
        assert data["scope"] == "global"
        assert len(data["written_files"]) == 3
        assert all(p.startswith(str(tmp_path / ".claude")) for p in data["written_files"])

    def test_project_scope_in_cwd(self, mex_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["integrate", "opencode", "--scope", "project"])

        assert result.exit_code == 0, result.stderr
        assert (tmp_path / ".opencode" / "hooks.md").exists()
        assert str(tmp_path) in result.stdout

    def test_not_initialized_exit_1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEX_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["integrate", "claude"])

        assert result.exit_code == 1
        assert "init" in result.stderr
        assert not (tmp_path / ".claude").exists()
