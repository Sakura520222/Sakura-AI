"""Refunds received before checkout and outside local intents remain source-linked."""

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql, postgresql
from starlette.requests import Request

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    PaymentRefundInboxAudit,
    PaymentRefundInboxEvent,
    PaymentRefundReference,
)
from backend.models.payment_models import Plan, UserSubscription
from backend.models.telegram_models import TelegramUser
from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
from backend.services.payment_event_service import PaymentEventService
from backend.services.payment_service import PaymentService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_review_stripe_refund_lifecycle import signed_refund

db = real_database_fixture


async def pending_checkout(db, name, *, plan=None, user=None, provider="stripe"):
    if user is None:
        user = TelegramUser(github_username=name, role="super_admin")
        db.add(user)
    if plan is None:
        plan = Plan(
            name="Ordering offer",
            plan_type="one_time",
            price_cents=100,
            credit_grant=Decimal(5),
        )
        db.add(plan)
    await db.flush()
    order = await PaymentService(db).create_order(user.id, plan.id)
    order.payment_provider = provider
    order.provider_tx_id = "cs_" + name
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 100, "gateway_currency": "CNY"}
    )
    await db.commit()
    return user, plan, order


def checkout_event(order, name):
    return WebhookEvent(
        event_type=WebhookEventType.PAYMENT_COMPLETED,
        provider_tx_id=order.provider_tx_id,
        payment_reference_id=(
            "pi_" + name if order.payment_provider == "stripe" else order.provider_tx_id
        ),
        order_no=order.order_no,
        amount_cents=100,
        currency="CNY",
        event_id="evt_paid_" + name,
    )


def early_refund(order, name, status, event_id, **changes):
    # The dashboard refund has no application metadata and only knows the PI.
    shadow = deepcopy(order)
    shadow.metadata_json = json.dumps({"payment_reference_id": "pi_" + name})
    changes.setdefault("metadata", {})
    return signed_refund(shadow, status, event_id, **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_first", [False, True])
async def test_checkout_reference_replays_earlier_native_stripe_refunds_once(
    db, monkeypatch, pending_first
):
    gateway_factory = AsyncMock()
    monkeypatch.setattr("backend.services.payment.get_gateway", gateway_factory)
    user, _, order = await pending_checkout(db, "early-refund")
    service = PaymentEventService(db)
    pending = None
    if pending_first:
        event, payload_hash = early_refund(
            order, "early-refund", "pending", "evt_pending_early"
        )
        pending = await service.accept("stripe", event, payload_hash=payload_hash)
        await db.commit()
    event, payload_hash = early_refund(
        order, "early-refund", "succeeded", "evt_terminal_early"
    )
    terminal = await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    assert terminal.status == "pending_reconciliation"
    original = deepcopy(terminal.evidence)
    paid = checkout_event(order, "early-refund")
    assert (
        await service.accept("stripe", paid, payload_hash="paid")
    ).status == "processed"
    await db.commit()
    await db.refresh(terminal)
    assert terminal.status == "processed"
    assert terminal.evidence == original
    assert terminal.order_id == order.id
    assert order.status == "refunded"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    if pending:
        await db.refresh(pending)
        assert pending.status == "processed"
        audit = (
            await db.execute(
                select(PaymentRefundInboxAudit).where(
                    PaymentRefundInboxAudit.inbox_event_id == pending.id,
                    PaymentRefundInboxAudit.status == "superseded",
                )
            )
        ).scalar_one()
        assert audit.evidence["terminal_event_id"] == terminal.id
    await service.accept("stripe", paid, payload_hash="paid")
    await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2
    gateway_factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("redelivery", [False, True])
async def test_webhook_drains_busy_receipt_after_payment_commit_and_on_redelivery(
    db, monkeypatch, redelivery
):
    from backend.api.webhook import handle_stripe_webhook

    user, _, order = await pending_checkout(db, "busy-refund")
    service = PaymentEventService(db)
    event, payload_hash = early_refund(
        order, "busy-refund", "succeeded", "evt_busy_terminal"
    )
    terminal = await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    original_query = PaymentEventService._stripe_refund_replay_query
    lock_modes = []

    def busy_inline_query(*args, skip_locked, event_id=None):
        query = original_query(*args, skip_locked=skip_locked, event_id=event_id)
        lock_modes.append(skip_locked)
        if skip_locked:
            # Another request owns this receipt until the payment commit.
            return query.where(PaymentRefundInboxEvent.id == -1)
        assert order.status in {"fulfilled", "refunded"}
        assert not db._session.new
        return query

    monkeypatch.setattr(
        PaymentEventService,
        "_stripe_refund_replay_query",
        staticmethod(busy_inline_query),
    )
    paid = checkout_event(order, "busy-refund")
    if redelivery:
        await service.accept("stripe", paid, payload_hash="paid-before-crash")
        await db.commit()
        assert order.status == "fulfilled"
        assert terminal.status == "pending_reconciliation"
        lock_modes.clear()
    gateway = AsyncMock()
    gateway.verify_webhook = lambda *_: paid
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )

    @asynccontextmanager
    async def session_context():
        yield db

    monkeypatch.setattr("backend.api.webhook.get_async_session", session_context)

    async def receive():
        return {
            "type": "http.request",
            "body": b"verified-test-payment",
            "more_body": False,
        }

    response = await handle_stripe_webhook(
        Request(
            {"type": "http", "method": "POST", "path": "/stripe", "headers": []},
            receive,
        )
    )
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "processed"
    if redelivery:
        assert lock_modes and all(skip is False for skip in lock_modes)
    else:
        assert lock_modes[:2] == [True, False]
    await db.refresh(terminal)
    assert terminal.status == "processed"
    assert order.status == "refunded"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    gateway.refund.assert_not_awaited()
    # A crash after the payment commit must also be recoverable on its redelivery.
    # Replaying a processed payment uses the post-commit drain without reissuing credits.
    await service.accept("stripe", paid, payload_hash="paid-redelivery")
    await db.commit()
    await service.replay_stripe_refunds_after_commit(
        (
            await db.execute(
                select(PaymentRefundInboxEvent).where(
                    PaymentRefundInboxEvent.event_key == paid.event_id
                )
            )
        )
        .scalar_one()
        .id
    )
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_two_early_partial_refunds_replay_cumulatively(db, monkeypatch):
    monkeypatch.setattr(
        "backend.services.payment_event_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    user, _, order = await pending_checkout(db, "partial-early")
    service = PaymentEventService(db)
    for index, amount in enumerate((30, 70)):
        event, payload_hash = early_refund(
            order,
            "partial-early",
            "succeeded",
            f"evt_partial_{index}",
            id=f"re_partial_{index}",
            amount=amount,
        )
        assert (
            await service.accept("stripe", event, payload_hash=payload_hash)
        ).status == "pending_reconciliation"
        await db.commit()
    paid = await service.accept(
        "stripe", checkout_event(order, "partial-early"), payload_hash="paid"
    )
    await db.commit()
    assert paid.status == "processed"
    assert order.refunded_amount_cents == 100
    assert order.status == "refunded"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    references = (
        (
            await db.execute(
                select(PaymentRefundReference).order_by(PaymentRefundReference.id)
            )
        )
        .scalars()
        .all()
    )
    assert [ref.amount_cents for ref in references] == [30, 70]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict", ["currency", "amount", "reference", "provider", "order"]
)
async def test_reference_replay_preserves_unrelated_or_conflicting_refunds_pending(
    db, conflict
):
    user, _, order = await pending_checkout(db, "isolated-refund")
    changes = {}
    provider = "stripe"
    if conflict == "currency":
        changes["currency"] = "usd"
    elif conflict == "amount":
        changes["amount"] = 101
    elif conflict == "reference":
        changes["payment_intent"] = "pi_other"
    elif conflict == "provider":
        provider = "paddle"
    else:
        changes["metadata"] = {"order_no": "foreign-order"}
    # Override the empty dashboard metadata explicitly for the ownership counterexample.
    event, payload_hash = early_refund(
        order, "isolated-refund", "succeeded", "evt_conflict", **changes
    )
    service = PaymentEventService(db)
    receipt = await service.accept(provider, event, payload_hash=payload_hash)
    await db.commit()
    original = deepcopy(receipt.evidence)
    paid_receipt = await service.accept(
        "stripe", checkout_event(order, "isolated-refund"), payload_hash="paid"
    )
    await db.commit()
    await service.replay_stripe_refunds_after_commit(paid_receipt.id)
    await db.refresh(receipt)
    assert receipt.status == "pending_reconciliation"
    assert receipt.evidence == original
    assert order.status == "fulfilled"
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []


@pytest.mark.parametrize("dialect", [mysql.dialect(), postgresql.dialect()])
def test_reference_replay_uses_current_locking_reads(dialect):
    for skip in (False, True):
        query = PaymentEventService._stripe_refund_replay_query(
            1, "order", "pi", 0, 3, skip_locked=skip, event_id=2
        )
        sql = str(query.compile(dialect=dialect))
        assert "FOR UPDATE" in sql
        assert ("SKIP LOCKED" in sql) is skip
        assert query.get_execution_options()["populate_existing"] is True
    discovery = PaymentEventService._stripe_refund_replay_query(
        1, "order", "pi", 0, 3, skip_locked=True
    )
    assert "FOR UPDATE" not in str(discovery.compile(dialect=dialect))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["stripe", "paddle"])
@pytest.mark.parametrize("periods", [1, 2])
async def test_external_full_refund_reconciles_remaining_same_plan_periods(
    db, provider, periods
):
    plan = Plan(
        name="Subscribed offer",
        plan_type="subscription",
        duration_days=30,
        price_cents=100,
        credit_grant=Decimal(5),
        pr_quota_bonus=3,
    )
    db.add(plan)
    await db.flush()
    user, _, first = await pending_checkout(
        db, "sub-first", plan=plan, provider=provider
    )
    service = PaymentEventService(db)
    await service.accept(
        provider, checkout_event(first, "sub-first"), payload_hash="first"
    )
    await db.commit()
    first_end = (
        (
            await db.execute(
                select(LegacyEntitlement).where(LegacyEntitlement.order_id == first.id)
            )
        )
        .scalar_one()
        .expires_at
    )
    refunded = first
    if periods == 2:
        _, _, refunded = await pending_checkout(
            db, "sub-second", plan=plan, user=user, provider=provider
        )
        await service.accept(
            provider, checkout_event(refunded, "sub-second"), payload_hash="second"
        )
        await db.commit()
    refund = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=refunded.order_no,
        currency="CNY",
        event_id="evt_external_sub_refund",
        refund_items=[
            {
                "id": "re_subscription",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
        original_amount_cents=100,
    )
    assert (
        await service.accept(provider, refund, payload_hash="refund")
    ).status == "processed"
    await db.commit()
    subscription = (
        await db.execute(
            select(UserSubscription).where(
                UserSubscription.user_id == user.id, UserSubscription.plan_id == plan.id
            )
        )
    ).scalar_one()
    active = await PaymentService(db).get_active_subscription(user.id)
    if periods == 1:
        assert active is None
        assert subscription.status == "expired"
    else:
        assert active.id == subscription.id
        assert subscription.expires_at == first_end
    assert refunded.status == "refunded"
    assert (
        await db.execute(
            select(LegacyEntitlement).where(LegacyEntitlement.order_id == refunded.id)
        )
    ).scalar_one().revoked_at is not None
    assert (await db.get(BillingWallet, user.id)).balance_units == (
        periods - 1
    ) * 5_000_000
