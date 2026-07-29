"""故障转移与上下文超限恢复测试 / Fallback & context-overflow recovery tests.

使用 respx/httpx MockTransport 模拟适配器响应，验证：
- 可恢复错误重试耗尽 → 跨协议回退
- 上下文超限 → 压缩恢复（需注入压缩器桩）
- 终端错误 → 直接报出，不回退
"""

from typing import Optional

import pytest

from backend.core.ai_protocol.errors import AIError, AllCandidatesFailedError
from backend.core.ai_protocol.models import (
    AIErrorCategory,
    ModelCapabilitySet,
    ModelMetadata,
    MetadataSource,
    ProtocolFamily,
    ProviderDeclaration,
    AuthScheme,
    ReasoningParams,
    ResolvedModel,
    UnifiedMessage,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.services.ai_reviewer import unified_client as unified_client_module
from backend.services.ai_reviewer.unified_client import (
    FallbackConfig,
    UnifiedAIClient,
)


def _candidate(
    family: ProtocolFamily,
    model_id: str,
    *,
    context_window_tokens: int = 128000,
) -> ResolvedModel:
    decl = ProviderDeclaration(
        id=f"prov-{model_id}",
        label=model_id,
        family=family,
        base_url="https://example.test/v1/",
        auth_scheme=AuthScheme.BEARER,
    )
    endpoint = resolve_endpoint(decl, None)
    metadata = ModelMetadata(
        model_id=model_id,
        provider_id=decl.id,
        display_name=model_id,
        context_window_tokens=context_window_tokens,
        max_output_tokens=4096,
        capabilities=ModelCapabilitySet(),
        reasoning_params=ReasoningParams(),
        source=MetadataSource.FALLBACK,
    )
    return ResolvedModel(
        provider=decl, model=metadata, credential="key", endpoint=endpoint
    )


class _StubAdapter:
    """记录调用次数与应抛出的错误的桩适配器 / Stub adapter."""

    family = ProtocolFamily.OPENAI_COMPATIBLE

    def __init__(
        self,
        *,
        fail_categories: Optional[list[AIErrorCategory]] = None,
        content: str = "ok",
    ):
        self._fail_categories = fail_categories or []
        self.calls = 0
        self._content = content

    async def list_models(self, *args, **kwargs):  # noqa: D401
        return []

    async def fetch_model_metadata(self, *args, **kwargs):
        return None

    async def chat(self, client, endpoint, credential, request, *, timeout=None):
        self.calls += 1
        if self.calls <= len(self._fail_categories):
            category = self._fail_categories[self.calls - 1]
            raise AIError(category, f"stub failure {category.value}")
        from backend.core.ai_protocol.models import (
            StopReason,
            UnifiedResponse,
            UnifiedUsage,
        )

        return UnifiedResponse(
            content=self._content,
            tool_calls=[],
            stop_reason=StopReason.END_TURN,
            usage=UnifiedUsage(input_tokens=5, output_tokens=3),
        )

    async def stream(self, *args, **kwargs):
        yield  # type: ignore[misc]

    def translate_error(self, status_code, body):
        return AIErrorCategory.UNKNOWN, ""

    def supports_capability(self, capability):
        return True

    def build_headers(self, credential, endpoint):
        return {}


def _install_stub(monkeypatch, family, stub):
    from backend.core.ai_protocol import registry as reg

    monkeypatch.setitem(reg._DEFAULT_CHAT_PATHS, family, "chat/completions")
    monkeypatch.setattr(reg, "get_adapter", lambda f: stub)


@pytest.mark.asyncio
async def test_recoverable_failure_exhausts_then_falls_back(monkeypatch):
    # 候选 1 连续 3 次 rate_limited → 重试耗尽 → 候选 2 成功
    primary_stub = _StubAdapter(
        fail_categories=[
            AIErrorCategory.RATE_LIMITED,
            AIErrorCategory.RATE_LIMITED,
            AIErrorCategory.RATE_LIMITED,
        ]
    )
    fallback_stub = _StubAdapter(content="fallback")

    def _adapter_for(family):
        if family == ProtocolFamily.OPENAI_COMPATIBLE:
            # 两个候选都是 openai-compatible；用 id 区分
            return primary_stub  # 仅作为默认；实际测试用 monkeypatch 切换
        return fallback_stub

    # 简化：直接让 get_adapter 按候选 provider id 返回不同桩
    from backend.core.ai_protocol import registry as reg

    def fake_get_adapter(family):
        return (
            primary_stub
            if family == ProtocolFamily.OPENAI_COMPATIBLE
            else fallback_stub
        )

    monkeypatch.setattr(reg, "get_adapter", fake_get_adapter)

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=3,
            total_timeout=10,
            initial_retry_delay=0,
        )
    )

    primary = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary")
    fallback = _candidate(ProtocolFamily.ANTHROPIC_NATIVE, "fallback")

    # 两个候选用不同协议族 → fake_get_adapter 返回不同桩
    response = await client.call_with_retry(
        [primary, fallback],
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="main",
    )
    assert response.content == "fallback"
    assert response.meta.served_by.endswith("/fallback")
    await client.aclose()


class _NonJsonThenSuccessAdapter(_StubAdapter):
    """首个候选返回非 JSON 协议错误，验证统一客户端继续回退。"""

    async def chat(self, client, endpoint, credential, request, *, timeout=None):
        self.calls += 1
        if self.calls <= len(self._fail_categories):
            raise AIError(
                self._fail_categories[self.calls - 1],
                "响应不是有效 JSON",
                status_code=200,
                provider=endpoint.base_url,
                model=request.model,
            )
        return await super().chat(
            client, endpoint, credential, request, timeout=timeout
        )


@pytest.mark.asyncio
async def test_non_json_response_exhausts_then_falls_back(monkeypatch):
    """非 JSON 的 2xx 响应属于可恢复错误，重试后必须切换备用模型。"""
    primary_stub = _NonJsonThenSuccessAdapter(
        fail_categories=[AIErrorCategory.UNKNOWN],
    )
    fallback_stub = _StubAdapter(content="fallback")

    from backend.core.ai_protocol import registry as reg

    monkeypatch.setattr(
        reg,
        "get_adapter",
        lambda family: (
            primary_stub
            if family == ProtocolFamily.OPENAI_COMPATIBLE
            else fallback_stub
        ),
    )

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=1,
            total_timeout=10,
            initial_retry_delay=0,
        )
    )
    primary = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary")
    fallback = _candidate(ProtocolFamily.ANTHROPIC_NATIVE, "fallback")

    response = await client.call_with_retry(
        [primary, fallback],
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="main",
    )

    assert response.content == "fallback"
    assert primary_stub.calls == 1
    assert fallback_stub.calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_terminal_error_surfaces_without_fallback(monkeypatch):
    primary_stub = _StubAdapter(fail_categories=[AIErrorCategory.AUTH_INVALID])

    from backend.core.ai_protocol import registry as reg

    monkeypatch.setattr(reg, "get_adapter", lambda _: primary_stub)

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=3,
            total_timeout=10,
            initial_retry_delay=0,
        )
    )
    primary = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary")
    fallback = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "fallback")

    with pytest.raises(AIError) as exc_info:
        await client.call_with_retry(
            [primary, fallback],
            [UnifiedMessage(role="user", content="hi")],
            model="primary",
            role="main",
        )
    assert exc_info.value.category == AIErrorCategory.AUTH_INVALID
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_chain_raises_all_candidates_failed():
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(enabled=True, max_candidates=3, max_retries=1)
    )
    with pytest.raises(AllCandidatesFailedError):
        await client.call_with_retry(
            [],
            [UnifiedMessage(role="user", content="hi")],
            model="any",
            role="main",
        )
    await client.aclose()


class _ModelRoutingAdapter(_StubAdapter):
    """按 request.model 路由到不同子桩，并记录调用顺序 / Route + record call order."""

    def __init__(self, behaviors: dict[str, _StubAdapter]):
        super().__init__()
        self._behaviors = behaviors
        self.call_models: list[str] = []

    async def chat(self, client, endpoint, credential, request, *, timeout=None):
        self.call_models.append(request.model)
        delegate = self._behaviors[request.model]
        return await delegate.chat(
            client, endpoint, credential, request, timeout=timeout
        )


def _install_router(monkeypatch, router):
    from backend.core.ai_protocol import registry as reg

    monkeypatch.setattr(reg, "get_adapter", lambda _family: router)


@pytest.mark.asyncio
async def test_sticky_candidate_promotes_last_successful(monkeypatch):
    """第 1 轮 primary 失败 → secondary 成功；第 2 轮应优先调用 secondary."""
    primary = _StubAdapter(fail_categories=[AIErrorCategory.RATE_LIMITED] * 5)
    secondary = _StubAdapter(content="ok")
    router = _ModelRoutingAdapter({"primary": primary, "secondary": secondary})
    _install_router(monkeypatch, router)

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=2,
            total_timeout=10,
            initial_retry_delay=0,
            sticky_candidate=True,
        )
    )
    candidates = [
        _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary"),
        _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "secondary"),
    ]

    r1 = await client.call_with_retry(
        candidates,
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="main",
    )
    assert r1.content == "ok"
    # 第 1 轮从首选 primary 开始
    assert router.call_models[0] == "primary"
    assert "secondary" in router.call_models

    # 第 2 轮 sticky：应优先调用上次成功的 secondary
    router.call_models.clear()
    secondary.calls = 0
    r2 = await client.call_with_retry(
        candidates,
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="main",
    )
    assert r2.content == "ok"
    assert router.call_models[0] == "secondary"
    await client.aclose()


@pytest.mark.asyncio
async def test_logs_selected_and_successful_fallback_candidate(monkeypatch):
    """调用日志必须记录实际候选，才能关联 Issue/记忆合并与故障转移结果。"""

    class _LogRecorder:
        def __init__(self):
            self.info_messages: list[str] = []

        def info(self, message, *args, **kwargs):
            self.info_messages.append(message.format(*args))

        def warning(self, *args, **kwargs):
            pass

    primary = _StubAdapter(fail_categories=[AIErrorCategory.SERVER_ERROR])
    fallback = _StubAdapter(content="fallback")
    router = _ModelRoutingAdapter({"primary": primary, "fallback": fallback})
    _install_router(monkeypatch, router)
    recorder = _LogRecorder()
    monkeypatch.setattr(unified_client_module, "logger", recorder)

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=1,
            total_timeout=10,
            initial_retry_delay=0,
        )
    )
    candidates = [
        _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary"),
        _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "fallback"),
    ]

    response = await client.call_with_retry(
        candidates,
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="summary",
    )

    assert response.content == "fallback"
    assert any(
        "AI 调用候选 [1/2]: role=summary provider=prov-primary model=primary" == message
        for message in recorder.info_messages
    )
    assert any(
        "AI 调用候选 [2/2]: role=summary provider=prov-fallback model=fallback"
        == message
        for message in recorder.info_messages
    )
    assert any(
        "AI 调用成功 [2/2]: role=summary served_by=prov-fallback/fallback" == message
        for message in recorder.info_messages
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_sticky_candidate_disabled_keeps_original_order(monkeypatch):
    """sticky_candidate=False 时第 2 轮仍从首选 primary 开始（不读取记忆）."""
    primary = _StubAdapter(fail_categories=[AIErrorCategory.RATE_LIMITED] * 5)
    secondary = _StubAdapter(content="ok-secondary")
    router = _ModelRoutingAdapter({"primary": primary, "secondary": secondary})
    _install_router(monkeypatch, router)

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=1,
            total_timeout=10,
            initial_retry_delay=0,
            sticky_candidate=False,
        )
    )
    candidates = [
        _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary"),
        _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "secondary"),
    ]

    # 第 1 轮：primary 失败 → secondary 成功
    r1 = await client.call_with_retry(
        candidates,
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="main",
    )
    assert r1.content == "ok-secondary"
    assert "secondary" in router.call_models

    # 关闭 sticky 后第 2 轮仍从 primary 开始（不读取 _last_successful）
    router.call_models.clear()
    await client.call_with_retry(
        candidates,
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="main",
    )
    assert router.call_models[0] == "primary"
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_event_aborts_backoff(monkeypatch):
    """退避等待期间 set cancel_event → 立即抛 ReviewCancelledError."""
    import asyncio
    import time

    from backend.core.ai_protocol.errors import ReviewCancelledError

    # 单候选持续失败，触发退避
    stub = _StubAdapter(fail_categories=[AIErrorCategory.SERVER_ERROR] * 10)
    from backend.core.ai_protocol import registry as reg

    monkeypatch.setattr(reg, "get_adapter", lambda _f: stub)

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=1,
            max_retries=5,
            total_timeout=30,
            initial_retry_delay=2.0,
        )
    )
    cancel_event = asyncio.Event()
    cancelled_at: float | None = None
    candidate = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "x")

    # 0.1s 后触发取消（远小于 2.0s 退避）
    async def _cancel_soon():
        nonlocal cancelled_at
        await asyncio.sleep(0.1)
        cancelled_at = time.monotonic()
        cancel_event.set()

    asyncio.create_task(_cancel_soon())

    with pytest.raises(ReviewCancelledError):
        await client.call_with_retry(
            [candidate],
            [UnifiedMessage(role="user", content="hi")],
            model="x",
            role="main",
            cancel_event=cancel_event,
        )
    assert cancelled_at is not None
    cancel_latency = time.monotonic() - cancelled_at
    # 取消触发后应在 1s 内返回（退避 2s 被 event 抢占）。
    # 以 event 设置时刻计时，避免测试进程调度延迟干扰断言。
    assert cancel_latency < 1.0, f"取消响应过慢: {cancel_latency:.2f}s"
    assert client._logical_attempt_counts == {}
    await client.aclose()


def test_unified_response_meta_exposes_context_window_tokens():
    """响应元数据应携带 winner 上下文窗口，默认 None（未命中候选时）。

    Issue 分析的 safe_context 需按实际服务的模型窗口计算，而不是角色首选。
    UnifiedResponseMeta 必须暴露该字段供调用方读取。
    """
    from backend.core.ai_protocol.models import UnifiedResponseMeta

    meta = UnifiedResponseMeta()
    assert meta.context_window_tokens is None


@pytest.mark.asyncio
async def test_call_with_retry_records_winner_context_window(monkeypatch):
    """fallback 到非首选候选时，响应 meta 应记录 winner 的上下文窗口。

    Issue 分析的 safe_context 必须按实际服务模型而非角色首选计算：primary(258K)
    失败后 fallback 到 1M 窗口的候选，response.meta.context_window_tokens 必须是
    1M，否则调用方只能拿到首选的 258K，导致上下文预算被低估、过早触发告警。
    """
    primary = _StubAdapter(fail_categories=[AIErrorCategory.SERVER_ERROR])
    fallback = _StubAdapter(content="ok")
    router = _ModelRoutingAdapter({"primary": primary, "fallback": fallback})
    _install_router(monkeypatch, router)

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=1,
            total_timeout=10,
            initial_retry_delay=0,
        )
    )
    candidates = [
        _candidate(
            ProtocolFamily.OPENAI_COMPATIBLE,
            "primary",
            context_window_tokens=258000,
        ),
        _candidate(
            ProtocolFamily.OPENAI_COMPATIBLE,
            "fallback",
            context_window_tokens=1000000,
        ),
    ]

    response = await client.call_with_retry(
        candidates,
        [UnifiedMessage(role="user", content="hi")],
        model="primary",
        role="main",
    )

    assert response.content == "ok"
    assert response.meta.served_by == "prov-fallback/fallback"
    assert response.meta.context_window_tokens == 1000000
    await client.aclose()
