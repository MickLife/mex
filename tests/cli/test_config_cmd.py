"""mex config 命令测试：llm 配置（参数/交互模式）、show 打码显示。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import urllib.error
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mex.cli import app
from mex.cli import config_cmd  # noqa: F401 - 导入即注册命令
from mex import llm
from mex.config import _write_config_yaml, load_config
from mex.store.connection import connect, init_db

runner = CliRunner()


@pytest.fixture()
def mex_home(tmp_path, monkeypatch) -> str:
    """MEX_HOME 指向临时目录。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    return str(tmp_path)


def _init(mex_home: str) -> None:
    """手动建库 + 生成 config.yaml（模拟 mex init 效果，不依赖 M3 命令）。"""
    init_db(Path(mex_home) / "mex.db")
    _write_config_yaml(
        Path(mex_home) / "config.yaml",
        {"llm": {"base_url": "", "api_key": "", "api_key_env": "", "model": ""}},
    )
    connect(Path(mex_home) / "mex.db").close()


def _invoke(*args: str, **kwargs):
    return runner.invoke(app, list(args), **kwargs)


class TestConfigLlmParamMode:
    def test_provider_preset(self, mex_home):
        _init(mex_home)
        result = _invoke("config", "llm", "--provider", "deepseek", "--api-key", "sk-test-1234567890")
        assert result.exit_code == 0
        cfg = load_config(mex_home)
        assert cfg.llm_base_url == "https://api.deepseek.com/v1"
        assert cfg.llm_api_key == "sk-test-1234567890"

    def test_explicit_url_and_model(self, mex_home):
        _init(mex_home)
        result = _invoke(
            "config",
            "llm",
            "--base-url",
            "https://my-proxy.example.com/v1",
            "--api-key",
            "sk-abc",
            "--model",
            "my-model",
        )
        assert result.exit_code == 0
        cfg = load_config(mex_home)
        assert cfg.llm_base_url == "https://my-proxy.example.com/v1"
        assert cfg.llm_model == "my-model"

    def test_preserves_other_fields(self, mex_home):
        _init(mex_home)
        _invoke("config", "llm", "--provider", "deepseek", "--api-key", "sk-1")
        _invoke("config", "llm", "--provider", "openai", "--model", "gpt-4o-mini")
        cfg = load_config(mex_home)
        assert cfg.llm_base_url == "https://api.openai.com/v1"
        assert cfg.llm_api_key == "sk-1"  # 第二次未传 api_key 时保留原值

    def test_config_file_permission_600(self, mex_home):
        _init(mex_home)
        _invoke("config", "llm", "--provider", "deepseek", "--api-key", "sk-2")
        mode = stat.S_IMODE(os.stat(Path(mex_home) / "config.yaml").st_mode)
        assert mode == 0o600

    def test_unknown_provider(self, mex_home):
        _init(mex_home)
        result = _invoke("config", "llm", "--provider", "nope")
        assert result.exit_code == 1
        assert "Unknown provider" in result.stderr

    def test_missing_base_url(self, mex_home):
        _init(mex_home)
        result = _invoke("config", "llm", "--api-key", "sk-x")
        assert result.exit_code == 1
        assert "base_url" in result.stderr

    def test_requires_init(self, mex_home):
        result = _invoke("config", "llm", "--provider", "deepseek")
        assert result.exit_code == 1
        assert "init" in result.stderr


class TestInteractiveLogic:
    """交互输入逻辑单测（CliRunner 无法模拟 TTY，直接测内部函数）。"""

    def test_interactive_full_with_dynamic_models(self, monkeypatch):
        """输入 key 后动态拉取模型列表并从中选择。"""
        answers = iter(["2", "", "sk-interactive-123456", "1"])
        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: next(answers))
        monkeypatch.setattr(
            config_cmd, "fetch_models", lambda *a, **kw: ["deepseek-v4-pro", "deepseek-v4-flash"],
        )
        provider, base_url, api_key, model = config_cmd._interactive_input()
        assert provider == "deepseek"
        assert base_url == "https://api.deepseek.com/v1"
        assert api_key == "sk-interactive-123456"
        assert model == "deepseek-v4-pro"

    def test_interactive_fetch_failure_falls_back_to_preset(self, monkeypatch):
        """动态拉取失败（如离线）时回退到预设模型列表。"""
        answers = iter(["2", "", "sk-1", "2"])

        def _fail(*_a, **_kw):  # noqa: ARG001
            raise config_cmd.LLMError("other", "网络错误")

        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: next(answers))
        monkeypatch.setattr(config_cmd, "fetch_models", _fail)
        provider, base_url, api_key, model = config_cmd._interactive_input()
        assert model == "deepseek-v4-flash"  # 预设列表第 2 项

    def test_interactive_no_key_uses_preset(self, monkeypatch):
        """未输入 key（跳过）时不拉取模型，直接用预设列表。"""
        answers = iter(["2", "", "", "1"])
        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: next(answers))
        provider, base_url, api_key, model = config_cmd._interactive_input()
        assert api_key == ""
        assert model == "deepseek-v4-pro"

    def test_interactive_skip_key_and_model(self, monkeypatch):
        answers = iter(["1", "", "", ""])  # OpenAI → 默认地址 → 跳过 key → 跳过模型
        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: next(answers))
        provider, base_url, api_key, model = config_cmd._interactive_input()
        assert provider == "openai"
        assert base_url == "https://api.openai.com/v1"
        assert api_key == ""
        assert model == ""

    def test_interactive_custom_model_name(self, monkeypatch):
        answers = iter(["2", "", "sk-1", "my-custom-model"])  # 直接输入模型名而非序号
        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: next(answers))
        monkeypatch.setattr(config_cmd, "fetch_models", lambda *a, **kw: [])
        _, _, _, model = config_cmd._interactive_input()
        assert model == "my-custom-model"

    def test_interactive_custom_provider(self, monkeypatch):
        answers = iter(["6", "https://proxy.example.com/v1", "sk-1", "m"])  # 自定义供应商
        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: next(answers))
        monkeypatch.setattr(config_cmd, "fetch_models", lambda *a, **kw: [])
        provider, base_url, _, _ = config_cmd._interactive_input()
        assert provider == "custom"
        assert base_url == "https://proxy.example.com/v1"

    def test_interactive_invalid_provider_number(self, monkeypatch):
        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: "99")
        with pytest.raises(Exception, match="Invalid number"):
            config_cmd._interactive_input()


class TestFetchModels:
    """llm.fetch_models 单测（mock urlopen，不发真实网络请求）。"""

    @staticmethod
    def _fake_response(body: bytes):
        """构造假的 urlopen 响应对象。"""

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        return _Resp()

    def test_success(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, **kwargs):  # noqa: ARG001
            captured["url"] = req.full_url
            captured["header"] = req.get_header("Authorization")
            return self._fake_response(b'{"object": "list", "data": [{"id": "m1"}, {"id": "m2"}]}')

        monkeypatch.setattr(llm.client, "urlopen", fake_urlopen)
        result = llm.client.fetch_models("https://api.x.com/v1", "sk-1")
        assert result == ["m1", "m2"]
        assert captured["url"] == "https://api.x.com/v1/models"
        assert captured["header"] == "Bearer sk-1"

    def test_http_error(self, monkeypatch):
        def fake_urlopen(req, **kwargs):  # noqa: ARG001
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, None)

        monkeypatch.setattr(llm.client, "urlopen", fake_urlopen)
        with pytest.raises(llm.client.LLMError, match="401"):
            llm.client.fetch_models("https://api.x.com/v1", "sk-1")

    def test_missing_data_field(self, monkeypatch):
        monkeypatch.setattr(
            llm.client, "urlopen", lambda req, **kwargs: self._fake_response(b'{"object": "list"}'),  # noqa: ARG001
        )
        with pytest.raises(llm.client.LLMError, match="data"):
            llm.client.fetch_models("https://api.x.com/v1", "sk-1")


class TestInteractiveEndToEnd:
    """交互触发条件（isatty 判定）与写入链路。"""

    def test_interactive_trigger_requires_tty(self, monkeypatch):
        """非 TTY 且无参数时必须报错（base_url 为空），而不是卡死等待输入。"""
        mex_home = _tmp_home(monkeypatch)
        _init(mex_home)
        monkeypatch.setattr(config_cmd.sys, "stdin", _FakeStdin(isatty_result=False))
        result = _invoke("config", "llm")
        assert result.exit_code == 1
        assert "base_url" in result.stderr

    def test_interactive_writes_config(self, monkeypatch):
        """TTY 下走交互分支并正确写入（绕过 CliRunner 的 stdin 接管，直接调函数）。"""
        mex_home = _tmp_home(monkeypatch)
        _init(mex_home)
        answers = iter(["2", "", "sk-interactive-1", "1"])
        monkeypatch.setattr(config_cmd.typer, "prompt", lambda *a, **kw: next(answers))
        monkeypatch.setattr(
            config_cmd, "fetch_models", lambda *a, **kw: ["deepseek-v4-pro", "deepseek-v4-flash"],
        )
        provider, base_url, api_key, model = config_cmd._interactive_input()
        config_cmd.write_llm_config(
            mex_home,
            base_url=base_url,
            api_key=api_key,
            model=model,
            api_key_env=None,
        )
        cfg = load_config(mex_home)
        assert cfg.llm_base_url == "https://api.deepseek.com/v1"
        assert cfg.llm_api_key == "sk-interactive-1"
        assert cfg.llm_model == "deepseek-v4-pro"


def _tmp_home(monkeypatch) -> str:
    """创建并返回一个 MEX_HOME 指向的临时目录（供无 CliRunner 的直接调用）。"""
    home = tempfile.mkdtemp()
    monkeypatch.setenv("MEX_HOME", home)
    return home


class _FakeStdin:
    """模拟 stdin：可配置 isatty 返回值。"""

    def __init__(self, isatty_result: bool = True) -> None:
        self._isatty = isatty_result

    def isatty(self) -> bool:
        return self._isatty


class TestConfigShow:
    def test_show_masks_key(self, mex_home):
        _init(mex_home)
        _invoke("config", "llm", "--provider", "deepseek", "--api-key", "sk-abcdef1234567890")
        result = _invoke("config", "show", "--json")
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["base_url"] == "https://api.deepseek.com/v1"
        assert data["api_key"] is not None
        assert "abcdef1234567890" not in data["api_key"]  # key 被打码
        assert data["api_key"].startswith("sk-")

    def test_show_includes_paths(self, mex_home):
        _init(mex_home)
        result = _invoke("config", "show", "--json")
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["config_path"].endswith("config.yaml")
        assert data["schema_path"].endswith("schema.yaml")
        assert data["db_path"].endswith("mex.db")

    def test_show_not_configured(self, mex_home):
        _init(mex_home)
        result = _invoke("config", "show", "--json")
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["api_key"] is None

    def test_unknown_action(self, mex_home):
        _init(mex_home)
        result = _invoke("config", "bogus")
        assert result.exit_code == 1
        assert "action must be 'llm' or 'show'" in result.stderr
