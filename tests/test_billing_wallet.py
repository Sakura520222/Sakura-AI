"""Billing service against persisted SQL, with no mocked balances."""

from contextlib import AbstractAsyncContextManager
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.database import Base
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import BillingError, BillingService


class AsyncNested(AbstractAsyncContextManager):
    def __init__(self, transaction):
        self.transaction = transaction

    async def __aenter__(self):
        self.transaction.__enter__()
        return self

    async def __aexit__(self, *args):
        return self.transaction.__exit__(*args)


class SQLSession:
    """Async-shaped adapter executing the real SQLAlchemy unit of work."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement, *args, **kwargs):
        return self.session.execute(statement, *args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def refresh(self, value, **kwargs):
        self.session.refresh(value, **kwargs)

    def add(self, value):
        self.session.add(value)

    async def flush(self):
        self.session.flush()

    def begin_nested(self):
        return AsyncNested(self.session.begin_nested())


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"autocommit": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(TelegramUser(id=1, telegram_id=123, daily_quota=10))
        session.commit()
        yield SQLSession(session)
    engine.dispose()


@pytest.mark.asyncio
async def test_grant_idempotency_and_ledger_rebuild(db):
    svc = BillingService(db)
    first = await svc.grant(1, "10.123456", "purchase:1", kind="purchase", order_id=1)
    second = await svc.grant(1, "10.123456", "purchase:1", kind="purchase", order_id=1)
    assert first.id == second.id
    db.session.commit()
    wallet = await svc.get_wallet(1)
    total = (
        await db.execute(select(func.sum(BillingTransaction.delta_units)))
    ).scalar_one()
    assert wallet.balance_units == total == 10_123_456
    report = await svc.reconcile_wallet(1)
    assert report["consistent"]


@pytest.mark.asyncio
async def test_grant_and_ledger_roll_back_together(db):
    await BillingService(db).grant(1, 10, "atomic:1")
    db.session.rollback()
    balance = (
        await db.execute(select(BillingWallet.balance_units))
    ).scalar_one_or_none()
    assert balance in (None, 0)
    assert (
        await db.execute(select(func.count(BillingTransaction.id)))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_reversal_is_append_only_and_duplicate_safe(db):
    svc = BillingService(db)
    grant = await svc.grant(1, 20, "grant:1")
    refund = await svc.reverse(
        grant.id, "reverse:1", actor_id=1, reason="test correction"
    )
    again = await svc.reverse(
        grant.id, "reverse:1", actor_id=1, reason="test correction"
    )
    assert refund.id == again.id
    assert grant.delta_units == 20_000_000
    assert (await svc.get_wallet(1)).balance_units == 0
    with pytest.raises(BillingError):
        await svc.reverse(grant.id, "reverse:2")


@pytest.mark.asyncio
async def test_idempotency_key_cannot_change_financial_intent(db):
    svc = BillingService(db)
    await svc.grant(1, 10, "same")
    with pytest.raises(BillingError):
        await svc.grant(1, 11, "same")


@pytest.mark.asyncio
async def test_low_balance_notice_rearms_after_topup(db):
    svc = BillingService(db)
    await svc.set_low_balance_threshold(1, 5)
    await svc.grant(1, 10, "notice:topup")
    await svc.adjust(
        1, Decimal(-7), idempotency_key="notice:debit", actor_id=1, reason="test"
    )
    assert (await svc.get_wallet(1)).low_balance_notified
    await svc.adjust(
        1, Decimal(-1), idempotency_key="notice:debit2", actor_id=1, reason="test"
    )
    await svc.grant(1, 10, "notice:topup2")
    assert not (await svc.get_wallet(1)).low_balance_notified


@pytest.mark.asyncio
async def test_database_rejects_raw_ledger_mutation_and_schema_is_repeatable(db):
    from backend.models.billing_schema import ensure_billing_schema

    await BillingService(db).grant(1, 10, "immutable:1")
    db.session.commit()
    ensure_billing_schema(db.session.connection())
    ensure_billing_schema(db.session.connection())
    db.session.commit()
    with pytest.raises(DatabaseError, match="append-only"):
        await db.execute(text("UPDATE billing_transactions SET delta_units = 0"))
    db.session.rollback()
    with pytest.raises(DatabaseError, match="append-only"):
        await db.execute(text("DELETE FROM billing_transactions"))
    db.session.rollback()
    assert (await BillingService(db).reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_refund_hold_reserves_only_source_and_can_resume_after_crash(db):
    service = BillingService(db)
    original = await service.grant(1, 10, "refund:purchase", kind="purchase")
    units = await service.hold_purchase_refund(
        original.id, "refund:event", units=3_000_000
    )
    assert units == 3_000_000
    assert (
        await service.hold_purchase_refund(original.id, "refund:event", units=units)
        == units
    )
    db.session.commit()
    assert (await service.get_wallet(1)).reserved_units == units
    assert (await service.reconcile_wallet(1))["consistent"]
    await service.release_purchase_refund(original.id, units, "refund:event")
    await service.reverse(original.id, "refund:posted", units=units)
    db.session.commit()
    await service.release_purchase_refund(original.id, units, "refund:event")
    await service.reverse(original.id, "refund:posted", units=units)
    assert (await service.get_wallet(1)).balance_units == 7_000_000
    assert (await service.get_wallet(1)).reserved_units == 0
    assert (await service.reconcile_wallet(1))["consistent"]
    with pytest.raises(BillingError):
        await service.reverse(original.id, "excess", units=8_000_000)
