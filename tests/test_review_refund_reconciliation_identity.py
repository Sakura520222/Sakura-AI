"""Provider reconciliation must preserve one native refund's financial effect."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import (
    PaymentRefundAttempt,
    PaymentRefundAttemptEvent,
    PaymentRefundReference,
)
from backend.services.payment.gateway_base import (
    RefundResult,
    WebhookEvent,
    WebhookEventType,
)
from backend.services.payment_event_service import PaymentEventService
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order

db = real_database_fixture


async def _unknown_partial(db, monkeypatch, name, refund_id=""):
    user, order = await paid_order(db, name)
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=False, refund_id=refund_id, amount_cents=30, status="pending"
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    with pytest.raises(PaymentError) as failure:
        await PaymentService(db).process_refund(
            order.id,
            amount_cents=30,
            operator_id=user.id,
            idempotency_key=f"partial:{name}",
        )
    assert failure.value.code == "refund_reconciliation_required"
    await db.commit()
    attempt = (
        await db.execute(
            select(PaymentRefundAttempt).where(
                PaymentRefundAttempt.order_id == order.id
            )
        )
    ).scalar_one()
    assert attempt.status == "unknown"
    assert (await db.get(BillingWallet, user.id)).reserved_units == 1_500_000
    return user, order, attempt


def _callback(order, event_id, refund_id):
    return WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id=event_id,
        original_amount_cents=100,
        refund_items=[
            {
                "id": refund_id,
                "amount_cents": 30,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
    )


async def _state(db, order, attempt):
    wallet = await db.get(BillingWallet, order.user_id)
    transactions = (
        await db.execute(
            select(
                BillingTransaction.id,
                BillingTransaction.delta_units,
                BillingTransaction.reference_transaction_id,
            ).order_by(BillingTransaction.id)
        )
    ).all()
    events = (
        (
            await db.execute(
                select(PaymentRefundAttemptEvent)
                .where(PaymentRefundAttemptEvent.attempt_id == attempt.id)
                .order_by(PaymentRefundAttemptEvent.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "attempt": (
            attempt.status,
            attempt.active_order_id,
            attempt.provider_refund_id,
            attempt.actor_id,
            json.dumps(attempt.evidence, sort_keys=True),
        ),
        "order": (order.status, order.refunded_amount_cents, order.metadata_json),
        "wallet": (wallet.balance_units, wallet.reserved_units),
        "transactions": transactions,
        "events": [
            (row.event_key, row.status, row.actor_id, json.dumps(row.evidence))
            for row in events
        ],
    }


async def _assert_one_partial_effect(db, user, order, attempt):
    await db.refresh(order)
    await db.refresh(attempt)
    assert order.refunded_amount_cents == 30
    assert json.loads(order.metadata_json)["refunded_gateway_amount_cents"] == 30
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 0)
    assert attempt.status == "succeeded"
    assert attempt.active_order_id is None
    assert (
        await db.execute(
            select(BillingTransaction.delta_units).order_by(BillingTransaction.id)
        )
    ).scalars().all() == [5_000_000, -1_500_000]
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1
    assert len((await db.execute(select(PaymentRefundAttempt))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_unbound_reconciliation_keeps_delayed_refund_one_financial_effect(
    db, monkeypatch
):
    user, order, attempt = await _unknown_partial(db, monkeypatch, "unbound-refund")
    rejected = None
    try:
        await PaymentService(db).reconcile_refund(
            attempt.id,
            operator_id=user.id,
            upstream_status="refunded",
            evidence="Dashboard confirms the partial refund, awaiting native identity",
        )
    except PaymentError as failure:
        rejected = failure
    await db.commit()

    inbox = PaymentEventService(db)
    callback = _callback(order, "delayed-refund", "re_delayed")
    record = await inbox.accept("stripe", callback, payload_hash="verified-delayed")
    await db.commit()
    assert record.status == "processed"
    assert (
        await inbox.accept("stripe", callback, payload_hash="verified-delayed")
    ).id == record.id
    assert (await inbox.replay(record.id)).status == "processed"
    assert (
        await inbox.accept(
            "stripe",
            _callback(order, "refund-updated", "re_delayed"),
            payload_hash="verified-update",
        )
    ).status == "processed"
    await db.commit()

    await _assert_one_partial_effect(db, user, order, attempt)
    assert attempt.provider_refund_id == "re_delayed"
    assert rejected is not None
    assert rejected.code == "refund_evidence_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refund_id", [None, "", " \t\n", " re_padded ", 123, "r" * 192]
)
async def test_invalid_native_identity_rejects_before_any_reconciliation_mutation(
    db, monkeypatch, refund_id
):
    user, order, attempt = await _unknown_partial(db, monkeypatch, "invalid-refund-id")
    before = await _state(db, order, attempt)
    with pytest.raises(PaymentError) as failure:
        await PaymentService(db).reconcile_refund(
            attempt.id,
            operator_id=user.id,
            upstream_status="refunded",
            evidence="Provider dashboard evidence",
            provider_refund_id=refund_id,
        )
    assert failure.value.code == "refund_evidence_incomplete"
    # A caller may commit other work after catching a validation error.
    await db.commit()
    assert await _state(db, order, attempt) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_identity", [False, True])
async def test_native_identity_binds_reconciliation_to_delayed_verified_callback(
    db, monkeypatch, saved_identity
):
    user, order, attempt = await _unknown_partial(
        db, monkeypatch, "bound-refund", "re_bound" if saved_identity else ""
    )
    service = PaymentService(db)
    await service.reconcile_refund(
        attempt.id,
        operator_id=user.id,
        upstream_status="refunded",
        evidence="Native provider refund confirmed",
        provider_refund_id=None if saved_identity else "re_bound",
    )
    await db.commit()
    await service.reconcile_refund(
        attempt.id,
        operator_id=user.id,
        upstream_status="refunded",
        evidence="Repeat confirmed provider observation",
    )
    await db.commit()
    inbox = PaymentEventService(db)
    for event_id in ("bound-callback", "bound-callback-reordered"):
        assert (
            await inbox.accept(
                "stripe", _callback(order, event_id, "re_bound"), payload_hash=event_id
            )
        ).status == "processed"
        await db.commit()
    await _assert_one_partial_effect(db, user, order, attempt)
    assert attempt.provider_refund_id == "re_bound"


@pytest.mark.asyncio
async def test_distinct_verified_refund_does_not_rebind_a_known_inflight_identity(
    db, monkeypatch
):
    user, order, attempt = await _unknown_partial(
        db, monkeypatch, "distinct-native-refund", "re_original"
    )
    inbox = PaymentEventService(db)
    assert (
        await inbox.accept(
            "stripe",
            _callback(order, "separate-native-callback", "re_separate"),
            payload_hash="verified-separate",
        )
    ).status == "processed"
    await db.commit()
    await db.refresh(attempt)
    assert attempt.provider_refund_id == "re_original"
    assert attempt.status == "unknown"
    assert attempt.active_order_id == order.id
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 1_500_000)
    assert order.refunded_amount_cents == 30

    await PaymentService(db).reconcile_refund(
        attempt.id,
        operator_id=user.id,
        upstream_status="refunded",
        evidence="The originally staged native refund is independently confirmed",
    )
    await db.commit()
    assert (
        await inbox.accept(
            "stripe",
            _callback(order, "original-native-callback", "re_original"),
            payload_hash="verified-original",
        )
    ).status == "processed"
    await db.commit()
    assert order.refunded_amount_cents == 60
    assert json.loads(order.metadata_json)["refunded_gateway_amount_cents"] == 60
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (2_000_000, 0)
    assert (
        await db.execute(
            select(BillingTransaction.delta_units).order_by(BillingTransaction.id)
        )
    ).scalars().all() == [5_000_000, -1_500_000, -1_500_000]
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("already_succeeded", [False, True])
async def test_conflicting_native_identity_preserves_saved_identity_and_financial_state(
    db, monkeypatch, already_succeeded
):
    user, order, attempt = await _unknown_partial(
        db, monkeypatch, "conflict-id", "re_saved"
    )
    service = PaymentService(db)
    if already_succeeded:
        await service.reconcile_refund(
            attempt.id,
            operator_id=user.id,
            upstream_status="refunded",
            evidence="Native saved provider refund confirmed",
        )
        await db.commit()
    before = await _state(db, order, attempt)
    with pytest.raises(PaymentError) as failure:
        await service.reconcile_refund(
            attempt.id,
            operator_id=user.id,
            upstream_status="refunded",
            evidence="Conflicting dashboard identity",
            provider_refund_id="re_other",
        )
    assert failure.value.code == "refund_reference_conflict"
    await db.commit()
    assert await _state(db, order, attempt) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_reference", [False, True])
async def test_native_identity_already_used_for_another_refund_cannot_finalize_new_attempt(
    db, monkeypatch, existing_reference
):
    if existing_reference:
        _, first_order = await paid_order(db, "existing-native-effect")
        assert (
            await PaymentEventService(db).accept(
                "stripe",
                _callback(first_order, "existing-native-callback", "re_used"),
                payload_hash="verified-existing",
            )
        ).status == "processed"
        await db.commit()
    else:
        first_user, _, first_attempt = await _unknown_partial(
            db, monkeypatch, "existing-native-attempt"
        )
        await PaymentService(db).reconcile_refund(
            first_attempt.id,
            operator_id=first_user.id,
            upstream_status="refunded",
            evidence="Provider native refund confirmed",
            provider_refund_id="re_used",
        )
        await db.commit()
    user, order, attempt = await _unknown_partial(db, monkeypatch, "reused-native-id")
    before = await _state(db, order, attempt)
    with pytest.raises(PaymentError) as failure:
        await PaymentService(db).reconcile_refund(
            attempt.id,
            operator_id=user.id,
            upstream_status="refunded",
            evidence="A native refund identity belonging to another payment",
            provider_refund_id="re_used",
        )
    assert failure.value.code == "refund_reference_conflict"
    await db.commit()
    assert await _state(db, order, attempt) == before


@pytest.mark.asyncio
async def test_successful_gateway_result_without_native_identity_keeps_hold_until_callback(
    db, monkeypatch
):
    user, order = await paid_order(db, "upstream-missing-identity")
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=True, amount_cents=30, status="succeeded"
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    with pytest.raises(PaymentError) as failure:
        await PaymentService(db).process_refund(
            order.id,
            amount_cents=30,
            operator_id=user.id,
            idempotency_key="success-without-native-id",
        )
    assert failure.value.code == "refund_evidence_incomplete"
    await db.commit()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    wallet = await db.get(BillingWallet, user.id)
    assert attempt.status == "upstream_succeeded"
    assert attempt.active_order_id == order.id
    assert order.refunded_amount_cents == 0
    assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 1_500_000)
    assert (
        await PaymentEventService(db).accept(
            "stripe",
            _callback(order, "native-success-arrived", "re_native_success"),
            payload_hash="verified-native-success",
        )
    ).status == "processed"
    await db.commit()
    await _assert_one_partial_effect(db, user, order, attempt)


@pytest.mark.asyncio
async def test_confirmed_upstream_outcome_cannot_write_unrefunded_reconciliation(
    db, monkeypatch
):
    user, order, attempt = await _unknown_partial(
        db, monkeypatch, "confirmed-upstream", "re_confirmed"
    )
    attempt.status = "upstream_succeeded"
    await db.commit()
    before = await _state(db, order, attempt)
    with pytest.raises(PaymentError):
        await PaymentService(db).reconcile_refund(
            attempt.id,
            operator_id=user.id,
            upstream_status="not_refunded",
            evidence="An outcome conflicting with already recorded success",
        )
    await db.commit()
    assert await _state(db, order, attempt) == before


@pytest.mark.asyncio
async def test_unrefunded_outcome_without_native_identity_releases_only_the_hold(
    db, monkeypatch
):
    user, order, attempt = await _unknown_partial(db, monkeypatch, "unrefunded-id")
    await PaymentService(db).reconcile_refund(
        attempt.id,
        operator_id=user.id,
        upstream_status="not_refunded",
        evidence="Provider confirms no refund was created",
    )
    await db.commit()
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 0)
    assert attempt.status == "failed"
    assert order.refunded_amount_cents == 0
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["manual", "redeem_code", "stripe"])
async def test_local_reversal_does_not_require_an_external_refund_identity(
    db, monkeypatch, provider
):
    user, order = await paid_order(db, "local-refund-id")
    order.payment_provider = provider
    order.provider_tx_id = None
    await db.commit()
    factory = AsyncMock(
        side_effect=AssertionError("Local reversal must not send a refund")
    )
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    await PaymentService(db).process_refund(order.id, operator_id=user.id)
    await db.commit()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    assert attempt.status == "succeeded"
    assert attempt.gateway_amount_cents is None
    assert attempt.provider_refund_id is None
    assert order.status == "refunded"
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (0, 0)
