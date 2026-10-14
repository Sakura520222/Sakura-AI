"""Cash evidence must match its intent and drive the associated review lifecycle."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.log_context import log_context
from backend.models import Base
from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import PaymentRefundAttempt
from backend.models.payment_models import RefundRequest
from backend.services.payment.gateway_base import (
    RefundResult,
    WebhookEvent,
    WebhookEventType,
)
from backend.services.payment_event_service import PaymentEventService
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order
from tests.test_logging_security import secured_logs as real_logging_fixture

db = real_database_fixture
secured_logs = real_logging_fixture


def install_gateway(monkeypatch, *, amount=100, status="succeeded", success=True):
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=success, refund_id="re_review", amount_cents=amount, status=status
    )
    factory = AsyncMock(return_value=gateway)
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    return gateway, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [99, 101, 0, -1, None, True, "100", 100.0])
async def test_successful_refund_with_inexact_amount_retains_intent_and_hold(
    db, monkeypatch, amount
):
    user, order = await paid_order(db, "amount-review")
    gateway, factory = install_gateway(monkeypatch, amount=amount)
    service = PaymentService(db)
    with pytest.raises(PaymentError) as error:
        await service.process_refund(order.id, idempotency_key="amount-review")
    assert error.value.code == "refund_reconciliation_required"
    await db.rollback()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    assert attempt.status == "unknown" and attempt.active_order_id == order.id
    assert attempt.evidence["pending_reason"] == "refund_amount_mismatch"
    assert attempt.gateway_amount_cents == 100
    assert attempt.provider_refund_id == "re_review"
    await db.refresh(order)
    assert (order.status, order.refunded_amount_cents) == ("fulfilled", 0)
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 5_000_000)
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
    factory.side_effect = AssertionError("An unresolved intent cannot send again")
    for key in ("amount-review", "another-review"):
        with pytest.raises(PaymentError) as replay:
            await service.process_refund(order.id, idempotency_key=key)
        assert replay.value.code == "refund_reconciliation_required"
        await db.rollback()
    gateway.refund.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reported,settled", [(137, True), (100, False)])
async def test_refund_compares_gateway_units_against_the_frozen_checkout(
    db, monkeypatch, reported, settled
):
    user, order = await paid_order(db, "gateway-unit-review")
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 137, "gateway_currency": "USD"}
    )
    await db.commit()
    gateway, _ = install_gateway(monkeypatch, amount=reported)
    if settled:
        await PaymentService(db).process_refund(order.id)
        assert (order.status, order.refunded_amount_cents) == ("refunded", 100)
        assert (await db.get(BillingWallet, user.id)).balance_units == 0
    else:
        with pytest.raises(PaymentError, match="reconciliation"):
            await PaymentService(db).process_refund(order.id)
        assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert gateway.refund.call_args.kwargs["amount_cents"] == 137


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["pending", "requires_action", "error", "timeout", "amount"]
)
@pytest.mark.parametrize("verified", ["refunded", "not_refunded"])
async def test_unknown_approval_stays_pending_until_verified_outcome(
    db, monkeypatch, outcome, verified
):
    user, order = await paid_order(db, "approval-review")
    service = PaymentService(db)
    request = await service.submit_refund_request(order.id, user.id)
    request_id, order_id, user_id = request.id, order.id, user.id
    await db.commit()
    gateway, factory = install_gateway(
        monkeypatch,
        status=outcome if outcome in {"pending", "requires_action"} else "succeeded",
        success=outcome != "error",
        amount=99 if outcome == "amount" else 100,
    )
    if outcome == "timeout":
        gateway.refund.side_effect = TimeoutError("Upstream acknowledgement lost")
    pending = await service.approve_refund_request(request_id, user_id)
    await db.commit()
    assert pending.status == "pending"
    assert pending.reviewed_by == user_id and pending.reviewed_at is not None
    assert pending.processed_at is None
    assert pending.error_message
    with pytest.raises(PaymentError) as rejection:
        await service.reject_refund_request(request_id, user_id)
    assert rejection.value.code == "refund_reconciliation_required"
    await db.rollback()
    factory.side_effect = AssertionError("Do not resend an ambiguous refund")
    await service.approve_refund_request(request_id, user_id)
    await db.commit()
    assert (await db.get(RefundRequest, request_id)).status == "pending"
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    await service.reconcile_refund(
        attempt.id,
        operator_id=user_id,
        upstream_status=verified,
        evidence="TEST independently confirmed exact provider outcome",
        provider_refund_id="re_review",
    )
    await db.commit()
    await db.refresh(pending)
    assert pending.status == ("approved" if verified == "refunded" else "failed")
    assert (pending.processed_at is not None) is (verified == "refunded")
    wallet = await db.get(BillingWallet, user_id)
    assert wallet.reserved_units == 0
    assert wallet.balance_units == (0 if verified == "refunded" else 5_000_000)
    if verified == "not_refunded":
        await service.reject_refund_request(request_id, user_id)
        await db.commit()
    gateway.refund.assert_awaited_once()
    assert order_id == attempt.order_id


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
async def test_verified_late_webhook_finalizes_the_pending_review_request(
    db, monkeypatch, outcome
):
    user, order = await paid_order(db, "webhook-request-review")
    service = PaymentService(db)
    request = await service.submit_refund_request(order.id, user.id)
    gateway, _ = install_gateway(monkeypatch, status="pending")
    await service.approve_refund_request(request.id, user.id)
    await db.commit()
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        event_id="late-approved-request",
        order_no=order.order_no,
        currency="CNY",
        original_amount_cents=100,
        refund_evidence_complete=True,
        refund_items=[
            {
                "id": "re_review",
                "amount_cents": 100,
                "currency": "CNY",
                "status": outcome,
            }
        ],
    )
    inbox = PaymentEventService(db)
    for _ in range(2):
        assert (
            await inbox.accept("stripe", event, payload_hash="exact-refund")
        ).status == "processed"
        await db.commit()
    await db.refresh(request)
    succeeded = outcome == "succeeded"
    assert request.status == ("approved" if succeeded else "failed")
    assert (request.processed_at is not None) is succeeded
    assert (request.error_message is None) is succeeded
    assert order.status == ("refunded" if succeeded else "fulfilled")
    wallet = await db.get(BillingWallet, user.id)
    assert wallet.balance_units == (0 if succeeded else 5_000_000)
    assert wallet.reserved_units == 0
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == (
        2 if succeeded else 1
    )
    gateway.refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_refund_timeout_rereads_expired_entities_before_pending_review(
    tmp_path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'refund-review.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            user, order = await paid_order(db, "native-async-review")
            service = PaymentService(db)
            request = await service.submit_refund_request(order.id, user.id)
            request_id, user_id = request.id, user.id
            await db.commit()
            gateway, _ = install_gateway(monkeypatch)
            gateway.refund.side_effect = TimeoutError(
                "TEST upstream acknowledgement lost"
            )
            pending = await service.approve_refund_request(request_id, user_id)
            await db.commit()
            assert pending.status == "pending" and pending.processed_at is None
            assert (await db.get(BillingWallet, user_id)).reserved_units == 5_000_000
            gateway.refund.assert_awaited_once()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_refund_amount_diagnostics_keep_ids_without_copying_provider_bodies(
    db, monkeypatch, secured_logs
):
    console, path, records = secured_logs
    user, order = await paid_order(db, "logged-amount-review")
    gateway, _ = install_gateway(monkeypatch, amount=99)
    private_message = "opaque-provider-response-private-value"
    gateway.refund.return_value.error_message = private_message
    with log_context(request_id="refund-review-log", task_id="refund-review-task"):
        with pytest.raises(PaymentError):
            await PaymentService(db).process_refund(
                order.id, idempotency_key="logged-refund"
            )
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    for output in (
        console.getvalue(),
        path.read_text(encoding="utf-8"),
        json.dumps(attempt.evidence),
    ):
        assert private_message not in output
    diagnostic = next(
        item
        for item in records
        if item["message"] == "Refund outcome committed for reconciliation"
    )
    assert diagnostic["extra"]["refund_attempt_id"] == attempt.id
    assert diagnostic["extra"]["order_id"] == order.id
    assert diagnostic["extra"]["user_id"] == user.id
    assert diagnostic["extra"]["request_id"] == "refund-review-log"
    assert diagnostic["extra"]["task_id"] == "refund-review-task"
    assert diagnostic["extra"]["pending_reason"] == "refund_amount_mismatch"
    assert diagnostic["extra"]["reported_gateway_amount_cents"] == 99
