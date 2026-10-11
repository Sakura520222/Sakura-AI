"""PR 660 payment lifecycle regressions using real isolated SQL persistence."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from backend.core.time_service import now_utc
from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import PaymentRefundAttempt
from backend.models.payment_models import Plan, RefundRequest
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import BillingService
from backend.services.payment.alipay_gateway import AlipayGateway
from backend.services.payment.gateway_base import (
    PaymentIntentResult,
    RefundResult,
    WebhookEvent,
    WebhookEventType,
)
from backend.services.payment_event_service import PaymentEventService
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order

db = real_database_fixture


@pytest.mark.asyncio
async def test_verified_late_payment_restores_hidden_pending_order_once(db):
    user = TelegramUser(github_username="hidden-pending-owner")
    plan = Plan(
        name="Hidden purchase",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    service = PaymentService(db)
    order = await service.create_order(user.id, plan.id)
    order.payment_provider = "stripe"
    order.hidden_by_user_at = now_utc()
    await db.commit()
    assert (await service.list_user_orders(user.id))[1] == 0
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_COMPLETED,
        provider_tx_id="cs_hidden",
        order_no=order.order_no,
        amount_cents=100,
        currency="CNY",
        event_id="hidden-paid",
    )
    inbox = PaymentEventService(db)
    assert (
        await inbox.accept("stripe", event, payload_hash="verified")
    ).status == "processed"
    await db.commit()
    assert order.hidden_by_user_at is None
    assert [row.id for row in (await service.list_user_orders(user.id))[0]] == [
        order.id
    ]
    request = await service.submit_refund_request(order.id, user.id)
    assert request.amount_cents == 100
    assert (
        await inbox.accept("stripe", event, payload_hash="verified")
    ).status == "processed"
    await db.commit()
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_unverified_amount_does_not_restore_hidden_order_or_grant(db):
    user = TelegramUser(github_username="hidden-invalid-owner")
    plan = Plan(
        name="Hidden purchase",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    order = await PaymentService(db).create_order(user.id, plan.id)
    order.payment_provider = "stripe"
    order.hidden_by_user_at = hidden_at = now_utc()
    await db.commit()
    received = await PaymentEventService(db).accept(
        "stripe",
        WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider_tx_id="cs_wrong",
            order_no=order.order_no,
            amount_cents=99,
            currency="CNY",
            event_id="invalid-hidden-paid",
        ),
        payload_hash="invalid",
    )
    await db.commit()
    assert received.status == "pending_reconciliation"
    assert order.hidden_by_user_at == hidden_at
    assert order.status == "pending"
    assert (await db.execute(select(BillingTransaction))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("request_before_partial", [False, True])
async def test_user_refund_after_partial_only_requests_remaining_cash(
    db, monkeypatch, request_before_partial
):
    user, order = await paid_order(db, f"partial-review-{request_before_partial}")
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    gateway = AsyncMock()
    gateway.refund.side_effect = lambda **kwargs: RefundResult(
        success=True,
        amount_cents=kwargs["amount_cents"],
        status="succeeded",
        refund_id=kwargs["idempotency_key"],
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    service = PaymentService(db)
    request = (
        await service.submit_refund_request(order.id, user.id)
        if request_before_partial
        else None
    )
    await service.process_refund(
        order.id, amount_cents=30, operator_id=user.id, idempotency_key="partial:review"
    )
    await db.commit()
    assert order.refunded_amount_cents == 30
    if request is None:
        request = await service.submit_refund_request(order.id, user.id)
        assert request.amount_cents == 70
    approved = await service.approve_refund_request(request.id, reviewer_id=user.id)
    await db.commit()
    assert approved.status == "approved"
    assert approved.amount_cents == 70
    assert order.refunded_amount_cents == 100
    assert order.status == "refunded"
    assert [call.kwargs["amount_cents"] for call in gateway.refund.await_args_list] == [
        30,
        70,
    ]
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]
    attempts = (await db.execute(select(PaymentRefundAttempt))).scalars().all()
    assert sorted(row.amount_cents for row in attempts) == [30, 70]
    assert [
        row.delta_units
        for row in (
            await db.execute(select(BillingTransaction).order_by(BillingTransaction.id))
        )
        .scalars()
        .all()
    ] == [5_000_000, -1_500_000, -3_500_000]


@pytest.mark.asyncio
async def test_plan_explicit_null_clears_limit_and_omission_preserves_it(db):
    service = PaymentService(db)
    plan = await service.create_plan("Editable", "one_time", 100, concurrency_limit=3)
    await db.commit()
    await service.update_plan(plan.id, name="Renamed", description=None)
    await db.commit()
    await db.refresh(plan)
    assert plan.concurrency_limit == 3
    assert plan.name == "Renamed"
    await service.update_plan(plan.id, concurrency_limit=None)
    await db.commit()
    await db.refresh(plan)
    assert plan.concurrency_limit is None


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["JPY", "USD", "USDT", "EUR"])
async def test_alipay_rejects_non_cny_before_signing(currency):
    gateway = AlipayGateway("test-app", "test-private-key")
    with patch.object(
        AlipayGateway, "_sign_with_rsa2", return_value="test-sign"
    ) as sign:
        result = await gateway.create_payment(
            "test-order",
            500,
            currency,
            "Test",
            1,
            "https://example.test/notify",
            "https://example.test/return",
        )
    assert not result.success
    assert "CNY" in result.error_message
    assert not result.checkout_url
    sign.assert_not_called()


@pytest.mark.asyncio
async def test_alipay_cny_exact_amount_survives_currency_validation():
    gateway = AlipayGateway("test-app", "test-private-key")
    with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="test-sign"):
        result = await gateway.create_payment(
            "test-order",
            501,
            "cny",
            "Test",
            1,
            "https://example.test/notify",
            "https://example.test/return",
        )
    assert result.success
    content = json.loads(
        parse_qs(urlparse(result.checkout_url).query)["biz_content"][0]
    )
    assert content["total_amount"] == "5.01"


@pytest.mark.asyncio
async def test_existing_refund_attempt_keeps_amount_and_is_not_requested_twice(
    db, monkeypatch
):
    user, order = await paid_order(db, "unknown-refund-review")
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=True, status="pending", refund_id="re_pending_review", amount_cents=100
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    service = PaymentService(db)
    request = await service.submit_refund_request(order.id, user.id)
    result = await service.approve_refund_request(request.id, reviewer_id=user.id)
    await db.commit()
    assert result.status == "failed"
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    original_identity = (attempt.id, attempt.idempotency_key, attempt.amount_cents)
    assert attempt.status == "unknown"
    result = await service.approve_refund_request(request.id, reviewer_id=user.id)
    await db.commit()
    assert result.status == "failed"
    assert (
        attempt.id,
        attempt.idempotency_key,
        attempt.amount_cents,
    ) == original_identity
    assert gateway.refund.await_count == 1
    wallet = await db.get(BillingWallet, user.id)
    assert wallet.reserved_units == 5_000_000


@pytest.mark.asyncio
async def test_fully_refunded_stale_request_never_creates_zero_refund(db, monkeypatch):
    user, order = await paid_order(db, "zero-review")
    service = PaymentService(db)
    request = await service.submit_refund_request(order.id, user.id)
    order.payment_provider = "manual"
    await service.process_refund(
        order.id, operator_id=user.id, idempotency_key="other-full"
    )
    await db.commit()
    gateway_factory = AsyncMock()
    monkeypatch.setattr("backend.services.payment.get_gateway", gateway_factory)
    result = await service.approve_refund_request(request.id, reviewer_id=user.id)
    await db.commit()
    assert result.status == "failed"
    assert "No refundable amount" in result.error_message
    assert len((await db.execute(select(PaymentRefundAttempt))).scalars().all()) == 1
    gateway_factory.assert_not_awaited()
    assert (await db.get(BillingWallet, user.id)).balance_units == 0


@pytest.mark.asyncio
async def test_submit_rejects_inconsistent_fulfilled_order_with_no_remaining_cash(db):
    user, order = await paid_order(db, "fully-refunded-inconsistent")
    order.refunded_amount_cents = order.amount_cents
    await db.commit()
    with pytest.raises(PaymentError, match="No refundable amount"):
        await PaymentService(db).submit_refund_request(order.id, user.id)
    assert (await db.execute(select(RefundRequest))).scalars().all() == []


@pytest.mark.asyncio
async def test_alipay_invalid_saved_currency_never_creates_mislabeled_checkout(
    db, monkeypatch
):
    user = TelegramUser(github_username="wrong-alipay-currency")
    db.add(user)
    await db.flush()
    service = PaymentService(db)
    plan = await service.create_plan(
        "JPY sample", "one_time", 500, currency="JPY", credit_grant="5"
    )
    monkeypatch.setattr(
        service, "_get_provider_currency", AsyncMock(return_value="JPY")
    )
    gateway = AsyncMock()
    gateway.create_payment.return_value = PaymentIntentResult(
        success=True, checkout_url="https://example.test/pay", provider_tx_id="test"
    )
    factory = AsyncMock(return_value=gateway)
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    with pytest.raises(PaymentError) as failure:
        await service.create_order(user.id, plan.id, provider="alipay")
    assert failure.value.code == "invalid_currency"
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_alipay_non_cny_historical_checkout_refund_requires_review(
    db, monkeypatch
):
    user, order = await paid_order(db, "wrong-alipay-refund")
    order.payment_provider = "alipay"
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 100, "gateway_currency": "JPY"}
    )
    await db.commit()
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=True, status="succeeded", refund_id="test-wrong-cur", amount_cents=100
    )
    factory = AsyncMock(return_value=gateway)
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    with pytest.raises(PaymentError) as failure:
        await PaymentService(db).process_refund(order.id, operator_id=user.id)
    assert failure.value.code == "invalid_currency"
    assert (await db.get(BillingWallet, user.id)).reserved_units == 0
    assert (await db.execute(select(PaymentRefundAttempt))).scalars().all() == []
    factory.assert_not_awaited()
