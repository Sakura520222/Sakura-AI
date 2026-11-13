"""AI API client dynamic configuration coverage."""

from datetime import UTC, datetime

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
from backend.services.activity_observability.contracts import (
    InvocationContext,
    RoleConfigSnapshot,
)
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


def _resolved_chain(
    model_id: str,
    context_window_tokens: int,
    *,
    protocol: ProtocolFamily | str | None = None,
) -> ResolvedChain:
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
        protocol=protocol,
    )
    return ResolvedChain(role="main", candidates=[candidate])


def _invocation_context() -> InvocationContext:
    return InvocationContext(
        invocation_id=1,
        work_unit_id=2,
        thread_id=3,
        role_snapshot=RoleConfigSnapshot(
            role="main",
            requested_provider="test-provider",
            requested_model="gpt-5.6-sol",
            requested_thinking_mode=None,
            candidate_chain=(("test-provider", "gpt-5.6-sol"),),
            account_id="test-provider",
            protocol_family="openai-compatible",
            endpoint_fingerprint="a" * 64,
            config_snapshot_version=1,
            captured_at=datetime.now(UTC),
        ),
    )


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


@pytest.mark.asyncio
async def test_role_snapshots_use_effective_account_protocol(monkeypatch):
    """公开 role 快照应与实际非默认协议 adapter 保持一致。"""
    api_client = AIApiClient()
    chain = _resolved_chain(
        "gpt-5.6-sol",
        512000,
        protocol=ProtocolFamily.OPENAI_RESPONSES,
    )

    async def resolve_chain(_role):
        return chain

    monkeypatch.setattr(api_client, "_resolve_role_chain", resolve_chain)

    snapshot = await api_client.resolve_role_config_snapshot("main")
    assert snapshot.protocol_family == ProtocolFamily.OPENAI_RESPONSES.value

    captured: dict[str, object] = {}

    class _CapturingUnifiedClient:
        async def call_with_retry(self, *args, **kwargs):
            captured["context"] = kwargs["context"]
            return UnifiedResponse(
                content="ok",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=UnifiedUsage(input_tokens=1, output_tokens=1),
            )

    api_client._unified_client = _CapturingUnifiedClient()
    context = _invocation_context()
    await api_client.call_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model="",
        role="main",
        context=context,
    )
    captured_context = captured["context"]
    assert isinstance(captured_context, InvocationContext)
    assert (
        captured_context.role_snapshot.protocol_family
        == ProtocolFamily.OPENAI_RESPONSES.value
    )


def test_cached_client_refreshes_fallback_policy_before_next_call(monkeypatch):
    from types import SimpleNamespace

    settings = SimpleNamespace(
        ai_fallback_enabled=True,
        ai_fallback_max_candidates=0,
        ai_fallback_sticky_candidate=False,
        ai_api_max_retries=0,
        ai_api_total_timeout_seconds=10,
        ai_api_initial_retry_delay_seconds=0,
    )
    monkeypatch.setattr("backend.core.config.get_settings", lambda: settings)
    client = AIApiClient(compressor=object())
    original_policy = client._get_unified_client().fallback_config
    settings.ai_fallback_max_candidates = 6
    settings.ai_fallback_sticky_candidate = True
    settings.ai_api_max_retries = 2
    refreshed = client._get_unified_client()
    assert original_policy.max_candidates == 0
    assert original_policy.max_retries == 0
    assert original_policy.sticky_candidate is False
    assert refreshed.fallback_config.max_candidates == 6
    assert refreshed.fallback_config.sticky_candidate is True
    assert refreshed.fallback_config.max_retries == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_new_policy_does_not_change_an_inflight_fallback_retry_budget(
    monkeypatch, stream
):
    import asyncio
    from types import SimpleNamespace

    from backend.core.ai_protocol.errors import AIError
    from backend.core.ai_protocol.models import AIErrorCategory, UnifiedStreamEvent

    settings = SimpleNamespace(
        ai_fallback_enabled=True,
        ai_fallback_max_candidates=0,
        ai_fallback_sticky_candidate=False,
        ai_api_max_retries=0,
        ai_api_total_timeout_seconds=10,
        ai_api_initial_retry_delay_seconds=0,
    )
    monkeypatch.setattr("backend.core.config.get_settings", lambda: settings)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Adapter(_CapturingAdapter):
        async def chat(self, http, endpoint, credential, request, *, timeout=None):
            lane = request.messages[0].content
            calls.append((lane, request.model))
            if lane == "old" and request.model == "primary":
                entered.set()
                await release.wait()
                raise AIError(AIErrorCategory.AUTH_INVALID, "Primary unavailable")
            if lane == "old" or calls.count((lane, request.model)) == 1:
                raise AIError(AIErrorCategory.SERVER_ERROR, "Transient failure")
            return await super().chat(
                http, endpoint, credential, request, timeout=timeout
            )

        async def stream(self, http, endpoint, credential, request, *, timeout=None):
            response = await self.chat(
                http, endpoint, credential, request, timeout=timeout
            )
            yield UnifiedStreamEvent(type="text_delta", text=response.content)
            yield UnifiedStreamEvent(type="done", usage=response.usage)

    adapter = Adapter()
    monkeypatch.setattr(
        "backend.core.ai_protocol.registry.get_adapter", lambda _family: adapter
    )
    chain = _resolved_chain("primary", 128000)
    chain.candidates.append(_resolved_chain("fallback", 128000).candidates[0])
    client = AIApiClient()
    client._compressor_initialized = True

    async def resolve(_role):
        return chain

    monkeypatch.setattr(client, "_resolve_role_chain", resolve)

    async def invoke(lane):
        messages = [{"role": "user", "content": lane}]
        if stream:
            events = [
                event
                async for event in await client.stream_with_retry(messages, role="main")
            ]
            return "".join(event.text or "" for event in events)
        response = await client.call_with_retry(messages, role="main")
        return response.content

    old = asyncio.create_task(invoke("old"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        settings.ai_api_max_retries = 1
        response = await invoke("new")
        assert response == "ok"
        assert calls.count(("new", "primary")) == 2
        release.set()
        with pytest.raises(AllCandidatesFailedError):
            await old
        assert calls.count(("old", "fallback")) == 1
    finally:
        release.set()
        if not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        await client._get_unified_client().aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_late_success_cannot_restore_sticky_order_from_before_a_reorder(
    monkeypatch, stream
):
    import asyncio
    from types import SimpleNamespace

    from backend.core.ai_protocol.errors import AIError
    from backend.core.ai_protocol.models import AIErrorCategory, UnifiedStreamEvent

    settings = SimpleNamespace(
        ai_fallback_enabled=True,
        ai_fallback_max_candidates=0,
        ai_fallback_sticky_candidate=True,
        ai_api_max_retries=0,
        ai_api_total_timeout_seconds=10,
        ai_api_initial_retry_delay_seconds=0,
    )
    monkeypatch.setattr("backend.core.config.get_settings", lambda: settings)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Adapter(_CapturingAdapter):
        async def chat(self, http, endpoint, credential, request, *, timeout=None):
            lane = request.messages[0].content
            calls.append((lane, request.model))
            if request.model == "primary":
                raise AIError(AIErrorCategory.AUTH_INVALID, "Primary unavailable")
            if lane == "old":
                entered.set()
                await release.wait()
            return await super().chat(
                http, endpoint, credential, request, timeout=timeout
            )

        async def stream(self, http, endpoint, credential, request, *, timeout=None):
            response = await self.chat(
                http, endpoint, credential, request, timeout=timeout
            )
            yield UnifiedStreamEvent(type="text_delta", text=response.content)
            yield UnifiedStreamEvent(type="done", usage=response.usage)

    monkeypatch.setattr(
        "backend.core.ai_protocol.registry.get_adapter", lambda _family: Adapter()
    )
    primary = _resolved_chain("primary", 128000).candidates[0]
    fallback = _resolved_chain("fallback", 128000).candidates[0]
    new_fallback = _resolved_chain("new-fallback", 128000).candidates[0]
    chain = ResolvedChain(role="main", candidates=[primary, fallback])
    client = AIApiClient()
    client._compressor_initialized = True

    async def resolve(_role):
        return chain

    monkeypatch.setattr(client, "_resolve_role_chain", resolve)

    async def invoke(lane):
        messages = [{"role": "user", "content": lane}]
        if stream:
            events = [
                event
                async for event in await client.stream_with_retry(messages, role="main")
            ]
            return "".join(event.text or "" for event in events)
        response = await client.call_with_retry(messages, role="main")
        return response.content

    old = asyncio.create_task(invoke("old"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        chain = ResolvedChain(role="main", candidates=[primary, new_fallback, fallback])
        await invoke("new")
        release.set()
        await old
        await invoke("after")
        assert [model for lane, model in calls if lane == "after"] == ["new-fallback"]
    finally:
        release.set()
        if not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        await client._get_unified_client().aclose()
