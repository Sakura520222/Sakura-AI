"""Issue #715: plain connection preflight, error reporting, and recoverable failures."""

import asyncio
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.exc import OperationalError

from backend.api.v1 import setup as api_setup
from backend.core import bootstrap, logging_bridge
from backend.core import setup_database as setup_db
from backend.core import setup_service as service_module
from backend.core.setup_service import SetupService
from backend.models import database as db_module
from backend.webui.routes import setup as web_setup


def db_error(code):
    return OperationalError("private SQL", {}, Exception(code, "opaque-password"))


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeConnection:
    """Minimal connection for the plain preflight; rejects every other query."""

    def __init__(self, *, dialect_name="mysql", database="sakura_test", error=None):
        self.dialect = SimpleNamespace(name=dialect_name)
        self.database = database
        self.error = error
        self.statements = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if self.error is not None:
            raise self.error
        if sql in {"SELECT DATABASE()", "SELECT current_database()"}:
            return Result(self.database)
        if sql in {"SELECT VERSION()", "SELECT version()"}:
            return Result("8.4.11-test")
        raise AssertionError(sql)


def test_mysql_preflight_only_checks_connection_and_selected_database():
    conn = FakeConnection()
    report = setup_db.preflight_database(conn)
    assert report["success"] is True
    assert report["connected"] is True
    assert report["database"] == "sakura_test"
    assert report["version"]
    assert {"code": "connection", "status": "passed"} in report["checks"]
    # The preflight must never inspect privileges, triggers or instance metadata.
    assert conn.statements == ["SELECT DATABASE()", "SELECT VERSION()"]
    assert not any(
        keyword in sql
        for sql in conn.statements
        for keyword in (
            "SHOW",
            "GRANT",
            "information_schema",
            "mysql.user",
            "CREATE",
            "SET GLOBAL",
        )
    )


def test_postgresql_preflight_uses_current_database():
    conn = FakeConnection(dialect_name="postgresql")
    report = setup_db.preflight_database(conn)
    assert report["success"] is True
    assert report["database"] == "sakura_test"
    assert conn.statements == ["SELECT current_database()", "SELECT version()"]


def test_missing_database_selection_fails_with_retryable_report():
    conn = FakeConnection(database=None)
    report = setup_db.preflight_database(conn)
    assert report["success"] is False
    assert report["code"] == "database_not_selected"
    assert report["retryable"] is True
    assert setup_db.setup_database_status(report) == 409


def test_preflight_query_failure_reports_real_driver_code_without_secrets():
    conn = FakeConnection(error=db_error(1045))
    report = setup_db.preflight_database(conn)
    assert report["success"] is False
    assert report["code"] == "database_preflight_failed"
    assert report["database_error_code"] == 1045
    assert "opaque-password" not in json.dumps(report)


@pytest.mark.asyncio
async def test_failed_preflight_leaves_engine_config_and_schema_untouched(monkeypatch):
    report = setup_db._fail(setup_db._report("mysql"), "database_preflight_failed")
    previous_engine = object()
    monkeypatch.setattr(db_module, "async_engine", previous_engine)
    check = AsyncMock(return_value=report)
    monkeypatch.setattr(service_module, "check_database", check)
    initializer = MagicMock()
    create = AsyncMock()
    migrate = AsyncMock()
    defaults = AsyncMock()
    monkeypatch.setattr(db_module, "init_async_db", initializer)
    monkeypatch.setattr(db_module, "create_tables_async", create)
    monkeypatch.setattr(db_module, "migrate_schema_async", migrate)
    monkeypatch.setattr(db_module, "insert_default_configs_async", defaults)
    with pytest.raises(setup_db.DatabaseSetupError):
        await SetupService().init_database("mysql+asyncmy://runtime:pass@db/sakura")
    initializer.assert_not_called()
    create.assert_not_awaited()
    migrate.assert_not_awaited()
    defaults.assert_not_awaited()
    assert db_module.async_engine is previous_engine


@pytest.mark.asyncio
async def test_partial_initialization_can_retry_without_losing_previous_engine(
    monkeypatch,
):
    monkeypatch.setattr(
        "backend.models.schema_state.schema_is_current", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "backend.models.schema_state.record_schema_checkpoint", AsyncMock()
    )
    monkeypatch.setattr(
        service_module,
        "check_database",
        AsyncMock(side_effect=lambda _: setup_db._report("mysql")),
    )
    monkeypatch.setattr(db_module, "async_engine", None)
    monkeypatch.setattr(db_module, "async_session", None)
    engines = []

    def initialize(_):
        engine = SimpleNamespace(dispose=AsyncMock())
        engines.append(engine)
        db_module.async_engine = engine
        db_module.async_session = object()

    monkeypatch.setattr(db_module, "init_async_db", initialize)
    create = AsyncMock()
    migrate = AsyncMock(side_effect=[db_error(1419), None])
    defaults = AsyncMock()
    monkeypatch.setattr(db_module, "create_tables_async", create)
    monkeypatch.setattr(db_module, "migrate_schema_async", migrate)
    monkeypatch.setattr(db_module, "insert_default_configs_async", defaults)
    with pytest.raises(setup_db.DatabaseSetupError) as failed:
        await SetupService().init_database("mysql+asyncmy://u:p@db/sakura")
    assert failed.value.report["stage"] == "migration"
    assert failed.value.report["code"] == "database_initialization_failed"
    assert failed.value.report["schema_changes_started"] is True
    assert db_module.async_engine is None
    engines[0].dispose.assert_awaited_once()
    defaults.assert_not_awaited()
    await SetupService().init_database("mysql+asyncmy://u:p@db/sakura")
    create.assert_not_awaited()
    defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_temporary_engine_disposed_after_cancellation_without_global_replacement(
    monkeypatch,
):
    connection = object()

    @asynccontextmanager
    async def connect():
        yield connection

    engine = SimpleNamespace(connect=connect, dispose=AsyncMock())
    create = MagicMock(return_value=engine)
    monkeypatch.setattr(setup_db, "create_async_engine", create)
    previous_engine = db_module.async_engine
    with pytest.raises(asyncio.CancelledError):
        async with setup_db.setup_connection(
            "mysql+asyncmy://app:app-pass@remote.example:3307/sakura",
            username="persistent_dba",
            password="admin-secret",
        ):
            raise asyncio.CancelledError
    engine.dispose.assert_awaited_once()
    used_url = create.call_args.args[0]
    assert used_url.username == "persistent_dba"
    assert used_url.password == "admin-secret"
    assert used_url.host == "remote.example"
    assert used_url.port == 3307
    assert used_url.database == "sakura"
    assert db_module.async_engine is previous_engine


@pytest.fixture
def api_app(monkeypatch):
    monkeypatch.setattr(web_setup, "is_bootstrap_mode", lambda: True)
    monkeypatch.setattr(api_setup, "is_bootstrap_mode", lambda: True)
    monkeypatch.setattr(web_setup.limiter, "enabled", False)
    app = FastAPI()
    app.include_router(web_setup.router)
    app.include_router(api_setup.router, prefix="/api/v1")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,module", [("/setup/api", web_setup), ("/api/v1/setup", api_setup)]
)
async def test_preflight_api_reports_blocked_schema_with_non_success_http(
    api_app, monkeypatch, prefix, module
):
    report = setup_db._fail(
        setup_db._report("mysql"),
        "database_preflight_failed",
        error=db_error(1045),
    )
    monkeypatch.setattr(module, "has_setup_database_token", lambda _: True)
    monkeypatch.setattr(module, "check_database", AsyncMock(return_value=report))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        response = await client.post(
            prefix + "/database-preflight",
            json={
                "url": "mysql://u:p@db/sakura",
                "database_url": "mysql://u:p@db/sakura",
            },
        )
    assert response.status_code == 409
    assert response.json()["success"] is False
    payload = response.json().get("data", response.json())
    assert payload["connected"] is True
    assert payload["schema_ready"] is False
    assert payload["database_error_code"] == 1045


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,module", [("/setup/api", web_setup), ("/api/v1/setup", api_setup)]
)
async def test_save_preflight_failure_cannot_write_config_or_report_success(
    api_app, monkeypatch, prefix, module
):
    report = setup_db._fail(
        setup_db._report("mysql"),
        "database_initialization_failed",
        error=db_error(1045),
    )
    service = module.setup_service
    monkeypatch.setattr(
        service, "test_database_connection", AsyncMock(return_value={"success": True})
    )
    monkeypatch.setattr(
        service,
        "init_database",
        AsyncMock(side_effect=setup_db.DatabaseSetupError(report)),
    )
    save = AsyncMock()
    write = MagicMock()
    monkeypatch.setattr(service, "save_configs_to_db", save)
    monkeypatch.setattr(web_setup, "write_connection_config", write)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        response = await client.post(
            prefix + "/save-step",
            json={"values": {"DATABASE_URL": "mysql://u:p@db/sakura"}},
        )
    assert response.status_code == 409
    assert response.json()["success"] is False
    save.assert_not_awaited()
    write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "incorrect", "fixture-setup-token"])
async def test_real_bootstrap_middleware_protects_and_admits_setup_api(
    api_app, monkeypatch, header
):
    monkeypatch.setattr(bootstrap, "is_bootstrap_mode", lambda: True)
    monkeypatch.setattr(
        bootstrap, "validate_setup_token", lambda token: token == "fixture-setup-token"
    )
    api_app.add_middleware(bootstrap.BootstrapMiddleware)
    report = setup_db._fail(setup_db._report("mysql"), "database_preflight_failed")
    check = AsyncMock(return_value=report)
    monkeypatch.setattr(api_setup, "check_database", check)
    headers = {"x-setup-token": header} if header else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/setup/database-preflight",
            json={"database_url": "mysql://u:p@db/sakura"},
            headers=headers,
        )
        other = await client.get("/api/v1/unrelated", headers=headers)
    assert other.status_code == 503
    if header == "fixture-setup-token":
        assert response.status_code == 409
        check.assert_awaited_once()
    else:
        assert response.status_code == 403
        check.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_failure_masks_opaque_credentials_in_actual_console_and_json_sinks(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(logger._core, "patcher", logging_bridge._patch_record)
    console = io.StringIO()
    path = tmp_path / "setup.log"
    sinks = [
        logger.add(
            console,
            format=logging_bridge._format_record,
            filter=logging_bridge._sink_filter,
            diagnose=False,
            backtrace=False,
        ),
        logger.add(
            path,
            format=logging_bridge._format_record,
            serialize=True,
            encoding="utf-8",
            filter=logging_bridge._sink_filter,
            diagnose=False,
            backtrace=False,
        ),
    ]

    def create_engine(*_args, **_kwargs):
        raise RuntimeError("connect failed: opaque-runtime-credential")

    monkeypatch.setattr(setup_db, "create_async_engine", create_engine)
    try:
        result = await setup_db.check_database(
            "mysql+asyncmy://runtime:opaque-runtime-credential@db/test"
        )
    finally:
        for sink in sinks:
            logger.remove(sink)
    assert not result["success"]
    serialized = path.read_text(encoding="utf-8")
    for output in (console.getvalue(), serialized, json.dumps(result)):
        assert "opaque-runtime-credential" not in output
    assert "RuntimeError" in console.getvalue()
    assert "RuntimeError" in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,module", [("/setup/api", web_setup), ("/api/v1/setup", api_setup)]
)
async def test_successful_database_save_reports_initialized(
    api_app, monkeypatch, prefix, module
):
    service = module.setup_service
    monkeypatch.setattr(
        service, "test_database_connection", AsyncMock(return_value={"success": True})
    )
    monkeypatch.setattr(service, "init_database", AsyncMock())
    monkeypatch.setattr(service, "save_configs_to_db", AsyncMock(return_value=1))
    monkeypatch.setattr(web_setup, "write_connection_config", MagicMock())
    monkeypatch.setattr(web_setup, "clear_bootstrap_cache", MagicMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        response = await client.post(
            prefix + "/save-step",
            json={"values": {"DATABASE_URL": "mysql://u:p@db/sakura"}},
        )
    assert response.status_code == 200
    payload = response.json().get("data", response.json())
    assert payload["initialized"] is True
    assert payload["schema_ready"] is True
    assert payload["stage"] == "initialized"
