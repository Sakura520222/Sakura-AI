"""Redeem replays use current reads and isolate uniqueness races in a savepoint."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.legacy_entitlement_models import RedeemCodeRedemption
from backend.models.payment_models import Order, Plan, RedeemCode
from backend.models.telegram_models import TelegramUser
from backend.services.payment_service import PaymentService
from tests.test_billing_entitlements import db as real_database_fixture

db = real_database_fixture


async def voucher(db):
    user = TelegramUser(github_username="concurrent-redeemer")
    plan = Plan(
        name="Concurrent voucher",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    code = RedeemCode(
        code="opaque-voucher-bearer",
        plan_id=plan.id,
        max_uses=2,
        plan_snapshot=PaymentService(db)._snapshot_plan(plan),
    )
    db.add(code)
    await db.commit()
    return user, plan, code


@pytest.mark.asyncio
async def test_replay_bypasses_repeatable_read_snapshot(db, monkeypatch):
    user, _, code = await voucher(db)
    service = PaymentService(db)
    winner = await service.redeem_code(user.id, code.code)
    await db.commit()
    original_execute = db.execute
    reads = []

    async def snapshot_read(statement, *args, **kwargs):
        if (
            isinstance(statement, type(select(RedeemCodeRedemption)))
            and statement.column_descriptions[0].get("entity") is RedeemCodeRedemption
        ):
            reads.append(statement)
            if statement._for_update_arg is None:
                return await original_execute(
                    statement.where(RedeemCodeRedemption.id == -1)
                )
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", snapshot_read)
    replay = await service.redeem_code(user.id, code.code)
    assert replay.id == winner.id
    assert len(reads) == 1
    assert "FOR UPDATE" in str(reads[0].compile(dialect=mysql.dialect()))
    assert code.used_count == 1


@pytest.mark.asyncio
async def test_uniqueness_conflict_returns_winner_without_second_order_or_use(
    db, monkeypatch
):
    user, plan, code = await voucher(db)
    service = PaymentService(db)
    winner = await service.redeem_code(user.id, code.code)
    await db.commit()
    plan.name = "Unrelated caller change"
    await db.flush()
    original_execute = db.execute
    reads = []

    async def miss_once(statement, *args, **kwargs):
        if (
            isinstance(statement, type(select(RedeemCodeRedemption)))
            and statement.column_descriptions[0].get("entity") is RedeemCodeRedemption
        ):
            reads.append(statement)
            if len(reads) == 1:
                return await original_execute(
                    statement.where(RedeemCodeRedemption.id == -1)
                )
        if (
            getattr(statement, "column_descriptions", [{}])[0].get("entity") is Order
            and len(reads) == 1
        ):
            return await original_execute(statement.where(Order.id == -1))
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", miss_once)
    replay = await service.redeem_code(user.id, code.code)
    await db.commit()
    assert replay.id == winner.id
    assert len(reads) == 2
    assert "FOR UPDATE" in str(reads[-1].compile(dialect=mysql.dialect()))
    await db.refresh(code)
    assert code.used_count == 1
    assert code.status == "active"
    assert plan.name == "Unrelated caller change"
    assert len((await db.execute(select(Order))).scalars().all()) == 1
    assert len((await db.execute(select(RedeemCodeRedemption))).scalars().all()) == 1
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000


@pytest.mark.asyncio
async def test_unrelated_integrity_failure_is_not_reported_as_replay(db, monkeypatch):
    user, plan, code = await voucher(db)
    service = PaymentService(db)
    existing = await service.grant_plan_to_user(user.id, plan.id, operator_id=user.id)
    await db.commit()
    plan.name = "Caller transaction survives"
    await db.flush()
    monkeypatch.setattr(service, "_generate_order_no", lambda: existing.order_no)
    with pytest.raises(IntegrityError):
        await service.redeem_code(user.id, code.code)
    await db.commit()
    await db.refresh(code)
    assert code.used_count == 0
    assert plan.name == "Caller transaction survives"
    assert (await db.execute(select(RedeemCodeRedemption))).scalars().all() == []
