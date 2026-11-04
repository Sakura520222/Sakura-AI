"""Upgrades retain per-user voucher replay semantics without reissuing benefits."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import RedeemCodeRedemption
from backend.models.payment_models import Order
from backend.models.telegram_models import TelegramUser
from backend.services.payment_service import PaymentService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_review_redeem_concurrency import voucher

db = real_database_fixture


async def legacy_order(db, user, plan, code, *, status="fulfilled"):
    order = Order(
        order_no="legacy-voucher-order",
        user_id=user.id,
        plan_id=plan.id,
        amount_cents=77,
        currency="CNY",
        status=status,
        payment_provider="redeem_code",
        provider_tx_id=code.code,
        plan_snapshot=None,
    )
    code.used_count = 1
    db.add(order)
    await db.commit()
    return order


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paid", "fulfilled", "refunded"])
async def test_restored_voucher_replays_legacy_order_and_backfills_once(
    db, monkeypatch, status
):
    user, plan, code = await voucher(db)
    old = await legacy_order(db, user, plan, code, status=status)
    snapshot = deepcopy(code.plan_snapshot)
    plan.name = "Caller edit survives replay"
    original_execute = db.execute
    historical_reads = []

    async def current_read(statement, *args, **kwargs):
        if getattr(statement, "column_descriptions", [{}])[0].get("entity") is Order:
            historical_reads.append(statement)
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", current_read)
    service = PaymentService(db)
    assert (await service.redeem_code(user.id, code.code)).id == old.id
    await db.commit()
    assert (await service.redeem_code(user.id, code.code)).id == old.id
    await db.commit()
    pointer = (await db.execute(select(RedeemCodeRedemption))).scalar_one()
    assert (pointer.redeem_code_id, pointer.user_id, pointer.order_id) == (
        code.id,
        user.id,
        old.id,
    )
    assert "FOR UPDATE" in str(historical_reads[0].compile(dialect=mysql.dialect()))
    assert code.used_count == 1
    assert code.status == "active"
    assert code.plan_snapshot == snapshot
    assert old.plan_snapshot is None
    assert old.status == status
    assert old.amount_cents == 77
    assert plan.name == "Caller edit survives replay"
    assert (await db.execute(select(BillingTransaction))).scalars().all() == []
    assert await db.get(BillingWallet, user.id) is None
    assert len((await db.execute(select(Order))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_legacy_replay_does_not_require_restored_snapshot_or_active_offer(db):
    user, plan, code = await voucher(db)
    old = await legacy_order(db, user, plan, code)
    code.plan_snapshot = None
    code.status = "disabled"
    await db.commit()
    assert (await PaymentService(db).redeem_code(user.id, code.code)).id == old.id
    assert code.used_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_user", [False, True])
async def test_other_user_and_unsuccessful_orders_do_not_block_new_redemption(
    db, same_user
):
    user, plan, code = await voucher(db)
    old = await legacy_order(
        db, user, plan, code, status="cancelled" if same_user else "fulfilled"
    )
    other = TelegramUser(github_username="another-legacy-redeemer")
    db.add(other)
    await db.commit()
    payer = user if same_user else other
    winner = await PaymentService(db).redeem_code(payer.id, code.code)
    assert winner.user_id == payer.id
    assert winner.id != old.id
    assert winner.status == "fulfilled"
    assert (await db.get(BillingWallet, payer.id)).balance_units == 5_000_000


@pytest.mark.asyncio
async def test_backfill_uniqueness_conflict_returns_current_winner_and_preserves_caller(
    db, monkeypatch
):
    user, plan, code = await voucher(db)
    service = PaymentService(db)
    winner = await service.redeem_code(user.id, code.code)
    await db.commit()
    plan.name = "Backfill caller edit"
    await db.flush()
    original_execute = db.execute
    pointer_reads = 0

    async def miss_first_pointer(statement, *args, **kwargs):
        nonlocal pointer_reads
        if (
            getattr(statement, "column_descriptions", [{}])[0].get("entity")
            is RedeemCodeRedemption
        ):
            pointer_reads += 1
            if pointer_reads == 1:
                return await original_execute(
                    statement.where(RedeemCodeRedemption.id == -1)
                )
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", miss_first_pointer)
    assert (await service.redeem_code(user.id, code.code)).id == winner.id
    await db.commit()
    assert pointer_reads == 2
    assert plan.name == "Backfill caller edit"
    assert code.used_count == 1
    assert len((await db.execute(select(Order))).scalars().all()) == 1
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
