"""AI API client dynamic configuration coverage."""

import pytest

from backend.core.ai_protocol.errors import AllCandidatesFailedError
from backend.core.ai_protocol.models import (
    AuthScheme,
    MetadataSource,
    ModelCapabilitySet,
    ModelMetadata,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedModel,
    StopReason,
    UnifiedResponse,
    UnifiedUsage,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.core.ai_protocol.resolver import ResolvedChain
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.unified_client import FallbackConfig, UnifiedAIClient


class _CapturingAdapter:
    """Capture the normalized request produced by UnifiedAIClient."""

    async def chat(self, _client, _endpoint, _credential, request, *, timeout=None):
        self.requests = getattr(self, "requests", [])
        self.requests.append(request)
        return UnifiedResponse(
            content="ok",
            tool_calls=[],
            stop_reason=StopReason.END_TURN,
            usage=UnifiedUsage(input_tokens=5, output_tokens=3),
        )


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


def test_client_rejects_legacy_endpoint_and_credential_constructor():
    """角色门面不能重新接纳旧 endpoint/key 构造方式。"""
    with pytest.raises(TypeError):
        AIApiClient("https://legacy.example/v1", "legacy-key")


@pytest.mark.asyncio
async def test_call_with_retry_requires_explicit_role():
    """角色门面拒绝未显式指定角色的调用。"""
    api_client = AIApiClient()

    with pytest.raises(ValueError, match="role"):
        await api_client.call_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.6-terra",
        )


@pytest.mark.asyncio
async def test_call_with_retry_wraps_role_resolution_errors(monkeypatch):
    """角色候选链解析异常统一转换为 AllCandidatesFailedError。"""
    api_client = AIApiClient()

    async def resolve_chain(_role):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(api_client, "_resolve_role_chain", resolve_chain)

    with pytest.raises(AllCandidatesFailedError, match="候选链解析失败"):
        await api_client.call_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.6-terra",
            role="main",
        )


@pytest.mark.asyncio
async def test_resolve_role_model_context_returns_primary_metadata(monkeypatch):
    """角色已绑定时返回 primary 的真实模型 ID 与上下文窗口。"""
    api_client = AIApiClient()
    chain = _resolved_chain("gpt-5.6-sol", 512000)

    async def resolve_chain(_role):
        return chain

    monkeypatch.setattr(api_client, "_resolve_role_chain", resolve_chain)

    assert await api_client.resolve_role_model_context("main") == (
        "gpt-5.6-sol",
        512000,
    )


@pytest.mark.asyncio
async def test_resolve_role_model_context_returns_empty_pair_on_resolution_error(
    monkeypatch,
):
    """上下文预算探测失败返回空值，不伪造扁平配置结果。"""
    api_client = AIApiClient()

    async def resolve_chain(_role):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(api_client, "_resolve_role_chain", resolve_chain)

    assert await api_client.resolve_role_model_context("main") == (None, None)


@pytest.mark.asyncio
async def test_call_with_retry_uses_primary_model_in_unified_request(monkeypatch):
    """门面传入的 model 不覆盖角色 primary 的真实请求模型。"""
    api_client = AIApiClient()
    chain = _resolved_chain("gpt-5.6-sol", 512000)
    adapter = _CapturingAdapter()
    unified_client = UnifiedAIClient(
        http_client=object(),
        fallback_config=FallbackConfig(
            enabled=False,
            max_candidates=1,
            max_retries=1,
            total_timeout=5.0,
        ),
    )
    api_client._unified_client = unified_client

    async def resolve_chain(_role):
        return chain

    monkeypatch.setattr(api_client, "_resolve_role_chain", resolve_chain)
    monkeypatch.setattr(
        "backend.core.ai_protocol.registry.get_adapter",
        lambda _family: adapter,
    )

    response = await api_client.call_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.6-terra",
        role="main",
    )

    assert response.choices[0].message.content == "ok"
    assert response.usage.prompt_tokens == 5
    assert adapter.requests[0].model == "gpt-5.6-sol"
