"""Global AI provider-usage ledger tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.core.ai_protocol.models import ProtocolFamily, UnifiedUsage
from backend.models.ai_usage_models import AIUsageRecord
from backend.services import ai_usage_service, dashboard_stats_service
from backend.services.ai_usage_service import (
    ACCOUNTED_CALL_KINDS,
    GlobalTokenTotals,
    build_usage_record_key,
    extract_provider_usage,
    fetch_global_token_totals,
)


class _AsyncExecuteAdapter:
    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement):
        return self._session.execute(statement)


@pytest.mark.asyncio
async def test_unified_usage_uses_candidate_effective_protocol(monkeypatch):
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(ai_usage_service, "record_ai_usage_best_effort", capture)
    candidate = SimpleNamespace(
        provider=SimpleNamespace(
            id="provider-default",
            family=ProtocolFamily.OPENAI_COMPATIBLE,
        ),
        effective_protocol=ProtocolFamily.ANTHROPIC_NATIVE,
        model=SimpleNamespace(model_id="model-1"),
    )

    assert await ai_usage_service.record_unified_ai_usage_best_effort(
        logical_call_id="call-1",
        call_kind="chat",
        role="main",
        candidate=candidate,
        usage=UnifiedUsage(input_tokens=1, output_tokens=2),
    )

    assert captured["protocol_family"] == ProtocolFamily.ANTHROPIC_NATIVE.value


@pytest.mark.asyncio
async def test_unified_usage_uses_legacy_candidate_protocol_family(monkeypatch):
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(ai_usage_service, "record_ai_usage_best_effort", capture)
    candidate = SimpleNamespace(
        provider=SimpleNamespace(
            id="provider-default",
            family=ProtocolFamily.OPENAI_COMPATIBLE,
        ),
        protocol_family=ProtocolFamily.GEMINI_NATIVE,
        model=SimpleNamespace(model_id="model-legacy"),
    )

    assert await ai_usage_service.record_unified_ai_usage_best_effort(
        logical_call_id="legacy-call",
        call_kind="chat",
        role="main",
        candidate=candidate,
        usage=UnifiedUsage(input_tokens=1, output_tokens=2),
    )

    assert captured["protocol_family"] == ProtocolFamily.GEMINI_NATIVE.value


def test_extract_provider_usage_preserves_missing_fields_and_dimensions():
    usage = UnifiedUsage(
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=80,
        reasoning_tokens=5,
        reported_fields=frozenset(
            {
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "reasoning_tokens",
            }
        ),
    )

    counters = extract_provider_usage(usage)

    assert counters.input_tokens == 100
    assert counters.output_tokens == 20
    assert counters.cached_input_tokens == 80
    assert counters.reasoning_tokens == 5
    assert counters.cache_creation_tokens is None
    # Cache/reasoning are dimensions, not additional billable parents.
    assert counters.input_tokens + counters.output_tokens == 120


def test_extract_provider_usage_accepts_embedding_and_rerank_envelopes():
    embedding = extract_provider_usage(
        type("EmbeddingUsage", (), {"prompt_tokens": 31, "total_tokens": 31})(),
        input_only=True,
    )
    rerank = extract_provider_usage(
        {"meta": {"tokens": {"total_tokens": 19}}},
        input_only=True,
    )

    assert embedding.input_tokens == 31
    assert embedding.output_tokens is None
    assert rerank.input_tokens == 19
    assert rerank.output_tokens is None


def test_usage_record_key_is_stable_and_mysql_index_safe():
    key = build_usage_record_key("chat", "x" * 400)

    assert key == build_usage_record_key("chat", "x" * 400)
    assert len(key) <= 191


@pytest.mark.asyncio
async def test_global_totals_only_include_accounted_ledger_calls_without_cache_double_count():
    engine = create_engine("sqlite:///:memory:")
    AIUsageRecord.__table__.create(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add_all(
            [
                AIUsageRecord(
                    record_key="legacy-module-baseline:v1",
                    call_kind="legacy_baseline",
                    role="legacy",
                    provider_id="legacy",
                    model_id="legacy",
                    protocol_family="legacy",
                    input_tokens=100,
                    output_tokens=20,
                    usage_reported=True,
                    occurred_at=now,
                ),
                AIUsageRecord(
                    record_key="chat:1",
                    call_kind="chat",
                    role="main",
                    provider_id="deepseek",
                    model_id="deepseek-v4-flash",
                    protocol_family="openai-compatible",
                    input_tokens=50,
                    output_tokens=5,
                    cached_input_tokens=40,
                    reasoning_tokens=2,
                    usage_reported=True,
                    occurred_at=now,
                ),
                AIUsageRecord(
                    record_key="embedding:1",
                    call_kind="embedding",
                    role="embedding",
                    provider_id="siliconflow",
                    model_id="bge-m3",
                    protocol_family="openai-compatible",
                    input_tokens=30,
                    usage_reported=True,
                    occurred_at=now,
                ),
                AIUsageRecord(
                    record_key="rerank:1",
                    call_kind="rerank",
                    role="rerank",
                    provider_id="siliconflow",
                    model_id="bge-reranker",
                    protocol_family="siliconflow-rerank",
                    usage_reported=False,
                    occurred_at=now,
                ),
            ]
        )
        session.commit()

        totals = await fetch_global_token_totals(_AsyncExecuteAdapter(session))

    assert totals.input_tokens == 80
    assert totals.output_tokens == 5
    assert totals.recorded_calls == 3
    assert totals.unreported_usage_calls == 1
    assert "legacy_baseline" not in ACCOUNTED_CALL_KINDS


@pytest.mark.asyncio
async def test_dashboard_uses_global_ledger_for_admin_and_keeps_cost_separate(
    monkeypatch,
):
    async def fake_cost(_db, _scope_user=None):
        return 77

    async def fake_global(_db):
        return GlobalTokenTotals(
            input_tokens=900,
            output_tokens=100,
            recorded_calls=12,
            unreported_usage_calls=2,
        )

    monkeypatch.setattr(
        dashboard_stats_service,
        "fetch_estimated_cost",
        fake_cost,
    )
    monkeypatch.setattr(
        dashboard_stats_service,
        "fetch_global_token_totals",
        fake_global,
    )

    result = await dashboard_stats_service.fetch_module_token_stats(object())

    assert result["total_prompt"] == 900
    assert result["total_completion"] == 100
    assert result["total_cost"] == 77
    assert result["token_usage_available"] is True


@pytest.mark.asyncio
async def test_dashboard_never_falls_back_to_legacy_token_columns_for_scoped_user(
    monkeypatch,
):
    async def fake_cost(_db, _scope_user=None):
        return 31

    async def fail_if_called(_db):
        raise AssertionError("scoped dashboard must not read global ledger")

    monkeypatch.setattr(dashboard_stats_service, "fetch_estimated_cost", fake_cost)
    monkeypatch.setattr(
        dashboard_stats_service,
        "fetch_global_token_totals",
        fail_if_called,
    )

    result = await dashboard_stats_service.fetch_module_token_stats(object(), "alice")

    assert result == {
        "total_prompt": None,
        "total_completion": None,
        "total_cost": 31,
        "token_usage_available": False,
    }
