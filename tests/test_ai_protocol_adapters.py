"""AI 协议层测试 / Tests for the AI protocol layer."""

import httpx
import pytest

from backend.core.ai_protocol.adapters.anthropic_native import AnthropicNativeAdapter
from backend.core.ai_protocol.adapters.gemini_native import GeminiNativeAdapter
from backend.core.ai_protocol.adapters.openai_compatible import OpenAICompatibleAdapter
from backend.core.ai_protocol.adapters.openai_responses import OpenAIResponsesAdapter
from backend.core.ai_protocol.errors import AIError
from backend.core.ai_protocol.models import (
    AuthScheme,
    ProtocolFamily,
    ResolvedEndpoint,
    StopReason,
    UnifiedMessage,
    UnifiedRequest,
    UnifiedTool,
    UnifiedToolCall,
    safe_provider_event_metadata,
)


def _endpoint(family: ProtocolFamily) -> ResolvedEndpoint:
    return ResolvedEndpoint(
        base_url="https://example.test/v1/",
        chat_path="chat/completions" if family == ProtocolFamily.OPENAI_COMPATIBLE else "messages",
        auth_scheme=AuthScheme.BEARER,
    )


def test_openai_adapter_serializes_tool_calls_and_parses_response():
    adapter = OpenAICompatibleAdapter()
    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
        tools=[
            UnifiedTool(
                name="read_file",
                description="Read a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        tool_choice="auto",
    )

    body = adapter.serialize_request(request)
    assert body["model"] == "test-model"
    assert body["tools"][0]["function"]["name"] == "read_file"

    response = adapter.parse_response(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        raw=None,
    )
    assert response.stop_reason == StopReason.TOOL_USE
    tool_call = response.choices[0].message.tool_calls[0]
    assert tool_call.name == "read_file"
    assert tool_call.function.name == "read_file"
    assert tool_call.function.arguments == '{"path":"a.py"}'
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5


def test_openai_adapter_normalizes_deepseek_reasoning_and_cache_usage():
    adapter = OpenAICompatibleAdapter()
    response = adapter.parse_response(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "done",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 4258,
                "completion_tokens": 181,
                "prompt_cache_hit_tokens": 3000,
                "prompt_cache_miss_tokens": 1258,
                "completion_tokens_details": {"reasoning_tokens": 120},
                "total_tokens": 4439,
            },
        },
        raw=None,
    )

    assert response.usage.input_tokens == 4258
    assert response.usage.output_tokens == 181
    assert response.usage.cache_read_tokens == 3000
    assert response.usage.reasoning_tokens == 120
    assert response.usage.reported_fields == frozenset(
        {
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "reasoning_tokens",
        }
    )
    assert response.to_dict()["usage"] == {
        "input_tokens": 4258,
        "output_tokens": 181,
        "cache_read_tokens": 3000,
        "reasoning_tokens": 120,
    }


@pytest.mark.asyncio
async def test_openai_adapter_converts_non_json_success_response_to_retryable_error():
    """2xx HTML 响应必须转为可重试 AIError，而非泄漏 JSONDecodeError。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Gateway</title></html>",
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            await OpenAICompatibleAdapter().chat(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            )
    finally:
        await client.aclose()

    error = exc_info.value
    assert getattr(error, "category", None).value == "unknown"
    assert getattr(error, "status_code", None) == 200
    assert getattr(error, "model", None) == "test-model"
    assert "content_type=text/html" in str(error)


@pytest.mark.asyncio
async def test_openai_adapter_rejects_redirect_response_as_retryable_error():
    """禁止跟随重定向时，3xx 必须进入统一错误和故障转移链路。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://gateway.example/login"},
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            await OpenAICompatibleAdapter().chat(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            )
    finally:
        await client.aclose()

    error = exc_info.value
    assert getattr(error, "category", None).value == "unknown"
    assert getattr(error, "status_code", None) == 302
    assert "location=https://gateway.example/login" in str(error)


@pytest.mark.asyncio
async def test_openai_adapter_redacts_redirect_query_from_error():
    """重定向诊断不得泄漏可能包含凭据的查询参数。"""
    secret = "test-secret-key"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": f"https://gateway.example/login?key={secret}"},
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            await OpenAICompatibleAdapter().chat(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            )
    finally:
        await client.aclose()

    assert secret not in str(exc_info.value)
    assert "location=https://gateway.example/login" in str(exc_info.value)


@pytest.mark.asyncio
async def test_openai_adapter_ignores_invalid_json_model_metadata_response():
    """可选模型详情探测遇到无效 JSON 时应返回 None，而非中断模型发现。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Gateway</title></html>",
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        result = await OpenAICompatibleAdapter().fetch_model_metadata(
            client,
            _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
            "test-key",
            "test-model",
        )
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.asyncio
async def test_openai_adapter_stream_preserves_http_error_category():
    """流式 4xx 必须沿用协议错误分类，不得退化为 UNKNOWN。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "bad key"}},
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
        stream=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            async for _ in OpenAICompatibleAdapter().stream(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            ):
                pass
    finally:
        await client.aclose()

    assert exc_info.value.category.value == "auth_invalid"
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_openai_adapter_stream_rejects_redirect_response():
    """流式请求收到 3xx 时必须抛出 AIError，不能静默结束。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://gateway.example/login"},
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
        stream=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            async for _ in OpenAICompatibleAdapter().stream(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            ):
                pass
    finally:
        await client.aclose()

    assert exc_info.value.category.value == "unknown"
    assert exc_info.value.status_code == 302


@pytest.mark.asyncio
async def test_openai_adapter_stream_rejects_non_sse_response():
    """流式请求收到 2xx HTML 时必须进入统一错误与故障转移链路。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Gateway</title></html>",
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
        stream=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            async for _ in OpenAICompatibleAdapter().stream(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            ):
                pass
    finally:
        await client.aclose()

    assert exc_info.value.category.value == "unknown"
    assert exc_info.value.status_code == 200


@pytest.mark.asyncio
async def test_anthropic_adapter_accepts_top_level_model_array():
    """Anthropic 兼容的模型列表也允许顶层 JSON 数组。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"id": "claude-test"}],
            request=request,
        )

    endpoint = ResolvedEndpoint(
        base_url="https://example.test/v1/",
        chat_path="messages",
        auth_scheme=AuthScheme.X_API_KEY,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        models = await AnthropicNativeAdapter().list_models(
            client,
            endpoint,
            "test-key",
        )
    finally:
        await client.aclose()

    assert [model.model_id for model in models] == ["claude-test"]


@pytest.mark.asyncio
async def test_openai_adapter_redacts_redirect_userinfo():
    """重定向诊断不得泄漏 URL 中的用户名或密码。"""
    username = "gateway-user"
    password = "gateway-password"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={
                "location": (
                    f"https://{username}:{password}@gateway.example:8443/login"
                )
            },
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            await OpenAICompatibleAdapter().chat(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            )
    finally:
        await client.aclose()

    message = str(exc_info.value)
    assert username not in message
    assert password not in message
    assert "location=https://gateway.example:8443/login" in message


@pytest.mark.asyncio
async def test_openai_adapter_redacts_malformed_redirect_location():
    """非法 Location 也必须转换为 AIError，确保故障转移仍可执行。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://["},
            request=request,
        )

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            await OpenAICompatibleAdapter().chat(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            )
    finally:
        await client.aclose()

    assert exc_info.value.category.value == "unknown"
    assert exc_info.value.status_code == 302


@pytest.mark.asyncio
async def test_openai_adapter_rejects_array_chat_response_as_retryable_error():
    """Chat Completions 的 JSON 根节点必须为对象，数组响应应进入故障转移链路。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    request = UnifiedRequest(
        model="test-model",
        messages=[UnifiedMessage(role="user", content="hello")],
        max_tokens=1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(AIError) as exc_info:
            await OpenAICompatibleAdapter().chat(
                client,
                _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
                "test-key",
                request,
            )
    finally:
        await client.aclose()

    assert exc_info.value.category.value == "unknown"
    assert exc_info.value.status_code == 200


@pytest.mark.asyncio
async def test_openai_adapter_accepts_top_level_model_array():
    """OpenAI 兼容端点允许以 JSON 数组作为 /models 根节点。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"id": "gpt-5.6-sol"}],
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        models = await OpenAICompatibleAdapter().list_models(
            client,
            _endpoint(ProtocolFamily.OPENAI_COMPATIBLE),
            "test-key",
        )
    finally:
        await client.aclose()

    assert [model.model_id for model in models] == ["gpt-5.6-sol"]


def test_openai_responses_adapter_serializes_typed_items_and_parses_output():
    adapter = OpenAIResponsesAdapter()
    request = UnifiedRequest(
        model="gpt-5.6-sol",
        messages=[
            UnifiedMessage(role="system", content="Be precise"),
            UnifiedMessage(role="user", content="Inspect code"),
            UnifiedMessage(
                role="assistant",
                tool_calls=[UnifiedToolCall("call_1", "read_file", '{"path":"a.py"}')],
            ),
            UnifiedMessage(role="tool", tool_call_id="call_1", content="content"),
        ],
        max_tokens=4096,
        tools=[
            UnifiedTool(
                name="read_file",
                description="Read a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        effort="medium",
    )

    body = adapter.serialize_request(request)
    assert body["instructions"] == "Be precise"
    assert body["max_output_tokens"] == 4096
    assert body["tools"][0]["name"] == "read_file"
    assert body["input"][1]["type"] == "function_call"
    assert body["input"][2]["type"] == "function_call_output"
    assert body["reasoning"] == {"effort": "medium"}

    response = adapter.parse_response(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "write_file",
                    "arguments": {"path": "b.py"},
                },
            ],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        },
        raw=None,
    )
    assert response.content == "done"
    assert response.tool_calls[0].name == "write_file"
    assert response.tool_calls[0].arguments == '{"path": "b.py"}'
    assert response.usage.input_tokens == 12
    assert response.usage.reasoning_tokens == 3



    adapter = AnthropicNativeAdapter()
    endpoint = _endpoint(ProtocolFamily.ANTHROPIC_NATIVE)
    headers = adapter.build_headers("sk-test", endpoint)
    assert headers["x-api-key"] == "sk-test"
    assert "anthropic-version" in headers

    request = UnifiedRequest(
        model="claude-sonnet-5",
        messages=[
            UnifiedMessage(role="system", content="Be concise"),
            UnifiedMessage(role="user", content="Hello"),
            UnifiedMessage(
                role="assistant",
                tool_calls=[
                    UnifiedToolCall("toolu_1", "read_file", '{"path":"a.py"}')
                ],
            ),
            UnifiedMessage(role="tool", tool_call_id="toolu_1", content="content"),
        ],
        max_tokens=2048,
    )

    body = adapter.serialize_request(request)
    assert body["system"] == "Be concise"
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"

    response = adapter.parse_response(
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "I will inspect it."},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
        raw=None,
    )
    assert response.stop_reason == StopReason.TOOL_USE
    assert response.content == "I will inspect it."
    assert response.tool_calls[0].arguments == '{"path": "a.py"}'


def test_gemini_adapter_uses_native_model_metadata_fields():
    adapter = GeminiNativeAdapter()
    results = adapter._parse_model_list(
        {
            "models": [
                {
                    "name": "models/gemini-test",
                    "displayName": "Gemini Test",
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": 8192,
                }
            ]
        }
    )
    assert results[0].model_id == "gemini-test"
    assert results[0].context_window_tokens == 1048576
    assert results[0].max_output_tokens == 8192


def test_protocol_adapters_classify_terminal_and_recoverable_errors():
    for adapter in (
        OpenAICompatibleAdapter(),
        OpenAIResponsesAdapter(),
        AnthropicNativeAdapter(),
        GeminiNativeAdapter(),
    ):
        category, _ = adapter.translate_error(401, {"error": {"message": "bad key"}})
        assert category.value == "auth_invalid"
        category, _ = adapter.translate_error(
            400, {"error": {"message": "prompt is too long for context window"}}
        )
        assert category.value == "context_overflow"
        category, _ = adapter.translate_error(429, {"error": {"message": "rate limited"}})
        assert category.value == "rate_limited"


def test_openai_responses_stream_reasoning_summary_lifecycle_and_usage_details():
    adapter = OpenAIResponsesAdapter()
    exposed = adapter._parse_responses_sse_line(
        'data: {"type":"response.reasoning_summary_text.delta","delta":"checking diff"}'
    )
    omitted = adapter._parse_responses_sse_line(
        'data: {"type":"response.reasoning.started"}'
    )
    done = adapter._parse_responses_sse_line(
        'data: {"type":"response.reasoning_summary_text.done"}'
    )
    completed = adapter._parse_responses_sse_line(
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":7,"input_tokens_details":{"cached_tokens":3},"output_tokens_details":{"reasoning_tokens":2}}}}'
    )

    assert exposed.type == "reasoning_delta"
    assert exposed.reasoning_availability == "summarized"
    assert exposed.text == "checking diff"
    assert omitted.type == "reasoning_start"
    assert omitted.reasoning_availability == "omitted"
    assert omitted.text is None
    assert done.type == "reasoning_end"
    assert done.reasoning_availability == "summarized"
    assert done.text is None
    assert completed.type == "done"
    assert completed.usage is not None
    assert completed.usage.input_tokens == 12
    assert completed.usage.cache_read_tokens == 3
    assert completed.usage.reasoning_tokens == 2
    assert completed.usage.reported_fields == frozenset({"input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens"})
    assert completed.usage.details == {"input_tokens": 12, "output_tokens": 7, "cached_tokens": 3, "reasoning_tokens": 2}


def test_openai_responses_unknown_events_are_ignored_safely():
    assert OpenAIResponsesAdapter._parse_responses_sse_line(
        'data: {"type":"response.internal.secret","url":"https://evil.test","payload":{"token":"secret"}}'
    ) is None


def test_anthropic_stream_reasoning_blocks_omit_text_and_redact_opaque_values():
    adapter = AnthropicNativeAdapter()
    current_tool = {}
    omitted = adapter._parse_stream_event(
        "content_block_start",
        {"content_block": {"type": "thinking", "thinking": ""}},
        current_tool,
    )
    delta = adapter._parse_stream_event(
        "content_block_delta",
        {"delta": {"type": "thinking_delta", "thinking": "plan"}},
        current_tool,
    )
    signature = adapter._parse_stream_event(
        "content_block_delta",
        {"delta": {"type": "signature_delta", "signature": "SECRET-SIGNATURE"}},
        current_tool,
    )
    redacted = adapter._parse_stream_event(
        "content_block_delta",
        {"delta": {"type": "redacted_thinking", "data": "SECRET-THOUGHT"}},
        current_tool,
    )
    ended = adapter._parse_stream_event("content_block_stop", {}, current_tool)
    usage = adapter._parse_stream_event(
        "message_delta",
        {"usage": {"input_tokens": 10, "output_tokens": 6, "cache_read_tokens": 2, "reasoning_tokens": 4}},
        current_tool,
    )

    assert omitted.type == "reasoning_start"
    assert omitted.text is None and omitted.reasoning_availability == "omitted"
    assert delta.type == "reasoning_delta"
    assert delta.text == "plan" and delta.reasoning_availability == "provider_exposed"
    assert signature.type == "reasoning_delta"
    assert signature.text is None and signature.reasoning_availability == "encrypted_opaque"
    assert redacted.type == "reasoning_delta"
    assert redacted.text is None and redacted.reasoning_availability == "encrypted_opaque"
    assert "SECRET" not in repr(signature.provider_event_metadata)
    assert "signature" not in (signature.provider_event_metadata or {})
    assert ended.type == "reasoning_end"
    assert ended.text is None
    assert usage.type == "usage"
    assert usage.usage is not None and usage.usage.cache_read_tokens == 2


def test_openai_compatible_reasoning_content_is_provider_exposed_without_false_events():
    adapter = OpenAICompatibleAdapter()
    reasoning = adapter._parse_sse_line(
        'data: {"choices":[{"delta":{"reasoning_content":"step"}}]}'
    )
    ordinary = adapter._parse_sse_line(
        'data: {"choices":[{"delta":{"content":"answer"}}]}'
    )
    no_reasoning = adapter._parse_sse_line(
        'data: {"choices":[{"delta":{"role":"assistant"}}]}'
    )
    usage_only = adapter._parse_sse_line(
        'data: {"usage":{"prompt_tokens":4,"completion_tokens":2}}'
    )

    assert reasoning.type == "reasoning_delta"
    assert reasoning.text == "step" and reasoning.reasoning_availability == "provider_exposed"
    assert ordinary.type == "text_delta" and ordinary.text == "answer"
    assert no_reasoning is None
    assert usage_only.type == "usage"
    assert usage_only.usage is not None and usage_only.usage.input_tokens == 4


def test_gemini_thought_and_signature_are_not_confused_with_ordinary_text():
    thought = GeminiNativeAdapter._parse_stream_chunk(
        {"candidates": [{"content": {"parts": [{"thought": True, "text": "plan"}]}}]}
    )
    signature = GeminiNativeAdapter._parse_stream_chunk(
        {"candidates": [{"content": {"parts": [{"thoughtSignature": "SECRET"}]}}]}
    )
    ordinary = GeminiNativeAdapter._parse_stream_chunk(
        {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}
    )
    usage = GeminiNativeAdapter._parse_stream_chunk(
        {"usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 3, "cachedContentTokenCount": 2, "thoughtsTokenCount": 1}}
    )

    assert thought.type == "reasoning_delta"
    assert thought.text == "plan" and thought.reasoning_availability == "provider_exposed"
    assert signature.type == "reasoning_end"
    assert signature.text is None and signature.reasoning_availability == "encrypted_opaque"
    assert "SECRET" not in repr(signature.provider_event_metadata)
    assert ordinary.type == "text_delta" and ordinary.text == "answer"
    assert usage.type == "done"
    assert usage.usage is not None and usage.usage.cache_read_tokens == 2
    assert usage.usage.reasoning_tokens == 1


def test_provider_event_metadata_is_scalar_allowlist_only():
    metadata = safe_provider_event_metadata(
        {
            "event": "response.completed",
            "index": 1,
            "url": "https://evil.test",
            "authorization": "Bearer secret",
            "raw": {"api_key": "secret"},
            "payload": "secret",
            "unknown": "secret",
        }
    )
    assert metadata == {"event": "response.completed", "index": 1}
    assert all(secret not in repr(metadata) for secret in ("https://", "Bearer", "secret", "api_key"))


@pytest.mark.asyncio
async def test_openai_responses_adapter_stream_iterates_real_sse_reasoning_events():
    sse = "\n".join(
        [
            'data: {"type":"response.reasoning.started"}',
            'data: {"type":"response.reasoning_summary_text.delta","delta":"summary"}',
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":4,"output_tokens":2}}}',
            "",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse,
            request=request,
        )

    endpoint = ResolvedEndpoint(
        base_url="https://example.test/v1/",
        chat_path="responses",
        auth_scheme=AuthScheme.BEARER,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = UnifiedRequest(
        model="gpt-5.6-sol",
        messages=[UnifiedMessage(role="user", content="inspect")],
        max_tokens=128,
        stream=True,
    )
    try:
        events = [
            event
            async for event in OpenAIResponsesAdapter().stream(
                client, endpoint, "test-key", request
            )
        ]
    finally:
        await client.aclose()

    assert [event.type for event in events] == [
        "reasoning_start", "reasoning_delta", "done"
    ]
    assert events[1].text == "summary"
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 4
