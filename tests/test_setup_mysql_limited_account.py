"""Real MySQL Setup checks with a limited account, using disposable databases.

Issue #715 acceptance: an empty database completes Setup through the application
account even when it has no TRIGGER/SUPER privileges and the server keeps
`log_bin_trust_function_creators=OFF`. SETUP_MYSQL_TEST_ADMIN_URL must target an
isolated local MySQL 8.4 test server. These tests never change global settings
and never use deployment configuration.
"""

import os
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.setup_database import check_database
from backend.core.setup_service import SetupService
from backend.models import database as db_module
from backend.models.billing_models import BillingPriceProfile


@pytest_asyncio.fixture
async def native_database():
    configured = os.environ.get("SETUP_MYSQL_TEST_ADMIN_URL")
    if not configured:
        pytest.skip("No isolated Setup MySQL server configured")
    admin = make_url(configured)
    if (
        admin.host not in {"127.0.0.1", "localhost"}
        or admin.get_backend_name() != "mysql"
    ):
        pytest.fail("Setup MySQL tests require an isolated localhost MySQL server")
    suffix = uuid.uuid4().hex[:12]
    database = "sakura_setup715_" + suffix
    account = "setup715_" + suffix
    engine = create_async_engine(admin, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            # The acceptance scenario targets the strict server mode: binlog
            # enabled with trigger creators untrusted. On other servers the
            # evidence would silently weaken, so skip instead of degrading.
            log_bin, trust_creators = (
                await connection.execute(
                    text("SELECT @@log_bin, @@log_bin_trust_function_creators")
                )
            ).one()
            if not (log_bin == 1 and trust_creators == 0):
                pytest.skip(
                    "This acceptance requires log_bin=ON and "
                    "log_bin_trust_function_creators=OFF"
                )
            await connection.execute(
                text(f"CREATE DATABASE {database} CHARACTER SET utf8mb4")
            )
            await connection.execute(
                text("CREATE USER :account@'%' IDENTIFIED BY :password"),
                {"account": account, "password": "isolated-test-password"},
            )
            # Ordinary schema privileges only: deliberately no TRIGGER.
            await connection.execute(
                text(
                    "GRANT CREATE, ALTER, INDEX, DROP, SELECT, INSERT, UPDATE, DELETE "
                    f"ON {database}.* TO :account@'%'"
                ),
                {"account": account},
            )
        app_url = admin.set(
            database=database, username=account, password="isolated-test-password"
        ).render_as_string(hide_password=False)
        yield {
            "engine": engine,
            "database": database,
            "account": account,
            "app_url": app_url,
            "admin_url": admin.set(database=database),
            "admin": admin,
        }
    finally:
        # Names above are locally generated and owned by this fixture.
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
            await connection.execute(
                text("DROP USER IF EXISTS :account@'%'"), {"account": account}
            )
        await engine.dispose()


async def trigger_count(native):
    async with native["engine"].connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA = :schema"
                ),
                {"schema": native["database"]},
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_real_setup_completes_without_trigger_privilege(
    native_database, monkeypatch
):
    native = native_database
    monkeypatch.setattr(db_module, "async_engine", None)
    monkeypatch.setattr(db_module, "async_session", None)
    try:
        preflight = await check_database(native["app_url"])
        assert preflight["success"], preflight
        assert preflight["connected"] is True
        assert preflight["code"] == "database_preflight_ready"

        await SetupService().init_database(native["app_url"])

        app_engine = create_async_engine(
            native["app_url"], poolclass=NullPool, hide_parameters=True
        )
        try:
            async with app_engine.begin() as connection:
                await connection.execute(
                    BillingPriceProfile.__table__.insert().values(
                        provider_id="fixture-provider",
                        model_id="fixture-model",
                        call_kind="chat",
                        version=1,
                        config={},
                        actor_id=1,
                    )
                )
            async with app_engine.connect() as connection:
                grants = "\n".join(
                    (await connection.execute(text("SHOW GRANTS"))).scalars()
                )
                assert "TRIGGER" not in grants
                assert "SUPER" not in grants
                assert "GRANT OPTION" not in grants
        finally:
            await app_engine.dispose()
        # Billing Guard triggers are neither required nor created anymore.
        assert await trigger_count(native) == 0
    finally:
        if db_module.async_engine is not None:
            await db_module.async_engine.dispose()
            monkeypatch.setattr(db_module, "async_engine", None)


@pytest.mark.asyncio
async def test_real_partial_schema_resumes_through_application_migrations(
    native_database, monkeypatch
):
    native = native_database
    admin_engine = create_async_engine(
        native["admin_url"], poolclass=NullPool, hide_parameters=True
    )
    monkeypatch.setattr(db_module, "async_engine", None)
    monkeypatch.setattr(db_module, "async_session", None)
    try:
        async with admin_engine.begin() as connection:
            await connection.run_sync(BillingPriceProfile.__table__.create)
            await connection.execute(
                BillingPriceProfile.__table__.insert().values(
                    provider_id="historical",
                    model_id="historical",
                    call_kind="chat",
                    version=1,
                    config={},
                    actor_id=1,
                )
            )
        # A partially created schema resumes directly through the application
        # account; no guard installation or repair step exists anymore.
        await SetupService().init_database(native["app_url"])
        repeat = AsyncMock(side_effect=AssertionError("Completed schema migrated again"))
        monkeypatch.setattr(db_module, "migrate_schema_async", repeat)
        await SetupService().init_database(native["app_url"])
        repeat.assert_not_awaited()
        async with db_module.async_engine.connect() as connection:
            assert (
                await connection.execute(
                    select(BillingPriceProfile.version).where(
                        BillingPriceProfile.provider_id == "historical"
                    )
                )
            ).scalar_one() == 1
            completed = (
                await connection.execute(
                    text(
                        "SELECT key_value FROM app_config WHERE key_name = 'setup_completed'"
                    )
                )
            ).scalar_one_or_none()
            assert completed not in {"true", "True", "1"}
    finally:
        if db_module.async_engine is not None:
            await db_module.async_engine.dispose()
            monkeypatch.setattr(db_module, "async_engine", None)
        await admin_engine.dispose()
