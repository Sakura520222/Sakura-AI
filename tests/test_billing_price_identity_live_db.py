"""Native exact pricing lookups and an idempotent, history-preserving MySQL upgrade."""

import os
from copy import deepcopy

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, select, text
from sqlalchemy.dialects.mysql import VARCHAR
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.models.billing_models import BillingPriceProfile
from backend.models.billing_schema import ensure_billing_schema
from backend.services.billing_service import BillingService

PRICE = {
    "currency": "USD",
    "settlement_currency": "USD",
    "fx_rate": "1",
    "markup": "1",
    "credits_per_currency_unit": "1",
    "unit": "tokens",
    "input_price": "1",
    "output_price": "1",
}


@pytest_asyncio.fixture
async def native_prices():
    url = os.environ.get("BILLING_IDENTITY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("No isolated native pricing database configured")
    parsed = make_url(url)
    if parsed.database != "sakura_notification_test" or parsed.host not in {
        "localhost",
        "127.0.0.1",
    }:
        pytest.fail("Pricing identity tests require the isolated localhost CI database")
    engine = create_async_engine(url)
    table = BillingPriceProfile.__table__
    async with engine.begin() as connection:
        await connection.run_sync(lambda db: table.drop(db, checkfirst=True))
        await connection.run_sync(lambda db: table.create(db))
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(lambda db: table.drop(db, checkfirst=True))
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dimension,other",
    [
        ("provider", "provider-a"),
        ("account", "account-a"),
        ("model", "model-a"),
        ("model", "Model-A "),
        ("model", "Módel-A"),
    ],
)
async def test_native_price_selection_is_exact(native_prices, dimension, other):
    _, sessions = native_prices
    first = {"provider": "Provider-A", "account": "Account-A", "model": "Model-A"}
    second = {**first, dimension: other}
    async with sessions() as db:
        billing = BillingService(db)
        profiles = []
        for identity, price in ((first, "1"), (second, "9")):
            config = {**PRICE, "input_price": price}
            profiles.append(
                await billing.publish_price(
                    identity["provider"],
                    identity["model"],
                    "chat",
                    config,
                    account_id=identity["account"],
                    actor_id=1,
                )
            )
            await db.commit()
        for identity, expected in zip((first, second), profiles, strict=True):
            selected = await billing._price(
                identity["provider"],
                identity["model"],
                "chat_stream",
                account_id=identity["account"],
            )
            assert selected.id == expected.id
            assert (selected.provider_id, selected.account_id, selected.model_id) == (
                identity["provider"],
                identity["account"],
                identity["model"],
            )
        assert (
            await billing._price(
                "missing-provider", "Model-A", "chat", account_id="Account-A"
            )
            is None
        )


@pytest.mark.asyncio
async def test_mysql_identity_upgrade_preserves_quotes_and_immutable_guards(
    native_prices,
):
    engine, sessions = native_prices
    if engine.dialect.name not in {"mysql", "mariadb"}:
        pytest.skip("MySQL collation upgrade does not apply to PostgreSQL")
    legacy = BillingPriceProfile.__table__.to_metadata(MetaData())
    for name in ("provider_id", "account_id", "scope_key", "model_id", "call_kind"):
        legacy.c[name].type = VARCHAR(
            legacy.c[name].type.length,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
        )
    async with engine.begin() as connection:
        await connection.run_sync(lambda db: legacy.drop(db, checkfirst=True))
        await connection.run_sync(lambda db: legacy.create(db))
        await connection.execute(
            legacy.insert(),
            [
                {
                    "id": 1,
                    "provider_id": "Provider-A",
                    "account_id": None,
                    "scope_key": "provider",
                    "model_id": "Model-A",
                    "call_kind": "chat",
                    "version": 1,
                    "config": PRICE,
                    "actor_id": 1,
                },
                {
                    "id": 2,
                    "provider_id": "Provider-A",
                    "account_id": None,
                    "scope_key": "provider",
                    "model_id": "model-a",
                    "call_kind": "chat",
                    "version": 2,
                    "config": {**PRICE, "input_price": "9"},
                    "actor_id": 1,
                },
            ],
        )
    async with sessions() as db:
        wrong = await BillingService(db)._price("Provider-A", "Model-A", "chat")
        assert (
            wrong.id == 2
        )  # Reproduce the old database's case-insensitive tariff selection.
        before = deepcopy(
            [
                dict(row)
                for row in (await db.execute(select(legacy).order_by(legacy.c.id)))
                .mappings()
                .all()
            ]
        )
        await db.commit()
    async with engine.begin() as connection:
        assert await connection.run_sync(ensure_billing_schema)
    async with engine.begin() as connection:
        assert not await connection.run_sync(ensure_billing_schema)
    async with sessions() as db:
        after = [
            dict(row)
            for row in (await db.execute(select(legacy).order_by(legacy.c.id)))
            .mappings()
            .all()
        ]
        assert after == before
        billing = BillingService(db)
        assert (await billing._price("Provider-A", "Model-A", "chat")).id == 1
        assert (await billing._price("Provider-A", "model-a", "chat")).id == 2
        await db.commit()
        for sql in (
            "UPDATE billing_price_profiles SET version = 99 WHERE id = 1",
            "DELETE FROM billing_price_profiles WHERE id = 1",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                await db.execute(text(sql))
            await db.rollback()
