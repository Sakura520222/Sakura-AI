"""超级管理员彻底清空数据库功能的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql

from backend.services import database_reset_service as reset_module
from backend.services.database_reset_service import (
    DATABASE_RESET_CONFIRMATION,
    DatabaseObjectInventory,
    DatabaseResetError,
    DatabaseResetResult,
    DatabaseResetService,
    _collect_database_objects,
    _drop_database_objects,
)
from backend.webui.deps import (
    get_db,
    get_user_preferences,
    require_csrf_header,
    require_super_admin,
)
from backend.webui.routes import system_config as system_config_routes
from backend.webui.routes.system_config import router


def _route(path: str, method: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route {method} {path} not found")


def _dependency_calls(route: APIRoute) -> list[object]:
    return [dependency.call for dependency in route.dependant.dependencies]


class _JsonRequest:
    def __init__(self, body: object) -> None:
        self.body = body
        self.app = SimpleNamespace(state=SimpleNamespace())

    async def json(self) -> object:
        return self.body


class _FakeAsyncConnection:
    def __init__(
        self,
        events: list[str],
        *,
        fail_inventory: bool = False,
        fail_drop: bool = False,
    ) -> None:
        self.events = events
        self.fail_inventory = fail_inventory
        self.fail_drop = fail_drop

    async def run_sync(self, function, *args):
        self.events.append(function.__name__)
        if function is _collect_database_objects:
            if self.fail_inventory:
                raise RuntimeError("inventory failed")
            return DatabaseObjectInventory(
                tables=("alembic_version", "users"),
                views=("active_users",),
            )
        if function is _drop_database_objects:
            if self.fail_drop:
                raise RuntimeError("drop failed")
            return DatabaseResetResult(
                tables_dropped=2,
                views_dropped=1,
                materialized_views_dropped=0,
                sequences_dropped=0,
            )
        raise AssertionError(f"Unexpected run_sync callback: {function}")


class _FakeBeginContext:
    def __init__(self, connection: _FakeAsyncConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeAsyncConnection:
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


class _FakeAsyncEngine:
    def __init__(self, connection: _FakeAsyncConnection) -> None:
        self.connection = connection
        self.dispose = AsyncMock()

    def begin(self) -> _FakeBeginContext:
        return _FakeBeginContext(self.connection)


class _FakeSyncConnection:
    def __init__(self, dialect) -> None:
        self.dialect = dialect
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)


def test_drop_database_objects_removes_tables_views_and_version_table():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text('CREATE TABLE "users" (id INTEGER PRIMARY KEY, name TEXT)')
        )
        connection.execute(
            text('CREATE TABLE "alembic_version" (version_num TEXT PRIMARY KEY)')
        )
        connection.execute(text('CREATE VIEW "active_users" AS SELECT * FROM "users"'))

        inventory = _collect_database_objects(connection)
        assert inventory.tables == ("alembic_version", "users")
        assert inventory.views == ("active_users",)

        result = _drop_database_objects(connection, inventory)

        assert result.tables_dropped == 2
        assert result.views_dropped == 1
        assert inspect(connection).get_table_names() == []
        assert inspect(connection).get_view_names() == []


def test_mysql_reset_disables_foreign_keys_and_quotes_every_object(monkeypatch):
    connection = _FakeSyncConnection(mysql.dialect())
    inventory = DatabaseObjectInventory(
        tables=("alembic_version", "user-data"),
        views=("active-users",),
    )
    monkeypatch.setattr(
        reset_module,
        "_collect_database_objects",
        lambda _connection: DatabaseObjectInventory(),
    )

    _drop_database_objects(connection, inventory)

    assert connection.statements == [
        "SET FOREIGN_KEY_CHECKS = 0",
        "DROP VIEW IF EXISTS `active-users`",
        "DROP TABLE IF EXISTS `alembic_version`",
        "DROP TABLE IF EXISTS `user-data`",
        "SET FOREIGN_KEY_CHECKS = 1",
    ]


def test_postgresql_reset_uses_cascade_for_all_supported_objects(monkeypatch):
    connection = _FakeSyncConnection(postgresql.dialect())
    inventory = DatabaseObjectInventory(
        tables=("users",),
        views=("active_users",),
        materialized_views=("user_totals",),
        sequences=("manual_counter",),
    )
    monkeypatch.setattr(
        reset_module,
        "_collect_database_objects",
        lambda _connection: DatabaseObjectInventory(),
    )

    _drop_database_objects(connection, inventory)

    assert connection.statements == [
        'DROP MATERIALIZED VIEW IF EXISTS "user_totals" CASCADE',
        'DROP VIEW IF EXISTS "active_users" CASCADE',
        'DROP TABLE IF EXISTS "users" CASCADE',
        'DROP SEQUENCE IF EXISTS "manual_counter" CASCADE',
    ]


@pytest.mark.asyncio
async def test_reset_preserves_original_database_url_and_orders_setup_before_drop(
    monkeypatch,
):
    events: list[str] = []
    connection = _FakeAsyncConnection(events)
    engine = _FakeAsyncEngine(connection)
    database_url = "mysql://user:secret@db.example/sakura"

    monkeypatch.setattr(
        reset_module,
        "read_connection_config",
        lambda: {"database_url": database_url, "setup_completed": True},
    )

    def fake_write(url: str, *, setup_completed: bool) -> None:
        events.append("write_connection_config")
        assert url == database_url
        assert setup_completed is False

    create_engine_mock = MagicMock(return_value=engine)
    monkeypatch.setattr(reset_module, "write_connection_config", fake_write)
    monkeypatch.setattr(reset_module, "create_async_engine", create_engine_mock)

    async def before_drop() -> None:
        events.append("before_drop")

    result = await DatabaseResetService().reset(before_drop=before_drop)

    assert result.total_dropped == 3
    assert events == [
        "_collect_database_objects",
        "write_connection_config",
        "before_drop",
        "_drop_database_objects",
    ]
    assert create_engine_mock.call_args.args[0].startswith("mysql+asyncmy://")
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_inventory_failure_does_not_change_setup_state(monkeypatch):
    connection = _FakeAsyncConnection([], fail_inventory=True)
    engine = _FakeAsyncEngine(connection)
    write_mock = MagicMock()
    before_drop = AsyncMock()

    monkeypatch.setattr(
        reset_module,
        "read_connection_config",
        lambda: {
            "database_url": "postgresql://user:secret@db.example/sakura",
            "setup_completed": True,
        },
    )
    monkeypatch.setattr(reset_module, "write_connection_config", write_mock)
    monkeypatch.setattr(reset_module, "create_async_engine", lambda *_a, **_kw: engine)

    with pytest.raises(DatabaseResetError) as exc_info:
        await DatabaseResetService().reset(before_drop=before_drop)

    assert exc_info.value.setup_state_reset is False
    write_mock.assert_not_called()
    before_drop.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_quiesce_failure_keeps_setup_in_incomplete_safe_state(monkeypatch):
    events: list[str] = []
    connection = _FakeAsyncConnection(events)
    engine = _FakeAsyncEngine(connection)

    monkeypatch.setattr(
        reset_module,
        "read_connection_config",
        lambda: {
            "database_url": "mysql+asyncmy://user:secret@db.example/sakura",
            "setup_completed": True,
        },
    )
    monkeypatch.setattr(
        reset_module,
        "write_connection_config",
        lambda *_args, **_kwargs: events.append("write_connection_config"),
    )
    monkeypatch.setattr(reset_module, "create_async_engine", lambda *_a, **_kw: engine)

    async def fail_before_drop() -> None:
        events.append("before_drop")
        raise RuntimeError("quiesce failed")

    with pytest.raises(DatabaseResetError) as exc_info:
        await DatabaseResetService().reset(before_drop=fail_before_drop)

    assert exc_info.value.setup_state_reset is True
    assert events == [
        "_collect_database_objects",
        "write_connection_config",
        "before_drop",
    ]
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_drop_failure_keeps_setup_in_incomplete_safe_state(monkeypatch):
    events: list[str] = []
    connection = _FakeAsyncConnection(events, fail_drop=True)
    engine = _FakeAsyncEngine(connection)

    monkeypatch.setattr(
        reset_module,
        "read_connection_config",
        lambda: {
            "database_url": "mysql+asyncmy://user:secret@db.example/sakura",
            "setup_completed": True,
        },
    )

    def fake_write(_url: str, *, setup_completed: bool) -> None:
        events.append("write_connection_config")
        assert setup_completed is False

    monkeypatch.setattr(reset_module, "write_connection_config", fake_write)
    monkeypatch.setattr(reset_module, "create_async_engine", lambda *_a, **_kw: engine)

    with pytest.raises(DatabaseResetError) as exc_info:
        await DatabaseResetService().reset()

    assert exc_info.value.setup_state_reset is True
    assert events == [
        "_collect_database_objects",
        "write_connection_config",
        "_drop_database_objects",
    ]
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispose_failure_cannot_mask_success_or_prevent_restart(monkeypatch):
    events: list[str] = []
    connection = _FakeAsyncConnection(events)
    engine = _FakeAsyncEngine(connection)
    engine.dispose.side_effect = RuntimeError("dispose failed")

    monkeypatch.setattr(
        reset_module,
        "read_connection_config",
        lambda: {
            "database_url": "mysql+asyncmy://user:secret@db.example/sakura",
            "setup_completed": True,
        },
    )
    monkeypatch.setattr(
        reset_module,
        "write_connection_config",
        lambda *_args, **_kwargs: events.append("write_connection_config"),
    )
    monkeypatch.setattr(reset_module, "create_async_engine", lambda *_a, **_kw: engine)

    result = await DatabaseResetService().reset()

    assert result.total_dropped == 3
    engine.dispose.assert_awaited_once()


def test_reset_route_requires_super_admin_and_header_csrf():
    dependencies = _dependency_calls(_route("/system-config/reset-database", "POST"))

    assert require_super_admin in dependencies
    assert require_csrf_header in dependencies
    # 请求级 DB session 会一直存活到响应完成，并与 DROP TABLE 形成锁等待。
    assert get_db not in dependencies
    assert get_user_preferences not in dependencies


def test_restart_route_requires_super_admin_csrf_and_audit_session():
    dependencies = _dependency_calls(_route("/system-config/restart", "POST"))

    assert require_super_admin in dependencies
    assert require_csrf_header in dependencies
    assert get_db in dependencies


def test_restart_button_waits_for_new_process_before_refreshing():
    navbar = Path("backend/webui/templates/components/navbar.html").read_text(
        encoding="utf-8"
    )

    assert "waitForApplicationRestart" in navbar
    assert "fetch('/health', { cache: 'no-store' })" in navbar
    assert "observedOffline || startupChanged" in navbar
    assert "window.location.reload()" in navbar


@pytest.mark.asyncio
async def test_restart_route_records_action_and_schedules_restart(monkeypatch):
    audit_mock = AsyncMock()
    restart_mock = MagicMock()
    monkeypatch.setattr(system_config_routes, "log_admin_action", audit_mock)
    monkeypatch.setattr(
        system_config_routes,
        "_schedule_application_restart",
        restart_mock,
    )

    response = await system_config_routes.restart_application(
        db=MagicMock(),
        user={"user_id": 1, "sub": "root"},
        _csrf="valid",
    )

    assert response.status_code == 202
    assert json.loads(response.body) == {"success": True, "restarting": True}
    assert response.headers["cache-control"] == "no-store, max-age=0"
    audit_mock.assert_awaited_once_with(
        ANY,
        1,
        "application_restart",
        "system_core",
        detail={"trigger": "webui"},
    )
    restart_mock.assert_called_once_with()


@pytest.mark.asyncio
async def test_reset_route_rejects_wrong_confirmation_without_mutation(monkeypatch):
    reset_mock = AsyncMock()
    monkeypatch.setattr(
        system_config_routes.database_reset_service,
        "reset",
        reset_mock,
    )

    response = await system_config_routes.reset_database(
        _JsonRequest({"confirmation": "wrong", "language": "en"}),
        user={"user_id": 1, "sub": "root"},
        _csrf="valid",
    )

    assert response.status_code == 400
    assert json.loads(response.body)["restarting"] is False
    assert response.headers["cache-control"] == "no-store, max-age=0"
    reset_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_route_schedules_restart_after_success(monkeypatch):
    reset_result = DatabaseResetResult(
        tables_dropped=12,
        views_dropped=2,
        materialized_views_dropped=1,
        sequences_dropped=3,
    )

    async def reset_with_quiesce(*, before_drop):
        await before_drop()
        return reset_result

    reset_mock = AsyncMock(side_effect=reset_with_quiesce)
    quiesce_mock = AsyncMock()
    restart_mock = MagicMock()
    monkeypatch.setattr(
        system_config_routes.database_reset_service,
        "reset",
        reset_mock,
    )
    monkeypatch.setattr(
        system_config_routes,
        "_schedule_application_restart",
        restart_mock,
    )
    monkeypatch.setattr(
        system_config_routes,
        "quiesce_database_reset_runtime",
        quiesce_mock,
    )

    request = _JsonRequest(
        {
            "confirmation": DATABASE_RESET_CONFIRMATION,
            "language": "zh-CN",
        }
    )
    response = await system_config_routes.reset_database(
        request,
        user={"user_id": 1, "sub": "root"},
        _csrf="valid",
    )
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["restarting"] is True
    assert payload["dropped"] == {
        "tables": 12,
        "views": 2,
        "materialized_views": 1,
        "sequences": 3,
    }
    quiesce_mock.assert_awaited_once_with(request.app)
    restart_mock.assert_called_once_with()


@pytest.mark.asyncio
async def test_reset_route_restarts_in_setup_mode_after_partial_failure(monkeypatch):
    reset_mock = AsyncMock(
        side_effect=DatabaseResetError(setup_state_reset=True),
    )
    restart_mock = MagicMock()
    monkeypatch.setattr(
        system_config_routes.database_reset_service,
        "reset",
        reset_mock,
    )
    monkeypatch.setattr(
        system_config_routes,
        "_schedule_application_restart",
        restart_mock,
    )

    response = await system_config_routes.reset_database(
        _JsonRequest(
            {
                "confirmation": DATABASE_RESET_CONFIRMATION,
                "language": "en",
            }
        ),
        user={"user_id": 1, "sub": "root"},
        _csrf="valid",
    )
    payload = json.loads(response.body)

    assert response.status_code == 500
    assert payload["success"] is False
    assert payload["restarting"] is True
    restart_mock.assert_called_once_with()


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_database_reset_user_visible_translations_exist(language):
    from backend.webui.i18n import i18n

    keys = (
        "system_config.database_reset_title",
        "system_config.database_reset_modal_warning",
        "system_config.database_reset_invalid_confirmation",
        "system_config.database_reset_success",
        "system_config.database_reset_failed",
        "system_config.database_reset_failed_restarting",
        "system_config.database_reset_restart_timeout",
    )
    for key in keys:
        assert i18n.t(key, lang=language) != key
