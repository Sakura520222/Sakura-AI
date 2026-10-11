"""Legacy TEXT metadata must never be passed to database JSON operators."""

import json

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import select
from sqlalchemy.dialects import mysql, postgresql

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import (
    PaymentRefundInboxAudit,
    PaymentRefundReference,
)
from backend.models.payment_models import Order
from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
from backend.services.payment_event_service import PaymentEventService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_billing_payment_events import paid_order

db = real_database_fixture


def refund_without_order(reference, event_id="TEST-native-refund"):
    return WebhookEvent(
        event_type=WebhookEventType.PAYMENT_REFUNDED,
        payment_reference_id=reference,
        provider_tx_id="TEST-refund-id",
        currency="CNY",
        event_id=event_id,
        refund_items=[
            {
                "id": "TEST-refund-id",
                "amount_cents": 100,
                "currency": "CNY",
                "status": "succeeded",
            }
        ],
        refund_evidence_complete=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["{broken-json", "null", "[]", '"not an object"'])
async def test_refund_reference_ignores_unrelated_malformed_text_and_grants_no_duplicate(
    db, malformed
):
    user, order = await paid_order(db, "TEST-text-metadata")
    reference = "pi_模型/ref%_one"
    metadata = json.loads(order.metadata_json)
    metadata["payment_reference_id"] = reference
    order.metadata_json = json.dumps(metadata)
    db.add(
        Order(
            order_no="TEST-broken-metadata",
            user_id=user.id,
            amount_cents=100,
            payment_provider="stripe",
            metadata_json=malformed,
        )
    )
    db.add(
        Order(
            order_no="TEST-other-provider",
            user_id=user.id,
            amount_cents=100,
            payment_provider="paddle",
            metadata_json=json.dumps(metadata),
        )
    )
    await db.commit()
    queries = []

    @sql_event.listens_for(db._session, "do_orm_execute")
    def capture(execution):
        queries.append(execution.statement)

    service = PaymentEventService(db)
    event = refund_without_order(reference)
    receipt = await service.accept("stripe", event, payload_hash="TEST-refund")
    await db.commit()
    assert receipt.status == "processed" and receipt.order_id == order.id
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert (
        await service.accept("stripe", event, payload_hash="TEST-refund")
    ).id == receipt.id
    redelivery = refund_without_order(reference, "TEST-distinct-delivery")
    assert (
        await service.accept("stripe", redelivery, payload_hash="TEST-again")
    ).status == "processed"
    await db.commit()
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 2
    assert len((await db.execute(select(PaymentRefundReference))).scalars().all()) == 1
    for query in queries:
        for dialect in (postgresql.dialect(), mysql.dialect()):
            sql = str(query.compile(dialect=dialect)).lower()
            if "payment_orders.metadata_json" in sql:
                assert "payment_orders.metadata_json ->" not in sql
                assert "json_extract(payment_orders.metadata_json" not in sql
                assert "cast(payment_orders.metadata_json" not in sql


@pytest.mark.asyncio
async def test_ambiguous_reference_is_pending_with_no_financial_effect(db):
    user, first = await paid_order(db, "TEST-ambiguous-first")
    _, second = await paid_order(db, "TEST-ambiguous-second")
    reference = "pi_duplicated_reference"
    for order in (first, second):
        metadata = json.loads(order.metadata_json)
        metadata["payment_reference_id"] = reference
        order.metadata_json = json.dumps(metadata)
    await db.commit()
    receipt = await PaymentEventService(db).accept(
        "stripe", refund_without_order(reference), payload_hash="TEST-ambiguous"
    )
    await db.commit()
    assert (
        receipt.status == "pending_reconciliation"
        and receipt.pending_reason == "ambiguous_order"
    )
    assert receipt.order_id is None
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []
    audits = (
        (
            await db.execute(
                select(PaymentRefundInboxAudit).where(
                    PaymentRefundInboxAudit.inbox_event_id == receipt.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1 and audits[0].status == "received"


@pytest.mark.asyncio
async def test_reference_lookup_crosses_batches_and_checks_direct_tx_collisions(db):
    user, first = await paid_order(db, "TEST-keyset-first")
    reference = "pi_TEST_late_batch"
    for index in range(201):
        db.add(
            Order(
                order_no=f"TEST-older-{index}",
                user_id=user.id,
                payment_provider="stripe",
                amount_cents=100,
                metadata_json="{broken-json" if index % 2 else "{}",
            )
        )
    await db.commit()
    later_user, later = await paid_order(db, "TEST-keyset-later")
    metadata = json.loads(later.metadata_json)
    metadata["payment_reference_id"] = reference
    later.metadata_json = json.dumps(metadata)
    await db.commit()
    service = PaymentEventService(db)
    assert await service._reference_order_id("stripe", reference) == later.id
    assert await service._reference_order_id("stripe", "not-present") is None
    first.provider_tx_id = reference
    await db.commit()
    receipt = await service.accept(
        "stripe", refund_without_order(reference), payload_hash="TEST-batch-ambiguous"
    )
    await db.commit()
    assert receipt.pending_reason == "ambiguous_order"
    assert (await db.get(BillingWallet, later_user.id)).balance_units == 5_000_000
    assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["reference", "order_no"])
async def test_selected_corrupt_metadata_is_pending_instead_of_zero_or_success(
    db, lookup
):
    user, order = await paid_order(db, "TEST-selected-broken")
    order.metadata_json = "[null]"
    order.provider_tx_id = "TEST-native-reference"
    await db.commit()
    refund = refund_without_order("TEST-native-reference")
    if lookup == "order_no":
        refund.order_no = order.order_no
        refund.payment_reference_id = ""
    receipt = await PaymentEventService(db).accept(
        "stripe", refund, payload_hash="TEST-corrupt-selected"
    )
    await db.commit()
    assert receipt.status == "pending_reconciliation"
    assert receipt.pending_reason == "order_metadata_required"
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert order.metadata_json == "[null]"
    assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []


@pytest.mark.asyncio
async def test_candidate_identity_is_rechecked_after_order_lock(db, monkeypatch):
    user, order = await paid_order(db, "TEST-changed-candidate")
    metadata = json.loads(order.metadata_json)
    metadata["payment_reference_id"] = "TEST-before-lock"
    order.metadata_json = json.dumps(metadata)
    await db.commit()
    service = PaymentEventService(db)
    find = service._reference_order_id

    async def change_after_lookup(provider, reference):
        candidate = await find(provider, reference)
        order.metadata_json = json.dumps(
            {**metadata, "payment_reference_id": "TEST-after-lock"}
        )
        await db.flush()
        return candidate

    monkeypatch.setattr(service, "_reference_order_id", change_after_lookup)
    receipt = await service.accept(
        "stripe", refund_without_order("TEST-before-lock"), payload_hash="TEST-changed"
    )
    await db.commit()
    assert receipt.status == "pending_reconciliation"
    assert receipt.pending_reason == "payment_reference_mismatch"
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert (await db.execute(select(PaymentRefundReference))).scalars().all() == []
