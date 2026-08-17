"""主动上下文压缩接线测试 / Proactive context compression wiring tests.

验证：
- maybe_compress 预算基于候选模型的 context_window_tokens（与日志分母一致），
  而非 ModelContextManager 兜底窗口（未注册模型会退 128K 导致永不触发）。
- UnifiedAIClient.call_with_retry 在调用适配器前执行主动压缩，超预算时
  压缩后的消息被真正发出。
- AIApiClient 将压缩器注入统一客户端。
"""

from types import SimpleNamespace

import pytest

from backend.core.ai_protocol.errors import AIError
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
    UnifiedToolCall,
    UnifiedUsage,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.core.model_context import get_model_context_manager
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
    provider_family: ProtocolFamily = ProtocolFamily.OPENAI_COMPATIBLE,
    protocol: ProtocolFamily | str | None = None,
) -> ResolvedModel:
    decl = ProviderDeclaration(
        id=f"prov-{model_id}",
        label=model_id,
        family=provider_family,
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
        provider=decl,
        model=metadata,
        credential="key",
        endpoint=endpoint,
        protocol=protocol,
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


class _OverflowBudgetAdapter(_RecordingAdapter):
    """首个主请求超限，且拒绝超出窗口的摘要请求。"""

    def __init__(self, *, context_window_tokens: int):
        super().__init__(content="recovered")
        self.context_window_tokens = context_window_tokens
        self.summary_totals: list[int] = []

    async def chat(self, client, endpoint, credential, request, *, timeout=None):
        self.calls += 1
        self.requests.append(request)
        estimator = get_model_context_manager()
        input_tokens = sum(
            estimator.estimate_tokens(message.content or "")
            for message in request.messages
        )
        is_summary = bool(
            request.messages
            and request.messages[0].role == "system"
            and "compress" in (request.messages[0].content or "").lower()
        )
        if is_summary:
            total = input_tokens + request.max_tokens
            self.summary_totals.append(total)
            if total > self.context_window_tokens:
                raise AIError(
                    AIErrorCategory.CONTEXT_OVERFLOW,
                    "summary request exceeds context window",
                )
            return UnifiedResponse(
                content="summary",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=UnifiedUsage(input_tokens=input_tokens, output_tokens=4),
            )
        if self.calls == 1:
            raise AIError(
                AIErrorCategory.CONTEXT_OVERFLOW,
                "main request exceeds context window",
            )
        return UnifiedResponse(
            content="recovered",
            tool_calls=[],
            stop_reason=StopReason.END_TURN,
            usage=UnifiedUsage(input_tokens=input_tokens, output_tokens=4),
        )


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
async def test_compression_summary_uses_effective_candidate_protocol(monkeypatch):
    """摘要请求也必须尊重账号的协议覆盖，而非 provider 默认族。"""
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    from backend.services.ai_reviewer.compression import unified_compressor as uc_module

    seen_families: list[ProtocolFamily] = []

    def fake_get_adapter(family):
        seen_families.append(family)
        return adapter

    monkeypatch.setattr(uc_module, "get_adapter", fake_get_adapter)
    candidate = _candidate(
        "protocol-summary",
        context_window_tokens=10_000,
        protocol=ProtocolFamily.ANTHROPIC_NATIVE,
    )
    compressor = UnifiedContextCompressor(threshold=0.8)

    compressed, _messages = await compressor.maybe_compress(
        candidate,
        [UnifiedMessage(role="user", content="x" * 50_000)],
    )

    assert compressed is True
    assert seen_families == [ProtocolFamily.ANTHROPIC_NATIVE]


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
async def test_overflow_recovery_bounds_summary_request_to_candidate_window(monkeypatch):
    """provider overflow recovery must not retry the full oversized history."""
    adapter = _OverflowBudgetAdapter(context_window_tokens=10_000)
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("overflow-recovery", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    response = await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="x" * 30_000)],
        model="",
        max_tokens=4096,
        role="main",
    )

    assert response.content == "recovered"
    assert adapter.calls == 3  # overflow + bounded summary + compressed retry
    assert adapter.summary_totals
    assert all(total <= 10_000 for total in adapter.summary_totals)
    compressed_request = adapter.requests[-1]
    assert any(
        message.content and message.content.startswith("## 已压缩的历史上下文")
        for message in compressed_request.messages
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_overflow_recovery_reuses_reasoning_snapshot(monkeypatch):
    """compressed retry keeps the original protocol/thinking observation snapshot."""
    adapter = _OverflowBudgetAdapter(context_window_tokens=10_000)
    _install_stub(monkeypatch, adapter)
    candidate = _candidate(
        "overflow-snapshot",
        context_window_tokens=10_000,
        protocol=ProtocolFamily.ANTHROPIC_NATIVE,
    )
    compressor = UnifiedContextCompressor(threshold=0.8)
    observer = _SnapshotObserver()
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    response = await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="x" * 30_000)],
        model="",
        max_tokens=4096,
        thinking={"type": "enabled"},
        effort="high",
        role="main",
        observer=observer,
    )

    assert response.content == "recovered"
    assert len(observer.snapshots) == 2
    assert observer.snapshots[0] is observer.snapshots[1]
    assert observer.snapshots[1].protocol_family == ProtocolFamily.ANTHROPIC_NATIVE.value
    assert observer.snapshots[1].effective_thinking_mode == "unsupported"
    assert observer.snapshots[1].effective_effort == "unsupported"
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


class _RecordingObserver:
    """记录 record_context_replacement 调用的桩 observer / Stub observer."""

    def __init__(self):
        self.context = None
        self.replacements: list[tuple[list, str]] = []
        self._attempt = 0

    async def record_context_replacement(self, messages, *, trigger_reason):
        self.replacements.append((list(messages), trigger_reason))

    async def send_chat(
        self,
        adapter,
        client,
        candidate,
        request,
        *,
        timeout=None,
        **_kwargs,
    ):
        self._attempt += 1
        response = await adapter.chat(
            client,
            candidate.endpoint,
            candidate.credential,
            request,
            timeout=timeout,
        )
        return response, self._attempt


class _SnapshotObserver(_RecordingObserver):
    def __init__(self):
        super().__init__()
        self.snapshots = []

    async def send_chat(self, *args, **kwargs):
        self.snapshots.append(kwargs.get("reasoning_snapshot"))
        return await super().send_chat(*args, **kwargs)


def test_split_message_blocks_keeps_non_adjacent_tool_results_in_order():
    messages = [
        UnifiedMessage(
            role="assistant",
            content="",
            tool_calls=[UnifiedToolCall(id="call-1", name="lookup", arguments="{}")],
        ),
        UnifiedMessage(role="user", content="intervening"),
        UnifiedMessage(role="tool", tool_call_id="call-1", content="result"),
        UnifiedMessage(role="user", content="latest"),
    ]

    blocks = UnifiedContextCompressor._split_message_blocks(messages)
    flattened = [message for block in blocks for message in block]

    assert flattened == messages
    assert len(blocks) == 2
    assert [message.role for message in blocks[0]] == ["assistant", "user", "tool"]
    assert sum(
        message.tool_call_id == "call-1"
        for message in flattened
        if message.role == "tool"
    ) == 1


def test_fit_message_blocks_keeps_non_adjacent_tool_pair_within_budget():
    messages = [
        UnifiedMessage(role="user", content="older"),
        UnifiedMessage(
            role="assistant",
            content="",
            tool_calls=[UnifiedToolCall(id="call-1", name="lookup", arguments="{}")],
        ),
        UnifiedMessage(role="user", content="intervening"),
        UnifiedMessage(role="tool", tool_call_id="call-1", content="result"),
    ]
    compressor = UnifiedContextCompressor()
    blocks = compressor._split_message_blocks(messages)
    pair_budget = compressor._estimate(blocks[-1])

    fitted = compressor._fit_message_blocks(messages, pair_budget)

    assert [message.role for message in fitted] == ["assistant", "user", "tool"]
    assert sum(
        message.tool_call_id == "call-1"
        for message in fitted
        if message.role == "tool"
    ) == 1


@pytest.mark.asyncio
async def test_call_with_retry_proactive_compression_records_observability(monkeypatch):
    """主动压缩成功后应记录 context replacement（trigger_reason=threshold）。

    若主动压缩路径不接入可观测性，observer 不会收到任何调用。
    """
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)
    observer = _RecordingObserver()
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="x" * 50_000)],
        model="",
        role="main",
        observer=observer,
    )

    assert len(observer.replacements) == 1
    replacement_messages, trigger = observer.replacements[0]
    assert trigger == "threshold"
    # 压缩后消息包含摘要 + 保留的最后一轮用户输入
    assert any(
        m.content and m.content.startswith("## 已压缩的历史上下文")
        for m in replacement_messages
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_call_with_retry_within_budget_no_observability_record(monkeypatch):
    """预算内不压缩，也不应记录 context replacement。"""
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)
    observer = _RecordingObserver()
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="hello")],
        model="",
        role="main",
        observer=observer,
    )

    assert observer.replacements == []
    await client.aclose()


@pytest.mark.asyncio
async def test_call_with_retry_observability_failure_does_not_block(monkeypatch):
    """record_context_replacement 失败不应阻塞主流程（仅降级）。"""
    adapter = _RecordingAdapter()
    _install_stub(monkeypatch, adapter)
    candidate = _candidate("mimo-v2.5", context_window_tokens=10_000)
    compressor = UnifiedContextCompressor(threshold=0.8)

    class _BoomObserver(_RecordingObserver):
        async def record_context_replacement(self, messages, *, trigger_reason):
            raise RuntimeError("observability down")

    observer = _BoomObserver()
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=1),
        compressor=compressor,
    )

    response = await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="x" * 50_000)],
        model="",
        role="main",
        observer=observer,
    )

    assert response is not None
    assert adapter.calls == 2  # 摘要 + 主调用
    await client.aclose()


@pytest.mark.asyncio
async def test_record_context_replacement_accepts_legacy_dict_messages():
    """record_context_replacement 应兼容旧版 dict 消息（PR 审查 ContextCompressor 输出）。

    PR 审查 `_run_tool_loop` 压缩分支传入 dict 列表（reviewer 消息形态），
    而非 UnifiedMessage 对象；若只支持对象形态，role/内容会丢失为空。
    """
    from backend.services.activity_observability.observer import ObservedModelSender

    class _FakeContext:
        thread_id = 1
        work_unit_id = 2

    class _FakeRevision:
        id = 99

    class _FakeToolService:
        def __init__(self):
            self.calls = []

        async def replace_context_messages(
            self, *, thread_id, work_unit_id, messages, lease, trigger_reason
        ):
            self.calls.append(
                {
                    "thread_id": thread_id,
                    "work_unit_id": work_unit_id,
                    "messages": messages,
                    "trigger_reason": trigger_reason,
                }
            )
            return _FakeRevision()

    tool_service = _FakeToolService()
    observer = ObservedModelSender(
        attempt_service=object(),
        context=_FakeContext(),
        tool_service=tool_service,
        lease=object(),
    )

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "## 已压缩的历史上下文\nsummary"},
        {"role": "user", "content": "latest user turn"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]

    revision_id = await observer.record_context_replacement(
        messages,
        trigger_reason="threshold",
    )

    assert revision_id == 99
    assert len(tool_service.calls) == 1
    assert tool_service.calls[0]["trigger_reason"] == "threshold"
    persisted = tool_service.calls[0]["messages"]
    assert persisted[0] == {"role": "system", "content": "sys"}
    assert persisted[1]["role"] == "user"
    assert persisted[3]["tool_calls"][0]["function"]["name"] == "read_file"
    assert persisted[4]["tool_call_id"] == "call_1"


# ---------------------------------------------------------------------------
# PR 审查 _run_tool_loop 显式压缩 → 可观测性
# ---------------------------------------------------------------------------


def _reviewer_under_test(monkeypatch, *, enable_compression, observer=None):
    """构造 _run_tool_loop 可测的最小 AIReviewer（沿用 test_ai_reviewer_incremental_callback 模式）。

    API 客户端首轮触发压缩（超大估算）后返回最终信封；压缩器直接返回
    压缩后消息（缩短的 user 消息），无需真实 AI 摘要调用。
    """
    from backend.services.ai_reviewer.reviewer import AIReviewer

    class _FakeApiClient:
        def __init__(self):
            self.calls = []

        async def resolve_role_model_context(self, role):
            return "test-model", 100_000

        async def call_with_retry(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                # 首轮返回工具调用，让循环继续走到压缩检查
                tool_call = SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(
                        name="read_file", arguments='{"path": "a.py"}'
                    ),
                )
                message = SimpleNamespace(content=None, tool_calls=[tool_call])
            else:
                message = SimpleNamespace(
                    content=VALID_REVIEW_ENVELOPE,
                    tool_calls=[],
                )
            choice = SimpleNamespace(message=message)
            usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)
            return SimpleNamespace(choices=[choice], usage=usage)

    class _FakeCompressor:
        def estimate_messages_tokens(self, msgs):
            return 100_000  # 远超阈值 → 触发压缩

        async def compress_conversation_history(
            self, messages, system_prompt, max_tokens, tracker=None
        ):
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "## 已压缩的历史上下文\nsummary"},
                {"role": "user", "content": "latest turn"},
            ]

    class _FakeResultParser:
        def parse_review_result(self, text, strategy):
            return {
                "ai_decision": "approve",
                "score": 8,
                "summary": "ok",
                "review": text,
                "comments": [],
                "inline_comments": [],
            }

    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer.api_client = _FakeApiClient()
    reviewer.result_parser = _FakeResultParser()
    reviewer.tool_handler = object()
    reviewer.model_context_mgr = SimpleNamespace(
        calculate_safe_context=lambda model, threshold: 100_000
    )
    reviewer.enable_compression = enable_compression
    reviewer.compression_threshold = 0.85
    reviewer.context_compressor = _FakeCompressor()

    strategy_config = SimpleNamespace(get_context_enhancement_config=dict)
    monkeypatch.setattr(
        "backend.services.ai_reviewer.reviewer.get_strategy_config",
        lambda: strategy_config,
    )
    return reviewer, observer


VALID_REVIEW_ENVELOPE = """<SAKURA_REVIEW>
<VERSION>1</VERSION>
<SCORE>8</SCORE>
<DECISION>approve</DECISION>
<DECISION_REASON>
No blocking defects were found.
</DECISION_REASON>
<SUMMARY>
The incremental change is safe.
</SUMMARY>
<FINDINGS>
</FINDINGS>
</SAKURA_REVIEW>"""


@pytest.mark.asyncio
async def test_reviewer_tool_loop_compression_records_observability(monkeypatch):
    """PR 审查 `_run_tool_loop` 显式压缩（ContextCompressor）应写入可观测性。

    此前该分支不调用 `record_context_replacement`，压缩事件在实时监控/
    对话流中不可见。
    """
    from backend.services.ai_reviewer.token_tracker import TokenTracker

    observer = _RecordingObserver()
    reviewer, observer = _reviewer_under_test(
        monkeypatch, enable_compression=True, observer=observer
    )

    await reviewer._run_tool_loop(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x" * 5_000},
        ],
        system_prompt="system",
        strategy="standard",
        enabled_tools=[],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        context={},
        observer=observer,
    )

    assert len(observer.replacements) == 1
    replacement_messages, trigger = observer.replacements[0]
    assert trigger == "threshold"
    assert any(
        m.get("content", "").startswith("## 已压缩的历史上下文")
        for m in replacement_messages
    )


@pytest.mark.asyncio
async def test_reviewer_tool_loop_compression_disabled_no_observability(monkeypatch):
    """压缩未启用时，不记录 context replacement。"""
    from backend.services.ai_reviewer.token_tracker import TokenTracker

    observer = _RecordingObserver()
    reviewer, observer = _reviewer_under_test(
        monkeypatch, enable_compression=False, observer=observer
    )

    await reviewer._run_tool_loop(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x" * 5_000},
        ],
        system_prompt="system",
        strategy="standard",
        enabled_tools=[],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        context={},
        observer=observer,
    )

    assert observer.replacements == []
