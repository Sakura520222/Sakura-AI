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
