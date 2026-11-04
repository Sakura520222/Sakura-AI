"""Native Stripe refund transitions close prior receipts without new effects."""

import hashlib
import hmac
import json
from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql, postgresql

from backend.core.time_service import now_utc
from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import (
    PaymentRefundInboxAudit,
    PaymentRefundInboxEvent,
    PaymentRefundReference,
)
from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
from backend.services.payment.stripe_gateway import StripeGateway
from backend.services.payment_event_service import PaymentEventService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order

db = real_database_fixture


async def stripe_order(db, name):
    user, order = await paid_order(db, name)
    metadata = json.loads(order.metadata_json)
    metadata["payment_reference_id"] = "pi_" + name
    order.metadata_json = json.dumps(metadata)
    await db.commit()
    return user, order


def signed_refund(order, status, event_id, *, event_type=None, **changes):
    native = {
        "id": "re_lifecycle",
        "payment_intent": json.loads(order.metadata_json)["payment_reference_id"],
        "amount": 100,
        "currency": "cny",
        "status": status,
        "metadata": {"order_no": order.order_no},
    }
    native.update(changes)
    body = json.dumps(
        {
            "id": event_id,
            "type": event_type
            or (
                "refund.created"
                if status in {"pending", "requires_action"}
                else "refund.updated"
            ),
            "data": {"object": native},
        }
    ).encode()
    timestamp = str(int(now_utc().timestamp()))
    secret = "local-stripe-refund-secret"
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    event = StripeGateway("local-test-key", secret).verify_webhook(
        body, {"stripe-signature": f"t={timestamp},v1={signature}"}
    )
    return event, hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_status", ["pending", "requires_action"])
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "canceled"])
@pytest.mark.parametrize("terminal_first", [False, True])
async def test_native_terminal_refund_supersedes_pending_and_preserves_audit(
    db, pending_status, terminal_status, terminal_first
):
    user, order = await stripe_order(db, "stripe-lifecycle")
    service = PaymentEventService(db)
    pending_event, pending_hash = signed_refund(order, pending_status, "evt_pending")
    terminal_event, terminal_hash = signed_refund(
        order, terminal_status, "evt_terminal"
    )
    if terminal_first:
        terminal = await service.accept(
            "stripe", terminal_event, payload_hash=terminal_hash
        )
        await db.commit()
    pending = await service.accept("stripe", pending_event, payload_hash=pending_hash)
    await db.commit()
    original = deepcopy(pending.evidence)
    received = (
        await db.execute(
            select(PaymentRefundInboxAudit).where(
                PaymentRefundInboxAudit.inbox_event_id == pending.id,
                PaymentRefundInboxAudit.status == "received",
            )
        )
    ).scalar_one()
    original_received = deepcopy(received.evidence)
    if not terminal_first:
        assert pending.status == "pending_reconciliation"
        await service.resolve(
            pending.id,
            operator_id=user.id,
            evidence="Verified refund awaiting native terminal status",
        )
        await db.commit()
        original = deepcopy(pending.evidence)
        terminal = await service.accept(
            "stripe", terminal_event, payload_hash=terminal_hash
        )
        await db.commit()
    await db.refresh(pending)
    assert terminal.status == pending.status == "processed"
    assert pending.pending_reason is None
    assert pending.evidence == original
    assert received.evidence == original_received
    await service.accept("stripe", pending_event, payload_hash=pending_hash)
    await service.accept("stripe", terminal_event, payload_hash=terminal_hash)
    await service.replay(pending.id)
    await db.commit()
    audits = (
        (
            await db.execute(
                select(PaymentRefundInboxAudit).where(
                    PaymentRefundInboxAudit.inbox_event_id == pending.id,
                    PaymentRefundInboxAudit.status == "superseded",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].evidence["terminal_event_id"] == terminal.id
    assert audits[0].evidence["refund_reference_id"] == "re_lifecycle"
    succeeded = terminal_status == "succeeded"
    assert (await db.get(BillingWallet, user.id)).balance_units == (
        0 if succeeded else 5_000_000
    )
    assert len(
        (await db.execute(select(PaymentRefundReference))).scalars().all()
    ) == int(succeeded)
    assert len(
        (await db.execute(select(BillingTransaction))).scalars().all()
    ) == 1 + int(succeeded)
    assert pending.id not in [row.id for row in await service.list_pending()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"id": "re_other"},
        {"amount": 99},
        {"currency": "usd"},
        {"payment_intent": "pi_other"},
        {"metadata": {"order_no": "other-order"}},
    ],
)
async def test_terminal_refund_keeps_conflicting_pending_evidence(db, changes):
    _, order = await stripe_order(db, "stripe-conflict")
    service = PaymentEventService(db)
    event, payload_hash = signed_refund(order, "pending", "evt_conflict", **changes)
    pending = await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    event, payload_hash = signed_refund(order, "succeeded", "evt_terminal")
    assert (
        await service.accept("stripe", event, payload_hash=payload_hash)
    ).status == "processed"
    await db.commit()
    await db.refresh(pending)
    assert pending.status == "pending_reconciliation"
    assert (
        await db.execute(
            select(PaymentRefundInboxAudit).where(
                PaymentRefundInboxAudit.inbox_event_id == pending.id,
                PaymentRefundInboxAudit.status == "superseded",
            )
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_current_terminal_read_skips_busy_receipt_and_replay_resolves(
    db, monkeypatch
):
    user, order = await stripe_order(db, "stripe-current")
    service = PaymentEventService(db)
    event, payload_hash = signed_refund(order, "succeeded", "evt_terminal")
    await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    original_execute = db.execute
    lookups = []

    async def skip_terminal(statement, *args, **kwargs):
        compiled = statement.compile(dialect=mysql.dialect())
        if (
            "payment_refund_inbox_events" in str(compiled)
            and compiled.params.get("status_1") == "processed"
        ):
            lookups.append(statement)
            return await original_execute(
                statement.where(PaymentRefundInboxEvent.id == -1)
            )
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", skip_terminal)
    event, payload_hash = signed_refund(order, "pending", "evt_pending")
    pending = await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    assert pending.status == "pending_reconciliation"
    assert len(lookups) == 1
    for dialect in (mysql.dialect(), postgresql.dialect()):
        assert "FOR UPDATE SKIP LOCKED" in str(lookups[0].compile(dialect=dialect))
    monkeypatch.setattr(db, "execute", original_execute)
    assert (await service.replay(pending.id)).status == "processed"
    await db.commit()
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["refund.failed", "charge.refund.updated"])
async def test_other_native_terminal_event_types_close_pending_receipt(db, event_type):
    user, order = await stripe_order(db, "stripe-terminal-type")
    service = PaymentEventService(db)
    event, payload_hash = signed_refund(order, "pending", "evt_pending")
    pending = await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    status = "failed" if event_type == "refund.failed" else "succeeded"
    event, payload_hash = signed_refund(
        order, status, "evt_terminal", event_type=event_type
    )
    terminal = await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    await db.refresh(pending)
    assert terminal.status == pending.status == "processed"
    assert (await db.get(BillingWallet, user.id)).balance_units == (
        5_000_000 if status == "failed" else 0
    )


@pytest.mark.asyncio
async def test_cumulative_charge_terminal_matches_each_native_refund(db):
    user, order = await stripe_order(db, "stripe-charge")
    service = PaymentEventService(db)
    pending_rows = []
    for reference, amount in (("re_part_one", 30), ("re_part_two", 70)):
        event, payload_hash = signed_refund(
            order, "pending", f"evt_{reference}", id=reference, amount=amount
        )
        pending_rows.append(
            await service.accept("stripe", event, payload_hash=payload_hash)
        )
        await db.commit()
    charge = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        event_id="evt_charge_refunded",
        provider_tx_id="ch_charge",
        order_no=order.order_no,
        payment_reference_id=json.loads(order.metadata_json)["payment_reference_id"],
        currency="CNY",
        original_amount_cents=100,
        refund_total_cents=100,
        refund_evidence_complete=True,
        wire_evidence={"event_type": "charge.refunded"},
        refund_items=[
            {
                "id": reference,
                "amount_cents": amount,
                "currency": "CNY",
                "status": "succeeded",
            }
            for reference, amount in (("re_part_one", 30), ("re_part_two", 70))
        ],
    )
    terminal = await service.accept("stripe", charge, payload_hash="charge-hash")
    await db.commit()
    assert terminal.status == "processed"
    for pending in pending_rows:
        await db.refresh(pending)
        assert pending.status == "processed"
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 2
    assert (await db.get(BillingWallet, user.id)).balance_units == 0


@pytest.mark.asyncio
async def test_failed_terminal_cannot_override_successful_refund(db):
    _, order = await stripe_order(db, "stripe-outcome-conflict")
    service = PaymentEventService(db)
    event, payload_hash = signed_refund(order, "succeeded", "evt_succeeded")
    assert (
        await service.accept("stripe", event, payload_hash=payload_hash)
    ).status == "processed"
    await db.commit()
    event, payload_hash = signed_refund(order, "failed", "evt_failed")
    conflicting = await service.accept("stripe", event, payload_hash=payload_hash)
    await db.commit()
    assert conflicting.status == "pending_reconciliation"
    assert conflicting.pending_reason == "refund_outcome_conflict"
