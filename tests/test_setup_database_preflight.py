"""Issue #682: preflight before DDL, explicit repair, and recoverable failures."""

import asyncio
import io
import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError

from backend.api.v1 import setup as api_setup
from backend.core import bootstrap, logging_bridge
from backend.core import setup_database as setup_db
from backend.core import setup_service as service_module
from backend.core.setup_service import SetupService
from backend.models import database as db_module
from backend.models.billing_schema import IMMUTABLE_TABLES, MYSQL_GUARD_BODY
from backend.webui.routes import setup as web_setup


def db_error(code):
    return OperationalError("private SQL", {}, Exception(code, "opaque-password"))


def guards():
    return {
        f"{table}_no_{action.lower()}": {
            "EVENT_OBJECT_TABLE": table,
            "EVENT_MANIPULATION": action,
            "ACTION_TIMING": "BEFORE",
            "ACTION_ORIENTATION": "ROW",
            "ACTION_STATEMENT": MYSQL_GUARD_BODY,
            "DEFINER": "persistent_dba@localhost",
        }
        for table in IMMUTABLE_TABLES
        for action in ("UPDATE", "DELETE")
    }


def test_repair_account_quoting_preserves_wildcard_hosts():
    quoted = setup_db._account_sql(
        SimpleNamespace(dialect=mysql.dialect()), "runtime@%"
    )
    assert quoted == chr(96) + "runtime" + chr(96) + "@" + chr(96) + "%" + chr(96)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value


class MySQL:
    dialect = mysql.dialect()

    def __init__(self, installed=None, *, failure=None, metadata_denied=False):
        self.installed = installed or {}
        self.failure = failure
        self.metadata_denied = metadata_denied
        self.statements = []
        self.tables = set(IMMUTABLE_TABLES) if installed else set()
        self.views = set()
        self.engine = self
        self.cleanup_failure = False
        self.definer_exists = True

    @contextmanager
    def begin(self):
        yield self

    def commit(self):
        return None

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if sql == "SELECT DATABASE()":
            return Result("sakura_test")
        if sql == "SELECT CURRENT_USER()":
            return Result("runtime@%")
        if sql == "SELECT VERSION()":
            return Result("8.4")
        if sql.startswith("SHOW GLOBAL VARIABLES"):
            if self.metadata_denied:
                raise db_error(1227)
            key = params["name"]
            return Result((key, "OFF" if "trust" in key else "ON"))
        if sql == "SHOW GRANTS":
            return Result(
                [
                    "GRANT SELECT, CREATE, ALTER, INDEX, DROP, TRIGGER ON sakura_test.* TO runtime"
                ]
            )
        if "FROM mysql.user" in sql:
            if self.metadata_denied:
                raise db_error(1142)
            return Result(int(self.definer_exists))
        if "information_schema.TRIGGERS" in sql:
            return Result(self.installed.get(params["name"]))
        if sql.startswith("CREATE TABLE"):
            name = sql.split()[2].strip(chr(96))
            self.tables.add(name)
        elif sql.startswith("CREATE TRIGGER"):
            if self.failure:
                raise db_error(self.failure)
            name = sql.split()[2]
            table = sql.split(" ON ")[1].split()[0]
            action = sql.split(" BEFORE ")[1].split()[0]
            self.installed[name] = {
                "EVENT_OBJECT_TABLE": table,
                "EVENT_MANIPULATION": action,
                "ACTION_TIMING": "BEFORE",
                "ACTION_ORIENTATION": "ROW",
                "ACTION_STATEMENT": MYSQL_GUARD_BODY,
                "DEFINER": "runtime@%",
            }
        elif sql.startswith(("UPDATE ", "DELETE ")):
            raise db_error(1644)
        elif sql.startswith("DROP TABLE"):
            if self.cleanup_failure:
                raise db_error(2013)
            table = sql.split()[2].strip(chr(96))
            self.tables.discard(table)
            self.installed = {
                name: guard
                for name, guard in self.installed.items()
                if guard["EVENT_OBJECT_TABLE"] != table
            }
        elif not sql.startswith(
            ("ALTER TABLE", "CREATE INDEX", "INSERT INTO", "GRANT TRIGGER")
        ):
            raise AssertionError(sql)
        return Result(None)


@pytest.fixture
def mysql_metadata(monkeypatch):
    monkeypatch.setattr(
        setup_db,
        "inspect",
        lambda conn: SimpleNamespace(
            get_table_names=lambda: list(conn.tables),
            get_view_names=lambda: list(conn.views),
        ),
    )
    monkeypatch.setattr(setup_db, "_schema_statements", lambda conn: iter(()))


@pytest.mark.parametrize("code", [1419, 1142, 1227])
def test_blocked_probe_cleans_up_before_business_ddl(mysql_metadata, code):
    conn = MySQL(failure=code)
    report = setup_db.preflight_database(conn)
    assert report["connected"] is True
    assert report["success"] is False
    assert report["schema_ready"] is False
    assert report["database_error_code"] == code
    assert report["code"] == "billing_schema_privilege_required"
    assert "opaque-password" not in json.dumps(report)
    assert conn.tables == set()
    mutations = [
        sql for sql in conn.statements if not sql.startswith(("SELECT", "SHOW"))
    ]
    assert all("sakura_setup_probe_" in sql for sql in mutations)
    assert not any(
        "SET GLOBAL" in sql or sql.startswith("GRANT") for sql in conn.statements
    )
    assert "GRANT TRIGGER ON " in report["repair_sql"]
    assert setup_db._account_sql(conn, "runtime@%") in report["repair_sql"]


def test_capable_account_checks_both_guard_effects_and_cleans_up(mysql_metadata):
    conn = MySQL()
    report = setup_db.preflight_database(conn)
    assert report["success"] is True
    assert report["initialized"] is False
    assert report["needs_guard_installation"] is True
    assert conn.tables == set()
    assert sum(sql.startswith("UPDATE ") for sql in conn.statements) == 1
    assert sum(sql.startswith("DELETE ") for sql in conn.statements) == 1


def test_valid_preinstalled_guards_require_no_creation_probe(mysql_metadata):
    conn = MySQL(guards(), failure=1419, metadata_denied=True)
    report = setup_db.preflight_database(conn)
    assert report["success"] is True
    assert report["needs_guard_installation"] is False
    assert len(report["guards"]) == 24
    assert not any(
        sql.startswith(("CREATE", "ALTER", "DROP")) for sql in conn.statements
    )
    assert any(
        item["code"] == "definer_dba_verification" for item in report["warnings"]
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("EVENT_OBJECT_TABLE", "other_table", "billing_guard_conflict"),
        ("ACTION_STATEMENT", "SET @bypass = 1", "billing_guard_conflict"),
        ("DEFINER", "", "billing_guard_definer_invalid"),
    ],
)
def test_conflicts_are_reported_without_replacement(mysql_metadata, field, value, code):
    installed = guards()
    installed["billing_transactions_no_update"][field] = value
    conn = MySQL(installed)
    report = setup_db.preflight_database(conn)
    assert report["code"] == code
    assert not report["success"]
    assert not any(sql.startswith(("CREATE", "DROP")) for sql in conn.statements)


def test_known_orphan_definer_blocks_setup(mysql_metadata):
    conn = MySQL(guards())
    conn.definer_exists = False
    report = setup_db.preflight_database(conn)
    assert report["code"] == "billing_guard_definer_invalid"
    assert report["failed_object"] == "billing_transactions_no_update"


def test_variable_metadata_denial_uses_actual_probe(mysql_metadata):
    conn = MySQL(metadata_denied=True)
    report = setup_db.preflight_database(conn)
    assert report["success"] is True
    assert report["log_bin"] is None
    assert report["checks"][0]["code"] == "guard_creation_and_rejection"


def test_connection_loss_is_not_reported_as_privilege_denial(mysql_metadata):
    conn = MySQL(failure=2013)
    report = setup_db.preflight_database(conn)
    assert report["code"] == "database_probe_failed"
    assert report["database_error_code"] == 2013
    assert conn.tables == set()


def test_cleanup_failure_is_visible_and_never_success(mysql_metadata):
    conn = MySQL()
    conn.cleanup_failure = True
    with pytest.raises(setup_db.DatabaseSetupError) as failed:
        setup_db.preflight_database(conn)
    report = failed.value.report
    assert report["code"] == "database_probe_cleanup_required"
    assert report["cleanup_sql"].startswith("DROP TABLE ")
    assert not report["success"]


def test_cancellation_cleans_probe_and_propagates(mysql_metadata, monkeypatch):
    conn = MySQL()
    execute = conn.execute

    def cancel_create(statement, params=None):
        if str(statement).startswith("CREATE TRIGGER"):
            raise asyncio.CancelledError
        return execute(statement, params)

    monkeypatch.setattr(conn, "execute", cancel_create)
    with pytest.raises(asyncio.CancelledError):
        setup_db.preflight_database(conn)
    assert conn.tables == set()


@pytest.mark.asyncio
async def test_failed_preflight_leaves_engine_config_and_schema_untouched(monkeypatch):
    report = setup_db._fail(
        setup_db._report("mysql"), "billing_schema_privilege_required"
    )
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
    assert failed.value.report["schema_changes_started"] is True
    assert db_module.async_engine is None
    engines[0].dispose.assert_awaited_once()
    defaults.assert_not_awaited()
    await SetupService().init_database("mysql+asyncmy://u:p@db/sakura")
    create.assert_not_awaited()
    defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_session_is_explicit_scoped_and_rechecks_runtime_identity(
    monkeypatch,
):
    report = setup_db._report("mysql") | {
        "database": "sakura_test",
        "identity": "runtime@%",
        "guards": [],
    }
    calls = []

    @asynccontextmanager
    async def connect(url, **credentials):
        calls.append((url, credentials))
        yield SimpleNamespace(run_sync=AsyncMock(return_value=report))

    monkeypatch.setattr(setup_db, "setup_connection", connect)
    recheck = AsyncMock(return_value=report.copy())
    monkeypatch.setattr(setup_db, "check_database", recheck)
    url = "mysql+asyncmy://runtime:runtime-pass@remote.example/sakura_test"
    with pytest.raises(ValueError, match="confirmation"):
        await setup_db.repair_database(url, "dba", "one-time-secret")
    assert calls == []
    result = await setup_db.repair_database(
        url, "dba", "one-time-secret", confirmed=True
    )
    assert calls == [
        (url, {}),
        (url, {"username": "dba", "password": "one-time-secret"}),
    ]
    recheck.assert_awaited_once_with(url)
    assert result["repaired"] is True
    assert "one-time-secret" not in json.dumps(result)


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
@pytest.mark.parametrize("prefix", ["/setup/api", "/api/v1/setup"])
async def test_repair_endpoint_requires_setup_token(api_app, prefix):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        response = await client.post(
            prefix + "/database-repair",
            json={
                "url": "mysql://u:p@db/sakura",
                "database_url": "mysql://u:p@db/sakura",
                "username": "admin",
                "password": "do-not-echo",
                "confirmed": True,
            },
        )
    assert response.status_code == 403
    assert "do-not-echo" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,module", [("/setup/api", web_setup), ("/api/v1/setup", api_setup)]
)
async def test_preflight_api_reports_blocked_schema_with_non_success_http(
    api_app, monkeypatch, prefix, module
):
    report = setup_db._fail(
        setup_db._report("mysql"),
        "billing_schema_privilege_required",
        error=db_error(1419),
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
    assert payload["database_error_code"] == 1419


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,module", [("/setup/api", web_setup), ("/api/v1/setup", api_setup)]
)
async def test_save_preflight_failure_cannot_write_config_or_report_success(
    api_app, monkeypatch, prefix, module
):
    report = setup_db._fail(
        setup_db._report("mysql"),
        "billing_schema_privilege_required",
        error=db_error(1419),
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
    report = setup_db._fail(
        setup_db._report("mysql"), "billing_schema_privilege_required"
    )
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
async def test_admin_failure_masks_opaque_credentials_in_actual_console_and_json_sinks(
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
    report = setup_db._report("mysql") | {
        "database": "test",
        "identity": "runtime@%",
        "guards": [],
    }

    @asynccontextmanager
    async def connect(url, **credentials):
        if credentials:
            raise RuntimeError(
                "failed login: opaque-admin-credential and opaque-runtime-credential"
            )
        yield SimpleNamespace(run_sync=AsyncMock(return_value=report))

    monkeypatch.setattr(setup_db, "setup_connection", connect)
    try:
        result = await setup_db.repair_database(
            "mysql://runtime:opaque-runtime-credential@db/test",
            "persistent_dba",
            "opaque-admin-credential",
            confirmed=True,
        )
    finally:
        for sink in sinks:
            logger.remove(sink)
    assert not result["success"]
    assert not result.get("schema_changes_started")
    serialized = path.read_text(encoding="utf-8")
    for output in (console.getvalue(), serialized, json.dumps(result)):
        assert "opaque-admin-credential" not in output
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
