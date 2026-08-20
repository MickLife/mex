"""config.py 与 cli 通用件单测（公共骨架）。"""

import pytest
from typer.testing import CliRunner

from mex import config
from mex.cli import app, run
from mex.cli.common import ExternalError, UserError, ensure_initialized, print_json, render_memory_entries
from mex.domain.memory import Confidence, Memory
from mex.store.connection import init_db

runner = CliRunner()


def _make_memory(**kwargs):
    defaults = {
        "id": "m-1",
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "is_ai_inferred": False,
        "confidence": Confidence.EXPLICIT,
        "evidence": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


@app.command("test-cmd-user-error")
@run
def _test_cmd_user_error() -> None:  # pragma: no cover - 仅测试注册
    raise UserError("参数不对")


@app.command("test-cmd-external-error")
@run
def _test_cmd_external_error() -> None:  # pragma: no cover - 仅测试注册
    raise ExternalError("外部挂了")


class TestGetMexHome:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("MEX_HOME", raising=False)
        assert config.get_mex_home() == config.DEFAULT_MEX_HOME

    def test_env_override(self, tmp_path, monkeypatch):
        custom = str(tmp_path / "custom-mex")
        monkeypatch.setenv("MEX_HOME", custom)
        assert config.get_mex_home() == custom


class TestLoadConfig:
    def test_missing_file_uses_defaults(self, tmp_path):
        cfg = config.load_config(str(tmp_path))
        assert cfg.mex_home == str(tmp_path)
        assert cfg.db_path == str(tmp_path / "mex.db")
        assert cfg.llm_base_url is None
        assert cfg.llm_timeout_seconds == 600
        assert cfg.profile_max_tokens == 3000

    def test_full_config_file(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "llm:\n  base_url: 'https://x/v1'\n  api_key: 'sk-file-key'\n  api_key_env: 'MY_KEY'\n  model: 'm'\n"
            "  timeout_seconds: 30\n  max_retries: 2\n  retry_base_seconds: 3\n"
            "profile:\n  max_tokens: 800\n",
            encoding="utf-8",
        )
        cfg = config.load_config(str(tmp_path))
        assert cfg.llm_base_url == "https://x/v1"
        assert cfg.llm_api_key == "sk-file-key"
        assert cfg.llm_api_key_env == "MY_KEY"
        assert cfg.llm_model == "m"
        assert cfg.llm_timeout_seconds == 30
        assert cfg.llm_max_retries == 2
        assert cfg.llm_retry_base_seconds == 3
        assert cfg.profile_max_tokens == 800

    def test_bad_yaml_uses_defaults(self, tmp_path):
        (tmp_path / "config.yaml").write_text("{{{ not yaml", encoding="utf-8")
        cfg = config.load_config(str(tmp_path))
        assert cfg.llm_base_url is None

    def test_api_key_from_file(self, tmp_path):
        (tmp_path / "config.yaml").write_text("llm:\n  api_key: 'sk-file-key'\n", encoding="utf-8")
        cfg = config.load_config(str(tmp_path))
        assert cfg.api_key() == "sk-file-key"

    def test_api_key_env_overrides_file(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "llm:\n  api_key: 'sk-file-key'\n  api_key_env: 'MY_KEY'\n", encoding="utf-8",
        )
        cfg = config.load_config(str(tmp_path))
        assert cfg.api_key() == "sk-file-key"  # 环境变量未设置时用文件值
        monkeypatch.setenv("MY_KEY", "sk-env-key")
        assert cfg.api_key() == "sk-env-key"  # 环境变量设置后优先

    def test_api_key_env_without_file(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text("llm:\n  api_key_env: 'MY_KEY'\n", encoding="utf-8")
        cfg = config.load_config(str(tmp_path))
        assert cfg.api_key() is None
        monkeypatch.setenv("MY_KEY", "sk-env-key")
        assert cfg.api_key() == "sk-env-key"


class TestCommon:
    def test_ensure_initialized_raises(self, tmp_path):
        with pytest.raises(UserError, match="init"):
            ensure_initialized(str(tmp_path))

    def test_ensure_initialized_ok(self, tmp_path):
        init_db(tmp_path / "mex.db")
        ensure_initialized(str(tmp_path))

    def test_render_table(self):
        text = render_memory_entries([_make_memory()])
        assert "work" in text and "company" in text
        assert "华为" in text

    def test_render_table_forgotten_mark(self):
        m = _make_memory(forgotten_at="2026-01-02T00:00:00Z")
        assert "[forgotten]" in render_memory_entries([m])

    def test_print_json(self, capsys):
        print_json({"a": "中文"})
        assert '"中文"' in capsys.readouterr().out


class TestCliRunDecorator:
    def test_user_error_exit_code(self):
        result = runner.invoke(app, ["test-cmd-user-error"])
        assert result.exit_code == 1
        assert "参数不对" in result.stderr

    def test_external_error_exit_code(self):
        result = runner.invoke(app, ["test-cmd-external-error"])
        assert result.exit_code == 2

    def test_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "mex" in result.stdout

    def test_no_args_help(self):
        result = runner.invoke(app, [])
        assert result.exit_code == 2  # Typer：无命令时显示帮助并返回 2
        assert "Usage" in result.stdout
