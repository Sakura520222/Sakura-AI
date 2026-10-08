"""Check Billing schema and CLI dry runs in a disposable SQLite database.

Requires the optional aiosqlite test driver. Never connects to a deployment
database or sends a payment, refund or AI request.
"""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DatabaseError

from backend.models import database
from backend.models.billing_models import BillingTransaction
from backend.models.billing_schema import IMMUTABLE_TABLES
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import BillingService


async def main(path):
    url = f"sqlite+aiosqlite:///{path}"
    database.init_async_db(url)
    await database.create_tables_async()
    await database.migrate_schema_async()
    await database.insert_default_configs_async()
    await database.migrate_schema_async()
    await database.insert_default_configs_async()
    async with database.async_session() as session:
        session.add(
            TelegramUser(id=1, telegram_id=123, role="super_admin", is_active=True)
        )
        await session.commit()
        await BillingService(session).grant(1, "2.123456", "smoke:grant")
        await session.commit()
        await BillingService(session).grant(1, "3", "smoke:rollback")
        await session.rollback()
        assert (await BillingService(session).get_wallet(1)).balance_units == 2_123_456
        assert (
            len((await session.execute(select(BillingTransaction))).scalars().all())
            == 1
        )
        assert (await BillingService(session).reconcile_wallet(1))["consistent"]
        await session.rollback()
        for action in (
            "UPDATE billing_transactions SET delta_units = 1",
            "DELETE FROM billing_transactions",
        ):
            try:
                await session.execute(text(action))
            except DatabaseError:
                await session.rollback()
            else:
                raise AssertionError("Immutable trigger failed")
    async with database.async_engine.connect() as connection:
        triggers = (
            (
                await connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger'")
                )
            )
            .scalars()
            .all()
        )
        for table in IMMUTABLE_TABLES:
            assert f"{table}_no_update" in triggers
            assert f"{table}_no_delete" in triggers
        indexes = await connection.run_sync(
            lambda conn: inspect(conn).get_indexes("ai_usage_records")
        )
        constraints = await connection.run_sync(
            lambda conn: inspect(conn).get_unique_constraints("ai_usage_records")
        )
        assert any(
            tuple(i["column_names"]) == ("actual_call_id",)
            for i in [*constraints, *(index for index in indexes if index["unique"])]
        )
    await database.close_async_db()
    # Exercise actual CLI dry-run handlers against this same isolated database.
    from scripts import (
        billing_maintenance,
        migrate_legacy_billing,
        reconcile_payment_events,
    )

    for module in (
        billing_maintenance,
        migrate_legacy_billing,
        reconcile_payment_events,
    ):
        module.get_settings = lambda: SimpleNamespace(database_url=url)
    await billing_maintenance.run(
        SimpleNamespace(
            command="audit",
            apply=False,
            actor_id=None,
            user_id=None,
            after_id=0,
            batch_size=100,
            manifest=None,
        )
    )
    await billing_maintenance.run(
        SimpleNamespace(
            command="recover",
            apply=False,
            actor_id=None,
            user_id=None,
            after_id=0,
            batch_size=100,
            manifest=None,
        )
    )
    await migrate_legacy_billing.run(
        SimpleNamespace(
            apply=False,
            actor_id=None,
            manifest=None,
            redeem_snapshots=None,
            source_conversions=None,
            after_id=0,
            batch_size=100,
        )
    )
    await database.close_async_db()
    await reconcile_payment_events.run(
        SimpleNamespace(apply=False, limit=100, offset=0)
    )
    print(
        "PASS: fresh/repeat async schema + all immutable triggers + atomic rollback + wallet audit + CLI dry runs"
    )


if __name__ == "__main__":
    with TemporaryDirectory(prefix="sakura-billing-check-") as directory:
        asyncio.run(main(Path(directory) / "billing.sqlite"))
