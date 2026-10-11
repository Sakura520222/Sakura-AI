"""Usage ownership and durable provider boundaries against actual persisted SQL."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from backend.core.ai_protocol.models import usage_from_mapping
from backend.models.agent_team_models import AgentTeamTask
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingOperation,
    BillingTransaction,
    BillingUsageCharge,
)
from backend.models.database import Base
from backend.models.legacy_entitlement_models import RateLimitAdmission
from backend.models.telegram_models import TelegramUser
from backend.services import ai_usage_service, billing_service
from backend.services.ai_usage_service import (
    ProviderUsageMeter,
    begin_ai_call,
    finish_ai_call,
    finish_billing_operation,
)
from backend.services.billing_context import (
    BillingContext,
    bind_billing_context,
    context_for_payload,
    get_billing_context,
)
from backend.services.billing_service import BillingService
from tests.test_billing_wallet import SQLSession


class DurableSQLSession(SQLSession):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc is not None:
            self.session.rollback()
        self.session.close()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, value, **kwargs):
        self.session.refresh(value, **kwargs)


@pytest.fixture
def sql_runtime(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'billing.sqlite'}")

    @event.listens_for(engine, "connect")
    def sqlite_transactions(connection, record):
        # Disable sqlite3 legacy transaction control so savepoint release cannot
        # commit a unit of work whose first statement was a SELECT.
        connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                TelegramUser(id=1, telegram_id=101, github_username="owner1"),
                TelegramUser(id=2, telegram_id=102, github_username="owner2"),
            ]
        )
        session.commit()

    def factory():
        return DurableSQLSession(Session(engine, expire_on_commit=False))

    from backend.models import database

    monkeypatch.setattr(database, "async_session", factory)
    policy = {
        "billing_enabled": True,
        "billing_charge_failed_operations": False,
        "billing_charge_failed_calls": False,
        "billing_initial_reserve_credits": "1",
        "billing_reservation_ttl_seconds": 3600,
    }

    async def setting(name, **kwargs):
        return policy[name]

    monkeypatch.setattr(billing_service, "get_dynamic_config", setting)
    yield factory, engine, policy
    engine.dispose()


async def fund_and_price(
    factory, *, kinds=("chat",), provider="provider", model="model"
):
    # Explicit fixture prices are isolated examples, never production settings.
    config = {
        "currency": "USD",
        "settlement_currency": "USD",
        "fx_rate": "1",
        "markup": "1",
        "credits_per_currency_unit": "100",
        "unit": "tokens",
        "input_price": "2",
        "output_price": "10",
        "cached_input_price": "0.2",
        "cache_creation_price": "2.5",
    }
    async with factory() as db:
        service = BillingService(db)
        for user_id in (1, 2):
            await service.grant(
                user_id, "100", f"test:purchase:{user_id}", kind="purchase"
            )
        for kind in kinds:
            await service.publish_price(provider, model, kind, config, actor_id=1)
        await db.commit()
    return config


def complete_usage(**overrides):
    values = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_read_tokens": 800,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 50,
    }
    values.update(overrides)
    return usage_from_mapping(values)


@pytest.mark.asyncio
async def test_concurrent_users_keep_feature_operation_and_auxiliary_ownership(
    sql_runtime,
):
    from backend.core.ai_protocol.models import UnifiedUsage

    factory, engine, _ = sql_runtime
    await fund_and_price(
        factory, kinds=("chat", "context_compression", "embedding", "rerank")
    )
    contexts = [
        BillingContext(i, str(uuid4()), feature, {"repo_full_name": f"owner{i}/repo"})
        for i, feature in ((1, "pr_review"), (2, "agent"))
    ]

    async def run(context):
        with bind_billing_context(context):
            await asyncio.sleep(0)
            for kind in ("chat", "context_compression", "embedding", "rerank"):
                async with ProviderUsageMeter(
                    provider_id="provider",
                    model_id="model",
                    protocol_family="openai_compatible",
                    call_kind=kind,
                    role=kind,
                    logical_call_id="shared-logical-label",
                    input_only=kind in {"embedding", "rerank"},
                ) as meter:
                    await asyncio.sleep(0)
                    meter.usage = (
                        UnifiedUsage(
                            input_tokens=1000,
                            reported_fields=frozenset({"input_tokens"}),
                        )
                        if kind in {"embedding", "rerank"}
                        else complete_usage()
                    )
            await finish_billing_operation("completed")

    await asyncio.gather(*(run(context) for context in contexts))
    assert get_billing_context() is None
    with Session(engine) as db:
        rows = db.scalars(select(AIUsageRecord)).all()
        assert len(rows) == 8
        assert len({row.actual_call_id for row in rows}) == 8
        for context in contexts:
            own = [row for row in rows if row.user_id == context.user_id]
            assert len(own) == 4
            assert {row.feature for row in own} == {context.feature}
            assert {row.operation_id for row in own} == {context.operation_id}
        assert (
            db.scalar(
                select(func.count(BillingTransaction.id)).where(
                    BillingTransaction.kind == "consumption"
                )
            )
            == 2
        )


@pytest.mark.asyncio
async def test_duplicate_result_and_price_changes_preserve_frozen_charge(sql_runtime):
    factory, engine, _ = sql_runtime
    config = await fund_and_price(factory)
    context = BillingContext(
        1, str(uuid4()), "issue_analysis", {"issue_analysis_id": 9}
    )
    kwargs = {
        "call_id": str(uuid4()),
        "logical_call_id": "logical",
        "provider_id": "provider",
        "model_id": "model",
        "protocol_family": "openai_compatible",
        "call_kind": "chat",
    }
    with bind_billing_context(context):
        await begin_ai_call(**kwargs)
        async with factory() as db:
            await BillingService(db).publish_price(
                "provider",
                "model",
                "chat",
                {**config, "input_price": "1000"},
                actor_id=1,
            )
            await db.commit()
        assert await finish_ai_call(**kwargs, role="main", usage=complete_usage())
        assert not await finish_ai_call(**kwargs, role="main", usage=complete_usage())
        await finish_billing_operation("completed")
        await finish_billing_operation("completed")
    with Session(engine) as db:
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 1
        transactions = db.scalars(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        ).all()
        assert len(transactions) == 1
        assert transactions[0].delta_units == -156000


@pytest.mark.asyncio
async def test_unknown_external_result_is_durable_and_does_not_fake_zero(sql_runtime):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context = BillingContext(1, str(uuid4()), "agent")
    with bind_billing_context(context):
        with pytest.raises(TimeoutError):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="agent",
                logical_call_id="logical",
            ):
                raise TimeoutError("external response unknown")
        await finish_billing_operation("failed")
    with Session(engine) as db:
        attempt = db.scalar(select(BillingCallAttempt))
        row = db.scalar(select(AIUsageRecord))
        operation = db.get(BillingOperation, context.operation_id)
        assert attempt.state == "pending_reconciliation"
        assert row.input_tokens is None and row.output_tokens is None
        assert not row.usage_reported
        assert operation.pending_reason
        assert operation.reserve_units == 0
        assert (
            db.scalar(
                select(func.count(BillingTransaction.id)).where(
                    BillingTransaction.kind == "consumption"
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_failure_with_known_usage_retains_cost_without_default_user_debit(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context = BillingContext(1, str(uuid4()), "repo_scan", {"scan_id": 3})
    with bind_billing_context(context):
        with pytest.raises(RuntimeError):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="logical",
            ) as meter:
                meter.usage = complete_usage()
                raise RuntimeError("business validation failed after response")
        await finish_billing_operation("failed")
    with Session(engine) as db:
        operation = db.get(BillingOperation, context.operation_id)
        assert operation.known_credits == "0"
        assert operation.settled_units == 0
        quote = db.scalar(select(BillingUsageCharge))
        assert quote.provider_cost == "0.00156"
        assert quote.credits == "0.15600"
        assert db.scalar(select(AIUsageRecord)).outcome == "failed"


@pytest.mark.asyncio
async def test_start_write_failure_prevents_external_send(sql_runtime, monkeypatch):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    from backend.models import database

    class CommitFailure(DurableSQLSession):
        async def commit(self):
            raise RuntimeError("durability unavailable")

    monkeypatch.setattr(
        database, "async_session", lambda: CommitFailure(Session(engine))
    )
    reached_provider = False
    with bind_billing_context(BillingContext(1, str(uuid4()), "pr_review")):
        with pytest.raises(RuntimeError, match="durability unavailable") as failure:
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="logical",
            ):
                reached_provider = True
        assert ai_usage_service.is_billing_failure(failure.value)
    assert not reached_provider
    with Session(engine) as db:
        assert db.scalar(select(func.count(BillingCallAttempt.call_id))) == 0


def test_payload_identity_recovery_manual_reruns_and_zero_semantics():
    payload = {"user_id": 1, "repo_full_name": "owner/repo", "pr_number": 7}
    original = context_for_payload(payload, "pr_review")
    assert context_for_payload(dict(payload), "pr_review") == original
    assert (
        context_for_payload({"user_id": 1, "pr_number": 7}, "pr_review").operation_id
        != original.operation_id
    )
    auto = {"user_id": 1, "delivery_id": "verified-delivery"}
    assert (
        context_for_payload(dict(auto), "pr_review").operation_id
        == context_for_payload(dict(auto), "pr_review").operation_id
    )
    anthropic = usage_from_mapping(
        {
            "input_tokens": 5,
            "output_tokens": 0,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 7,
        }
    )
    assert anthropic.cache_read_tokens == 20
    assert anthropic.cache_creation_tokens == 7
    assert "output_tokens" in anthropic.reported_fields
    assert "reasoning_tokens" not in anthropic.reported_fields
    assert (
        ai_usage_service.usage_semantics("anthropic_native")[
            "input_includes_cache_read"
        ]
        is False
    )
    assert (
        ai_usage_service.usage_semantics("gemini_native")["output_includes_reasoning"]
        is False
    )
    with pytest.raises(TypeError):
        original.source["repo_full_name"] = "malicious/repo"


@pytest.mark.asyncio
async def test_actual_fallback_requests_keep_usage_and_actual_model(
    sql_runtime, monkeypatch
):
    from backend.core.ai_protocol.errors import AIError
    from backend.core.ai_protocol.models import (
        AIErrorCategory,
        ProtocolFamily,
        StopReason,
        UnifiedResponse,
    )
    from backend.services.ai_reviewer import unified_client
    from backend.services.ai_reviewer.unified_client import (
        FallbackConfig,
        UnifiedAIClient,
    )
    from tests.test_unified_client_fallback import _candidate

    factory, engine, _ = sql_runtime
    primary = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary")
    fallback = _candidate(ProtocolFamily.ANTHROPIC_NATIVE, "fallback")
    await fund_and_price(factory, provider=primary.provider.id, model="primary")
    await fund_and_price(factory, provider=fallback.provider.id, model="fallback")

    class Adapter:
        async def chat(self, client, endpoint, credential, request, **kwargs):
            if request.model == "primary":
                raise AIError(
                    AIErrorCategory.SERVER_ERROR,
                    "provider error after usage",
                    usage=complete_usage(),
                )
            return UnifiedResponse(
                content="ok",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=complete_usage(input_tokens=200),
            )

    monkeypatch.setattr(unified_client, "_get_adapter", lambda family: Adapter())
    context = BillingContext(1, str(uuid4()), "pr_review", {"review_id": 7})
    with bind_billing_context(context):
        async with UnifiedAIClient(
            fallback_config=FallbackConfig(max_retries=0)
        ) as client:
            response = await client.call_with_retry(
                [primary, fallback],
                [{"role": "user", "content": "review"}],
                model="primary",
            )
        assert response.content == "ok"
        await finish_billing_operation("completed")
    with Session(engine) as db:
        rows = db.scalars(select(AIUsageRecord).order_by(AIUsageRecord.id)).all()
        assert len(rows) == 2
        assert {row.operation_id for row in rows} == {context.operation_id}
        assert len({row.actual_call_id for row in rows}) == 2
        assert len({row.logical_call_id for row in rows}) == 1
        assert [(row.model_id, row.outcome) for row in rows] == [
            ("primary", "failed"),
            ("fallback", "completed"),
        ]
        assert all(row.usage_reported and row.usage_complete for row in rows)
        assert rows[1].usage_semantics["input_includes_cache_read"] is False


@pytest.mark.asyncio
async def test_partial_stream_usage_merges_cumulative_fields_and_needs_reconciliation(
    sql_runtime,
):
    from backend.core.ai_protocol.models import ProtocolFamily, UnifiedStreamEvent
    from backend.services.ai_usage_service import metered_stream
    from tests.test_unified_client_fallback import _candidate

    factory, engine, _ = sql_runtime
    candidate = _candidate(ProtocolFamily.ANTHROPIC_NATIVE, "partial")
    await fund_and_price(
        factory, kinds=("chat_stream",), provider=candidate.provider.id, model="partial"
    )

    async def events():
        yield UnifiedStreamEvent(
            type="usage",
            usage=usage_from_mapping(
                {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 0,
                }
            ),
        )
        yield UnifiedStreamEvent(
            type="usage", usage=usage_from_mapping({"output_tokens": 3})
        )
        yield UnifiedStreamEvent(
            type="usage", usage=usage_from_mapping({"output_tokens": 5})
        )
        raise TimeoutError("no final upstream response")

    context = BillingContext(1, str(uuid4()), "agent")
    with bind_billing_context(context):
        with pytest.raises(TimeoutError):
            async for _ in metered_stream(
                events(), candidate=candidate, role="agent", logical_call_id="logical"
            ):
                pass
        await finish_billing_operation("failed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.input_tokens == 100
        assert usage.cached_input_tokens == 20
        assert usage.output_tokens == 5
        assert usage.usage_reported and usage.usage_complete is False
        assert db.scalar(select(BillingCallAttempt)).state == "pending_reconciliation"
        assert db.get(BillingOperation, context.operation_id).pending_reason


@pytest.mark.asyncio
async def test_result_write_failure_leaves_started_attempt_for_recovery(
    sql_runtime, monkeypatch
):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    from backend.models import database

    context = BillingContext(1, str(uuid4()), "issue_analysis")
    with bind_billing_context(context):
        meter = ProviderUsageMeter(
            provider_id="provider",
            model_id="model",
            protocol_family="openai_compatible",
            call_kind="chat",
            role="main",
            logical_call_id="logical",
        )
        await meter.__aenter__()
        meter.usage = complete_usage()

        class ResultCommitFailure(DurableSQLSession):
            async def commit(self):
                raise RuntimeError("result commit interrupted")

        monkeypatch.setattr(
            database, "async_session", lambda: ResultCommitFailure(Session(engine))
        )
        with pytest.raises(RuntimeError, match="result commit interrupted"):
            await meter.__aexit__(None, None, None)
    with Session(engine) as db:
        assert db.get(BillingCallAttempt, meter.call_id).state == "started"
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 0
        assert (
            db.scalar(
                select(func.count(BillingTransaction.id)).where(
                    BillingTransaction.kind == "consumption"
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_user_usage_aggregation_preserves_unknown_and_rejects_changed_replay(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context = BillingContext(1, str(uuid4()), "agent")
    kwargs = {
        "call_id": str(uuid4()),
        "logical_call_id": "logical",
        "provider_id": "provider",
        "model_id": "model",
        "protocol_family": "openai_compatible",
        "call_kind": "chat",
    }
    with bind_billing_context(context):
        await begin_ai_call(**kwargs)
        await finish_ai_call(**kwargs, role="main", usage=None, outcome="failed")
        with pytest.raises(RuntimeError, match="immutable financial intent"):
            await finish_ai_call(
                **kwargs, role="main", usage=complete_usage(), outcome="failed"
            )
        await finish_billing_operation("failed")
    async with factory() as db:
        rows = await ai_usage_service.fetch_usage_summary(
            db, user_id=1, feature="agent"
        )
        assert len(rows) == 1
        assert rows[0]["operation_id"] == context.operation_id
        assert rows[0]["input_tokens"] is None
        assert rows[0]["output_tokens"] is None
        assert rows[0]["unknown_calls"] == 1
        assert await ai_usage_service.fetch_usage_summary(db, user_id=2) == []
    with Session(engine) as db:
        assert db.scalar(select(AIUsageRecord)).usage_reported is False


def test_non_token_units_are_reported_and_unknown_requests_are_not_fabricated():
    native = {"meta": {"billed_units": {"search_units": 2, "documents": 7}}}
    assert ai_usage_service.extract_reported_billing_units(
        native, actual_response=True
    ) == {
        "requests": 1,
        "search_units": 2,
        "documents": 7,
    }
    assert (
        ai_usage_service.extract_reported_billing_units(None, actual_response=False)
        == {}
    )


@pytest.mark.asyncio
async def test_optional_helper_cannot_hide_a_financial_write_failure(
    sql_runtime, monkeypatch
):
    from backend.services.billing_context import (
        billable_payload,
        get_pending_billing_failure,
    )

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    reached_provider = []

    async def fail_result(**kwargs):
        raise RuntimeError("usage write failed after external request")

    monkeypatch.setattr(ai_usage_service, "finish_ai_call", fail_result)

    class Worker:
        @billable_payload("pr_review")
        async def execute(self, payload):
            for _ in range(2):
                try:
                    async with ProviderUsageMeter(
                        provider_id="provider",
                        model_id="model",
                        protocol_family="openai_compatible",
                        call_kind="chat",
                        role="main",
                        logical_call_id="logical",
                    ) as meter:
                        reached_provider.append(meter.call_id)
                        meter.usage = complete_usage()
                except RuntimeError:
                    # Existing optional RAG helpers may degrade on ordinary
                    # provider errors. A financial error must still propagate.
                    pass
            return "fake successful business result"

    payload = {"user_id": 1, "repo_full_name": "owner/repo", "pr_number": 7}
    with pytest.raises(RuntimeError, match="usage write failed"):
        await Worker().execute(payload)
    assert len(reached_provider) == 1
    assert get_billing_context() is None
    assert get_pending_billing_failure() is None
    with Session(engine) as db:
        operation = db.get(BillingOperation, payload["billing_context"]["operation_id"])
        assert operation.outcome == "failed"
        assert operation.reserve_units == 0
        assert db.scalar(select(BillingCallAttempt)).state == "started"
        assert (
            db.scalar(
                select(func.count(BillingTransaction.id)).where(
                    BillingTransaction.kind == "consumption"
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_record_worker_resume_and_manual_new_run_persist_distinct_identity(
    sql_runtime,
):
    from backend.services.billing_context import billable_record

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    async with factory() as db:
        db.add(
            AgentTeamTask(
                id=7,
                source_type="manual_issue",
                repo_full_name="owner1/repo",
                repo_owner="owner1",
                repo_name="repo",
                title="task",
                summary="goal",
                status="queued",
                billing_user_id=1,
                started_by="owner1",
            )
        )
        await db.commit()

    class Worker:
        async def run(self, task_id):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="agent",
                logical_call_id=str(uuid4()),
            ) as meter:
                meter.usage = complete_usage()
            async with factory() as db:
                task = await db.get(AgentTeamTask, task_id)
                task.status = "completed"
                await db.commit()
            return task_id

        @billable_record("agent")
        async def process_task(self, task_id):
            return await self.run(task_id)

        @billable_record("agent")
        async def process_external_review_iteration(self, task_id):
            return await self.run(task_id)

        @billable_record("agent")
        async def process_human_followup_iteration(self, task_id):
            return await self.run(task_id)

    worker = Worker()
    await worker.process_task(7)
    await worker.process_external_review_iteration(7)
    with Session(engine) as db:
        rows = db.scalars(select(AIUsageRecord)).all()
        assert len(rows) == 2 and len({row.operation_id for row in rows}) == 1
        original_id = rows[0].operation_id
        assert db.get(AgentTeamTask, 7).billing_operation_id == original_id
        assert db.get(BillingOperation, original_id).source["agent_task_id"] == 7
    await worker.process_human_followup_iteration(7)
    assert get_billing_context() is None
    with Session(engine) as db:
        latest_id = db.get(AgentTeamTask, 7).billing_operation_id
        assert latest_id != original_id
        assert db.get(BillingOperation, original_id).settled_units == 312000
        assert db.get(BillingOperation, latest_id).settled_units == 156000


@pytest.mark.asyncio
async def test_successful_non_token_request_has_exact_meter_without_fake_tokens(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    config = await fund_and_price(factory)
    async with factory() as db:
        await BillingService(db).publish_price(
            "provider",
            "model",
            "chat",
            {
                **config,
                "unit": "requests",
                "unit_price": "0.5",
                "meter": "requests",
            },
            actor_id=1,
        )
        await db.commit()
    context = BillingContext(1, str(uuid4()), "agent")
    with bind_billing_context(context):
        async with ProviderUsageMeter(
            provider_id="provider",
            model_id="model",
            protocol_family="openai_compatible",
            call_kind="chat",
            role="agent",
            logical_call_id="logical",
        ):
            # A real successful response has no token counters on a request tariff.
            pass
        await finish_billing_operation("completed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.input_tokens is None and usage.output_tokens is None
        assert usage.usage_reported is False
        assert usage.billing_units["requests"] == 1
        assert (
            db.get(BillingOperation, context.operation_id).settled_units == 50_000_000
        )


def test_deepseek_cache_miss_remains_regular_input_not_cache_creation():
    from decimal import Decimal
    from types import SimpleNamespace

    from backend.services.billing_pricing import calculate_price

    raw = {
        "prompt_tokens": 100,
        "completion_tokens": 0,
        "prompt_cache_hit_tokens": 40,
        "prompt_cache_miss_tokens": 60,
    }
    direct = ai_usage_service.extract_provider_usage(raw)
    normalized = usage_from_mapping(raw)
    counters = ai_usage_service.extract_provider_usage(normalized)
    assert direct == counters
    assert counters.input_tokens == 100
    assert counters.cached_input_tokens == 40
    assert counters.cache_creation_tokens is None
    assert normalized.details["prompt_cache_miss_tokens"] == 60
    assert ai_usage_service.safe_usage_snapshot(raw)["prompt_cache_miss_tokens"] == 60
    sample = SimpleNamespace(
        input_tokens=100,
        output_tokens=0,
        cached_input_tokens=40,
        cache_creation_tokens=None,
        reasoning_tokens=None,
        usage_reported=True,
        call_kind="chat",
        usage_semantics=ai_usage_service.usage_semantics("openai_compatible"),
        billing_units={},
    )
    quote = calculate_price(
        sample,
        {
            "currency": "USD",
            "settlement_currency": "USD",
            "fx_rate": "1",
            "markup": "1",
            "credits_per_currency_unit": "100",
            "unit": "tokens",
            "input_price": "2",
            "output_price": "10",
            "cached_input_price": "0.2",
        },
    )
    assert quote.provider_cost == Decimal("0.000128")


@pytest.mark.asyncio
async def test_gathered_child_calls_settle_on_parent_worker_scope(sql_runtime):
    from backend.services.billing_context import billable_payload

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)

    async def child(role):
        async with ProviderUsageMeter(
            provider_id="provider",
            model_id="model",
            protocol_family="openai_compatible",
            call_kind="chat",
            role=role,
            logical_call_id=role,
        ) as meter:
            await asyncio.sleep(0)
            meter.usage = complete_usage()

    class Worker:
        @billable_payload("pr_review")
        async def execute(self, payload):
            await asyncio.gather(child("main"), child("labels"))
            return "completed"

    payload = {"user_id": 1, "repo_full_name": "owner/repo", "pr_number": 7}
    assert await Worker().execute(payload) == "completed"
    with Session(engine) as db:
        operation = db.get(BillingOperation, payload["billing_context"]["operation_id"])
        assert operation.outcome == "completed"
        assert operation.settled_units == 312000
        assert operation.reserve_units == 0
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 2


@pytest.mark.asyncio
async def test_optional_gathered_child_cannot_hide_financial_error(
    sql_runtime, monkeypatch
):
    from backend.services.billing_context import billable_payload

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)

    async def fail_result(**kwargs):
        raise RuntimeError("optional child usage commit failed")

    monkeypatch.setattr(ai_usage_service, "finish_ai_call", fail_result)

    async def child():
        async with ProviderUsageMeter(
            provider_id="provider",
            model_id="model",
            protocol_family="openai_compatible",
            call_kind="chat",
            role="labels",
            logical_call_id="logical",
        ) as meter:
            meter.usage = complete_usage()

    class Worker:
        @billable_payload("pr_review")
        async def execute(self, payload):
            await asyncio.gather(child(), return_exceptions=True)
            return "fake successful parent result"

    payload = {"user_id": 1, "repo_full_name": "owner/repo", "pr_number": 7}
    with pytest.raises(RuntimeError, match="optional child usage commit failed"):
        await Worker().execute(payload)
    with Session(engine) as db:
        operation = db.get(BillingOperation, payload["billing_context"]["operation_id"])
        assert operation.outcome == "failed"
        assert operation.settled_units == 0
        assert operation.reserve_units == 0


@pytest.mark.asyncio
async def test_verified_agent_delivery_replay_survives_completed_task_and_unique_race(
    sql_runtime, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.services.agent_team.billing_admission import (
        delivery_operation_id,
        persist_delivery_task,
    )
    from backend.services.agent_team.candidate_service import AgentTeamCandidateService

    factory, engine, _ = sql_runtime
    service = AgentTeamCandidateService()
    draft = AsyncMock(
        return_value={
            "source_type": "manual_issue",
            "source_issue_number": 42,
            "repo_full_name": "owner1/repo",
            "repo_owner": "owner1",
            "repo_name": "repo",
            "title": "task",
            "summary": "goal",
            "status": "queued",
        }
    )
    monkeypatch.setattr(service, "build_manual_issue_task_draft", draft)
    async with factory() as db:
        task = await service.create_task_from_manual_issue(
            db, "owner1/repo", 42, "owner1", webhook_delivery_id="verified-delivery-1"
        )
        task.status = "completed"
        await db.commit()
        first_id = task.id
        first_operation = task.billing_operation_id
        replayed = await service.create_task_from_manual_issue(
            db, "owner1/repo", 42, "owner1", webhook_delivery_id="verified-delivery-1"
        )
        assert replayed.id == first_id
        assert replayed._billing_delivery_replayed
        assert (
            replayed.billing_operation_id
            == first_operation
            == delivery_operation_id("verified-delivery-1")
        )
        assert draft.await_count == 1
        fresh = await service.create_task_from_manual_issue(
            db, "owner1/repo", 42, "owner1", webhook_delivery_id="verified-delivery-2"
        )
        assert fresh.id != first_id and fresh.billing_operation_id != first_operation
        assert draft.await_count == 2
    # Reproduce the concurrent loser that observed no prior row, then attempts
    # to insert after the winner committed. SQL uniqueness resolves the replay.
    async with factory() as db:
        losing = AgentTeamTask(
            source_type="manual_issue",
            source_issue_number=42,
            repo_full_name="owner1/repo",
            repo_owner="owner1",
            repo_name="repo",
            title="race",
            summary="goal",
            webhook_delivery_id="verified-delivery-1",
        )
        replayed = await persist_delivery_task(db, losing)
        assert replayed.id == first_id and replayed._billing_delivery_replayed
    with Session(engine) as db:
        assert db.scalar(select(func.count(AgentTeamTask.id))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pr", [False, True])
async def test_agent_webhook_replay_returns_persisted_task_before_scheduling_or_github_work(
    sql_runtime, monkeypatch, is_pr
):
    import json
    from unittest.mock import AsyncMock

    from backend.api import webhook

    factory, engine, _ = sql_runtime
    async with factory() as db:
        db.add(
            AgentTeamTask(
                id=99,
                source_type="pr_review" if is_pr else "manual_issue",
                source_issue_number=42,
                repo_full_name="owner1/repo",
                repo_owner="owner1",
                repo_name="repo",
                title="already finished",
                summary="goal",
                status="completed",
                webhook_delivery_id="verified-delivery",
            )
        )
        await db.commit()
    payload = {
        "repository": {
            "owner": {"login": "owner1"},
            "name": "repo",
            "full_name": "owner1/repo",
        },
        "issue": {"number": 42},
        "comment": {"body": "/agent", "user": {"login": "collaborator"}},
        "_sakura_delivery_id": "verified-delivery",
    }
    monkeypatch.setattr(webhook, "get_async_session", factory)
    monkeypatch.setattr(
        webhook, "_check_agent_team_enabled", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        webhook, "_check_agent_permission", AsyncMock(return_value=None)
    )

    class NoExternalGitHubWork:
        def get_repo_client(self, *args):
            pytest.fail("Verified replay must return before fetching source/head")

    monkeypatch.setattr(webhook, "GitHubAppClient", NoExternalGitHubWork)
    monkeypatch.setattr(
        webhook,
        "create_registered_background_task",
        lambda *args: pytest.fail("Verified replay must not schedule a new worker"),
    )
    response = await (
        webhook.handle_pr_agent_command if is_pr else webhook.handle_agent_command
    )(payload)
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "accepted",
        "task_id": 99,
        "duplicate": True,
    }
    with Session(engine) as db:
        assert db.scalar(select(func.count(AgentTeamTask.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("administrator", [False, True])
async def test_human_followup_snapshots_authenticated_payer_and_rate_admission(
    sql_runtime, monkeypatch, administrator
):
    import json
    from unittest.mock import AsyncMock

    from backend.webui.routes import agent_team

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    old_operation_id = str(uuid4())
    async with factory() as db:
        db.add(
            AgentTeamTask(
                id=7,
                source_type="manual_issue",
                repo_full_name="owner1/repo",
                repo_owner="owner1",
                repo_name="repo",
                title="task",
                summary="goal",
                status="waiting_human",
                workspace_path="/tmp/existing-agent-workspace",
                branch_name="feature/existing",
                pr_number=42,
                billing_operation_id=old_operation_id,
                billing_user_id=1,
                started_by="owner1",
            )
        )
        await db.commit()
    scheduled = []

    def schedule(coroutine, source):
        coroutine.close()
        scheduled.append(source)

    monkeypatch.setattr(agent_team, "create_registered_background_task", schedule)
    monkeypatch.setattr("backend.webui.sse.publish_event", AsyncMock())
    auth = {
        "user_id": 2 if administrator else 1,
        "sub": "admin" if administrator else "owner1",
        "role": "admin" if administrator else "user",
    }
    async with factory() as db:
        response = await agent_team.submit_user_prompt(
            7, content="Continue with this instruction", user=auth, db=db
        )
    assert json.loads(response.body)["success"]
    assert scheduled == ["agent_team_human_followup"]
    with Session(engine) as db:
        task = db.get(AgentTeamTask, 7)
        assert task.billing_operation_id != old_operation_id
        assert task.started_by == "owner1"
        assert task.current_phase == "human_followup"
        if administrator:
            assert task.billing_user_id is None
            assert task.billing_platform_reason == "administrator_agent_human_followup"
            assert db.scalar(select(func.count(RateLimitAdmission.id))) == 0
        else:
            assert task.billing_user_id == 1
            assert task.billing_platform_reason is None
            admission = db.scalar(select(RateLimitAdmission))
            assert admission.event_key == f"operation:{task.billing_operation_id}:agent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "family_name",
    ["OPENAI_COMPATIBLE", "OPENAI_RESPONSES", "ANTHROPIC_NATIVE", "GEMINI_NATIVE"],
)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("has_usage", [False, True])
async def test_http_failure_preserves_actual_protocol_usage_without_faking_missing(
    sql_runtime, family_name, streaming, has_usage
):
    from decimal import Decimal

    import httpx

    from backend.core.ai_protocol.errors import AIError
    from backend.core.ai_protocol.models import (
        ProtocolFamily,
        UnifiedMessage,
        UnifiedRequest,
    )
    from backend.core.ai_protocol.registry import get_adapter
    from backend.services.ai_usage_service import metered_stream
    from tests.test_unified_client_fallback import _candidate

    factory, engine, _ = sql_runtime
    family = getattr(ProtocolFamily, family_name)
    candidate = _candidate(family, "http-failure")
    kind = "chat_stream" if streaming else "chat"
    await fund_and_price(
        factory, kinds=(kind,), provider=candidate.provider.id, model="http-failure"
    )
    body = {
        "error": {
            "code": 500,
            "status": "INTERNAL",
            "type": "api_error",
            "message": "safe upstream failure",
        }
    }
    if has_usage:
        if family_name == "GEMINI_NATIVE":
            body["usageMetadata"] = {
                "promptTokenCount": 100,
                "candidatesTokenCount": 5,
                "cachedContentTokenCount": 0,
                "thoughtsTokenCount": 0,
            }
        elif family_name == "ANTHROPIC_NATIVE":
            body["usage"] = {
                "input_tokens": 100,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        else:
            body["usage"] = {
                "input_tokens": 100,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            }
    request = UnifiedRequest(
        model="http-failure",
        messages=[UnifiedMessage(role="user", content="test")],
        max_tokens=100,
    )
    context = BillingContext(1, str(uuid4()), "agent")
    adapter = get_adapter(family)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json=body))
    ) as http:
        with bind_billing_context(context):
            with pytest.raises(AIError) as failure:
                if streaming:
                    events = adapter.stream(
                        http, candidate.endpoint, "test-only-key", request
                    )
                    async for _ in metered_stream(
                        events,
                        candidate=candidate,
                        role="agent",
                        logical_call_id="logical",
                    ):
                        pass
                else:
                    async with ProviderUsageMeter.for_candidate(
                        candidate,
                        call_kind=kind,
                        role="agent",
                        logical_call_id="logical",
                    ):
                        await adapter.chat(
                            http, candidate.endpoint, "test-only-key", request
                        )
            assert failure.value.status_code == 500
            assert (failure.value.usage is not None) is has_usage
            await finish_billing_operation("failed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.outcome == "failed"
        assert usage.usage_reported is has_usage
        assert usage.input_tokens == (100 if has_usage else None)
        assert usage.output_tokens == (5 if has_usage else None)
        assert db.get(BillingOperation, context.operation_id).settled_units == 0
        quote = db.scalar(select(BillingUsageCharge))
        if has_usage:
            assert usage.usage_complete is True
            assert Decimal(quote.provider_cost) == Decimal("0.00025")
            assert db.scalar(select(BillingCallAttempt)).state == "usage_known"
        else:
            assert quote is None
            assert (
                db.scalar(select(BillingCallAttempt)).state == "pending_reconciliation"
            )


@pytest.mark.asyncio
async def test_non_sse_json_response_retains_usage_when_stream_protocol_fails(
    sql_runtime,
):
    from decimal import Decimal

    import httpx

    from backend.core.ai_protocol.errors import AIError
    from backend.core.ai_protocol.models import (
        ProtocolFamily,
        UnifiedMessage,
        UnifiedRequest,
    )
    from backend.core.ai_protocol.registry import get_adapter
    from backend.services.ai_usage_service import metered_stream
    from tests.test_unified_client_fallback import _candidate

    factory, engine, _ = sql_runtime
    candidate = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "non-sse")
    await fund_and_price(
        factory, kinds=("chat_stream",), provider=candidate.provider.id, model="non-sse"
    )
    payload = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
        }
    }
    request = UnifiedRequest(
        model="non-sse",
        messages=[UnifiedMessage(role="user", content="test")],
        max_tokens=100,
    )
    context = BillingContext(1, str(uuid4()), "agent")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        with bind_billing_context(context):
            with pytest.raises(AIError, match="非 SSE"):
                events = get_adapter(candidate.effective_protocol).stream(
                    http, candidate.endpoint, "test-only", request
                )
                async for _ in metered_stream(
                    events, candidate=candidate, role="agent", logical_call_id="logical"
                ):
                    pass
            await finish_billing_operation("failed")
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.input_tokens == 100 and usage.output_tokens == 5
        assert usage.outcome == "failed" and usage.usage_complete is True
        assert Decimal(db.scalar(select(BillingUsageCharge)).provider_cost) == Decimal(
            "0.00025"
        )
        assert db.get(BillingOperation, context.operation_id).settled_units == 0
