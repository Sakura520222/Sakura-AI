"""A late actual result is audit evidence after reviewed Usage has been selected."""

from decimal import Decimal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingOperation,
    BillingReconciliationEvent,
    BillingTransaction,
    BillingUsageCharge,
    BillingWallet,
)
from backend.services.ai_usage_service import (
    fetch_global_token_totals,
    fetch_usage_summary,
    finish_ai_call,
)
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_reconciliation_service import resolve_billing_call
from backend.services.billing_service import BillingService
from tests.test_billing_usage_attribution import complete_usage
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_review_usage_reconciliation_liveness import (
    evidence,
    owner,
    pending_call,
)

sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["event", "call", "selected", "original"])
async def test_usage_totals_do_not_trust_unvalidated_late_labels(sql_runtime, change):
    from copy import deepcopy

    factory, _engine, _ = sql_runtime
    await reviewed_call(factory)
    await finish_result(factory, usage=complete_usage(input_tokens=2000))
    async with factory() as db:
        valid = (
            await db.execute(
                select(AIUsageRecord).where(AIUsageRecord.role == "late_actual")
            )
        ).scalar_one()
        source = deepcopy(valid.source)
        reference = source["billing_late_result"]
        if change == "event":
            reference["reconciliation_event_id"] = True
        elif change == "call":
            reference["call_id"] = "another-call"
        elif change == "selected":
            reference["selected_usage_record_key"] = "missing-selection"
        else:
            reference["original_usage_record_key"] = "missing-original"
        db.add(
            AIUsageRecord(
                record_key="forged-label",
                operation_id=valid.operation_id,
                user_id=valid.user_id,
                feature=valid.feature,
                provider_id=valid.provider_id,
                account_id=valid.account_id,
                model_id=valid.model_id,
                call_kind=valid.call_kind,
                protocol_family=valid.protocol_family,
                role="late_actual",
                input_tokens=7,
                output_tokens=3,
                usage_reported=True,
                source=source,
            )
        )
        await db.commit()
        rows = await fetch_usage_summary(db, user_id=1, operation_id="review-operation")
        assert rows[0]["calls"] == 2
        assert rows[0]["input_tokens"] == 1007
        assert rows[0]["output_tokens"] == 103
        global_totals = await fetch_global_token_totals(db)
        assert global_totals.recorded_calls == 2
        assert (global_totals.input_tokens, global_totals.output_tokens) == (1007, 103)


async def finish_result(
    factory,
    *,
    call_id="actual-call",
    usage=None,
    usage_complete=True,
    role="main",
    source=None,
):
    with bind_billing_context(
        BillingContext(1, "review-operation", "pr_review", source or {})
    ):
        return await finish_ai_call(
            call_id=call_id,
            logical_call_id="logical-call",
            provider_id="provider",
            model_id="model",
            call_kind="chat",
            protocol_family="openai_compatible",
            role=role,
            usage=complete_usage() if usage is None else usage,
            usage_complete=usage_complete,
            session_factory=factory,
        )


async def reviewed_call(factory, *, state=None, expired=False):
    await pending_call(factory)
    async with factory() as db:
        await BillingService(db).finish_operation("review-operation", "completed")
        if state is not None:
            db.add(owner(state=state, expired=expired))
        await db.commit()
        reviewed = await resolve_billing_call(db, **evidence())
        selected = (await db.get(BillingCallAttempt, "actual-call")).usage_record_key
        event_id = reviewed.id
        await db.commit()
    return selected, event_id


def assert_one_reviewed_charge(engine, selected):
    with Session(engine) as db:
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 1
        assert db.get(BillingWallet, 1).balance_units == 99_844_000
        operation = db.get(BillingOperation, "review-operation")
        assert operation.status == "settled" and operation.pending_reason is None
        assert operation.settled_units == 156_000
        attempt = db.get(BillingCallAttempt, "actual-call")
        assert attempt.usage_record_key == selected and attempt.state == "usage_known"
        quote = db.scalar(select(BillingUsageCharge))
        assert quote.usage_record_key == selected
        assert Decimal(quote.credits) == Decimal("0.156")
        reviewed = db.scalar(
            select(AIUsageRecord).where(AIUsageRecord.record_key == selected)
        )
        assert reviewed.role == "reconciliation"
        assert reviewed.input_tokens == 1000 and reviewed.output_tokens == 100
        consumption = db.scalars(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        ).all()
        assert len(consumption) == 1 and consumption[0].delta_units == -156_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expired",
    [(None, False), ("queued", True), ("running", True), ("released", False)],
)
@pytest.mark.parametrize("late_input", [1000, 2000])
async def test_late_actual_result_preserves_reviewed_selection_and_one_charge(
    sql_runtime, state, expired, late_input
):
    factory, engine, _ = sql_runtime
    selected, event_id = await reviewed_call(factory, state=state, expired=expired)
    usage = complete_usage(input_tokens=late_input)
    assert await finish_result(factory, usage=usage)
    assert not await finish_result(factory, usage=usage)
    async with factory() as db:
        totals = await fetch_usage_summary(
            db, user_id=1, operation_id="review-operation"
        )
        assert len(totals) == 1
        assert totals[0]["calls"] == 1
        assert totals[0]["input_tokens"] == 1000
        assert totals[0]["output_tokens"] == 100
        global_totals = await fetch_global_token_totals(db)
        assert global_totals.recorded_calls == 1
        assert global_totals.unreported_usage_calls == 0
        assert (global_totals.input_tokens, global_totals.output_tokens) == (1000, 100)
    async with factory() as db:
        assert (await resolve_billing_call(db, **evidence())).id == event_id
        service = BillingService(db)
        await service.settle_operation("review-operation")
        await service.settle_operation("review-operation")
        assert (await service.reconcile_wallet(1))["consistent"]
        await db.commit()
    assert_one_reviewed_charge(engine, selected)
    with Session(engine) as db:
        actual = db.scalar(
            select(AIUsageRecord).where(AIUsageRecord.role == "late_actual")
        )
        assert actual.input_tokens == late_input and actual.usage_reported
        assert actual.output_tokens == 100
        assert actual.actual_call_id is None
        assert actual.source["billing_late_result"] == {
            "call_id": "actual-call",
            "reconciliation_event_id": event_id,
            "selected_usage_record_key": selected,
            "original_usage_record_key": None,
        }
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 2
        assert db.scalar(select(func.count(BillingReconciliationEvent.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["insert", "commit"])
async def test_late_usage_write_failure_rolls_back_and_can_retry(sql_runtime, failure):
    factory, engine, _ = sql_runtime
    selected, _ = await reviewed_call(factory, state="running", expired=True)

    def fail_usage_insert(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO ai_usage_records"):
            raise RuntimeError("late Usage write unavailable")

    def fail_outer_commit(session):
        if not session.in_nested_transaction():
            raise RuntimeError("late Usage write unavailable")

    target, name, listener = (
        (engine, "before_cursor_execute", fail_usage_insert)
        if failure == "insert"
        else (Session, "before_commit", fail_outer_commit)
    )
    event.listen(target, name, listener)
    try:
        with pytest.raises(RuntimeError, match="late Usage write unavailable"):
            await finish_result(factory, usage=complete_usage(input_tokens=2000))
    finally:
        event.remove(target, name, listener)
    assert_one_reviewed_charge(engine, selected)
    with Session(engine) as db:
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 1
    assert await finish_result(factory, usage=complete_usage(input_tokens=2000))
    assert not await finish_result(factory, usage=complete_usage(input_tokens=2000))
    assert_one_reviewed_charge(engine, selected)


@pytest.mark.asyncio
async def test_an_unrelated_later_real_attempt_still_adds_its_own_charge(sql_runtime):
    factory, engine, _ = sql_runtime
    selected, _ = await reviewed_call(factory, state="running", expired=True)
    assert await finish_result(factory)
    async with factory() as db:
        service = BillingService(db)
        await service.resume_operation("review-operation")
        await service.start_call(
            "review-operation",
            "another-real-call",
            "provider",
            "model",
            "chat",
            # Logical identity cannot erase a distinct actual upstream attempt.
            logical_call_id="logical-call",
            protocol_family="openai_compatible",
        )
        await db.commit()
    assert await finish_result(factory, call_id="another-real-call")
    async with factory() as db:
        service = BillingService(db)
        await service.finish_operation("review-operation", "completed")
        await service.settle_operation("review-operation")
        assert (await service.reconcile_wallet(1))["consistent"]
        await db.commit()
    with Session(engine) as db:
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 2
        assert db.get(BillingWallet, 1).balance_units == 99_688_000
        assert db.get(BillingOperation, "review-operation").settled_units == 312_000
        assert db.get(BillingCallAttempt, "actual-call").usage_record_key == selected
        other = db.get(BillingCallAttempt, "another-real-call")
        assert other.usage_record_key == "chat:another-real-call"
        assert other.state == "usage_known"
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_input", [None, 400])
async def test_late_result_preserves_original_unknown_or_partial_actual_usage(
    sql_runtime, partial_input
):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    partial = (
        {}
        if partial_input is None
        else complete_usage(input_tokens=partial_input, cache_read_tokens=0)
    )
    assert await finish_result(factory, usage=partial, usage_complete=False)
    async with factory() as db:
        await BillingService(db).finish_operation("review-operation", "completed")
        first = await resolve_billing_call(db, **evidence())
        event_id = first.id
        selected = (await db.get(BillingCallAttempt, "actual-call")).usage_record_key
        await db.commit()
    assert await finish_result(factory, usage=complete_usage(input_tokens=2000))
    assert not await finish_result(factory, usage=complete_usage(input_tokens=2000))
    assert_one_reviewed_charge(engine, selected)
    with Session(engine) as db:
        original = db.scalar(
            select(AIUsageRecord).where(AIUsageRecord.actual_call_id == "actual-call")
        )
        assert original.record_key == "chat:actual-call"
        assert original.input_tokens == partial_input
        assert not original.usage_complete
        audit = db.scalar(
            select(AIUsageRecord).where(AIUsageRecord.role == "late_actual")
        )
        assert audit.input_tokens == 2000 and audit.usage_complete
        assert audit.source["billing_late_result"] == {
            "call_id": "actual-call",
            "reconciliation_event_id": event_id,
            "selected_usage_record_key": selected,
            "original_usage_record_key": original.record_key,
        }
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 3


@pytest.mark.asyncio
async def test_distinct_contradictory_late_evidence_appends_without_repricing(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    selected, _ = await reviewed_call(factory, state="running", expired=True)
    assert await finish_result(factory)
    assert await finish_result(factory, usage=complete_usage(input_tokens=2000))
    assert not await finish_result(factory, usage=complete_usage(input_tokens=2000))
    assert_one_reviewed_charge(engine, selected)
    with Session(engine) as db:
        audits = db.scalars(
            select(AIUsageRecord)
            .where(AIUsageRecord.role == "late_actual")
            .order_by(AIUsageRecord.id)
        ).all()
        assert [row.input_tokens for row in audits] == [1000, 2000]
        assert len({row.record_key for row in audits}) == 2
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 3


@pytest.mark.asyncio
async def test_audit_role_and_another_calls_reference_cannot_hide_a_real_charge(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    selected, event_id = await reviewed_call(factory)
    async with factory() as db:
        service = BillingService(db)
        await service.resume_operation("review-operation")
        await service.start_call(
            "review-operation",
            "another-real-call",
            "provider",
            "model",
            "chat",
            logical_call_id="logical-call",
            protocol_family="openai_compatible",
        )
        await db.commit()
    assert await finish_result(
        factory,
        call_id="another-real-call",
        role="late_actual",
        source={
            "billing_late_result": {
                "call_id": "actual-call",
                "reconciliation_event_id": event_id,
                "selected_usage_record_key": selected,
                "original_usage_record_key": None,
            }
        },
    )
    async with factory() as db:
        await BillingService(db).finish_operation("review-operation", "completed")
        await db.commit()
    with Session(engine) as db:
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 2
        assert db.get(BillingWallet, 1).balance_units == 99_688_000


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [None, "missing-event", "another-usage"])
async def test_unverified_audit_reference_is_retained_as_unpriced_evidence(
    sql_runtime, reference
):
    factory, engine, _ = sql_runtime
    selected, event_id = await reviewed_call(factory)
    source = {}
    if reference is not None:
        source["billing_late_result"] = {
            "call_id": "actual-call",
            "reconciliation_event_id": event_id + 100
            if reference == "missing-event"
            else event_id,
            "selected_usage_record_key": "not-selected"
            if reference == "another-usage"
            else selected,
            "original_usage_record_key": None,
        }
    async with factory() as db:
        db.add(
            AIUsageRecord(
                record_key=f"unverified-audit:{reference}",
                actual_call_id=None,
                operation_id="review-operation",
                user_id=1,
                feature="pr_review",
                source=source,
                call_kind="chat",
                role="late_actual",
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                input_tokens=2000,
                output_tokens=100,
                usage_reported=True,
                usage_complete=True,
            )
        )
        operation = await BillingService(db).settle_operation("review-operation")
        assert operation.status == "pending_pricing"
        assert operation.pending_reason == "missing_price"
        await db.commit()
    with Session(engine) as db:
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 2
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 1
        assert db.get(BillingWallet, 1).balance_units == 99_844_000


@pytest.mark.asyncio
async def test_late_completion_refreshes_a_cached_attempt_before_selecting_usage(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    await pending_call(factory)
    async with factory() as cached_db:
        cached = await cached_db.get(BillingCallAttempt, "actual-call")
        assert cached.usage_record_key is None
        await cached_db.commit()
        async with factory() as reviewer_db:
            await BillingService(reviewer_db).finish_operation(
                "review-operation", "completed"
            )
            await resolve_billing_call(reviewer_db, **evidence())
            selected = (
                await reviewer_db.get(BillingCallAttempt, "actual-call")
            ).usage_record_key
            await reviewer_db.commit()
        assert cached.usage_record_key is None
        assert await finish_result(lambda: cached_db)
    assert_one_reviewed_charge(engine, selected)
