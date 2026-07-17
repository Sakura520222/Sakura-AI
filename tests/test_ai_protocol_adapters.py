"""AI 协议层测试 / Tests for the AI protocol layer."""

from backend.core.ai_protocol.adapters.anthropic_native import AnthropicNativeAdapter
from backend.core.ai_protocol.adapters.gemini_native import GeminiNativeAdapter
from backend.core.ai_protocol.adapters.openai_compatible import OpenAICompatibleAdapter
from backend.core.ai_protocol.adapters.openai_responses import OpenAIResponsesAdapter
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
    assert response.choices[0].message.tool_calls[0].name == "read_file"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5


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
