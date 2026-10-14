"""External refund evidence cannot override the configured partial-refund policy."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.models.billing_models import (
    BillingCreditDebt,
    BillingTransaction,
    BillingWallet,
)
from backend.models.legacy_entitlement_models import PaymentRefundReference
from backend.models.payment_models import Plan
from backend.services import payment_event_service
from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
from backend.services.payment_event_service import PaymentEventService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order

db = real_database_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["stripe", "paddle"])
@pytest.mark.parametrize("policy", ["reject", "proportional_unused_credits"])
async def test_dashboard_partial_refund_obeys_policy_before_any_financial_effect(
    db, monkeypatch, provider, policy
):
    user, order = await paid_order(db, "policy-owner")
    order.payment_provider = provider
    await db.commit()
    monkeypatch.setattr(
        payment_event_service,
        "get_dynamic_config",
        AsyncMock(return_value=policy),
        raising=False,
    )
    refund = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="evt_partial_policy",
        refund_items=[
            {
                "id": "re_partial_policy",
                "amount_cents": 30,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
        original_amount_cents=100,
    )
    service = PaymentEventService(db)
    receipt = await service.accept(provider, refund, payload_hash="partial")
    await db.commit()
    if policy == "reject":
        assert receipt.status == "pending_reconciliation"
        assert receipt.pending_reason == "partial_refund_unsupported"
        assert order.refunded_amount_cents == 0
        assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
        assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
        assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []
        assert (await db.execute(select(BillingCreditDebt))).scalars().all() == []
        monkeypatch.setattr(
            payment_event_service,
            "get_dynamic_config",
            AsyncMock(return_value="proportional_unused_credits"),
        )
        receipt = await service.replay(receipt.id)
        await db.commit()
    assert receipt.status == "processed"
    assert order.refunded_amount_cents == 30
    assert (await db.get(BillingWallet, user.id)).balance_units == 3_500_000
    await service.accept(provider, refund, payload_hash="partial")
    await db.commit()
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_reject_policy_still_accepts_atomic_full_refund_bundle_for_mixed_offer(
    db, monkeypatch
):
    from decimal import Decimal

    from tests.test_review_payment_ordering_and_subscriptions import (
        checkout_event,
        pending_checkout,
    )

    plan = Plan(
        name="Mixed full refund",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
        pr_quota_bonus=3,
    )
    db.add(plan)
    user, _, order = await pending_checkout(db, "full-bundle", plan=plan)
    service = PaymentEventService(db)
    await service.accept(
        "stripe", checkout_event(order, "full-bundle"), payload_hash="paid"
    )
    await db.commit()
    monkeypatch.setattr(
        payment_event_service, "get_dynamic_config", AsyncMock(return_value="reject")
    )
    refund = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        order_no=order.order_no,
        currency="CNY",
        event_id="evt_full_bundle",
        refund_items=[
            {
                "id": f"re_bundle_{amount}",
                "amount_cents": amount,
                "currency": "CNY",
                "status": "succeeded",
            }
            for amount in (30, 70)
        ],
        refund_evidence_complete=True,
        original_amount_cents=100,
    )
    received = await service.accept("stripe", refund, payload_hash="bundle")
    await db.commit()
    assert received.status == "processed"
    assert order.status == "refunded"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
