"""M5 CLI：``mex extract`` 命令端到端测试（fake LLM 注入，不调真实 API）。

``_make_client`` 工厂替换与 fixture 注入是依赖注入所需。
"""

# ruff: noqa: ARG001, ARG002, ARG005

import json

import pytest
from typer.testing import CliRunner

from mex.cli import app
from mex.cli.extract import extract_cmd  # noqa: F401 - import 触发命令注册
from mex.llm import LLMResponse
from mex.store.connection import init_db

runner = CliRunner()

SAMPLE_YAML = """\
topics:
  work:
    description: 工作与职业
    sub_topics:
      company: { description: 公司 }
  finance:
    description: 财务
    sub_topics:
      risk_appetite: { description: 风险偏好 }
"""

CONFIG_WITH_LLM = (
    "llm:\n  base_url: 'https://api.example.com/v1'\n"
    "  api_key_env: 'DEEPSEEK_API_KEY'\n  model: 'deepseek-chat'\n"
)


class FakeLLM:
    """CLI 测试替身：返回录制响应，记录收到的消息。"""

    model = "fake-model"

    def __init__(self, data: dict):
        self._data = data
        self.calls: list[list[dict]] = []

    def chat_json(self, messages: list[dict]):
        self.calls.append(messages)
        return (
            self._data,
            LLMResponse(text=json.dumps(self._data, ensure_ascii=False), prompt_tokens=10, completion_tokens=5),
        )


def sample_response() -> dict:
    return {
        "memories": [
            {
                "topic": "finance", "sub_topic": None,
                "content": "今天完成了抽取模块开发", "is_ai_inferred": True,
                "confidence": "inferred", "evidence": "用户提到",
            },
        ],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """初始化 MEX_HOME 数据目录并注入 fake LLM。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    (tmp_path / "config.yaml").write_text(CONFIG_WITH_LLM, encoding="utf-8")
    (tmp_path / "schema.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
    init_db(tmp_path / "mex.db")
    fake = FakeLLM(sample_response())
    monkeypatch.setattr("mex.cli.extract._make_client", lambda cfg, key: fake)
    return fake


class TestExtractCommand:
    def test_inline_text(self, env):
        """直接传对话文本。"""
        result = runner.invoke(app, ["extract", "我喜欢喝美式咖啡"])
        assert result.exit_code == 0
        assert "Added 1" in result.stdout
        assert "AI-inferred" in result.stdout
        assert "10 prompt" in result.stdout
        assert env.calls[0][-1]["content"] == "我喜欢喝美式咖啡"

    def test_file_input(self, env, tmp_path):
        p = tmp_path / "conv.txt"
        p.write_text("对话内容", encoding="utf-8")
        result = runner.invoke(app, ["extract", "--file", str(p)])
        assert result.exit_code == 0
        assert "Added 1" in result.stdout
        assert env.calls[0][-1]["content"] == "对话内容"

    def test_from_claude(self, env, tmp_path):
        p = tmp_path / "session.jsonl"
        p.write_text(
            '{"type": "user", "message": {"role": "user", "content": "你好"}}\n'
            '{"type": "assistant", "message": {"role": "assistant", "content": "你好！"}}\n',
            encoding="utf-8",
        )
        result = runner.invoke(app, ["extract", "--file", str(p), "--from", "claude"])
        assert result.exit_code == 0
        assert env.calls[0][-1]["content"] == "用户：你好\n助手：你好！"

    def test_json_output(self, env, tmp_path):
        p = tmp_path / "conv.txt"
        p.write_text("对话内容", encoding="utf-8")
        result = runner.invoke(app, ["extract", "--file", str(p), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["added"] == 1
        assert data["updated"] == 0
        assert data["inferred_count"] == 1
        assert data["llm_usage"]["model"] == "deepseek-chat"

    def test_incremental_state_saved(self, env, tmp_path, monkeypatch):
        p = tmp_path / "conv.txt"
        p.write_text("一" * 50, encoding="utf-8")
        assert runner.invoke(app, ["extract", "--file", str(p)]).exit_code == 0
        assert env.calls[0][-1]["content"] == "一" * 50
        fake2 = FakeLLM({"memories": []})
        monkeypatch.setattr("mex.cli.extract._make_client", lambda cfg, key: fake2)
        p.write_text("一" * 50 + "二" * 30, encoding="utf-8")
        assert runner.invoke(app, ["extract", "--file", str(p)]).exit_code == 0
        assert fake2.calls[0][-1]["content"] == "二" * 30

    def test_no_input_usage_error(self, env):
        result = runner.invoke(app, ["extract"])
        assert result.exit_code == 1
        assert "Provide exactly one input" in result.stderr

    def test_both_text_and_file_rejected(self, env, tmp_path):
        p = tmp_path / "conv.txt"
        p.write_text("x", encoding="utf-8")
        result = runner.invoke(app, ["extract", "对话内容", "--file", str(p)])
        assert result.exit_code == 1
        assert "Provide exactly one input" in result.stderr

    def test_not_initialized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEX_HOME", str(tmp_path))
        result = runner.invoke(app, ["extract", "对话内容"])
        assert result.exit_code == 1
        assert "init" in result.stderr


class TestUnconfiguredLLM:
    def test_missing_api_key(self, tmp_path, monkeypatch):
        """配置了 api_key_env 但环境变量缺失，且文件无 api_key → 未配置。"""
        monkeypatch.setenv("MEX_HOME", str(tmp_path))
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        (tmp_path / "config.yaml").write_text(CONFIG_WITH_LLM, encoding="utf-8")
        (tmp_path / "schema.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
        init_db(tmp_path / "mex.db")
        result = runner.invoke(app, ["extract", "对话内容"])
        assert result.exit_code == 2
        assert "mex config llm" in result.stderr

    def test_missing_llm_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEX_HOME", str(tmp_path))
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
        (tmp_path / "config.yaml").write_text("llm: {}\n", encoding="utf-8")
        (tmp_path / "schema.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
        init_db(tmp_path / "mex.db")
        result = runner.invoke(app, ["extract", "对话内容"])
        assert result.exit_code == 2
        assert "base_url" in result.stderr
