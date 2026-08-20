"""OpenAI 兼容 LLM 客户端（架构文档 §7：LLM 集成契约）。

实现说明：
- 使用标准库 urllib，不引入 requests；
- 失败重试策略（§7.3）：超时 / HTTP 429 / 5xx 走指数退避，间隔
  ``retry_base_seconds × 2^(n-1)``（2s → 4s → 8s），最多重试 ``max_retries`` 次；
  其余 4xx 与网络错误不重试；
- 每次调用返回 usage（prompt/completion tokens）。
"""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import dataclass
from time import sleep
from typing import Callable, Literal
from urllib.request import Request, urlopen

from loguru import logger

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_REMINDER = "你的输出不是合法 JSON，请只输出 JSON，不要任何其他文字。"
_SSE_DONE = "[DONE]"  # SSE 流的结束标记帧

ErrorKind = Literal["timeout", "rate_limit", "server", "parse", "other"]


@dataclass(frozen=True)
class LLMConfig:
    """LLM 端点配置（来自 config.yaml，见架构文档 §9）。"""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 60
    max_retries: int = 3
    retry_base_seconds: int = 2


class LLMError(Exception):
    """LLM 调用失败。

    Attributes:
        kind: 失败类型：timeout / rate_limit / server / parse / other。
    """

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class LLMResponse:
    """一次成功的 LLM 调用结果。"""

    text: str
    prompt_tokens: int
    completion_tokens: int


class _RetryableError(Exception):
    """可重试的临时失败（超时 / 限流 / 服务端错误）。"""

    def __init__(self, kind: Literal["timeout", "rate_limit", "server"], message: str) -> None:
        super().__init__(message)
        self.kind = kind


class LLMClient:
    """基于 urllib 的 OpenAI 兼容 ``chat/completions`` 客户端。

    Args:
        config: 端点配置（base_url / api_key / model / 重试参数）。
        stream: 实例级流式开关；单次调用的 ``stream`` 参数若为 None 则回退到它。
        on_chunk: 实例级片段回调（流式时每次增量触发；单次调用未显式传回调时用它）。
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        stream: bool = False,
        on_chunk: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._stream = stream
        self._on_chunk = on_chunk

    @property
    def model(self) -> str:
        """模型名（llm_usage 记录用）。"""
        return self._config.model

    @property
    def stream(self) -> bool:
        """实例级流式开关。"""
        return self._stream

    def chat(
        self,
        messages: list[dict],
        *,
        stream: bool | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """调用 chat/completions，失败按指数退避重试。

        Args:
            messages: OpenAI 消息列表（``[{role, content}, ...]``）。
            stream: 是否开启流式输出（SSE）。None（默认）表示用实例级 :attr:`stream`。
                开启时通过 ``on_chunk`` 回调增量片段，CLI/网页可借此给出实时进度。
            on_chunk: 每个生成片段（content 或推理过程文本）的回调。None（默认）表示
                用实例级回调；可用于展示"网络正常、正在生成"。非流式下忽略。

        Returns:
            响应文本与 usage。

        Raises:
            LLMError: 重试耗尽（kind 为 timeout/rate_limit/server）或不可重试错误（other）。
        """
        use_stream = self._stream if stream is None else stream
        use_chunk = self._on_chunk if on_chunk is None else on_chunk
        body_fields: dict = {
            "model": self._config.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if use_stream:
            # stream_options.include_usage 让最后一个 data 帧携带 usage，便于记录用量
            body_fields["stream"] = True
            body_fields["stream_options"] = {"include_usage": True}
        body = json.dumps(body_fields, ensure_ascii=False)
        for attempt in range(self._config.max_retries + 1):
            try:
                return self._post_once(body, stream=use_stream, on_chunk=use_chunk)
            except _RetryableError as exc:
                if attempt >= self._config.max_retries:
                    raise LLMError(exc.kind, str(exc)) from exc
                wait = self._config.retry_base_seconds * (2**attempt)
                logger.warning("LLM request failed ({}), retrying in {}s (attempt {})", exc.kind, wait, attempt + 1)
                sleep(wait)
        raise LLMError("other", "LLM request failed")  # pragma: no cover - 循环必在重试耗尽时抛出

    def chat_json(
        self,
        messages: list[dict],
        *,
        stream: bool | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> tuple[dict, LLMResponse]:
        """chat + JSON 解析（§7.3 失败处理契约）。

        解析顺序：``json.loads`` → 提取 ```json 代码块 → 追加提醒消息重试 1 次 → 抛
        ``LLMError('parse')``。裸数组结果包装为 ``{"memories": [...]}``。

        Args:
            messages: 消息列表。
            stream: 是否开启流式输出（透传给 :meth:`chat`，None 用实例默认）。
            on_chunk: 流式片段回调（透传给 :meth:`chat`）。

        Returns:
            ``(解析后的 JSON 对象, 对应的 LLMResponse)``。

        Raises:
            LLMError: 解析失败（kind='parse'）或底层调用失败。
        """
        response = self.chat(messages, stream=stream, on_chunk=on_chunk)
        parsed = self._parse_json(response.text)
        if parsed is None:
            messages = [*messages, {"role": "user", "content": _JSON_REMINDER}]
            response = self.chat(messages, stream=stream, on_chunk=on_chunk)
            parsed = self._parse_json(response.text)
        if parsed is None:
            raise LLMError("parse", "LLM output could not be parsed as valid JSON")
        return self._normalize_json(parsed), response

    def _post_once(
        self,
        body: str,
        *,
        stream: bool = False,
        on_chunk: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """发送一次 POST 请求并解析响应（不重试）。

        流式下逐行解析 SSE，聚合出完整 ``content`` 并触发 ``on_chunk``。
        """
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        # S310：项目唯一的网络出口（ADR-10），URL 来自用户配置
        request = Request(  # noqa: S310
            url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._config.api_key}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as resp:  # noqa: S310
                if stream:
                    return self._read_stream(resp, on_chunk)
                return self._parse_response(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except TimeoutError as exc:
            raise _RetryableError("timeout", "request timed out") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise _RetryableError("timeout", "request timed out") from exc
            raise LLMError("other", f"Network error: {exc}") from exc

    @staticmethod
    def _read_stream(
        resp: object,
        on_chunk: Callable[[str], None] | None,
    ) -> LLMResponse:
        """逐行解析 OpenAI 兼容 SSE 流，聚合 content 与 usage。

        Args:
            resp: 可逐行迭代的响应体（如 ``HTTPResponse``）。
            on_chunk: 每个生成片段（content 或 reasoning_content）的回调。

        Returns:
            聚合后的响应文本与 usage。
        """
        parts: list[str] = []
        usage: dict = {}
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            event = LLMClient._parse_sse_line(line)
            if event is None:
                continue
            if event == _SSE_DONE:
                break
            if event["usage"]:
                usage = event["usage"]
            for field in ("content", "reasoning"):
                fragment = event.get(field) or ""
                if fragment and on_chunk is not None:
                    on_chunk(fragment)
            content = event.get("content") or ""
            if content:
                parts.append(content)
        return LLMResponse(
            text="".join(parts),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    @staticmethod
    def _parse_sse_line(line: str) -> dict | str | None:
        """解析一行 SSE：返回事件字典、结束标记或 None（忽略）。

        Args:
            line: 原始 SSE 行（如 ``data: {...}``）。

        Returns:
            ``None`` 表示忽略（非 data 行 / 空 / 非法 JSON）；
            ``_SSE_DONE`` 表示流结束；否则为含 content/reasoning/usage 的事件字典。
        """
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload:
            return None
        if payload == _SSE_DONE:
            return _SSE_DONE
        try:
            frame = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(frame, dict):
            return None
        event: dict = {"content": "", "reasoning": "", "usage": None}
        if frame.get("usage"):
            event["usage"] = frame["usage"]
        for choice in frame.get("choices") or []:
            delta = choice.get("delta") or {}
            event["content"] += delta.get("content") or ""
            event["reasoning"] += delta.get("reasoning_content") or ""
        return event

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> Exception:
        """HTTP 错误分类：429/5xx 可重试，其余不可重试。"""
        if exc.code == 429:
            return _RetryableError("rate_limit", "HTTP 429 rate limited")
        if 500 <= exc.code < 600:
            return _RetryableError("server", f"HTTP {exc.code} server error")
        return LLMError("other", f"HTTP {exc.code}")

    @staticmethod
    def _parse_response(raw: str) -> LLMResponse:
        """解析 OpenAI 兼容响应体：choices[0].message.content + usage。"""
        try:
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return LLMResponse(
                text=text,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("other", "LLM response is missing required fields or is not valid JSON") from exc

    @staticmethod
    def _parse_json(text: str) -> object | None:
        """先整体解析，失败则提取 ```json 代码块；两者都失败返回 None。"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _normalize_json(parsed: object) -> dict:
        """顶层归一：dict 原样；list（裸数组）包装为 ``{"memories": [...]}``。"""
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"memories": parsed}
        raise LLMError("parse", "JSON top level must be an object or array")


def fetch_models(base_url: str, api_key: str, timeout_seconds: int = 10) -> list[str]:
    """动态拉取 OpenAI 兼容端点的可用模型列表（``GET {base_url}/models``）。

    Args:
        base_url: 端点地址（如 https://api.deepseek.com/v1）。
        api_key: API key（多数端点要求鉴权）。
        timeout_seconds: 单次请求超时。

    Returns:
        模型 id 列表（按服务端返回顺序）。

    Raises:
        LLMError: 网络错误 / 非 2xx / 响应格式非法（kind 为 other）。
    """
    url = f"{base_url.rstrip('/')}/models"
    req = Request(url, headers={"Authorization": f"Bearer {api_key}"})  # noqa: S310 - base_url 来自用户配置
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 - 同上
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LLMError("other", f"Failed to fetch model list (HTTP {exc.code}): {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LLMError("other", f"Failed to fetch model list: {exc}") from exc
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LLMError("other", "Failed to fetch model list: response is missing the data array")
    return [str(m["id"]) for m in items if isinstance(m, dict) and m.get("id")]
