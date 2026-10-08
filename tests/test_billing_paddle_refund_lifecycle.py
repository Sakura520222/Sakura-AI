"""Verified Paddle adjustment lifecycle receipts close without double refunds."""

import hashlib
import hmac
import json
from copy import deepcopy
from decimal import Decimal

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
from backend.models.payment_models import Plan
from backend.models.telegram_models import TelegramUser
from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
from backend.services.payment.paddle_gateway import PaddleGateway
from backend.services.payment_event_service import PaymentEventService
from backend.services.payment_service import PaymentService
from tests.test_billing_entitlements import db as real_database_fixture

db = real_database_fixture


async def paddle_paid_order(db, name):
    user = TelegramUser(github_username=name, role="super_admin")
    plan = Plan(
        name="TEST Paddle purchased snapshot",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    order = await PaymentService(db).create_order(user.id, plan.id)
    order.payment_provider = "paddle"
    order.provider_tx_id = "txn_" + name
    order.metadata_json = json.dumps(
        {
            "gateway_amount_cents": 100,
            "gateway_currency": "CNY",
            "payment_reference_id": order.provider_tx_id,
        }
    )
    await db.commit()
    receipt = await PaymentEventService(db).accept(
        "paddle",
        WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider_tx_id=order.provider_tx_id,
            payment_reference_id=order.provider_tx_id,
            order_no=order.order_no,
            amount_cents=100,
            currency="CNY",
            event_id="paid_" + name,
        ),
        payload_hash="paid",
    )
    await db.commit()
    assert receipt.status == "processed"
    return user, order


def signed_adjustment(
    order,
    status,
    *,
    event_id,
    adjustment_id="adj_test",
    amount=100,
    currency="CNY",
    transaction_id=None,
    order_no=None,
):
    secret = "test-webhook-secret"
    body = {
        "event_id": event_id,
        "event_type": "adjustment.created"
        if status == "pending_approval"
        else "adjustment.updated",
        "data": {
            "id": adjustment_id,
            "action": "refund",
            "status": status,
            "transaction_id": transaction_id or order.provider_tx_id,
            "currency_code": currency,
            "totals": {"total": str(amount)},
            "custom_data": {"order_no": order_no or order.order_no},
        },
    }
    payload = json.dumps(body).encode()
    timestamp = str(int(now_utc().timestamp()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b":" + payload, hashlib.sha256
    ).hexdigest()
    event = PaddleGateway("test-key", secret).verify_webhook(
        payload, {"paddle-signature": f"ts={timestamp};h1={signature}"}
    )
    assert event.event_type == WebhookEventType.PAYMENT_REFUNDED
    return event, hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["approved", "rejected"])
@pytest.mark.parametrize("terminal_first", [False, True])
async def test_terminal_paddle_event_closes_matching_pending_receipt_without_losing_evidence(
    db, terminal_status, terminal_first
):
    user, order = await paddle_paid_order(db, "paddle-lifecycle")
    service = PaymentEventService(db)
    pending_event, pending_hash = signed_adjustment(
        order, "pending_approval", event_id="evt_pending"
    )
    terminal_event, terminal_hash = signed_adjustment(
        order, terminal_status, event_id="evt_terminal"
    )
    if terminal_first:
        terminal = await service.accept(
            "paddle", terminal_event, payload_hash=terminal_hash
        )
        await db.commit()
    pending = await service.accept("paddle", pending_event, payload_hash=pending_hash)
    await db.commit()
    if not terminal_first:
        await service.resolve(
            pending.id,
            operator_id=user.id,
            evidence="Confirmed adjustment is awaiting Paddle approval",
        )
        await db.commit()
    original_evidence = deepcopy(pending.evidence)
    received_audit = (
        await db.execute(
            select(PaymentRefundInboxAudit).where(
                PaymentRefundInboxAudit.inbox_event_id == pending.id,
                PaymentRefundInboxAudit.status == "received",
            )
        )
    ).scalar_one()
    original_received = deepcopy(received_audit.evidence)
    reviewed_audits = (
        (
            await db.execute(
                select(PaymentRefundInboxAudit).where(
                    PaymentRefundInboxAudit.inbox_event_id == pending.id,
                    PaymentRefundInboxAudit.status == "reviewed",
                )
            )
        )
        .scalars()
        .all()
    )
    original_reviewed = [deepcopy(row.evidence) for row in reviewed_audits]
    if not terminal_first:
        assert pending.status == "pending_reconciliation"
        terminal = await service.accept(
            "paddle", terminal_event, payload_hash=terminal_hash
        )
        await db.commit()
    assert terminal.status == "processed"
    await db.refresh(pending)
    assert pending.status == "processed"
    assert pending.pending_reason is None
    assert pending.evidence == original_evidence
    assert received_audit.evidence == original_received
    assert [row.evidence for row in reviewed_audits] == original_reviewed
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
    assert audits[0].evidence["refund_reference_id"] == "adj_test"
    await service.accept("paddle", pending_event, payload_hash=pending_hash)
    await service.accept("paddle", terminal_event, payload_hash=terminal_hash)
    await service.replay(pending.id)
    await db.commit()
    wallet = await db.get(BillingWallet, user.id)
    assert wallet.balance_units == (0 if terminal_status == "approved" else 5_000_000)
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == (
        1 if terminal_status == "approved" else 0
    )
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == (
        2 if terminal_status == "approved" else 1
    )
    assert pending.id not in [row.id for row in await service.list_pending()]
    assert len((await db.execute(select(PaymentRefundInboxEvent))).scalars().all()) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["amount", "currency", "transaction", "adjustment", "order"]
)
async def test_terminal_receipt_never_supersedes_conflicting_pending_evidence(
    db, mismatch
):
    _, order = await paddle_paid_order(db, "paddle-mismatch")
    _, other = await paddle_paid_order(db, "paddle-other")
    kwargs = {"event_id": "evt_conflict"}
    if mismatch == "amount":
        kwargs["amount"] = 99
    elif mismatch == "currency":
        kwargs["currency"] = "USD"
    elif mismatch == "transaction":
        kwargs["transaction_id"] = other.provider_tx_id
    elif mismatch == "adjustment":
        kwargs["adjustment_id"] = "adj_other"
    elif mismatch == "order":
        kwargs["order_no"] = other.order_no
    service = PaymentEventService(db)
    pending_event, pending_hash = signed_adjustment(order, "pending_approval", **kwargs)
    pending = await service.accept("paddle", pending_event, payload_hash=pending_hash)
    await db.commit()
    terminal_event, terminal_hash = signed_adjustment(
        order, "approved", event_id="evt_valid"
    )
    assert (
        await service.accept("paddle", terminal_event, payload_hash=terminal_hash)
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
async def test_rejected_paddle_event_cannot_override_verified_successful_refund(db):
    user, order = await paddle_paid_order(db, "paddle-terminal-conflict")
    service = PaymentEventService(db)
    approved, approved_hash = signed_adjustment(
        order, "approved", event_id="evt_approved"
    )
    assert (
        await service.accept("paddle", approved, payload_hash=approved_hash)
    ).status == "processed"
    await db.commit()
    rejected, rejected_hash = signed_adjustment(
        order, "rejected", event_id="evt_rejected"
    )
    conflict = await service.accept("paddle", rejected, payload_hash=rejected_hash)
    await db.commit()
    assert conflict.status == "pending_reconciliation"
    assert conflict.pending_reason == "refund_outcome_conflict"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"currency": "USD"}, "refund_currency_mismatch"),
        ({"amount": 101}, "refund_overflow"),
    ],
)
async def test_rejected_paddle_evidence_must_match_actual_checkout_before_closing_pending(
    db, kwargs, reason
):
    _, order = await paddle_paid_order(db, "paddle-denied-conflict")
    service = PaymentEventService(db)
    pending, pending_hash = signed_adjustment(
        order, "pending_approval", event_id="evt_bad_pending", **kwargs
    )
    pending_receipt = await service.accept("paddle", pending, payload_hash=pending_hash)
    await db.commit()
    rejected, rejected_hash = signed_adjustment(
        order, "rejected", event_id="evt_bad_rejected", **kwargs
    )
    rejected_receipt = await service.accept(
        "paddle", rejected, payload_hash=rejected_hash
    )
    await db.commit()
    assert rejected_receipt.status == "pending_reconciliation"
    assert rejected_receipt.pending_reason == reason
    await db.refresh(pending_receipt)
    assert pending_receipt.status == "pending_reconciliation"


@pytest.mark.asyncio
async def test_paddle_terminal_lookup_uses_current_read_without_event_lock_wait(
    db, monkeypatch
):
    _, order = await paddle_paid_order(db, "paddle-current-read")
    service = PaymentEventService(db)
    terminal, terminal_hash = signed_adjustment(
        order, "approved", event_id="evt_current_terminal"
    )
    await service.accept("paddle", terminal, payload_hash=terminal_hash)
    await db.commit()
    original_execute = db.execute
    terminal_lookups = []

    async def trace_execute(statement, *args, **kwargs):
        compiled = statement.compile(dialect=mysql.dialect())
        if compiled.params.get(
            "status_1"
        ) == "processed" and "payment_refund_inbox_events" in str(compiled):
            terminal_lookups.append(statement)
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", trace_execute)
    pending, pending_hash = signed_adjustment(
        order, "pending_approval", event_id="evt_current_pending"
    )
    receipt = await service.accept("paddle", pending, payload_hash=pending_hash)
    await db.commit()
    assert receipt.status == "processed"
    assert len(terminal_lookups) == 1
    for dialect in (mysql.dialect(), postgresql.dialect()):
        assert "FOR UPDATE SKIP LOCKED" in str(
            terminal_lookups[0].compile(dialect=dialect)
        )


@pytest.mark.asyncio
async def test_skipped_locked_terminal_remains_pending_then_replay_closes_once(
    db, monkeypatch
):
    user, order = await paddle_paid_order(db, "paddle-skipped-terminal")
    service = PaymentEventService(db)
    terminal, terminal_hash = signed_adjustment(
        order, "approved", event_id="evt_skip_terminal"
    )
    await service.accept("paddle", terminal, payload_hash=terminal_hash)
    await db.commit()
    original_execute = db.execute

    async def simulate_skipped_row(statement, *args, **kwargs):
        compiled = statement.compile(dialect=mysql.dialect())
        if compiled.params.get(
            "status_1"
        ) == "processed" and "payment_refund_inbox_events" in str(compiled):
            return await original_execute(
                statement.where(PaymentRefundInboxEvent.id == -1)
            )
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", simulate_skipped_row)
    pending, pending_hash = signed_adjustment(
        order, "pending_approval", event_id="evt_skip_pending"
    )
    receipt = await service.accept("paddle", pending, payload_hash=pending_hash)
    await db.commit()
    assert receipt.status == "pending_reconciliation"
    assert receipt.pending_reason == "refund_evidence_incomplete"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    monkeypatch.setattr(db, "execute", original_execute)
    assert (await service.replay(receipt.id)).status == "processed"
    await db.commit()
    assert (await service.replay(receipt.id)).status == "processed"
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2
    assert (
        len(
            (
                await db.execute(
                    select(PaymentRefundInboxAudit).where(
                        PaymentRefundInboxAudit.status == "superseded"
                    )
                )
            )
            .scalars()
            .all()
        )
        == 1
    )
