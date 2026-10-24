"""Real MySQL binlog/GRANT/DEFINER checks, using disposable localhost databases.

SETUP_MYSQL_TEST_ADMIN_URL must target an isolated local MySQL 8.4 test server.
These tests never change global settings and never use deployment configuration.
"""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.setup_database import check_database, repair_database
from backend.core.setup_service import SetupService
from backend.models import database as db_module
from backend.models.billing_models import BillingPriceProfile
from backend.models.billing_schema import IMMUTABLE_TABLES, mysql_guard_ddl


@pytest_asyncio.fixture
async def native_database(request):
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
    database = "sakura_setup682_" + suffix
    account = "setup682_" + suffix
    engine = create_async_engine(admin, poolclass=NullPool, hide_parameters=True)
    trigger = getattr(request, "param", True)
    try:
        async with engine.begin() as connection:
            settings = (
                await connection.execute(
                    text("SELECT @@log_bin, @@log_bin_trust_function_creators")
                )
            ).one()
            if tuple(settings) != (1, 0):
                pytest.skip(
                    "This matrix requires binlog ON and trust_function_creators OFF"
                )
            await connection.execute(
                text(f"CREATE DATABASE {database} CHARACTER SET utf8mb4")
            )
            await connection.execute(
                text("CREATE USER :account@'%' IDENTIFIED BY :password"),
                {"account": account, "password": "isolated-test-password"},
            )
            privileges = "CREATE, ALTER, INDEX, DROP, SELECT, INSERT, UPDATE, DELETE"
            if trigger:
                privileges += ", TRIGGER"
            await connection.execute(
                text(f"GRANT {privileges} ON {database}.* TO :account@'%'"),
                {"account": account},
            )
        app_url = admin.set(
            database=database, username=account, password="isolated-test-password"
        ).render_as_string(hide_password=False)
        yield SimpleNamespace(
            engine=engine,
            database=database,
            account=account,
            app_url=app_url,
            admin_url=admin.set(database=database),
            admin=admin,
        )
    finally:
        # Names above are locally generated and owned by this fixture.
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
            await connection.execute(
                text("DROP USER IF EXISTS :account@'%'"), {"account": account}
            )
        await engine.dispose()


async def table_count(native):
    async with native.engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = :schema"
                ),
                {"schema": native.database},
            )
        ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_database", [True, False], indirect=True)
async def test_real_permission_failure_precedes_all_application_ddl(native_database):
    native = native_database
    result = await check_database(native.app_url)
    assert not result["success"], result
    assert result["connected"] is True
    assert result["schema_ready"] is False
    assert result["code"] == "billing_schema_privilege_required"
    assert result["database_error_code"] in {1142, 1419}
    assert await table_count(native) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("native_database", [True, False], indirect=True)
async def test_real_admin_repair_installs_and_rechecks_without_global_changes(
    native_database,
):
    native = native_database
    result = await repair_database(
        native.app_url,
        native.admin.username,
        native.admin.password or "",
        confirmed=True,
    )
    assert result["success"], result
    assert result["repaired"] is True
    assert len(result["guards"]) == 2 * len(IMMUTABLE_TABLES)
    assert all(item["status"] == "valid" for item in result["guards"])
    assert result["initialized"] is False
    assert result["log_bin"] == 1
    assert result["log_bin_trust_function_creators"] == 0
    app_engine = create_async_engine(
        native.app_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        async with app_engine.begin() as connection:
            assert (
                await connection.execute(text("SELECT COUNT(*) FROM app_config"))
            ).scalar_one() == 0
            inserted = await connection.execute(
                BillingPriceProfile.__table__.insert().values(
                    provider_id="fixture-provider",
                    model_id="fixture-model",
                    call_kind="chat",
                    version=1,
                    config={},
                    actor_id=1,
                )
            )
            price_id = inserted.lastrowid
        for sql in (
            "UPDATE billing_price_profiles SET version = 2 WHERE id = :id",
            "DELETE FROM billing_price_profiles WHERE id = :id",
        ):
            async with app_engine.connect() as connection:
                with pytest.raises(DBAPIError) as failed:
                    await connection.execute(text(sql), {"id": price_id})
                assert failed.value.orig.args[0] == 1644
        repeated = await repair_database(
            native.app_url,
            native.admin.username,
            native.admin.password or "",
            confirmed=True,
        )
        assert repeated["success"], repeated
        async with app_engine.connect() as connection:
            assert (
                await connection.execute(
                    select(BillingPriceProfile.version).where(
                        BillingPriceProfile.id == price_id
                    )
                )
            ).scalar_one() == 1
            grants = "\n".join(
                (await connection.execute(text("SHOW GRANTS"))).scalars()
            )
            assert "SUPER" not in grants
            assert "GRANT OPTION" not in grants
            assert "TRIGGER" in grants
    finally:
        await app_engine.dispose()


@pytest.mark.asyncio
async def test_real_capable_account_probe_cleans_up(native_database):
    native = native_database
    result = await check_database(
        native.admin_url.render_as_string(hide_password=False)
    )
    assert result["success"], result
    assert result["needs_guard_installation"] is True
    assert await table_count(native) == 0


@pytest.mark.asyncio
async def test_real_manual_sql_is_repeatable_from_an_empty_database(native_database):
    native = native_database
    report = await check_database(native.app_url)
    assert report["database_error_code"] == 1419
    admin_engine = create_async_engine(
        native.admin_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        for _ in range(2):
            async with admin_engine.connect() as connection:
                # Generated statements are separated by this exact delimiter.
                # No external or deployment SQL is accepted by this test.
                for statement in report["repair_sql"].split(";\n\n"):
                    if statement.strip() and not statement.startswith("-- Conflicting"):
                        await connection.execute(text(statement.rstrip(";\n")))
                await connection.commit()
        result = await check_database(native.app_url)
        assert result["success"], result
        assert result["needs_guard_installation"] is False
    finally:
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_real_conflicting_guard_preserves_existing_rows(native_database):
    native = native_database
    admin_engine = create_async_engine(native.admin_url, poolclass=NullPool)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE billing_transactions (id INT PRIMARY KEY)")
            )
            await connection.execute(
                text("INSERT INTO billing_transactions VALUES (1)")
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER billing_transactions_no_update BEFORE UPDATE ON billing_transactions "
                    "FOR EACH ROW SET @unrelated = 1"
                )
            )
        result = await check_database(native.app_url)
        assert result["code"] == "billing_guard_conflict"
        repair = await repair_database(
            native.app_url,
            native.admin.username,
            native.admin.password or "",
            confirmed=True,
        )
        assert repair["code"] == "billing_guard_conflict"
        assert await table_count(native) == 1
        async with admin_engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT id FROM billing_transactions"))
            ).scalar_one() == 1
    finally:
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_real_partial_schema_resumes_through_application_migrations(
    native_database, monkeypatch
):
    native = native_database
    admin_engine = create_async_engine(native.admin_url, poolclass=NullPool)
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
            await connection.execute(
                text(mysql_guard_ddl("billing_price_profiles", "UPDATE"))
            )
        blocked = await check_database(native.app_url)
        assert blocked["database_error_code"] == 1419
        assert blocked["partial_schema"] is True
        repaired = await repair_database(
            native.app_url,
            native.admin.username,
            native.admin.password or "",
            confirmed=True,
        )
        assert repaired["success"], repaired
        await SetupService().init_database(native.app_url)
        repeat = AsyncMock(side_effect=AssertionError("Completed schema migrated again"))
        monkeypatch.setattr(db_module, "migrate_schema_async", repeat)
        await SetupService().init_database(native.app_url)
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
        await admin_engine.dispose()
