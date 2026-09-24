"""Issue #613: effective AI request parameter policy regression tests."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from typing import Any

import pytest

from backend.core.ai_protocol.models import (
    AuthScheme,
    MetadataSource,
    ModelCapabilitySet,
    ModelMetadata,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedEndpoint,
    ResolvedModel,
    StopReason,
    UnifiedMessage,
    UnifiedResponse,
    UnifiedUsage,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.core.config import Settings
from backend.services.ai_reviewer import unified_client as unified_module
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.unified_client import FallbackConfig, UnifiedAIClient


def _candidate(
    *,
    context_window_tokens: int,
    max_output_tokens: int,
    temperature: float | None = None,
) -> ResolvedModel:
    declaration = ProviderDeclaration(
        id="custom",
        label="Custom",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://example.test/v1/",
        auth_scheme=AuthScheme.BEARER,
    )
    endpoint: ResolvedEndpoint = resolve_endpoint(declaration, None)
    metadata = ModelMetadata(
        model_id="gpt-oss-120b",
        provider_id="custom",
        display_name="gpt-oss-120b",
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        capabilities=ModelCapabilitySet(
            temperature=True,
            top_p=True,
            thinking=True,
            effort=True,
        ),
        reasoning_params=ReasoningParams(
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        ),
        source=MetadataSource.USER_OVERRIDE,
    )
    return ResolvedModel(
        provider=declaration,
        model=metadata,
        credential="key",
        endpoint=endpoint,
    )


class _RecordingAdapter:
    family = ProtocolFamily.OPENAI_COMPATIBLE

    def __init__(self):
        self.requests: list[Any] = []

    async def chat(self, _client, _endpoint, _credential, request, *, timeout=None):
        self.requests.append(request)
        return UnifiedResponse(
            content="ok",
            tool_calls=[],
            stop_reason=StopReason.END_TURN,
            usage=UnifiedUsage(input_tokens=1, output_tokens=1),
        )


def _install_adapter(monkeypatch, adapter: _RecordingAdapter) -> None:
    monkeypatch.setattr(unified_module, "_get_adapter", lambda _family: adapter)


@pytest.mark.asyncio
async def test_model_output_and_temperature_override_win(monkeypatch):
    adapter = _RecordingAdapter()
    _install_adapter(monkeypatch, adapter)
    candidate = _candidate(
        context_window_tokens=128_000,
        max_output_tokens=32_000,
        temperature=0.15,
    )
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=0),
        compressor=None,
    )

    response = await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="hello")],
        model="",
        # Legacy callers may still pass the old global value. It is now a cap,
        # never an override above model metadata.
        max_tokens=128_000,
        role="summary",
    )

    assert adapter.requests[0].max_tokens == 32_000
    assert adapter.requests[0].temperature == 0.15
    assert response.meta.effective_request["effective_max_output_tokens"] == 32_000
    assert response.meta.effective_request["temperature"] == 0.15
    assert response.meta.effective_request["parameter_sources"]["max_output_tokens"] == (
        "model_override"
    )
    assert "hello" not in str(response.meta.effective_request)
    assert "key" not in str(response.meta.effective_request).lower()
    await client.aclose()


@pytest.mark.asyncio
async def test_task_output_cap_is_applied_without_exceeding_model_limit(monkeypatch):
    adapter = _RecordingAdapter()
    _install_adapter(monkeypatch, adapter)
    candidate = _candidate(context_window_tokens=128_000, max_output_tokens=32_000)
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=0),
        compressor=None,
    )

    await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="hello")],
        model="",
        output_token_cap=5_000,
        role="summary",
    )

    assert adapter.requests[0].max_tokens == 5_000
    await client.aclose()


@pytest.mark.asyncio
async def test_context_budget_clamps_output_before_provider_call(monkeypatch):
    adapter = _RecordingAdapter()
    _install_adapter(monkeypatch, adapter)
    candidate = _candidate(context_window_tokens=100, max_output_tokens=4_096)
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=0),
        compressor=None,
    )
    message = UnifiedMessage(role="user", content="x" * 200)

    await client.call_with_retry(
        [candidate],
        [message],
        model="",
        role="summary",
    )

    request = adapter.requests[0]
    estimated_input = len(message.content) // 4
    assert request.max_tokens > 0
    assert estimated_input + request.max_tokens + 32 <= 100
    await client.aclose()


@pytest.mark.asyncio
async def test_compressor_preflight_and_wire_request_share_effective_output(monkeypatch):
    adapter = _RecordingAdapter()
    _install_adapter(monkeypatch, adapter)
    candidate = _candidate(context_window_tokens=128_000, max_output_tokens=32_000)

    @dataclass
    class RecordingCompressor:
        effective_outputs: list[int]

        async def maybe_compress(
            self,
            _candidate,
            messages,
            *,
            tracker=None,
            effective_max_output_tokens=None,
            safety_reserve_tokens=None,
        ):
            self.effective_outputs.append(effective_max_output_tokens)
            return False, messages

    compressor = RecordingCompressor([])
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=0),
        compressor=compressor,
    )

    await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="hello")],
        model="",
        max_tokens=128_000,
        role="summary",
    )

    assert compressor.effective_outputs == [32_000]
    assert adapter.requests[0].max_tokens == 32_000
    await client.aclose()


@pytest.mark.asyncio
async def test_compression_preserves_the_preflight_output_budget(monkeypatch):
    adapter = _RecordingAdapter()
    _install_adapter(monkeypatch, adapter)
    candidate = _candidate(context_window_tokens=10_000, max_output_tokens=4_096)

    @dataclass
    class ReplacingCompressor:
        effective_outputs: list[int]

        async def maybe_compress(
            self,
            _candidate,
            _messages,
            *,
            tracker=None,
            effective_max_output_tokens=None,
            safety_reserve_tokens=None,
        ):
            self.effective_outputs.append(effective_max_output_tokens)
            return True, [UnifiedMessage(role="user", content="compressed")]

    compressor = ReplacingCompressor([])
    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=0),
        compressor=compressor,
    )

    await client.call_with_retry(
        [candidate],
        [UnifiedMessage(role="user", content="x" * 100_000)],
        model="",
        role="summary",
    )

    assert compressor.effective_outputs == [4_096]
    assert adapter.requests[0].max_tokens == 4_096
    await client.aclose()


def test_facade_temperature_defaults_are_undefined():
    """A facade default must mean 'use model configuration', not implicit 0.7."""
    assert signature(AIApiClient.call_with_retry).parameters["temperature"].default is None
    assert signature(AIApiClient.stream_with_retry).parameters["temperature"].default is None


def test_legacy_global_model_parameters_are_removed():
    assert {"ai_temperature", "ai_max_tokens"}.isdisjoint(Settings.model_fields)


def test_business_ai_calls_do_not_pass_raw_model_parameters():
    """Business modules may cap output, but must not override model parameters."""
    root = Path(__file__).parents[1] / "backend"
    allowed_files = {
        "services/ai_reviewer/api_client.py",
        "services/ai_reviewer/unified_client.py",
        "services/ai_reviewer/compression/unified_compressor.py",
    }
    violations: list[str] = []

    paths = [*(root / "services").rglob("*.py"), *(root / "workers").rglob("*.py")]
    for path in paths:
        relative = str(path.relative_to(root))
        if relative in allowed_files:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"call_with_retry", "stream_with_retry"}:
                continue
            raw_names = {
                keyword.arg
                for keyword in node.keywords
                if keyword.arg in {"temperature", "max_tokens"}
            }
            if raw_names:
                violations.append(
                    f"{path}:{node.lineno} passes {sorted(raw_names)} to "
                    f"{node.func.attr}"
                )

    assert violations == []
