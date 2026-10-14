"""Alipay refund identity includes its trade and distinct out_request_no."""

import json
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import (
    PaymentRefundAttempt,
    PaymentRefundAttemptEvent,
    PaymentRefundInboxEvent,
    PaymentRefundReference,
)
from backend.services.billing_service import BillingService
from backend.services.payment.alipay_gateway import AlipayGateway
from backend.services.payment.gateway_base import WebhookEventType
from backend.services.payment_event_service import PaymentEventService
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order
from tests.test_billing_payment_events import (
    proportional_policy as proportional_fixture,
)

proportional_policy = proportional_fixture
from tests.test_review_refund_reconciliation_identity import _state

db = real_database_fixture


async def _alipay_order(db, name):
    user, order = await paid_order(db, name)
    order.payment_provider = "alipay"
    order.provider_tx_id = "2026101300001"
    await db.commit()
    return user, order


def _offline_gateway(monkeypatch):
    gateway = AlipayGateway("offline-app", "offline-key", "offline-public")
    monkeypatch.setattr(gateway, "_sign_with_rsa2", lambda *args: "offline-signature")
    monkeypatch.setattr(gateway, "_verify_rsa2", lambda *args: True)
    requests = []

    async def respond(params):
        wire = json.loads(params["biz_content"])
        requests.append(wire)
        return {
            "alipay_trade_refund_response": {
                "code": "10000",
                "trade_no": wire["trade_no"],
                "fund_change": "Y",
            }
        }

    monkeypatch.setattr(gateway, "_post", respond)
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    return gateway, requests


def _notification(gateway, order, request_no, total, event_id):
    values = {
        "trade_no": order.provider_tx_id,
        "out_trade_no": order.order_no,
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "1.00",
        "refund_fee": total,
        "notify_id": event_id,
        "sign": "offline-signature",
    }
    if request_no is not None:
        values["out_biz_no"] = request_no
    return gateway.verify_webhook(urlencode(values).encode(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_request", [False, True])
async def test_second_alipay_partial_can_reconcile_after_local_interruption(
    db, monkeypatch, explicit_request
):
    user, order = await _alipay_order(db, "alipay-distinct-request")
    gateway, requests = _offline_gateway(monkeypatch)
    service = PaymentService(db)
    await service.process_refund(
        order.id, amount_cents=30, operator_id=user.id, idempotency_key="alipay:first"
    )
    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            service,
            "_finalize_refund",
            AsyncMock(side_effect=RuntimeError("local interruption")),
        )
        with pytest.raises(RuntimeError, match="local interruption"):
            await service.process_refund(
                order.id,
                amount_cents=30,
                operator_id=user.id,
                idempotency_key="alipay:second",
            )
    await db.rollback()
    attempts = (
        (
            await db.execute(
                select(PaymentRefundAttempt).order_by(PaymentRefundAttempt.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.status for row in attempts] == ["succeeded", "upstream_succeeded"]
    await service.reconcile_refund(
        attempts[1].id,
        operator_id=user.id,
        upstream_status="refunded",
        evidence="Provider confirms the second distinct out_request_no",
        provider_refund_id="alipay:second" if explicit_request else None,
    )
    await db.commit()
    assert [wire["out_request_no"] for wire in requests] == [
        "alipay:first",
        "alipay:second",
    ]
    assert attempts[0].provider_refund_id != attempts[1].provider_refund_id
    assert all(row.provider_refund_id != order.provider_tx_id for row in attempts)
    assert order.refunded_amount_cents == 60
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (2_000_000, 0)
    inbox = PaymentEventService(db)
    for key, total in (("alipay:second", "0.60"), ("alipay:first", "0.30")):
        event = _notification(gateway, order, key, total, f"callback:{key}")
        assert event.event_type == WebhookEventType.PAYMENT_REFUNDED
        assert (
            await inbox.accept("alipay", event, payload_hash=key)
        ).status == "processed"
        await db.commit()
    assert order.refunded_amount_cents == 60
    assert (await db.get(BillingWallet, user.id)).balance_units == 2_000_000
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 2
    assert (
        await db.execute(
            select(BillingTransaction.delta_units).order_by(BillingTransaction.id)
        )
    ).scalars().all() == [5_000_000, -1_500_000, -1_500_000]


@pytest.mark.asyncio
async def test_same_alipay_request_number_in_different_trades_has_distinct_identity(
    monkeypatch,
):
    gateway, requests = _offline_gateway(monkeypatch)
    first = await gateway.refund("trade-one", 30, idempotency_key="same-request")
    second = await gateway.refund("trade-two", 30, idempotency_key="same-request")
    assert first.success and second.success
    assert [row["out_request_no"] for row in requests] == [
        "same-request",
        "same-request",
    ]
    assert first.refund_id != second.refund_id
    repeated = await gateway.refund("trade-one", 30, idempotency_key="same-request")
    assert repeated.refund_id == first.refund_id


@pytest.mark.asyncio
async def test_long_local_refund_key_has_a_stable_protocol_request_number(monkeypatch):
    gateway, requests = _offline_gateway(monkeypatch)
    first = await gateway.refund("trade-one", 30, idempotency_key="r" * 160)
    second = await gateway.refund("trade-one", 30, idempotency_key="r" * 160)
    assert first.success and second.success
    assert len(requests[0]["out_request_no"]) <= 64
    assert requests[0]["out_request_no"] == requests[1]["out_request_no"]
    assert first.refund_id == second.refund_id


@pytest.mark.parametrize(
    "trade_status,expected",
    [
        ("TRADE_SUCCESS", WebhookEventType.PAYMENT_COMPLETED),
        ("TRADE_CLOSED", WebhookEventType.PAYMENT_EXPIRED),
    ],
)
def test_zero_refund_field_preserves_ordinary_alipay_payment_status(
    monkeypatch, trade_status, expected
):
    gateway, _ = _offline_gateway(monkeypatch)
    event = gateway.verify_webhook(
        urlencode(
            {
                "trade_no": "2026101300001",
                "out_trade_no": "ordinary-order",
                "trade_status": trade_status,
                "total_amount": "1.00",
                "refund_fee": "0.00",
                "sign": "offline-signature",
            }
        ).encode(),
        {},
    )
    assert event.event_type == expected


async def _legacy_attempt(
    db, user, order, *, trusted_intent=True, confirmed_upstream=True
):
    key = "legacy-alipay-request"
    purchase = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.order_id == order.id)
        )
    ).scalar_one()
    attempt = PaymentRefundAttempt(
        order_id=order.id,
        active_order_id=order.id,
        idempotency_key=key,
        amount_cents=30,
        currency="CNY",
        gateway_amount_cents=30,
        gateway_currency="CNY",
        status="upstream_succeeded" if confirmed_upstream else "unknown",
        provider_refund_id=order.provider_tx_id if confirmed_upstream else None,
        credit_transaction_id=purchase.id,
        credit_hold_units=1_500_000,
        actor_id=user.id,
        evidence={"requested_total_cents": 30, "full": False},
    )
    db.add(attempt)
    await db.flush()
    if trusted_intent:
        db.add(
            PaymentRefundAttemptEvent(
                attempt_id=attempt.id,
                event_key=f"{key}:intent",
                status="pending",
                actor_id=user.id,
                evidence={"requested_total_cents": 30, "full": False},
            )
        )
    if confirmed_upstream:
        db.add(
            PaymentRefundAttemptEvent(
                attempt_id=attempt.id,
                event_key=f"{key}:upstream",
                status="upstream_succeeded",
                actor_id=user.id,
                evidence={"provider_refund_id": order.provider_tx_id},
            )
        )
    await BillingService(db).hold_purchase_refund(
        purchase.id, f"{key}:hold", units=1_500_000
    )
    await db.commit()
    return attempt


@pytest.mark.asyncio
async def test_legacy_trade_number_can_recover_with_audited_exact_request_number(
    db, monkeypatch
):
    user, order = await _alipay_order(db, "legacy-alipay-recovery")
    gateway, _ = _offline_gateway(monkeypatch)
    attempt = await _legacy_attempt(db, user, order)
    await PaymentService(db).reconcile_refund(
        attempt.id,
        operator_id=user.id,
        upstream_status="refunded",
        evidence="Verified request-specific provider query: request legacy-alipay-request, amount 0.30 CNY",
        provider_refund_id="legacy-alipay-request",
    )
    await db.commit()
    assert attempt.provider_refund_id != order.provider_tx_id
    event = _notification(
        gateway, order, "legacy-alipay-request", "0.30", "legacy-delayed"
    )
    assert (
        await PaymentEventService(db).accept(
            "alipay", event, payload_hash="verified-legacy"
        )
    ).status == "processed"
    await db.commit()
    assert order.refunded_amount_cents == 30
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 0)
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trusted_intent,request_number",
    [(True, None), (False, "legacy-alipay-request"), (True, "different-request")],
)
async def test_ambiguous_legacy_alipay_identity_cannot_mutate_reconciliation(
    db, trusted_intent, request_number
):
    user, order = await _alipay_order(db, "legacy-alipay-ambiguous")
    attempt = await _legacy_attempt(db, user, order, trusted_intent=trusted_intent)
    before = await _state(db, order, attempt)
    with pytest.raises(PaymentError):
        await PaymentService(db).reconcile_refund(
            attempt.id,
            operator_id=user.id,
            upstream_status="refunded",
            evidence="Unbound provider history",
            provider_refund_id=request_number,
        )
    await db.commit()
    assert await _state(db, order, attempt) == before


@pytest.mark.asyncio
async def test_verified_alipay_callback_recovers_exact_legacy_request_once(
    db, monkeypatch
):
    user, order = await _alipay_order(db, "legacy-alipay-callback")
    gateway, _ = _offline_gateway(monkeypatch)
    attempt = await _legacy_attempt(db, user, order)
    inbox = PaymentEventService(db)
    event = _notification(
        gateway, order, "legacy-alipay-request", "0.30", "legacy-native"
    )
    assert event.event_type == WebhookEventType.PAYMENT_REFUNDED
    record = await inbox.accept("alipay", event, payload_hash="verified-native")
    await db.commit()
    assert record.status == "processed"
    assert attempt.status == "succeeded"
    assert attempt.provider_refund_id != order.provider_tx_id
    assert (await inbox.replay(record.id)).status == "processed"
    assert (
        await inbox.accept("alipay", event, payload_hash="verified-native")
    ).id == record.id
    await db.commit()
    assert order.refunded_amount_cents == 30
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 0)
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("request_number", [None, "unbound-native-request"])
async def test_cumulative_only_alipay_notification_keeps_financial_state_pending(
    db, monkeypatch, request_number
):
    user, order = await _alipay_order(db, "cumulative-alipay")
    gateway, _ = _offline_gateway(monkeypatch)
    event = _notification(gateway, order, request_number, "0.30", "cumulative-only")
    assert event.event_type == WebhookEventType.PAYMENT_REFUNDED
    record = await PaymentEventService(db).accept(
        "alipay", event, payload_hash="verified-cumulative"
    )
    await db.commit()
    assert record.status == "pending_reconciliation"
    assert order.refunded_amount_cents == 0
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 0)
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_intent", [False, True])
async def test_reviewed_alipay_native_request_and_amount_match_later_callback(
    db, monkeypatch, legacy_intent
):
    user, order = await _alipay_order(db, "reviewed-alipay-native")
    gateway, _ = _offline_gateway(monkeypatch)
    attempt = await _legacy_attempt(db, user, order) if legacy_intent else None
    request_no = "legacy-alipay-request" if legacy_intent else "reviewed-native-request"
    inbox = PaymentEventService(db)
    record = await inbox.accept(
        "alipay",
        _notification(gateway, order, None, "0.30", "reviewed-missing-request"),
        payload_hash="verified-incomplete",
    )
    await db.commit()
    assert record.status == "pending_reconciliation"
    reviewed = await inbox.resolve(
        record.id,
        operator_id=user.id,
        evidence="Provider query confirms this exact out_request_no refunded 0.30 CNY",
        refund_reference_id=request_no,
        refund_amount_cents=30,
        refund_currency="CNY",
    )
    await db.commit()
    assert reviewed.status == "processed"
    if attempt is not None:
        await db.refresh(attempt)
        assert attempt.status == "succeeded"
        assert attempt.active_order_id is None
    assert (
        await inbox.accept(
            "alipay",
            _notification(gateway, order, request_no, "0.30", "reviewed-native-late"),
            payload_hash="verified-review-late",
        )
    ).status == "processed"
    await db.commit()
    assert order.refunded_amount_cents == 30
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 0)
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cumulative", ["0.20", "0.60"])
async def test_alipay_native_callback_requires_exact_staged_amount(
    db, monkeypatch, cumulative
):
    user, order = await _alipay_order(db, "alipay-native-mismatch")
    gateway, _ = _offline_gateway(monkeypatch)
    attempt = await _legacy_attempt(db, user, order)
    before = await _state(db, order, attempt)
    record = await PaymentEventService(db).accept(
        "alipay",
        _notification(
            gateway, order, "legacy-alipay-request", cumulative, "amount-mismatch"
        ),
        payload_hash="verified-amount-mismatch",
    )
    await db.commit()
    assert record.status == "pending_reconciliation"
    assert record.pending_reason == "refund_intent_mismatch"
    assert await _state(db, order, attempt) == before


@pytest.mark.asyncio
async def test_legacy_payment_id_financial_reference_needs_mapping_before_native_replay(
    db, monkeypatch
):
    user, order = await _alipay_order(db, "alipay-legacy-financial-ref")
    gateway, _ = _offline_gateway(monkeypatch)
    purchase = (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.order_id == order.id)
        )
    ).scalar_one()
    reversal = await BillingService(db).reverse_external_purchase(
        purchase.id,
        1_500_000,
        "legacy-financial-ref",
        reason="Historical verified refund",
        snapshot={"provider": "alipay", "refund_id": order.provider_tx_id},
    )
    order.refunded_amount_cents = 30
    order.metadata_json = json.dumps(
        {
            "gateway_amount_cents": 100,
            "gateway_currency": "CNY",
            "refunded_gateway_amount_cents": 30,
        }
    )
    previous = PaymentRefundInboxEvent(
        provider="alipay",
        event_key="old-financial-event",
        status="processed",
        order_id=order.id,
        evidence={"type": "payment_refunded"},
    )
    db.add(previous)
    await db.flush()
    db.add(
        PaymentRefundReference(
            provider="alipay",
            reference_id=order.provider_tx_id,
            order_id=order.id,
            amount_cents=30,
            currency="CNY",
            ledger_transaction_id=reversal.id,
            inbox_event_id=previous.id,
        )
    )
    await db.commit()
    inbox = PaymentEventService(db)
    record = await inbox.accept(
        "alipay",
        _notification(gateway, order, "old-native-request", "0.30", "old-native-late"),
        payload_hash="verified-old-native",
    )
    await db.commit()
    assert record.status == "pending_reconciliation"
    reviewed = await inbox.resolve(
        record.id,
        operator_id=user.id,
        evidence="Request-specific provider query; old trade-only financial mapping is unresolved",
        refund_reference_id="old-native-request",
        refund_amount_cents=30,
        refund_currency="CNY",
    )
    await db.commit()
    assert reviewed.status == "pending_reconciliation"
    assert order.refunded_amount_cents == 30
    assert (await db.get(BillingWallet, user.id)).balance_units == 3_500_000
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2


async def _settled_legacy_attempt(db, user, order):
    attempt = await _legacy_attempt(db, user, order)
    await BillingService(db).finalize_purchase_refund(
        attempt.credit_transaction_id,
        attempt.credit_hold_units,
        f"{attempt.idempotency_key}:hold",
        f"{attempt.idempotency_key}:ledger",
        actor_id=user.id,
        reason="Historical verified order refund",
    )
    order.refunded_amount_cents = 30
    metadata = json.loads(order.metadata_json)
    metadata["refunded_gateway_amount_cents"] = 30
    order.metadata_json = json.dumps(metadata)
    attempt.status = "succeeded"
    attempt.active_order_id = None
    db.add(
        PaymentRefundAttemptEvent(
            attempt_id=attempt.id,
            event_key=f"{attempt.idempotency_key}:settled",
            status="succeeded",
            actor_id=user.id,
            evidence={"refunded_total_cents": 30},
        )
    )
    await db.commit()
    assert (await db.get(BillingWallet, user.id)).balance_units == 3_500_000
    assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []
    return attempt


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["canonical", "request_number"])
async def test_reviewed_identity_cannot_repeat_a_settled_trade_only_alipay_refund(
    db, monkeypatch, reference_kind
):
    user, order = await _alipay_order(db, "settled-legacy-alipay")
    gateway, _ = _offline_gateway(monkeypatch)
    attempt = await _settled_legacy_attempt(db, user, order)
    before = await _state(db, order, attempt)
    canonical = gateway.refund_reference_id(
        order.provider_tx_id, attempt.idempotency_key
    )
    inbox = PaymentEventService(db)
    record = await inbox.accept(
        "alipay",
        _notification(gateway, order, None, "0.30", "settled-legacy-missing-request"),
        payload_hash="verified-settled-legacy",
    )
    await db.commit()
    assert record.status == "pending_reconciliation"
    reviewed = await inbox.resolve(
        record.id,
        operator_id=user.id,
        evidence="Provider query confirms the existing 0.30 CNY refund",
        refund_reference_id=canonical
        if reference_kind == "canonical"
        else attempt.idempotency_key,
        refund_amount_cents=30,
        refund_currency="CNY",
    )
    await db.commit()
    wallet = await db.get(BillingWallet, user.id)
    deltas = (
        (
            await db.execute(
                select(BillingTransaction.delta_units).order_by(BillingTransaction.id)
            )
        )
        .scalars()
        .all()
    )
    assert (
        order.refunded_amount_cents,
        wallet.balance_units,
        wallet.reserved_units,
        deltas,
    ) == (
        30,
        3_500_000,
        0,
        [5_000_000, -1_500_000],
    )
    if reference_kind == "canonical":
        assert reviewed.status == "pending_reconciliation"
        assert reviewed.pending_reason == "refund_evidence_incomplete"
        assert await _state(db, order, attempt) == before
    else:
        assert reviewed.status == "processed"
        assert attempt.provider_refund_id == canonical
    # Native request evidence can resolve a rejected canonical-only review;
    # neither receiving it nor replaying the earlier event repeats the refund.
    native = _notification(
        gateway, order, attempt.idempotency_key, "0.30", "settled-legacy-native"
    )
    assert (
        await inbox.accept("alipay", native, payload_hash="verified-native-settled")
    ).status == "processed"
    await db.commit()
    assert (await inbox.replay(record.id)).status == "processed"
    await db.commit()
    assert order.refunded_amount_cents == 30
    assert (await db.get(BillingWallet, user.id)).balance_units == 3_500_000
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1


async def _unconfirmed_alipay_attempt(db, monkeypatch):
    user, order = await _alipay_order(db, "qualified-pending-alipay")
    gateway, _ = _offline_gateway(monkeypatch)
    monkeypatch.setattr(
        gateway, "_post", AsyncMock(side_effect=TimeoutError("offline timeout"))
    )
    with pytest.raises(PaymentError) as failure:
        await PaymentService(db).process_refund(
            order.id,
            amount_cents=30,
            operator_id=user.id,
            idempotency_key="qualified-pending-request",
        )
    assert failure.value.code == "refund_reconciliation_required"
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    assert attempt.provider_refund_id is None
    assert attempt.status == "unknown"
    assert (await db.get(BillingWallet, user.id)).reserved_units == 1_500_000
    return user, order, gateway, attempt


@pytest.mark.asyncio
async def test_canonical_review_matches_durable_alipay_request_proof_before_api_confirmation(
    db, monkeypatch
):
    user, order, gateway, attempt = await _unconfirmed_alipay_attempt(db, monkeypatch)
    canonical = gateway.refund_reference_id(
        order.provider_tx_id, attempt.idempotency_key
    )
    inbox = PaymentEventService(db)
    record = await inbox.accept(
        "alipay",
        _notification(gateway, order, None, "0.30", "pending-qualified-review"),
        payload_hash="verified-pending-qualified",
    )
    await db.commit()
    reviewed = await inbox.resolve(
        record.id,
        operator_id=user.id,
        evidence="Provider confirms the exact previously staged native request refunded 0.30 CNY",
        refund_reference_id=canonical,
        refund_amount_cents=30,
        refund_currency="CNY",
    )
    await db.commit()
    assert reviewed.status == "processed"
    await db.refresh(attempt)
    assert attempt.status == "succeeded"
    assert attempt.provider_refund_id == canonical
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 0)
    assert order.refunded_amount_cents == 30
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["canonical", "request_number"])
async def test_separate_alipay_refund_keeps_original_request_hold_until_its_native_outcome(
    db, monkeypatch, reference_kind
):
    user, order, gateway, attempt = await _unconfirmed_alipay_attempt(db, monkeypatch)
    separate_request = "separate-native-request"
    reference = gateway.refund_reference_id(order.provider_tx_id, separate_request)
    inbox = PaymentEventService(db)
    record = await inbox.accept(
        "alipay",
        _notification(gateway, order, None, "0.30", "separate-reviewed-refund"),
        payload_hash="verified-separate-refund",
    )
    await db.commit()
    reviewed = await inbox.resolve(
        record.id,
        operator_id=user.id,
        evidence="Provider confirms a distinct request refunded 0.30 CNY",
        refund_reference_id=reference
        if reference_kind == "canonical"
        else separate_request,
        refund_amount_cents=30,
        refund_currency="CNY",
    )
    await db.commit()
    assert reviewed.status == "processed"
    await db.refresh(attempt)
    assert attempt.status == "unknown"
    assert attempt.provider_refund_id is None
    assert attempt.active_order_id == order.id
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 1_500_000)
    assert order.refunded_amount_cents == 30
    native = _notification(
        gateway, order, attempt.idempotency_key, "0.60", "original-request-outcome"
    )
    assert (
        await inbox.accept("alipay", native, payload_hash="verified-original-outcome")
    ).status == "processed"
    await db.commit()
    assert attempt.status == "succeeded"
    assert attempt.provider_refund_id != reference
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (2_000_000, 0)
    assert order.refunded_amount_cents == 60
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 3
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["canonical", "request_number"])
async def test_legacy_unconfirmed_alipay_request_needs_raw_identity_before_hold_settlement(
    db, monkeypatch, reference_kind
):
    user, order = await _alipay_order(db, "unconfirmed-legacy-alipay")
    gateway, _ = _offline_gateway(monkeypatch)
    attempt = await _legacy_attempt(db, user, order, confirmed_upstream=False)
    before = await _state(db, order, attempt)
    canonical = gateway.refund_reference_id(
        order.provider_tx_id, attempt.idempotency_key
    )
    inbox = PaymentEventService(db)
    record = await inbox.accept(
        "alipay",
        _notification(gateway, order, None, "0.30", "unconfirmed-legacy-review"),
        payload_hash="verified-unconfirmed-legacy",
    )
    await db.commit()
    reviewed = await inbox.resolve(
        record.id,
        operator_id=user.id,
        evidence="Provider query confirms the existing legacy native request refunded 0.30 CNY",
        refund_reference_id=canonical
        if reference_kind == "canonical"
        else attempt.idempotency_key,
        refund_amount_cents=30,
        refund_currency="CNY",
    )
    await db.commit()
    if reference_kind == "canonical":
        assert reviewed.status == "pending_reconciliation"
        assert await _state(db, order, attempt) == before
    else:
        assert reviewed.status == "processed"
        assert attempt.status == "succeeded"
        assert attempt.provider_refund_id == canonical
        wallet = await db.get(BillingWallet, user.id)
        assert (wallet.balance_units, wallet.reserved_units) == (3_500_000, 0)
        assert order.refunded_amount_cents == 30
