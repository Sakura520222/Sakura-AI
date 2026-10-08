"""Independent SQL connections race; no application or process wallet lock."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.database import Base
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import (
    BillingConflict,
    BillingError,
    BillingService,
)
from tests.test_billing_wallet import SQLSession


def runtime(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(TelegramUser(id=1, telegram_id=1))
        session.commit()
        asyncio.run(
            BillingService(SQLSession(session)).grant(1, 5, "purchase", kind="purchase")
        )
        session.commit()
    return engine


def race(engine, operations):
    barrier = Barrier(len(operations))

    def worker(operation):
        for retry in range(20):
            with Session(engine, expire_on_commit=False) as session:

                class RaceSession(SQLSession):
                    async def execute(self, statement, *args, _retry=retry, **kwargs):
                        result = await super().execute(statement, *args, **kwargs)
                        if (
                            _retry == 0
                            and not getattr(self, "read_seen", False)
                            and str(statement).startswith("SELECT billing_wallets")
                        ):
                            self.read_seen = True
                            barrier.wait(timeout=10)
                        return result

                service = BillingService(RaceSession(session))
                try:
                    value = asyncio.run(operation(service))
                    session.commit()
                    return ("ok", value.id)
                except OperationalError, BillingConflict:
                    session.rollback()
                    time.sleep(0.002 * (retry + 1))
                except BillingError as exc:
                    session.rollback()
                    return (exc.code, None)
        raise AssertionError("SQL transaction did not converge")

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(worker, operations))


def verify(engine):
    with Session(engine) as session:
        report = asyncio.run(BillingService(SQLSession(session)).reconcile_wallet(1))
        assert report["consistent"]
        return session.get(BillingWallet, 1).balance_units


def test_parallel_idempotent_grants_have_one_financial_effect(tmp_path):
    engine = runtime(tmp_path / "grant.db")
    results = race(
        engine, [lambda service: service.grant(1, 1, "duplicate") for _ in range(4)]
    )
    assert len({value for status, value in results if status == "ok"}) == 1
    assert len([status for status, _ in results if status == "ok"]) == 4
    assert verify(engine) == 6_000_000
    engine.dispose()


def test_concurrent_debits_cannot_spend_same_available_credits(tmp_path):
    engine = runtime(tmp_path / "consume.db")
    results = race(
        engine,
        [
            lambda service, i=i: service.adjust(
                1, -2, idempotency_key=f"consume:{i}", actor_id=1, reason="race"
            )
            for i in range(4)
        ],
    )
    assert [status for status, _ in results].count("ok") == 2
    assert [status for status, _ in results].count("insufficient_credits") == 2
    assert verify(engine) == 1_000_000
    engine.dispose()


def test_refund_competes_with_consumption_without_revoking_other_sources(tmp_path):
    engine = runtime(tmp_path / "refund.db")
    with Session(engine) as session:
        transaction_id = session.execute(select(BillingTransaction.id)).scalar_one()
    results = race(
        engine,
        [
            lambda service: service.reverse(transaction_id, "refund"),
            lambda service: service.adjust(
                1, -1, idempotency_key="consume", actor_id=1, reason="race"
            ),
        ],
    )
    successes = [status for status, _ in results].count("ok")
    assert successes == 1
    assert verify(engine) in {0, 4_000_000}
    assert {status for status, _ in results} <= {
        "ok",
        "insufficient_credits",
        "credits_already_consumed",
    }
    engine.dispose()
