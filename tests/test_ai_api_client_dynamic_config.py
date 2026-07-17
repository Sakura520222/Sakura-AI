"""AI API client dynamic configuration coverage."""

from types import SimpleNamespace

import pytest

from backend.core.ai_protocol.models import (
    AuthScheme,
    ModelCapabilitySet,
    ModelMetadata,
    MetadataSource,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedModel,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.core.ai_protocol.resolver import ResolvedChain
from backend.core.config import get_settings
from backend.services.ai_reviewer.api_client import AIApiClient


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        raise RuntimeError("boom")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAIClient:
    def __init__(self):
        self.chat = _FakeChat()


@pytest.mark.asyncio
async def test_call_with_retry_uses_dynamic_timeout_and_retry(monkeypatch):
    settings = get_settings()
    old_values = {
        "ai_api_timeout_seconds": settings.ai_api_timeout_seconds,
        "ai_api_max_retries": settings.ai_api_max_retries,
    }
    try:
        settings.ai_api_timeout_seconds = 3.5
        settings.ai_api_max_retries = 1

        sleep_calls = []

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        monkeypatch.setattr(
            "backend.services.ai_reviewer.api_client.asyncio.sleep", fake_sleep
        )

        api_client = AIApiClient("https://example.invalid/v1", "test-key")
        fake_client = _FakeOpenAIClient()
        api_client.client = fake_client

        with pytest.raises(RuntimeError, match="boom"):
            await api_client.call_with_retry(
                messages=[{"role": "user", "content": "hi"}],
                model="test-model",
            )

        assert fake_client.chat.completions.calls == 1
        assert fake_client.chat.completions.kwargs["timeout"] == 3.5
        assert sleep_calls == []
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


def _resolved_chain(model_id: str, context_window_tokens: int) -> ResolvedChain:
    provider = ProviderDeclaration(
        id="test-provider",
        label="Test Provider",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://example.test/v1/",
        auth_scheme=AuthScheme.BEARER,
    )
    metadata = ModelMetadata(
        model_id=model_id,
        provider_id=provider.id,
        display_name=model_id,
        context_window_tokens=context_window_tokens,
        max_output_tokens=4096,
        capabilities=ModelCapabilitySet(),
        reasoning_params=ReasoningParams(),
        source=MetadataSource.USER_OVERRIDE,
    )
    candidate = ResolvedModel(
        provider=provider,
        model=metadata,
        credential="test-key",
        endpoint=resolve_endpoint(provider, None),
    )
    return ResolvedChain(role="main", candidates=[candidate])


@pytest.mark.asyncio
async def test_resolve_role_model_context_uses_primary_candidate_metadata(monkeypatch):
    """角色已绑定时，应返回实际 primary 模型及其单模型上下文配置。"""
    api_client = AIApiClient("https://example.invalid/v1", "test-key")
    chain = _resolved_chain("gpt-5.6-sol", 512000)

    async def resolve_chain(_):
        return chain

    monkeypatch.setattr(api_client, "_resolve_role_chain", resolve_chain)

    model_id, context_window_tokens = await api_client.resolve_role_model_context("main")

    assert model_id == "gpt-5.6-sol"
    assert context_window_tokens == 512000


@pytest.mark.asyncio
async def test_resolve_role_model_context_ignores_resolution_error(monkeypatch):
    """上下文预算探测失败不能阻断统一客户端的旧路径回退。"""
    api_client = AIApiClient("https://example.invalid/v1", "test-key")

    async def resolve_chain(_):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(api_client, "_resolve_role_chain", resolve_chain)

    assert await api_client.resolve_role_model_context("main") == (None, None)


def test_calculate_delay_uses_dynamic_initial_delay(monkeypatch):
    settings = get_settings()
    old_value = settings.ai_api_initial_retry_delay_seconds
    try:
        settings.ai_api_initial_retry_delay_seconds = 2.0
        monkeypatch.setattr(
            "backend.services.ai_reviewer.api_client.random.uniform", lambda _a, _b: 1.0
        )

        api_client = AIApiClient("https://example.invalid/v1", "test-key")

        assert api_client._calculate_delay(0) == 2.0
        assert api_client._calculate_delay(1) == 4.0
        assert api_client._calculate_delay(3) == 16.0
    finally:
        settings.ai_api_initial_retry_delay_seconds = old_value


def test_estimate_prompt_tokens_supports_sdk_tool_call_objects():
    tool_call = SimpleNamespace(
        function=SimpleNamespace(
            name="fetch_url",
            arguments='{"url":"https://example.com"}',
        )
    )

    tokens = AIApiClient._estimate_prompt_tokens(
        [
            {"role": "user", "content": "读取网页"},
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        ]
    )

    assert tokens > 0


def test_estimate_prompt_tokens_supports_dict_tool_calls():
    tokens = AIApiClient._estimate_prompt_tokens(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_web",
                            "arguments": '{"query":"动态配置"}',
                        }
                    }
                ],
            }
        ]
    )

    assert tokens > 0
