"""Unknown requests are resolved from reviewed evidence, never replayed."""

import pytest
from sqlalchemy import func, select

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import BillingReconciliationEvent, BillingUsageCharge
from backend.models.telegram_models import TelegramUser
from backend.services.billing_reconciliation_service import resolve_billing_call
from backend.services.billing_service import BillingError
from tests.test_billing_settlement import add_call, prepare
from tests.test_billing_wallet import db as wallet_db

db = wallet_db


@pytest.mark.asyncio
async def test_resolution_appends_usage_and_settlement_without_erasing_unknown(db):
    service = await prepare(db)
    await add_call(db, service, "unknown", reported=False)
    await service.finish_operation("op-1", "completed")
    user = await db.get(TelegramUser, 1)
    user.role = "super_admin"
    await db.flush()
    evidence = {
        "call_id": "unknown",
        "event_key": "provider-invoice-1",
        "actor_id": 1,
        "reason": "Provider invoice confirms exact usage",
        "usage": {"input_tokens": 1000, "output_tokens": 0},
    }
    event = await resolve_billing_call(db, **evidence)
    again = await resolve_billing_call(db, **evidence)
    assert event.id == again.id
    records = (
        (await db.execute(select(AIUsageRecord).order_by(AIUsageRecord.id)))
        .scalars()
        .all()
    )
    assert len(records) == 2
    assert records[0].usage_reported is False
    assert records[1].input_tokens == 1000
    assert (await service._operation("op-1")).status == "settled"
    assert (await service.get_wallet(1)).balance_units == 9_999_000
    assert (
        await db.execute(select(func.count(BillingUsageCharge.id)))
    ).scalar_one() == 1
    assert (
        await db.execute(select(func.count(BillingReconciliationEvent.id)))
    ).scalar_one() == 1
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_reconciliation_requires_authority_and_cannot_rewrite_frozen_quote(db):
    service = await prepare(db)
    await add_call(db, service, "known", tokens=100)
    await service.finish_operation("op-1", "completed")
    args = {
        "call_id": "known",
        "event_key": "correct",
        "actor_id": 1,
        "reason": "invoice",
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    with pytest.raises(BillingError, match="super administrator"):
        await resolve_billing_call(db, **args)
    (await db.get(TelegramUser, 1)).role = "super_admin"
    await db.flush()
    with pytest.raises(BillingError, match="immutable"):
        await resolve_billing_call(db, **args)
    assert (await service.get_wallet(1)).balance_units == 9_999_900


@pytest.mark.asyncio
async def test_pending_price_blocks_more_sends_and_requires_explicit_audited_version(
    db,
):
    from tests.test_billing_settlement import PRICE

    service = await prepare(db)
    await add_call(db, service, "new-cache-capability", tokens=100)
    usage = (await db.execute(select(AIUsageRecord))).scalar_one()
    usage.cached_input_tokens = 20
    await db.flush()
    operation = await service.settle_operation("op-1")
    assert operation.status == "pending_pricing"
    with pytest.raises(BillingError, match="reconciliation"):
        await service.start_call("op-1", "must-not-send", "p1", "m1", "chat")
    await service.finish_operation("op-1", "completed")
    (await db.get(TelegramUser, 1)).role = "super_admin"
    reviewed = await service.publish_price(
        "p1",
        "m1",
        "chat",
        {**PRICE, "cache_read_supported": True, "cached_input_price": "0.5"},
        actor_id=1,
    )
    await db.flush()
    event = await resolve_billing_call(
        db,
        call_id="new-cache-capability",
        event_key="reviewed-price",
        actor_id=1,
        reason="Provider invoice proves cached input and reviewed tariff",
        usage={
            "input_tokens": 100,
            "output_tokens": 0,
            "cached_input_tokens": 20,
            "cache_creation_tokens": 0,
            "reasoning_tokens": 0,
        },
        price_profile_id=reviewed.id,
    )
    assert event.evidence["original_price_profile_id"] != reviewed.id
    assert event.evidence["resolved_price_profile_id"] == reviewed.id
    assert (await service._operation("op-1")).status == "settled"
    assert (await service.get_wallet(1)).balance_units == 10_000_000 - 90
