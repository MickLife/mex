"""M5 LLM 层：client.py 单测（绝不调用真实 API，全部打桩 urlopen）。

fake urlopen 桩未使用的 req/timeout 参数是接口兼容需要。
"""

# ruff: noqa: ARG001, ARG002, ARG005

import json
import urllib.error

import pytest

from mex.llm import LLMClient, LLMConfig, LLMError

OK_BODY = json.dumps(
    {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    },
).encode()


_LLM_CONFIG_FIELDS = {
    "base_url", "api_key", "api_key_env", "model",
    "timeout_seconds", "max_retries", "retry_base_seconds",
}


def make_client(**kwargs):
    base = {"base_url": "https://api.example.com/v1", "api_key": "k", "model": "m"}
    config_kwargs = {k: v for k, v in kwargs.items() if k in _LLM_CONFIG_FIELDS}
    client_kwargs = {k: v for k, v in kwargs.items() if k not in _LLM_CONFIG_FIELDS}
    base.update(config_kwargs)
    return LLMClient(LLMConfig(**base), **client_kwargs)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(request, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(request.full_url, code, "err", {}, None)


def ok_response(content: str) -> bytes:
    """OpenAI 兼容响应体：content 作为 message.content，usage 固定 10/5。"""
    return json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    ).encode()


class TestChat:
    def test_success_and_usage(self, monkeypatch):
        monkeypatch.setattr("mex.llm.client.urlopen", lambda req, timeout=None: _FakeResponse(OK_BODY))
        resp = make_client().chat([{"role": "user", "content": "hi"}])
        assert resp.text == "hello"
        assert resp.prompt_tokens == 10
        assert resp.completion_tokens == 5

    def test_request_body(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            captured["headers"] = req.headers
            captured["url"] = req.full_url
            return _FakeResponse(OK_BODY)

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        make_client().chat([{"role": "user", "content": "hi"}])
        assert captured["url"] == "https://api.example.com/v1/chat/completions"
        assert captured["body"]["model"] == "m"
        assert captured["body"]["temperature"] == 0.2
        assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
        assert captured["headers"]["Authorization"] == "Bearer k"

    def test_exponential_backoff_on_429(self, monkeypatch):
        calls = {"n": 0}
        waits = []

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise _http_error(req, 429)
            return _FakeResponse(OK_BODY)

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        monkeypatch.setattr("mex.llm.client.sleep", waits.append)
        resp = make_client(max_retries=3).chat([{"role": "user", "content": "hi"}])
        assert calls["n"] == 4
        assert waits == [2, 4, 8]
        assert resp.text == "hello"

    def test_retry_server_error_once(self, monkeypatch):
        calls = {"n": 0}
        waits = []

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(req, 500)
            return _FakeResponse(OK_BODY)

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        monkeypatch.setattr("mex.llm.client.sleep", waits.append)
        make_client().chat([{"role": "user", "content": "hi"}])
        assert calls["n"] == 2
        assert waits == [2]

    def test_retry_timeout(self, monkeypatch):
        calls = {"n": 0}
        waits = []

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return _FakeResponse(OK_BODY)

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        monkeypatch.setattr("mex.llm.client.sleep", waits.append)
        make_client().chat([{"role": "user", "content": "hi"}])
        assert calls["n"] == 2
        assert waits == [2]

    def test_other_4xx_no_retry(self, monkeypatch):
        calls = {"n": 0}
        waits = []

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise _http_error(req, 400)

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        monkeypatch.setattr("mex.llm.client.sleep", waits.append)
        with pytest.raises(LLMError) as exc_info:
            make_client().chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.kind == "other"
        assert calls["n"] == 1
        assert waits == []

    def test_network_error_no_retry(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        with pytest.raises(LLMError) as exc_info:
            make_client().chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.kind == "other"
        assert calls["n"] == 1

    def test_retries_exhausted(self, monkeypatch):
        waits = []

        def fake_urlopen(req, timeout=None):
            raise _http_error(req, 429)

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        monkeypatch.setattr("mex.llm.client.sleep", waits.append)
        with pytest.raises(LLMError) as exc_info:
            make_client(max_retries=3).chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.kind == "rate_limit"
        assert waits == [2, 4, 8]

    def test_malformed_response(self, monkeypatch):
        monkeypatch.setattr("mex.llm.client.urlopen", lambda req, timeout=None: _FakeResponse(b"not json"))
        with pytest.raises(LLMError) as exc_info:
            make_client().chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.kind == "other"


class TestChatJson:
    def test_plain_json(self, monkeypatch):
        monkeypatch.setattr(
            "mex.llm.client.urlopen",
            lambda req, timeout=None: _FakeResponse(ok_response('{"memories": []}')),
        )
        data, resp = make_client().chat_json([{"role": "user", "content": "hi"}])
        assert data == {"memories": []}
        assert resp.prompt_tokens == 10

    def test_json_code_block_extraction(self, monkeypatch):
        content = '好的，以下是结果：\n```json\n{"memories": [{"layer": "event"}]}\n```\n'
        monkeypatch.setattr("mex.llm.client.urlopen", lambda req, timeout=None: _FakeResponse(ok_response(content)))
        data, _ = make_client().chat_json([{"role": "user", "content": "hi"}])
        assert data == {"memories": [{"layer": "event"}]}

    def test_bare_list_wrapped(self, monkeypatch):
        body = json.dumps(
            {"choices": [{"message": {"content": '[{"layer": "event"}]'}}], "usage": {}},
        ).encode()
        monkeypatch.setattr("mex.llm.client.urlopen", lambda req, timeout=None: _FakeResponse(body))
        data, _ = make_client().chat_json([{"role": "user", "content": "hi"}])
        assert data == {"memories": [{"layer": "event"}]}

    def test_parse_fail_retries_once_then_raise(self, monkeypatch):
        calls = {"n": 0}
        captured = {}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            captured["messages"] = json.loads(req.data)["messages"]
            return _FakeResponse(ok_response("not valid json at all"))

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        with pytest.raises(LLMError) as exc_info:
            make_client().chat_json([{"role": "user", "content": "hi"}])
        assert exc_info.value.kind == "parse"
        assert calls["n"] == 2
        assert "合法 JSON" in captured["messages"][-1]["content"]

    def test_parse_fail_retry_succeeds(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(ok_response("garbage"))
            return _FakeResponse(ok_response('{"memories": []}'))

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        data, _ = make_client().chat_json([{"role": "user", "content": "hi"}])
        assert calls["n"] == 2
        assert data == {"memories": []}

    def test_top_level_scalar_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "mex.llm.client.urlopen",
            lambda req, timeout=None: _FakeResponse(ok_response('"just a string"')),
        )
        with pytest.raises(LLMError) as exc_info:
            make_client().chat_json([{"role": "user", "content": "hi"}])
        assert exc_info.value.kind == "parse"


class _FakeStream:
    """可逐行迭代的 SSE 流式响应（模拟 HTTPResponse）。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def sse_frame(obj: object) -> bytes:
    """构造一行 SSE data 帧（含结尾空行）。"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def sse_delta(text: str, *, usage: dict | None = None, finish: str | None = None) -> bytes:
    """构造一个携带 content 增量的 data 帧。"""
    frame: dict = {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}
    if usage:
        frame["usage"] = usage
    return sse_frame(frame)


class TestStreaming:
    """流式输出分支：SSE 聚合、回调与 usage 记录。"""

    def test_chat_stream_aggregates_and_calls_back(self, monkeypatch):
        done: list[str] = []
        stream = _FakeStream(
            [
                sse_frame({"choices": [{"delta": {"reasoning_content": "思考"}}]}),
                sse_delta('{"mem'),
                sse_delta('ories": []}'),
                sse_frame({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7}}),
                b"data: [DONE]\n\n",
            ],
        )
        monkeypatch.setattr("mex.llm.client.urlopen", lambda req, timeout=None: stream)
        resp = make_client().chat([{"role": "user", "content": "hi"}], stream=True, on_chunk=done.append)
        assert resp.text == '{"memories": []}'
        assert resp.prompt_tokens == 12
        assert resp.completion_tokens == 7
        # 推理片段与 content 片段都被回调，但不含 usage 帧
        assert done == ["思考", '{"mem', 'ories": []}']

    def test_chat_stream_body_includes_stream_flag(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeStream([b"data: [DONE]\n\n"])

        monkeypatch.setattr("mex.llm.client.urlopen", fake_urlopen)
        make_client().chat([{"role": "user", "content": "hi"}], stream=True)
        assert captured["body"]["stream"] is True
        assert captured["body"]["stream_options"]["include_usage"] is True

    def test_instance_level_stream_default(self, monkeypatch):
        """实例级 stream/on_chunk：chat 不显式传参也走流式。"""
        done: list[str] = []
        stream = _FakeStream([sse_delta('{"memories": []}'), b"data: [DONE]\n\n"])
        monkeypatch.setattr("mex.llm.client.urlopen", lambda req, timeout=None: stream)
        data, _ = make_client(stream=True, on_chunk=done.append).chat_json([{"role": "user", "content": "hi"}])
        assert done == ['{"memories": []}']
        assert data == {"memories": []}
