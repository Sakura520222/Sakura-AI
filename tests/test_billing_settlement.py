"""Real SQL settlement, immutable prices and explicit uncertain outcomes."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from backend.core.time_service import now_utc
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingTransaction,
    BillingUsageCharge,
)
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_wallet import db as wallet_db

db = wallet_db

POLICY = {
    "billing_enabled": True,
    "billing_charge_failed_operations": False,
    "billing_charge_failed_calls": False,
    "billing_initial_reserve_credits": "1",
    "billing_reservation_ttl_seconds": 3600,
}
PRICE = {
    "currency": "USD",
    "settlement_currency": "USD",
    "fx_rate": "1",
    "markup": "1",
    "credits_per_currency_unit": "1",
    "unit": "tokens",
    "input_price": "1",
    "output_price": "2",
    "cache_read_supported": False,
    "cache_creation_supported": False,
}


async def prepare(db, operation_id="op-1"):
    service = BillingService(db, policy=POLICY)
    await service.grant(1, 10, "purchase:1", kind="purchase", order_id=1)
    await service.publish_price("p1", "m1", "chat", PRICE, actor_id=1)
    await service.register_operation(
        1, operation_id, "pr_review", {"repo_full_name": "a/b", "pr_number": 1}
    )
    return service


async def add_call(
    db, service, call_id, operation_id="op-1", *, tokens=1, reported=True
):
    attempt = await service.start_call(
        operation_id,
        call_id,
        "p1",
        "m1",
        "chat",
        logical_call_id="same-logical",
        protocol_family="openai_compatible",
    )
    db.add(
        AIUsageRecord(
            record_key=call_id,
            actual_call_id=call_id,
            operation_id=operation_id,
            user_id=1,
            feature="pr_review",
            call_kind="chat",
            role="main",
            provider_id="p1",
            model_id="m1",
            protocol_family="openai_compatible",
            input_tokens=tokens if reported else None,
            output_tokens=0 if reported else None,
            usage_reported=reported,
            usage_semantics={
                "input_includes_cache_read": True,
                "input_includes_cache_creation": True,
                "output_includes_reasoning": True,
            },
            outcome="completed",
        )
    )
    attempt.state = "usage_known" if reported else "pending_usage"
    attempt.usage_record_key = call_id
    await db.flush()
    return attempt


@pytest.mark.asyncio
async def test_actual_attempts_same_logical_call_and_cumulative_settlement(db):
    service = await prepare(db)
    for i in range(5):
        await add_call(db, service, f"actual-{i}")
        await service.settle_operation("op-1")
    first = await service.finish_operation("op-1", "completed")
    assert first.settled_units == 5
    await service.settle_operation("op-1")
    assert (
        await db.execute(
            select(func.count(BillingTransaction.id)).where(
                BillingTransaction.kind == "consumption"
            )
        )
    ).scalar_one() == 1
    assert (await service.get_wallet(1)).balance_units == 10_000_000 - 5
    assert (await service.get_wallet(1)).reserved_units == 0
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_price_is_pinned_before_send_and_history_stays_reproducible(db):
    service = await prepare(db)
    await add_call(db, service, "actual-old", tokens=1000)
    await service.publish_price(
        "p1", "m1", "chat", {**PRICE, "input_price": "999"}, actor_id=1
    )
    await service.finish_operation("op-1", "completed")
    charge = (await db.execute(select(BillingUsageCharge))).scalar_one()
    assert charge.provider_cost == "0.001"
    assert charge.snapshot["price_version"] == 1
    assert charge.snapshot["input_price"] == "1"


@pytest.mark.asyncio
async def test_failed_with_provider_usage_keeps_cost_without_default_debit(db):
    service = await prepare(db)
    await add_call(db, service, "actual-failed", tokens=1000)
    operation = await service.finish_operation("op-1", "failed")
    assert operation.status == "failed"
    assert operation.settled_units == 0
    assert (await service.get_wallet(1)).balance_units == 10_000_000
    assert (
        await db.execute(select(func.count(BillingUsageCharge.id)))
    ).scalar_one() == 1
    assert (await service.get_wallet(1)).reserved_units == 0


@pytest.mark.asyncio
async def test_unknown_usage_is_pending_and_releases_terminal_reservation(db):
    service = await prepare(db)
    await add_call(db, service, "unknown", reported=False)
    operation = await service.finish_operation("op-1", "cancelled")
    assert operation.status == "pending_reconciliation"
    assert operation.pending_reason
    assert operation.settled_units == 0
    assert (await service.get_wallet(1)).reserved_units == 0


@pytest.mark.asyncio
async def test_missing_price_blocks_external_request_and_owner_conflicts(db):
    service = await prepare(db)
    with pytest.raises(BillingError, match="No exact price"):
        await service.start_call("op-1", "no-price", "other", "other", "chat")
    assert (
        await db.execute(select(func.count(BillingCallAttempt.call_id)))
    ).scalar_one() == 0
    with pytest.raises(BillingError, match="attribution conflict"):
        await service.register_operation(2, "op-1", "pr_review", {})


@pytest.mark.asyncio
async def test_crash_recovery_dry_run_and_release_are_idempotent(db):
    service = await prepare(db)
    await service.start_call("op-1", "sent-unknown", "p1", "m1", "chat")
    operation = await service._operation("op-1")
    operation.expires_at = now_utc() - timedelta(seconds=1)
    assert len(await service.recover_operations(dry_run=True)) == 1
    assert (await service.get_wallet(1)).reserved_units == 1_000_000
    await service.recover_operations(dry_run=False)
    assert (await service.get_wallet(1)).reserved_units == 0
    assert (await service._operation("op-1")).status == "pending_reconciliation"
    assert await service.recover_operations(dry_run=False) == []


@pytest.mark.asyncio
async def test_consumed_purchase_cannot_revoke_other_sources_and_refund_restores_source(
    db,
):
    service = await prepare(db)
    await add_call(db, service, "paid-use", tokens=1_000_000)
    await service.finish_operation("op-1", "completed")
    consumption = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        )
    ).scalar_one()
    purchase = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "purchase")
        )
    ).scalar_one()
    await service.grant(1, 100, "other-source")
    with pytest.raises(BillingError, match="already been consumed"):
        await service.reverse(purchase.id, "purchase-refund")
    await service.reverse(consumption.id, "usage-refund")
    await service.reverse(consumption.id, "usage-refund")
    await service.reverse(purchase.id, "purchase-refund")
    assert (await service.get_wallet(1)).balance_units == 100_000_000
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_automatic_resume_incremental_delta_and_new_user_run_are_distinct(db):
    service = await prepare(db)
    await add_call(db, service, "first", tokens=1)
    await service.finish_operation("op-1", "completed")
    await service.resume_operation("op-1")
    await add_call(db, service, "continuation", tokens=1)
    await service.finish_operation("op-1", "completed")
    await service.register_operation(1, "new-run", "pr_review", {"pr_number": 1})
    await add_call(db, service, "new-attempt", "new-run", tokens=1)
    await service.finish_operation("new-run", "completed")
    assert (await service.get_wallet(1)).balance_units == 10_000_000 - 3


@pytest.mark.asyncio
async def test_known_cost_overrun_topup_repays_attributable_debt(db):
    from backend.models.billing_models import BillingCreditLot

    service = await prepare(db)
    await add_call(db, service, "overrun", tokens=15_000_000)
    await service.finish_operation("op-1", "completed")
    assert (await service.get_wallet(1)).balance_units == -5_000_000
    repayment = await service.grant(1, 5, "debt-repayment", kind="purchase")
    other = await service.grant(1, 2, "other-source", kind="purchase")
    assert (await db.get(BillingCreditLot, repayment.id)).remaining_units == 0
    with pytest.raises(BillingError, match="already been consumed"):
        await service.reverse(repayment.id, "refund-repayment", units=2_000_000)
    consumption = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        )
    ).scalar_one()
    await service.reverse(consumption.id, "reverse-overrun")
    assert (await db.get(BillingCreditLot, repayment.id)).remaining_units == 5_000_000
    await service.reverse(repayment.id, "refund-repayment")
    assert (await db.get(BillingCreditLot, other.id)).remaining_units == 2_000_000
    assert (await service.get_wallet(1)).balance_units == 12_000_000
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_inflight_usage_during_refund_hold_is_funded_by_topup_source(db):
    service = await prepare(db)
    purchase = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "purchase")
        )
    ).scalar_one()
    # The task's existing one-Credit admission reservation is separate. A hold
    # protects the remaining source before an already sent call reports usage.
    await service.hold_purchase_refund(purchase.id, "hold", units=9_000_000)
    await add_call(db, service, "held-overrun", tokens=5_000_000)
    await service.finish_operation("op-1", "completed")
    repayment = await service.grant(1, 5, "fund-held-overrun", kind="purchase")
    other = await service.grant(1, 2, "unrelated", kind="purchase")
    await service.finalize_purchase_refund(
        purchase.id, 9_000_000, "hold", "upstream-refund"
    )
    with pytest.raises(BillingError, match="already been consumed"):
        await service.reverse(repayment.id, "repayment-refund", units=2_000_000)
    assert (await service.get_wallet(1)).balance_units == 3_000_000
    assert (await service.reconcile_wallet(1))["consistent"]
    assert other.delta_units == 2_000_000


@pytest.mark.asyncio
async def test_verified_refund_during_inflight_overrun_posts_debt_without_fake_balance(
    db,
):
    service = await prepare(db)
    purchase = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "purchase")
        )
    ).scalar_one()
    await service.hold_purchase_refund(purchase.id, "protected", units=9_000_000)
    await add_call(db, service, "overrun-before-topup", tokens=5_000_000)
    await service.finish_operation("op-1", "completed")
    await service.finalize_purchase_refund(
        purchase.id, 9_000_000, "protected", "confirmed"
    )
    await service.finalize_purchase_refund(
        purchase.id, 9_000_000, "protected", "confirmed"
    )
    assert (await service.get_wallet(1)).balance_units == -4_000_000
    assert (await service.get_wallet(1)).reserved_units == 0
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_resolved_failed_or_cancelled_operation_resumes_same_identity(
    db, outcome
):
    service = await prepare(db)
    await add_call(db, service, "pre-resume", tokens=100)
    await service.finish_operation("op-1", outcome)
    operation = await service.resume_operation("op-1")
    assert operation.operation_id == "op-1" and operation.outcome is None
    await add_call(db, service, "after-resume", tokens=100)
    await service.finish_operation("op-1", "completed")
    assert operation.settled_units == 200
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_verified_external_refund_keeps_other_sources_and_explicit_debt(db):
    from backend.models.billing_models import BillingCreditLot

    service = await prepare(db)
    await add_call(db, service, "spent-before-external", tokens=5_000_000)
    await service.finish_operation("op-1", "completed")
    other = await service.grant(1, 10, "other-source", kind="purchase")
    original = (
        await db.execute(
            select(BillingTransaction).where(
                BillingTransaction.idempotency_key == "purchase:1"
            )
        )
    ).scalar_one()
    evidence = {"event_id": "fixture-provider-event", "amount_cents": 100}
    refund = await service.reverse_external_purchase(
        original.id,
        10_000_000,
        "provider:event",
        reason="Signed provider refund",
        snapshot=evidence,
    )
    again = await service.reverse_external_purchase(
        original.id,
        10_000_000,
        "provider:event",
        reason="Signed provider refund",
        snapshot=evidence,
    )
    assert refund.id == again.id
    assert (await db.get(BillingCreditLot, other.id)).remaining_units == 10_000_000
    report = await service.reconcile_wallet(1)
    assert (
        report["balance_units"] == 5_000_000
        and report["outstanding_debt_units"] == 5_000_000
    )
    assert report["consistent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refunded_units, expected_remaining", [(10_000_000, 0), (8_000_000, 2_000_000)]
)
async def test_usage_reversal_clears_debt_of_its_already_refunded_purchase(
    db, refunded_units, expected_remaining
):
    from backend.models.billing_models import BillingCreditLot

    service = await prepare(db)
    await add_call(db, service, "used-and-refunded", tokens=3_000_000)
    await service.finish_operation("op-1", "completed")
    original = (
        await db.execute(
            select(BillingTransaction).where(
                BillingTransaction.idempotency_key == "purchase:1"
            )
        )
    ).scalar_one()
    consumption = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        )
    ).scalar_one()
    await service.reverse_external_purchase(
        original.id,
        refunded_units,
        "external-refund",
        reason="Verified money returned",
        snapshot={"event_id": "ref-source"},
    )
    await service.reverse(consumption.id, "return-usage")
    report = await service.reconcile_wallet(1)
    assert report["outstanding_debt_units"] == 0
    assert report["balance_units"] == expected_remaining
    assert (
        await db.get(BillingCreditLot, original.id)
    ).remaining_units == expected_remaining
    fresh = await service.grant(1, 3, "fresh-topup", kind="purchase")
    assert (await db.get(BillingCreditLot, fresh.id)).remaining_units == 3_000_000
    await service.check_reversible(fresh.id)


@pytest.mark.asyncio
async def test_returned_usage_restores_topup_that_funded_revoked_purchase_debt(db):
    from backend.models.billing_models import BillingCreditLot

    service = await prepare(db)
    await add_call(db, service, "spent-purchase", tokens=3_000_000)
    await service.finish_operation("op-1", "completed")
    original = (
        await db.execute(
            select(BillingTransaction).where(
                BillingTransaction.idempotency_key == "purchase:1"
            )
        )
    ).scalar_one()
    usage = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        )
    ).scalar_one()
    await service.reverse_external_purchase(
        original.id,
        10_000_000,
        "signed-full-refund",
        reason="Verified refund",
        snapshot={"event_id": "external-full"},
    )
    funding = await service.grant(1, 3, "debt-funding", kind="purchase")
    assert (await db.get(BillingCreditLot, funding.id)).remaining_units == 0
    await service.reverse(usage.id, "refund-usage-after-funded-revocation")
    assert (await db.get(BillingCreditLot, original.id)).remaining_units == 0
    assert (await db.get(BillingCreditLot, funding.id)).remaining_units == 3_000_000
    assert (await service.reconcile_wallet(1))["outstanding_debt_units"] == 0
    await service.check_reversible(funding.id)


@pytest.mark.asyncio
async def test_usage_return_handles_revoked_debt_funding_source_without_resurrection(
    db,
):
    from backend.models.billing_models import BillingCreditLot

    service = await prepare(db)
    await add_call(db, service, "overrun-to-be-corrected", tokens=13_000_000)
    await service.finish_operation("op-1", "completed")
    funding = await service.grant(1, 3, "fund-overrun", kind="purchase")
    await service.reverse_external_purchase(
        funding.id,
        3_000_000,
        "funding-refund",
        reason="Verified funding refund",
        snapshot={"event_id": "fund-returned"},
    )
    usage = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        )
    ).scalar_one()
    await service.reverse(usage.id, "return-whole-usage")
    assert (await db.get(BillingCreditLot, funding.id)).remaining_units == 0
    report = await service.reconcile_wallet(1)
    assert (
        report["outstanding_debt_units"] == 0 and report["balance_units"] == 10_000_000
    )
    assert report["consistent"]
