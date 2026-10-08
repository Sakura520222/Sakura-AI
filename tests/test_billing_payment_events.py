"""Verified received cash events never invoke payment/refund APIs a second time."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.models.billing_models import (
    BillingCreditDebt,
    BillingTransaction,
    BillingWallet,
)
from backend.models.legacy_entitlement_models import (
    PaymentRefundInboxEvent,
    PaymentRefundReference,
)
from backend.models.payment_models import Plan
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import BillingService
from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
from backend.services.payment_event_service import PaymentEventService
from backend.services.payment_service import PaymentService
from tests.test_billing_entitlements import db as real_database_fixture

db = real_database_fixture


async def paid_order(db, name):
    user = TelegramUser(github_username=name, role="super_admin")
    plan = Plan(
        name="TEST verified cash",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    order = await PaymentService(db).create_order(user.id, plan.id)
    order.payment_provider = "stripe"
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 100, "gateway_currency": "CNY"}
    )
    await db.commit()
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_COMPLETED,
        provider_tx_id=f"payment:{name}",
        order_no=order.order_no,
        amount_cents=100,
        currency="CNY",
        event_id=f"paid:{name}",
    )
    received = await PaymentEventService(db).accept(
        "stripe", event, payload_hash="payment"
    )
    await db.commit()
    assert received.status == "processed"
    return user, order


@pytest.mark.asyncio
async def test_paid_receipt_and_refund_are_idempotent_without_second_gateway_request(
    db, monkeypatch
):
    gateway_factory = AsyncMock()
    monkeypatch.setattr("backend.services.payment.get_gateway", gateway_factory)
    user, order = await paid_order(db, "inbound-owner")
    refund = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="refund-event",
        refund_items=[
            {
                "id": "re_one",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        original_amount_cents=100,
        refund_evidence_complete=True,
    )
    svc = PaymentEventService(db)
    received = await svc.accept("stripe", refund, payload_hash="refund")
    await db.commit()
    assert received.status == "processed"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert order.status == "refunded"
    assert (await svc.accept("stripe", refund, payload_hash="refund")).id == received.id
    await db.commit()
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2
    gateway_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_charge_refund_and_refund_updated_reordering_share_financial_ref_identity(
    db,
):
    user, order = await paid_order(db, "reordered-owner")
    svc = PaymentEventService(db)
    item = {
        "id": "re_first",
        "amount_cents": 30,
        "currency": "CNY",
        "status": "succeeded",
    }
    charge = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="charge-cumulative",
        refund_items=[item],
        original_amount_cents=100,
        refund_total_cents=30,
        refund_evidence_complete=True,
    )
    assert (
        await svc.accept("stripe", charge, payload_hash="charge")
    ).status == "processed"
    await db.commit()
    update = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="refund-updated",
        refund_items=[item],
        refund_evidence_complete=True,
    )
    assert (
        await svc.accept("stripe", update, payload_hash="update")
    ).status == "processed"
    await db.commit()
    assert order.refunded_amount_cents == 30
    assert (await db.get(BillingWallet, user.id)).balance_units == 3_500_000
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_unknown_order_is_durable_and_reviewed_replay_grants_once(db):
    actor = TelegramUser(github_username="inbox-operator", role="super_admin")
    user = TelegramUser(github_username="inbox-user")
    plan = Plan(
        name="TEST replay",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(actor)
    db.add(user)
    db.add(plan)
    await db.flush()
    order = await PaymentService(db).create_order(user.id, plan.id)
    order.payment_provider = "stripe"
    await db.commit()
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_COMPLETED,
        provider_tx_id="cs_unknown",
        order_no="no-original-mapping",
        amount_cents=100,
        currency="CNY",
        event_id="unknown-paid",
    )
    service = PaymentEventService(db)
    received = await service.accept("stripe", event, payload_hash="unknown")
    await db.commit()
    assert received.status == "pending_reconciliation"
    assert (await db.execute(select(BillingTransaction))).scalars().all() == []
    assert (await service.replay(received.id)).status == "pending_reconciliation"
    assert (
        await service.resolve(
            received.id,
            operator_id=actor.id,
            evidence="Verified merchant checkout mapping",
            order_id=order.id,
        )
    ).status == "processed"
    await db.commit()
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert (await service.replay(received.id)).status == "processed"
    assert len((await db.execute(select(PaymentRefundInboxEvent))).scalars().all()) == 1
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_external_refund_of_spent_credits_records_debt_keeps_other_lots(db):
    user, order = await paid_order(db, "external-spent")
    billing = BillingService(db)
    await billing.adjust(
        user.id,
        "-3",
        idempotency_key="spend-old",
        actor_id=user.id,
        reason="Test actual source allocation",
    )
    await billing.grant(user.id, "10", "separate-source")
    await db.commit()
    refund = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="spent-refund",
        refund_items=[
            {
                "id": "re_spent",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
    )
    record = await PaymentEventService(db).accept(
        "stripe", refund, payload_hash="spent"
    )
    await db.commit()
    assert record.status == "processed"
    assert (await db.get(BillingWallet, user.id)).balance_units == 7_000_000
    debts = (await db.execute(select(BillingCreditDebt))).scalars().all()
    assert sum(debt.outstanding_units for debt in debts) == 3_000_000
    assert (await billing.reconcile_wallet(user.id))["consistent"]


@pytest.mark.asyncio
async def test_provider_pending_refund_waits_then_callback_finishes_original_hold(
    db, monkeypatch
):
    from backend.models.legacy_entitlement_models import PaymentRefundAttempt
    from backend.services.payment.gateway_base import RefundResult
    from backend.services.payment_service import PaymentError

    user, order = await paid_order(db, "held-inbound")
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=True, refund_id="re_pending", amount_cents=100, status="pending"
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    with pytest.raises(PaymentError, match="reconciliation"):
        await PaymentService(db).process_refund(order.id, operator_id=user.id)
    await db.rollback()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    assert attempt.status == "unknown"
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 5_000_000)
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="confirmed-held",
        refund_items=[
            {
                "id": "re_pending",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
    )
    assert (
        await PaymentEventService(db).accept("stripe", event, payload_hash="confirmed")
    ).status == "processed"
    await db.commit()
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (0, 0)
    assert order.status == "refunded"
    assert gateway.refund.await_count == 1


@pytest.mark.asyncio
async def test_checkout_snapshot_missing_keeps_received_refund_pending_then_reviewed_money_replays(
    db,
):
    user, order = await paid_order(db, "checkout-review")
    order.metadata_json = None
    await db.commit()
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="legacy-checkout-refund",
        refund_items=[
            {
                "id": "re_reviewed",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
    )
    service = PaymentEventService(db)
    record = await service.accept("stripe", event, payload_hash="reviewed")
    await db.commit()
    assert record.status == "pending_reconciliation"
    assert record.pending_reason == "checkout_snapshot_required"
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    record = await service.resolve(
        record.id,
        operator_id=user.id,
        evidence="Verified original captured amount and currency",
        order_id=order.id,
        checkout_amount_cents=100,
        checkout_currency="CNY",
    )
    await db.commit()
    assert record.status == "processed"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_status", ["succeeded", "pending"])
async def test_refund_callback_before_api_return_keeps_settled_attempt(
    db, monkeypatch, gateway_status
):
    from sqlalchemy.orm import Session

    from backend.models.legacy_entitlement_models import (
        PaymentRefundAttempt,
        PaymentRefundAttemptEvent,
    )
    from backend.services.payment.gateway_base import RefundResult
    from tests.test_billing_entitlements import _AsyncSQLiteSession

    user, order = await paid_order(db, f"callback-race-{gateway_status}")
    gateway = AsyncMock()

    async def refund_with_earlier_callback(**_kwargs):
        callback_db = _AsyncSQLiteSession(
            Session(db._session.get_bind(), expire_on_commit=False)
        )
        try:
            event = WebhookEvent(
                event_type=WebhookEventType.PAYMENT_REFUNDED,
                order_no=order.order_no,
                currency="CNY",
                event_id="earlier-callback",
                refund_items=[
                    {
                        "id": "re_race",
                        "amount_cents": 100,
                        "currency": "CNY",
                        "status": "succeeded",
                    }
                ],
                refund_evidence_complete=True,
            )
            assert (
                await PaymentEventService(callback_db).accept(
                    "stripe", event, payload_hash="earlier-callback"
                )
            ).status == "processed"
            await callback_db.commit()
        finally:
            callback_db._session.close()
        return RefundResult(
            success=True, refund_id="re_race", amount_cents=100, status=gateway_status
        )

    gateway.refund.side_effect = refund_with_earlier_callback
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    result = await PaymentService(db).process_refund(order.id, operator_id=user.id)
    await db.commit()
    db._session.expire_all()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    settled = (
        (
            await db.execute(
                select(PaymentRefundAttemptEvent).where(
                    PaymentRefundAttemptEvent.status == "succeeded"
                )
            )
        )
        .scalars()
        .all()
    )
    assert result.status == "refunded"
    assert attempt.status == "succeeded"
    assert len(settled) == 1
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert gateway.refund.await_count == 1


@pytest.mark.asyncio
async def test_reviewed_receipt_accepts_original_redelivery_and_rejects_changed_evidence(
    db,
):
    from dataclasses import replace

    from backend.services.payment_service import PaymentError

    user, order = await paid_order(db, "reviewed-redelivery")
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no="unmapped-original",
        currency="CNY",
        event_id="reviewed-redelivery",
        refund_items=[],
        refund_evidence_complete=False,
    )
    service = PaymentEventService(db)
    receipt = await service.accept("stripe", event, payload_hash="original")
    await db.commit()
    assert receipt.status == "pending_reconciliation"
    receipt = await service.resolve(
        receipt.id,
        operator_id=user.id,
        evidence="Provider source independently verified",
        order_id=order.id,
        refund_reference_id="re_review",
        refund_amount_cents=100,
        refund_currency="CNY",
    )
    await db.commit()
    assert receipt.status == "processed"
    repeated = await service.accept("stripe", event, payload_hash="original")
    assert repeated.id == receipt.id
    assert repeated.status == "processed"
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1
    with pytest.raises(PaymentError, match="conflicts"):
        await service.accept(
            "stripe", replace(event, currency="USD"), payload_hash="changed"
        )


@pytest.mark.asyncio
async def test_stripe_refund_payment_intent_matches_authoritative_checkout_reference(
    db,
):
    user, order = await paid_order(db, "native-reference")
    metadata = json.loads(order.metadata_json)
    metadata["payment_reference_id"] = "pi_checkout_owner"
    order.metadata_json = json.dumps(metadata)
    await db.commit()
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        provider_tx_id="re_native",
        payment_reference_id="pi_checkout_owner",
        currency="CNY",
        event_id="native-reference-refund",
        refund_items=[
            {
                "id": "re_native",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
    )
    receipt = await PaymentEventService(db).accept(
        "stripe", event, payload_hash="native-reference"
    )
    await db.commit()
    assert receipt.status == "processed"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0


@pytest.mark.asyncio
async def test_refund_order_number_cannot_override_conflicting_native_payment_reference(
    db,
):
    user, order = await paid_order(db, "wrong-native-reference")
    metadata = json.loads(order.metadata_json)
    metadata["payment_reference_id"] = "pi_checkout_owner"
    order.metadata_json = json.dumps(metadata)
    await db.commit()
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        provider_tx_id="re_other",
        payment_reference_id="pi_other_owner",
        currency="CNY",
        event_id="wrong-native-reference",
        refund_items=[
            {
                "id": "re_other",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
    )
    receipt = await PaymentEventService(db).accept(
        "stripe", event, payload_hash="wrong-reference"
    )
    await db.commit()
    assert receipt.status == "pending_reconciliation"
    assert receipt.pending_reason == "payment_reference_mismatch"
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "currency", "amount"),
    [
        ("refund.updated", "ZZZ", 100),
        ("refund.updated", "ISK", 125),
        ("checkout.session.completed", "CNY", "100.25"),
    ],
)
async def test_signed_financial_amount_error_is_durable_before_ack(
    db, monkeypatch, event_type, currency, amount
):
    import hashlib
    import hmac
    from contextlib import asynccontextmanager

    from starlette.requests import Request

    from backend.api.webhook import handle_stripe_webhook
    from backend.core.time_service import now_utc
    from backend.services.payment.stripe_gateway import StripeGateway

    body = json.dumps(
        {
            "id": "evt_unresolved_units",
            "type": event_type,
            "data": {
                "object": {
                    "id": "re_or_cs_verified",
                    "payment_intent": "pi_verified",
                    "amount": amount,
                    "amount_total": amount,
                    "currency": currency,
                    "status": "succeeded",
                    "payment_status": "paid",
                    "metadata": {},
                }
            },
        }
    ).encode()
    timestamp = str(int(now_utc().timestamp()))
    signature = hmac.new(
        b"whsec_local_test", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/stripe",
            "headers": [
                (b"stripe-signature", f"t={timestamp},v1={signature}".encode())
            ],
            "query_string": b"",
        },
        receive,
    )
    gateway = StripeGateway("sk_test_local", "whsec_local_test")
    gateway.refund = AsyncMock(
        side_effect=AssertionError("Incoming evidence must not send money")
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )

    @asynccontextmanager
    async def session_context():
        yield db

    monkeypatch.setattr("backend.api.webhook.get_async_session", session_context)
    response = await handle_stripe_webhook(request)
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "pending_reconciliation"
    receipt = (await db.execute(select(PaymentRefundInboxEvent))).scalar_one()
    assert receipt.pending_reason == "provider_amount_requires_review"
    assert receipt.evidence["amount_cents"] is None
    assert receipt.evidence["wire_evidence"]["amount"] == amount
    assert receipt.evidence["wire_evidence"]["currency"] == currency
    assert (await db.execute(select(BillingTransaction))).scalars().all() == []
    gateway.refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_wire_units_require_reviewed_canonical_refund_before_replay(
    db,
):
    user, order = await paid_order(db, "reviewed-wire-units")
    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        currency="ZZZ",
        amount_cents=None,
        event_id="reviewed-wire-units",
        normalization_error="provider_amount_requires_review",
        wire_evidence={"amount": 100, "currency": "ZZZ"},
    )
    service = PaymentEventService(db)
    receipt = await service.accept("stripe", event, payload_hash="wire-original")
    await db.commit()
    reviewed = await service.resolve(
        receipt.id,
        operator_id=user.id,
        evidence="Ownership checked; original units still unresolved",
        order_id=order.id,
    )
    assert reviewed.status == "pending_reconciliation"
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    reviewed = await service.resolve(
        receipt.id,
        operator_id=user.id,
        evidence="Verified actual CNY refund from provider source",
        order_id=order.id,
        refund_reference_id="re_wire_reviewed",
        refund_amount_cents=100,
        refund_currency="CNY",
    )
    await db.commit()
    assert reviewed.status == "processed"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert (
        await service.accept("stripe", event, payload_hash="wire-original")
    ).id == receipt.id


def signed_native_request(
    provider, amount, currency, *, refund=False, secret="local-native-secret"
):
    """Sign test-only evidence locally; no payment provider is contacted."""
    import hashlib
    import hmac
    from urllib.parse import urlencode

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from starlette.requests import Request

    from backend.core.time_service import now_utc
    from backend.services.payment.alipay_gateway import AlipayGateway
    from backend.services.payment.nowpayments_gateway import NowPaymentsGateway
    from backend.services.payment.paddle_gateway import PaddleGateway
    from backend.services.payment.stripe_gateway import StripeGateway

    timestamp = str(int(now_utc().timestamp()))
    if provider == "nowpayments":
        data = {
            "payment_id": "payment-local",
            "order_id": "signed-local-order",
            "payment_status": "refunded" if refund else "finished",
            "price_amount": amount,
            "price_currency": currency,
        }
        body = json.dumps(data).encode()
        canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        signature = hmac.new(secret.encode(), canonical, hashlib.sha512).hexdigest()
        headers = [(b"x-nowpayments-sig", signature.encode())]
        gateway = NowPaymentsGateway("test-local", secret)
    elif provider == "paddle":
        data = {
            "event_id": "evt-local-paddle",
            "event_type": "adjustment.updated" if refund else "transaction.completed",
            "data": {
                "id": "txn-or-refund-local",
                "currency_code": currency,
                "custom_data": {"order_no": "signed-local-order"},
                "details": {"totals": {"total": amount}},
                "totals": {"total": amount},
                "action": "refund",
                "status": "approved",
                "transaction_id": "txn-local",
            },
        }
        body = json.dumps(data).encode()
        signature = hmac.new(
            secret.encode(), timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
        headers = [(b"paddle-signature", f"ts={timestamp};h1={signature}".encode())]
        gateway = PaddleGateway("test-local", secret)
    elif provider == "stripe":
        data = {
            "id": "evt-local-stripe",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs-local",
                    "amount_total": amount,
                    "currency": currency,
                    "payment_status": "paid",
                    "metadata": {"order_no": "signed-local-order"},
                }
            },
        }
        body = json.dumps(data).encode()
        signature = hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        headers = [(b"stripe-signature", f"t={timestamp},v1={signature}".encode())]
        gateway = StripeGateway("test-local", secret)
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public_pem = (
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        data = {
            "trade_no": "trade-local",
            "out_trade_no": "signed-local-order",
            "trade_status": "TRADE_SUCCESS",
            "notify_id": "notify-local",
        }
        if amount is not None:
            data["total_amount"] = amount
        data["sign"] = AlipayGateway._sign_with_rsa2(data, private_pem)
        data["sign_type"] = "RSA2"
        body = urlencode(data).encode()
        headers = [(b"content-type", b"application/x-www-form-urlencoded")]
        gateway = AlipayGateway("test-local", private_pem, alipay_public_key=public_pem)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return gateway, Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/{provider}",
            "query_string": b"",
            "headers": headers,
        },
        receive,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["nowpayments", "paddle", "stripe"])
async def test_missing_verification_secret_never_persists_financial_receipt(
    db, monkeypatch, provider
):
    from contextlib import asynccontextmanager

    from backend.api.webhook import _handle_payment_webhook

    gateway, request = signed_native_request(provider, 100, "USD", secret="")
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )

    @asynccontextmanager
    async def session_context():
        yield db

    monkeypatch.setattr("backend.api.webhook.get_async_session", session_context)
    response = await _handle_payment_webhook(request, provider)
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "ignored"
    assert (await db.execute(select(PaymentRefundInboxEvent))).scalars().all() == []
    assert (await db.execute(select(BillingTransaction))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "amount", "currency", "refund"),
    [
        ("nowpayments", "100", "ZZZ", False),
        ("nowpayments", "100", None, True),
        ("nowpayments", "not-money", "USD", False),
        ("nowpayments", "1.001", "USD", False),
        ("paddle", "not-money", "USD", True),
        ("paddle", 100.5, "USD", False),
        ("paddle", "100", None, False),
        ("alipay", "NaN", "CNY", False),
        ("alipay", "1.001", "CNY", False),
        ("alipay", None, "CNY", False),
    ],
)
async def test_verified_native_money_normalization_error_has_durable_original_units(
    db, monkeypatch, provider, amount, currency, refund
):
    from contextlib import asynccontextmanager

    from backend.api.webhook import _handle_payment_webhook

    gateway, request = signed_native_request(provider, amount, currency, refund=refund)
    gateway.refund = AsyncMock(
        side_effect=AssertionError("Receiving evidence must not send money")
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )

    @asynccontextmanager
    async def session_context():
        yield db

    monkeypatch.setattr("backend.api.webhook.get_async_session", session_context)
    response = await _handle_payment_webhook(request, provider)
    assert response.status_code == 200
    if provider == "alipay":
        assert response.body == b"success"
    else:
        assert json.loads(response.body)["status"] == "pending_reconciliation"
    receipt = (await db.execute(select(PaymentRefundInboxEvent))).scalar_one()
    assert receipt.status == "pending_reconciliation"
    assert receipt.pending_reason == "provider_amount_requires_review"
    assert receipt.evidence["amount_cents"] is None
    assert receipt.evidence["wire_evidence"]["amount"] == amount
    assert receipt.evidence["wire_evidence"]["currency"] == currency
    assert (await db.execute(select(BillingTransaction))).scalars().all() == []
    gateway.refund.assert_not_awaited()
