"""Actual auxiliary HTTP failures retain provider meters without response content."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from openai import APIStatusError, AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingOperation,
    BillingTransaction,
    BillingUsageCharge,
)
from backend.services import embedding_service
from backend.services.ai_usage_service import finish_billing_operation
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_service import BillingService
from tests.test_billing_usage_attribution import (
    fund_and_price,
)
from tests.test_billing_usage_attribution import (
    sql_runtime as runtime_fixture,
)

sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("charge_failed", [False, True])
@pytest.mark.parametrize(
    ("ranking", "strict"),
    [
        ({}, False),
        ({"results": None}, False),
        ({"results": [None]}, False),
        ({"results": [{"relevance_score": 0.9}]}, False),
        ({"results": [{"index": 0}]}, False),
        ({"results": [{"index": True, "relevance_score": 0.9}]}, False),
        ({"results": [{"index": 9, "relevance_score": 0.9}]}, False),
        ({"results": [{"index": 0, "relevance_score": "bad"}]}, False),
        ({"results": [{"index": 0, "relevance_score": float("inf")}]}, False),
        ({"results": [{"index": 0, "relevance_score": 0.9}] * 2}, False),
        ({"results": [{"index": 0, "relevance_score": 0.9}]}, True),
    ],
)
async def test_malformed_success_response_preserves_usage_as_failed_call(
    sql_runtime, monkeypatch, charge_failed, ranking, strict
):
    factory, engine, policy = sql_runtime
    policy["billing_charge_failed_calls"] = charge_failed
    await fund_and_price(
        factory, kinds=("rerank",), provider="siliconflow", model="aux"
    )
    monkeypatch.setattr(
        embedding_service, "get_settings", lambda: SimpleNamespace(rerank_model="aux")
    )
    payload = {
        **ranking,
        "usage": {
            "prompt_tokens": 1000,
            "total_tokens": 1000,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    # A response double permits non-finite values without a JSON serializer rejecting them.
    service = embedding_service.RerankerService.__new__(
        embedding_service.RerankerService
    )
    service.provider = "siliconflow"
    service.client = SimpleNamespace(
        post=AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: payload
            )
        )
    )
    docs = [{"content": "first"}, {"content": "second"}]
    context = BillingContext(1, str(uuid4()), "pr_review")
    with bind_billing_context(context):
        if strict:
            with pytest.raises(ValueError, match="Malformed"):
                await service._rerank_via_siliconflow("test", docs, 2, 0.1, strict=True)
        else:
            assert await service._rerank_via_siliconflow("test", docs, 2, 0.1) == docs
        # Settlement must use the policy frozen before the provider call.
        policy["billing_charge_failed_calls"] = not charge_failed
        await finish_billing_operation("completed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.outcome == "failed"
        assert usage.input_tokens == 1000
        assert usage.usage_reported is True
        assert Decimal(db.scalar(select(BillingUsageCharge)).provider_cost) == Decimal(
            "0.002"
        )
        assert db.get(BillingOperation, context.operation_id).settled_units == (
            200_000 if charge_failed else 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("call_kind", ["embedding", "rerank"])
@pytest.mark.parametrize("reported_tokens", [25, 0, None])
async def test_auxiliary_http_error_retains_reported_tokens_and_unknowns(
    sql_runtime, monkeypatch, call_kind, reported_tokens
):
    factory, engine, _ = sql_runtime
    provider = "openai" if call_kind == "embedding" else "siliconflow"
    await fund_and_price(factory, kinds=(call_kind,), provider=provider, model="aux")
    payload = {
        "error": {"message": "private response text", "prompt_tokens": 99999},
        "prompt": "private prompt",
        "credential": "private credential",
    }
    if reported_tokens is not None:
        payload["usage"] = {
            "prompt_tokens": reported_tokens,
            "total_tokens": reported_tokens,
            "prompt_tokens_details": {"cached_tokens": 0},
            "prompt": "private nested prompt",
            "credential": "private nested credential",
        }
    settings = SimpleNamespace(
        embedding_model="aux", embedding_batch_size=10, rerank_model="aux"
    )
    monkeypatch.setattr(embedding_service, "get_settings", lambda: settings)
    context = BillingContext(1, str(uuid4()), "pr_review")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json=payload)),
        base_url="https://example.invalid",
    ) as http:
        with bind_billing_context(context):
            if call_kind == "embedding":
                service = embedding_service.EmbeddingService.__new__(
                    embedding_service.EmbeddingService
                )
                service.provider = provider
                service.client = AsyncOpenAI(
                    api_key="test-only",
                    base_url="https://example.invalid",
                    http_client=http,
                    max_retries=0,
                )
                with pytest.raises(APIStatusError) as failure:
                    await service._embed_via_openai_api(["test"])
            else:
                service = embedding_service.RerankerService.__new__(
                    embedding_service.RerankerService
                )
                service.provider = provider
                service.client = http
                with pytest.raises(httpx.HTTPStatusError) as failure:
                    await service._rerank_via_siliconflow(
                        "test", [{"content": "document"}], 1, 0.1, strict=True
                    )
            assert failure.value.response.status_code == 500
            await finish_billing_operation("failed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.outcome == "failed"
        assert usage.input_tokens == reported_tokens
        assert usage.output_tokens is None
        assert usage.usage_reported is (reported_tokens is not None)
        assert "private" not in str(usage.raw_usage)
        assert "99999" not in str(usage.raw_usage)
        assert db.get(BillingOperation, context.operation_id).settled_units == 0
        assert (
            db.scalar(
                select(BillingTransaction).where(
                    BillingTransaction.kind == "consumption"
                )
            )
            is None
        )
        quote = db.scalar(select(BillingUsageCharge))
        if reported_tokens is None:
            assert quote is None
            assert (
                db.scalar(select(BillingCallAttempt)).state == "pending_reconciliation"
            )
            assert usage.raw_usage == {}
        else:
            assert usage.cached_input_tokens == 0
            assert db.scalar(select(BillingCallAttempt)).state == "usage_known"
            assert Decimal(quote.provider_cost) == Decimal(reported_tokens) * Decimal(
                "0.000002"
            )


@pytest.mark.asyncio
async def test_rerank_http_failure_keeps_native_document_meter(
    sql_runtime, monkeypatch
):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory, kinds=())
    async with factory() as db:
        await BillingService(db).publish_price(
            "siliconflow",
            "native",
            "rerank",
            {
                "currency": "USD",
                "settlement_currency": "USD",
                "fx_rate": "1",
                "markup": "1",
                "credits_per_currency_unit": "100",
                "unit": "documents",
                "unit_price": "0.01",
            },
            actor_id=1,
        )
        await db.commit()
    monkeypatch.setattr(
        embedding_service,
        "get_settings",
        lambda: SimpleNamespace(rerank_model="native"),
    )
    payload = {
        "error": {"message": "private response text"},
        "meta": {"billed_units": {"documents": 7, "search_units": 0, "requests": 1}},
    }
    context = BillingContext(1, str(uuid4()), "issue_analysis")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json=payload)),
        base_url="https://example.invalid",
    ) as http:
        service = embedding_service.RerankerService.__new__(
            embedding_service.RerankerService
        )
        service.provider = "siliconflow"
        service.client = http
        with bind_billing_context(context):
            with pytest.raises(httpx.HTTPStatusError):
                await service._rerank_via_siliconflow(
                    "test", [{"content": "document"}], 1, 0.1, strict=True
                )
            await finish_billing_operation("failed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.usage_reported is False
        assert usage.input_tokens is None
        native_units = {"documents": 7, "search_units": 0, "requests": 1}
        assert {key: usage.billing_units[key] for key in native_units} == native_units
        assert usage.raw_usage == {"meta": {"billed_units": native_units}}
        assert Decimal(db.scalar(select(BillingUsageCharge)).provider_cost) == Decimal(
            "0.07"
        )
        assert db.get(BillingOperation, context.operation_id).settled_units == 0
