"""主动上下文压缩接线测试 / Proactive context compression wiring tests.

验证：
- maybe_compress 预算基于候选模型的 context_window_tokens（与日志分母一致），
  而非 ModelContextManager 兜底窗口（未注册模型会退 128K 导致永不触发）。
- UnifiedAIClient.call_with_retry 在调用适配器前执行主动压缩，超预算时
  压缩后的消息被真正发出。
- AIApiClient 将压缩器注入统一客户端。
"""

import pytest

from backend.core.ai_protocol.models import (
    AIErrorCategory,
    AuthScheme,
    MetadataSource,
    ModelCapabilitySet,
    ModelMetadata,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedModel,
    StopReason,
    UnifiedMessage,
    UnifiedResponse,
    UnifiedUsage,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.services.ai_reviewer.compression.unified_compressor import (
    UnifiedContextCompressor,
)
from backend.services.ai_reviewer.unified_client import (
    FallbackConfig,
    UnifiedAIClient,
)


def _candidate(
    model_id: str,
    *,
    context_window_tokens: int = 128000,
) -> ResolvedModel:
    decl = ProviderDeclaration(
        id=f"prov-{model_id}",
        label=model_id,
        family=ProtocolFamily.OPENAI_COMPATIBLE,
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


class _RecordingAdapter:
    """记录每次 chat 请求的桩适配器 / Stub adapter that records requests."""

    family = ProtocolFamily.OPENAI_COMPATIBLE

    def __init__(self, *, content: str = "ok"):
        self.calls = 0
        self.requests: list = []
        self._content = content

    async def list_models(self, *args, **kwargs):
        return []

    async def fetch_model_metadata(self, *args, **kwargs):
        return None

    async def chat(self, client, endpoint, credential, request, *, timeout=None):
        self.calls += 1
        self.requests.append(request)
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


def _install_stub(monkeypatch, adapter):
    from backend.core.ai_protocol import registry as reg
    from backend.services.ai_reviewer.compression import unified_compressor as uc_module

    monkeypatch.setitem(
        reg._DEFAULT_CHAT_PATHS, ProtocolFamily.OPENAI_COMPATIBLE, "chat/completions"
    )
    monkeypatch.setattr(reg, "get_adapter", lambda f: adapter)
    # unified_compressor 通过 `from ... import get_adapter` 绑定模块属性，
    # 需直接替换该模块内的引用，否则摘要调用会落到真实适配器。
    monkeypatch.setattr(uc_module, "get_adapter", lambda f: adapter)


def _message_texts(request) -> list[str]:
    return [m.content or "" for m in request.messages]


@pytest.mark.asyncio
async def test_maybe_compress_budget_uses_candidate_context_window(monkeypatch):
    """预算按候选模型窗口计算：10K 窗口 × 0.8 = 8K，12K tokens 应触发压缩。

    若误用 ModelContextManager 兜底（未注册模型 → 128K），则 12K 不会触发。
    """
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)

    compressed, messages = await compressor.maybe_compress(
        candidate,
        [UnifiedMessage(role="user", content="x" * 50_000)],
    )

    assert compressed is True
    # 压缩后为摘要 + 保留最后一轮用户输入
    assert len(messages) >= 2
    assert any(
        m.content and m.content.startswith("## 已压缩的历史上下文")
        for m in messages
    )


@pytest.mark.asyncio
async def test_maybe_compress_skips_within_budget(monkeypatch):
    """预算内不压缩。"""
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)

    compressed, messages = await compressor.maybe_compress(
        candidate,
        [UnifiedMessage(role="user", content="hello")],
    )

    assert compressed is False
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_call_with_retry_compresses_when_over_budget(monkeypatch):
    """call_with_retry 接线：超预算时先摘要压缩，再以压缩后消息调用主模型。"""
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    response = await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="x" * 50_000)],
        model="",
        role="main",
    )

    assert response is not None
    # 摘要调用 + 主调用
    assert adapter.calls == 2
    main_texts = _message_texts(adapter.requests[1])
    assert any(t.startswith("## 已压缩的历史上下文") for t in main_texts)
    await client.aclose()


@pytest.mark.asyncio
async def test_call_with_retry_skips_compression_within_budget(monkeypatch):
    """预算内：不压缩，仅一次适配器调用。"""
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="hello")],
        model="",
        role="main",
    )

    assert adapter.calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_call_with_retry_respects_disabled_compressor(monkeypatch):
    """压缩器禁用时跳过。"""
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8, enabled=False)
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="x" * 50_000)],
        model="",
        role="main",
    )

    assert adapter.calls == 1
    await client.aclose()


def test_api_client_injects_compressor_into_unified_client():
    """AIApiClient 显式注入的压缩器应传给 UnifiedAIClient。"""
    from backend.services.ai_reviewer.api_client import AIApiClient

    compressor = UnifiedContextCompressor(threshold=0.8)
    api = AIApiClient(compressor=compressor)

    unified = api._get_unified_client()

    assert unified._compressor is compressor


def test_api_client_auto_builds_compressor_when_enabled():
    """默认配置启用压缩时，AIApiClient 自动构建压缩器。"""
    from backend.core.config import get_settings
    from backend.services.ai_reviewer.api_client import AIApiClient

    if not get_settings().enable_context_compression:
        pytest.skip("enable_context_compression 未启用")
    api = AIApiClient()

    unified = api._get_unified_client()

    assert unified._compressor is not None
    assert isinstance(unified._compressor, UnifiedContextCompressor)
