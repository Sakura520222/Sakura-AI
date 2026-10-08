"""Refund configuration failures must not be mistaken for ambiguous requests."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.billing_models import (
    BillingReservationEvent,
    BillingTransaction,
    BillingWallet,
)
from backend.models.legacy_entitlement_models import (
    PaymentRefundAttempt,
    PaymentRefundAttemptEvent,
)
from backend.services.billing_service import BillingService
from backend.services.payment.gateway_base import RefundResult
from backend.services.payment.stripe_gateway import StripeGateway
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_entitlements import (
    _AsyncSQLiteSession,
)
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order

db = real_database_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_configuration", ["disabled", "api_key"])
async def test_presend_configuration_failure_has_no_hold_and_same_key_can_retry(
    db, monkeypatch, missing_configuration
):
    user, order = await paid_order(db, f"presend-{missing_configuration}")
    configuration = {
        "stripe_enabled": missing_configuration != "disabled",
        "stripe_api_key": "" if missing_configuration == "api_key" else "test-key",
        "stripe_webhook_secret": "test-secret",
    }
    monkeypatch.setattr(
        "backend.core.config.get_dynamic_config",
        AsyncMock(side_effect=lambda key: configuration[key]),
    )
    refund = AsyncMock(
        return_value=RefundResult(
            success=True, refund_id="re_presend", amount_cents=100, status="succeeded"
        )
    )
    monkeypatch.setattr(StripeGateway, "refund", refund)
    service = PaymentService(db)
    key = "presend:stable-request"
    for _ in range(2):
        with pytest.raises(ValueError, match="disabled|missing API key"):
            await service.process_refund(order.id, idempotency_key=key)
        # Approval handlers may commit their failed review result. A caller's
        # commit must not accidentally retain a hold for a request never sent.
        await db.commit()
        wallet = await db.get(BillingWallet, user.id)
        assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 0)
        for model in (PaymentRefundAttempt, PaymentRefundAttemptEvent):
            assert (await db.execute(select(model))).scalars().all() == []
        assert (await db.execute(select(BillingReservationEvent))).scalars().all() == []
        assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
        assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]
        assert order.status == "fulfilled"
        refund.assert_not_awaited()

    configuration.update(stripe_enabled=True, stripe_api_key="test-key")
    await service.process_refund(order.id, idempotency_key=key)
    await db.commit()
    monkeypatch.setattr(
        "backend.services.payment.get_gateway",
        AsyncMock(side_effect=AssertionError("Completed refund must not send again")),
    )
    await service.process_refund(order.id, idempotency_key=key)
    await db.commit()
    assert refund.await_count == 1
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    assert (attempt.idempotency_key, attempt.status) == (key, "succeeded")
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (0, 0)
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2
    assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]


@pytest.mark.asyncio
async def test_failed_refund_approval_can_retry_after_gateway_configuration_repair(
    db, monkeypatch
):
    user, order = await paid_order(db, "presend-approval")
    service = PaymentService(db)
    request = await service.submit_refund_request(order.id, user.id)
    factory = AsyncMock(side_effect=ValueError("Payment provider stripe is disabled"))
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    await service.approve_refund_request(request.id, reviewer_id=user.id)
    await db.commit()
    assert request.status == "failed"
    assert (await db.get(BillingWallet, user.id)).reserved_units == 0
    assert (await db.execute(select(PaymentRefundAttempt))).scalars().all() == []

    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=True, refund_id="re_approval", amount_cents=100, status="succeeded"
    )
    factory.side_effect = None
    factory.return_value = gateway
    await service.approve_refund_request(request.id, reviewer_id=user.id)
    await db.commit()
    assert request.status == "approved"
    assert order.status == "refunded"
    gateway.refund.assert_awaited_once()
    assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "cancel", "unknown"])
async def test_actual_request_unknown_window_keeps_hold_and_never_replays(
    db, monkeypatch, outcome
):
    user, order = await paid_order(db, f"presend-unknown-{outcome}")
    gateway = AsyncMock()

    async def refund(**kwargs):
        assert not db._session.in_transaction(), "Do not hold locks during refund I/O"
        if outcome == "timeout":
            raise TimeoutError("Upstream request result is unknown")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return RefundResult(success=True, status="pending", refund_id="re_unknown")

    gateway.refund.side_effect = refund
    factory = AsyncMock(return_value=gateway)
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    service = PaymentService(db)
    key = "presend:unknown-request"
    failure_type = {
        "timeout": TimeoutError,
        "cancel": asyncio.CancelledError,
        "unknown": PaymentError,
    }[outcome]
    with pytest.raises(failure_type):
        await service.process_refund(order.id, idempotency_key=key)
    await db.rollback()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    identity = (attempt.id, attempt.idempotency_key, attempt.amount_cents)
    assert attempt.status == ("unknown" if outcome == "unknown" else "pending")
    factory.side_effect = AssertionError("Do not resolve a gateway for existing intent")
    for retry_key in (key, "presend:other-request"):
        with pytest.raises(PaymentError) as error:
            await service.process_refund(order.id, idempotency_key=retry_key)
        assert error.value.code == "refund_reconciliation_required"
        await db.rollback()
    await db.refresh(attempt)
    assert (attempt.id, attempt.idempotency_key, attempt.amount_cents) == identity
    assert (await db.get(BillingWallet, user.id)).reserved_units == 5_000_000
    assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]
    assert factory.await_count == 1
    gateway.refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_refund_request_during_io_sees_durable_intent_and_cannot_send(
    db, monkeypatch
):
    user, order = await paid_order(db, "presend-inflight")
    started = asyncio.Event()
    finish = asyncio.Event()
    gateway = AsyncMock()

    async def refund(**kwargs):
        assert not db._session.in_transaction()
        started.set()
        await finish.wait()
        return RefundResult(success=True, status="succeeded", refund_id="re_inflight")

    gateway.refund.side_effect = refund
    factory = AsyncMock(return_value=gateway)
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    first = asyncio.create_task(
        PaymentService(db).process_refund(order.id, idempotency_key="presend:first")
    )
    await started.wait()
    second_database = _AsyncSQLiteSession(
        Session(db._session.get_bind(), expire_on_commit=False)
    )
    try:
        for key in ("presend:first", "presend:second"):
            with pytest.raises(PaymentError) as error:
                await PaymentService(second_database).process_refund(
                    order.id, idempotency_key=key
                )
            assert error.value.code == "refund_reconciliation_required"
            await second_database.rollback()
    finally:
        second_database._session.close()
        finish.set()
        await first
    assert factory.await_count == 1
    gateway.refund.assert_awaited_once()
    assert len((await db.execute(select(PaymentRefundAttempt))).scalars().all()) == 1
    assert (await db.get(BillingWallet, user.id)).reserved_units == 0
    assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]
