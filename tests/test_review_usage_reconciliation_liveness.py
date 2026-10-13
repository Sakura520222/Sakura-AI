"""Reviewed Usage cannot replace a result while a worker can still report it."""

from datetime import timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingOperation,
    BillingReconciliationEvent,
    BillingTransaction,
    BillingUsageCharge,
    BillingWallet,
)
from backend.models.service_execution_models import ServiceExecutionOwnership
from backend.models.telegram_models import TelegramUser
from backend.services.ai_usage_service import (
    ProviderUsageMeter,
    finish_billing_operation,
)
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_reconciliation_service import resolve_billing_call
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_usage_attribution import complete_usage, fund_and_price
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


def evidence(call_id="actual-call"):
    return {
        "call_id": call_id,
        "event_key": "reviewed-provider-invoice",
        "actor_id": 1,
        "reason": "Verified exact Usage from the provider invoice",
        "usage": complete_usage(),
    }


def owner(*, state="running", expired=False):
    return ServiceExecutionOwnership(
        token="worker-token",
        operation_id="review-operation",
        feature="pr_review",
        owner_id="worker",
        user_id=1,
        state=state,
        expires_at=now_utc() + timedelta(hours=-1 if expired else 1),
    )


async def pending_call(factory):
    await fund_and_price(factory)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        service = BillingService(db)
        await service.register_operation(1, "review-operation", "pr_review")
        await service.start_call(
            "review-operation",
            "actual-call",
            "provider",
            "model",
            "chat",
            logical_call_id="logical-call",
            protocol_family="openai_compatible",
        )
        await db.commit()


def assert_no_reviewed_mutation(engine):
    with Session(engine) as db:
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 0
        assert db.scalar(select(func.count(BillingReconciliationEvent.id))) == 0
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 0
        attempt = db.get(BillingCallAttempt, "actual-call")
        assert attempt.usage_record_key is None
        assert db.get(BillingWallet, 1).balance_units == 100_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["started", "pending_usage", "pending_reconciliation"]
)
async def test_nonterminal_call_without_owner_cannot_receive_reviewed_usage(
    sql_runtime, state
):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    async with factory() as db:
        (await db.get(BillingCallAttempt, "actual-call")).state = state
        operation = await BillingService(db).settle_operation("review-operation")
        assert operation.outcome is None
        assert operation.status == "pending_reconciliation"
        await db.commit()
    async with factory() as db:
        with pytest.raises(BillingError, match="terminal"):
            await resolve_billing_call(db, **evidence())
        await db.commit()
    assert_no_reviewed_mutation(engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "running"])
async def test_terminal_call_with_live_worker_cannot_receive_reviewed_usage(
    sql_runtime, state
):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    async with factory() as db:
        await BillingService(db).finish_operation("review-operation", "completed")
        db.add(owner(state=state))
        await db.commit()
    async with factory() as db:
        with pytest.raises(BillingError, match="live worker"):
            await resolve_billing_call(db, **evidence())
        await db.commit()
    assert_no_reviewed_mutation(engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_late_actual_response_after_review_attempt_has_one_financial_effect(
    sql_runtime, terminal
):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        await db.commit()
    context = BillingContext(1, "review-operation", "pr_review")
    rejected = False
    with bind_billing_context(context):
        async with ProviderUsageMeter(
            provider_id="provider",
            model_id="model",
            call_kind="chat",
            protocol_family="openai_compatible",
            role="main",
            logical_call_id="logical-call",
        ) as meter:
            async with factory() as db:
                db.add(owner())
                if terminal:
                    await BillingService(db).finish_operation(
                        context.operation_id, "completed"
                    )
                await db.commit()
            async with factory() as db:
                try:
                    await resolve_billing_call(db, **evidence(meter.call_id))
                except BillingError:
                    rejected = True
                await db.commit()
            # The provider response arrives only after the reviewed attempt.
            meter.usage = complete_usage()
        async with factory() as db:
            (await db.get(ServiceExecutionOwnership, "worker-token")).state = "released"
            await db.commit()
        await finish_billing_operation("completed")
        await finish_billing_operation("completed")
    with Session(engine) as db:
        # (200 * 2 + 800 * 0.2 + 100 * 10) / 1e6 * 100 Credits.
        assert (
            db.scalar(select(func.count(BillingUsageCharge.id))),
            db.get(BillingWallet, 1).balance_units,
        ) == (1, 99_844_000)
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 1
        assert db.scalar(select(func.count(BillingReconciliationEvent.id))) == 0
        consumption = db.scalars(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        ).all()
        assert len(consumption) == 1
        assert consumption[0].delta_units == -156_000
    assert rejected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expired",
    [(None, False), ("queued", True), ("running", True), ("released", False)],
)
async def test_terminal_abandoned_call_allows_one_reviewed_settlement(
    sql_runtime, state, expired
):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    async with factory() as db:
        await BillingService(db).finish_operation("review-operation", "completed")
        if state is not None:
            db.add(owner(state=state, expired=expired))
        await db.commit()
    async with factory() as db:
        first = await resolve_billing_call(db, **evidence())
        event_id = first.id
        await db.commit()
    async with factory() as db:
        assert (await resolve_billing_call(db, **evidence())).id == event_id
        service = BillingService(db)
        await service.settle_operation("review-operation")
        await service.settle_operation("review-operation")
        assert (await service.reconcile_wallet(1))["consistent"]
        await db.commit()
    with Session(engine) as db:
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 1
        assert db.scalar(select(func.count(BillingReconciliationEvent.id))) == 1
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 1
        assert (
            db.scalar(
                select(func.count(BillingTransaction.id)).where(
                    BillingTransaction.kind == "consumption"
                )
            )
            == 1
        )
        assert db.get(BillingWallet, 1).balance_units == 99_844_000


@pytest.mark.asyncio
async def test_recovered_abandoned_attempt_needs_real_usage_evidence(sql_runtime):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    async with factory() as db:
        operation = await db.get(BillingOperation, "review-operation")
        operation.expires_at = now_utc() - timedelta(hours=1)
        db.add(owner(expired=True))
        await db.commit()
        await BillingService(db).recover_operations(dry_run=False)
        await db.commit()
    assert_no_reviewed_mutation(engine)
    with Session(engine) as db:
        operation = db.get(BillingOperation, "review-operation")
        assert operation.outcome == "failed"
        assert operation.status == "pending_reconciliation"
        assert (
            db.get(BillingCallAttempt, "actual-call").state == "pending_reconciliation"
        )
    async with factory() as db:
        await resolve_billing_call(db, **evidence())
        await resolve_billing_call(db, **evidence())
        await db.commit()
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        assert usage.usage_reported and usage.input_tokens == 1000
        assert usage.output_tokens == 100
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 1
        assert db.get(BillingWallet, 1).balance_units == 100_000_000


@pytest.mark.asyncio
async def test_reconciliation_refreshes_terminal_operation_and_locks_owner_read(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    async with factory() as db:
        operation = await db.get(BillingOperation, "review-operation")
        assert operation.outcome is None
        await db.commit()
        async with factory() as worker_db:
            await BillingService(worker_db).finish_operation(
                "review-operation", "completed"
            )
            await worker_db.commit()
        statements = []

        @event.listens_for(db.session, "do_orm_execute")
        def record_statement(state):
            statements.append(str(state.statement.compile(dialect=mysql.dialect())))

        await resolve_billing_call(db, **evidence())
        await db.commit()
        assert operation.outcome == "completed"
        ownership_reads = [
            statement
            for statement in statements
            if "FROM service_execution_ownerships" in statement
        ]
        assert ownership_reads and all(
            "FOR UPDATE" in statement for statement in ownership_reads
        )
    with Session(engine) as db:
        assert db.get(BillingWallet, 1).balance_units == 99_844_000


@pytest.mark.asyncio
async def test_existing_review_event_replays_during_a_resumed_live_operation(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    async with factory() as db:
        service = BillingService(db)
        await service.finish_operation("review-operation", "completed")
        first = await resolve_billing_call(db, **evidence())
        event_id = first.id
        await service.resume_operation("review-operation")
        db.add(owner())
        await db.commit()
    async with factory() as db:
        assert (await resolve_billing_call(db, **evidence())).id == event_id
        await db.commit()
    with Session(engine) as db:
        assert db.get(BillingOperation, "review-operation").outcome is None
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 1
        assert db.scalar(select(func.count(BillingReconciliationEvent.id))) == 1
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 1
        assert db.get(BillingWallet, 1).balance_units == 99_844_000
