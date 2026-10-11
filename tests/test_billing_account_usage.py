"""Actual provider attempts retain configured account identity across fallback."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.ai_protocol.errors import AIError
from backend.core.ai_protocol.models import (
    AIErrorCategory,
    ProtocolFamily,
    StopReason,
    UnifiedMessage,
    UnifiedResponse,
    UnifiedStreamEvent,
    usage_from_mapping,
)
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingOperation,
    BillingPriceProfile,
    BillingUsageCharge,
    BillingWallet,
)
from backend.services import ai_usage_service
from backend.services.ai_reviewer import unified_client
from backend.services.ai_reviewer.compression import unified_compressor
from backend.services.ai_reviewer.compression.unified_compressor import (
    UnifiedContextCompressor,
)
from backend.services.ai_reviewer.unified_client import FallbackConfig, UnifiedAIClient
from backend.services.ai_usage_service import (
    ProviderUsageMeter,
    begin_ai_call,
    finish_ai_call,
    finish_billing_operation,
    metered_stream,
)
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_service import BillingService
from tests.test_billing_usage_attribution import (
    complete_usage,
    fund_and_price,
)
from tests.test_billing_usage_attribution import (
    sql_runtime as runtime_fixture,
)
from tests.test_unified_client_fallback import _candidate

sql_runtime = runtime_fixture


@pytest.mark.asyncio
async def test_selected_account_follows_each_meter_boundary_without_global_state(
    monkeypatch,
):
    started = {}
    finished = {}

    async def begin(**kwargs):
        started[kwargs["call_id"]] = kwargs

    async def finish(**kwargs):
        finished[kwargs["call_id"]] = kwargs

    monkeypatch.setattr(ai_usage_service, "begin_ai_call", begin)
    monkeypatch.setattr(ai_usage_service, "finish_ai_call", finish)

    async def run(account_id):
        candidate = _candidate(
            ProtocolFamily.OPENAI_COMPATIBLE, "shared-model", account_id=account_id
        )
        async with ProviderUsageMeter.for_candidate(
            candidate, call_kind="chat", role="main", logical_call_id="shared"
        ):
            await asyncio.sleep(0)

    await asyncio.gather(run("account-a"), run("account-b"), run(None))
    assert len(started) == len(finished) == 3
    assert {item.get("account_id") for item in started.values()} == {
        "account-a",
        "account-b",
        None,
    }
    for call_id, item in started.items():
        assert finished[call_id].get("account_id") == item.get("account_id")


async def account_prices(factory, candidate, *, kinds=("chat",)):
    config = await fund_and_price(
        factory,
        kinds=kinds,
        provider=candidate.provider.id,
        model=candidate.model.model_id,
    )
    async with factory() as db:
        service = BillingService(db)
        for kind in kinds:
            await service.publish_price(
                candidate.provider.id,
                candidate.model.model_id,
                kind,
                config,
                actor_id=1,
                account_id="account-a",
            )
            await service.publish_price(
                candidate.provider.id,
                candidate.model.model_id,
                kind,
                {
                    **config,
                    "input_price": "20",
                    "output_price": "100",
                    "cached_input_price": "2",
                    "cache_creation_price": "25",
                },
                actor_id=1,
                account_id="account-b",
            )
        await db.commit()


@pytest.mark.asyncio
async def test_same_provider_model_fallback_accounts_use_their_own_frozen_prices(
    sql_runtime, monkeypatch
):
    factory, engine, policy = sql_runtime
    policy["billing_charge_failed_calls"] = True
    candidates = [
        _candidate(
            ProtocolFamily.OPENAI_COMPATIBLE,
            "shared-model",
            account_id=f"account-{name}",
            credential=f"test-key-{name}",
        )
        for name in ("a", "b")
    ]
    await account_prices(factory, candidates[0])

    class ProviderBoundary:
        async def chat(self, client, endpoint, credential, request, **kwargs):
            if credential == "test-key-a":
                raise AIError(
                    AIErrorCategory.MODEL_NOT_FOUND,
                    "isolated first-account failure with real usage",
                    usage=complete_usage(),
                )
            return UnifiedResponse(
                content="fallback success",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=complete_usage(),
            )

    monkeypatch.setattr(
        unified_client, "_get_adapter", lambda family: ProviderBoundary()
    )
    context = BillingContext(1, str(uuid4()), "pr_review", {"review_id": 9})
    with bind_billing_context(context):
        async with UnifiedAIClient(
            fallback_config=FallbackConfig(max_retries=0)
        ) as client:
            response = await client.call_with_retry(
                candidates,
                [UnifiedMessage(role="user", content="isolated input")],
                model="shared-model",
                role="main",
            )
        assert response.content == "fallback success"
        await finish_billing_operation("completed")
    with Session(engine) as db:
        records = db.scalars(select(AIUsageRecord).order_by(AIUsageRecord.id)).all()
        assert [record.account_id for record in records] == ["account-a", "account-b"]
        assert len({record.actual_call_id for record in records}) == 2
        assert len({record.logical_call_id for record in records}) == 1
        assert {record.operation_id for record in records} == {context.operation_id}
        assert [record.outcome for record in records] == ["failed", "completed"]
        attempts = db.scalars(select(BillingCallAttempt)).all()
        for attempt in attempts:
            assert db.get(BillingPriceProfile, attempt.price_profile_id).account_id == (
                attempt.account_id
            )
        charges = db.scalars(select(BillingUsageCharge)).all()
        assert sorted(Decimal(charge.credits) for charge in charges) == [
            Decimal("0.156"),
            Decimal("1.56"),
        ]
        assert db.get(BillingWallet, 1).balance_units == 98_284_000


@pytest.mark.asyncio
async def test_compression_and_partial_stream_keep_the_actual_account(
    sql_runtime, monkeypatch
):
    factory, engine, _ = sql_runtime
    candidate = _candidate(
        ProtocolFamily.OPENAI_COMPATIBLE, "shared-model", account_id="account-b"
    )
    await account_prices(
        factory, candidate, kinds=("context_compression", "chat_stream")
    )

    class SummaryBoundary:
        async def chat(self, *args, **kwargs):
            return UnifiedResponse(
                content="isolated summary",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=complete_usage(),
            )

    monkeypatch.setattr(
        unified_compressor, "get_adapter", lambda family: SummaryBoundary()
    )
    context = BillingContext(1, str(uuid4()), "agent", {"task_id": 5})
    with bind_billing_context(context):
        compressor = UnifiedContextCompressor(http_client=object())
        result = await compressor._summarize(
            candidate, [UnifiedMessage(role="user", content="historical input")]
        )
        assert result

        async def stream():
            yield UnifiedStreamEvent(type="usage", usage=complete_usage())
            raise TimeoutError("isolated stream timeout")

        with pytest.raises(TimeoutError):
            async for _ in metered_stream(
                stream(), candidate=candidate, role="agent", logical_call_id="stream"
            ):
                pass
        await finish_billing_operation("failed")
    with Session(engine) as db:
        records = db.scalars(select(AIUsageRecord).order_by(AIUsageRecord.id)).all()
        assert [record.call_kind for record in records] == [
            "context_compression",
            "chat_stream",
        ]
        assert {record.account_id for record in records} == {"account-b"}
        assert {record.operation_id for record in records} == {context.operation_id}
        assert records[1].usage_complete is False
        assert db.get(BillingOperation, context.operation_id).pending_reason


@pytest.mark.asyncio
async def test_result_cannot_change_selected_account_or_its_frozen_price(sql_runtime):
    factory, engine, _ = sql_runtime
    candidate = _candidate(
        ProtocolFamily.OPENAI_COMPATIBLE, "shared-model", account_id="account-a"
    )
    await account_prices(factory, candidate)
    kwargs = {
        "call_id": str(uuid4()),
        "logical_call_id": "fixed-result",
        "provider_id": candidate.provider.id,
        "model_id": candidate.model.model_id,
        "call_kind": "chat",
        "protocol_family": "openai_compatible",
        "account_id": "account-a",
    }
    with bind_billing_context(BillingContext(1, str(uuid4()), "issue_analysis")):
        await begin_ai_call(**kwargs)
        assert await finish_ai_call(**kwargs, role="main", usage=complete_usage())
        with pytest.raises(RuntimeError, match="routing does not match"):
            await finish_ai_call(
                **{**kwargs, "account_id": "account-b"},
                role="main",
                usage=complete_usage(),
            )
        await finish_billing_operation("completed")
    with Session(engine) as db:
        records = db.scalars(select(AIUsageRecord)).all()
        assert len(records) == 1 and records[0].account_id == "account-a"
        assert db.get(BillingWallet, 1).balance_units == 99_844_000


@pytest.mark.asyncio
async def test_independent_embedding_does_not_inherit_a_previous_chat_account(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    candidate = _candidate(
        ProtocolFamily.OPENAI_COMPATIBLE, "shared-model", account_id="account-b"
    )
    await account_prices(factory, candidate)
    await fund_and_price(factory, kinds=("embedding",))
    with bind_billing_context(BillingContext(1, str(uuid4()), "repo_scan")):
        async with ProviderUsageMeter.for_candidate(
            candidate, call_kind="chat", role="main", logical_call_id="chat"
        ) as meter:
            meter.usage = complete_usage()
        async with ProviderUsageMeter(
            provider_id="provider",
            model_id="model",
            protocol_family="openai_compatible",
            call_kind="embedding",
            role="embedding",
            logical_call_id="embedding",
            input_only=True,
        ) as meter:
            meter.usage = usage_from_mapping({"input_tokens": 1000})
        await finish_billing_operation("completed")
    with Session(engine) as db:
        records = db.scalars(select(AIUsageRecord).order_by(AIUsageRecord.id)).all()
        assert [record.account_id for record in records] == ["account-b", None]
        attempts = db.scalars(select(BillingCallAttempt)).all()
        for attempt in attempts:
            assert db.get(BillingPriceProfile, attempt.price_profile_id).account_id == (
                attempt.account_id
            )


@pytest.mark.asyncio
async def test_platform_task_retains_selected_account_without_debiting_a_user(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    candidate = _candidate(
        ProtocolFamily.OPENAI_COMPATIBLE, "shared-model", account_id="account-a"
    )
    await account_prices(factory, candidate)
    context = BillingContext(
        None,
        str(uuid4()),
        "system_task",
        {"role": "summary"},
        platform_reason="shared_index_maintenance",
    )
    with bind_billing_context(context):
        async with ProviderUsageMeter.for_candidate(
            candidate, call_kind="chat", role="summary", logical_call_id="platform"
        ) as meter:
            meter.usage = complete_usage()
        await finish_billing_operation("completed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.account_id == "account-a"
        assert usage.user_id is None
        assert usage.platform_reason == "shared_index_maintenance"
        assert db.scalar(select(BillingCallAttempt)).account_id == "account-a"
        assert db.get(BillingWallet, 1).balance_units == 100_000_000


@pytest.mark.asyncio
async def test_concurrent_user_operations_keep_different_account_prices(sql_runtime):
    factory, engine, _ = sql_runtime
    candidates = [
        _candidate(
            ProtocolFamily.OPENAI_COMPATIBLE,
            "shared-model",
            account_id=f"account-{name}",
        )
        for name in ("a", "b")
    ]
    await account_prices(factory, candidates[0])
    contexts = [
        BillingContext(user_id, str(uuid4()), feature)
        for user_id, feature in ((1, "pr_review"), (2, "agent"))
    ]

    async def run(context, candidate):
        with bind_billing_context(context):
            async with ProviderUsageMeter.for_candidate(
                candidate, call_kind="chat", role="main", logical_call_id="shared"
            ) as meter:
                await asyncio.sleep(0)
                meter.usage = complete_usage()
            await finish_billing_operation("completed")

    await asyncio.gather(
        *(run(context, candidate) for context, candidate in zip(contexts, candidates))
    )
    with Session(engine) as db:
        usage = db.scalars(select(AIUsageRecord).order_by(AIUsageRecord.user_id)).all()
        assert [
            (record.user_id, record.account_id, record.operation_id) for record in usage
        ] == [
            (1, "account-a", contexts[0].operation_id),
            (2, "account-b", contexts[1].operation_id),
        ]
        assert db.get(BillingWallet, 1).balance_units == 99_844_000
        assert db.get(BillingWallet, 2).balance_units == 98_440_000
