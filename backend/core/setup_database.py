"""Setup-only database connection checks.

Connections here are short lived and never replace the application's engine.
Setup verifies connectivity and that a database is selected; every schema
capability question is answered by the application's own engine when it runs
its ordinary CREATE/ALTER/INDEX migrations, and any real error is reported
with its actual stage. Historical note: the former Billing Guard preflight
(probe tables, DEFINER/binlog/TRIGGER checks, admin repair flow) was removed
entirely (issue #715).
"""

from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.core.log_sanitizer import sensitive_log_scope


class DatabaseSetupError(RuntimeError):
    """A safe, structured Setup error, with no driver payload or credentials."""

    def __init__(self, report):
        self.report = report
        super().__init__(report["code"])


def has_setup_database_token(request):
    from backend.core.bootstrap import _COOKIE_NAME, validate_setup_token

    token = request.cookies.get(_COOKIE_NAME) or request.headers.get(
        "x-setup-token", ""
    )
    return bool(token and validate_setup_token(token))


def setup_database_status(report):
    if report["success"]:
        return 200
    if report.get("stage") in {"ddl", "migration", "defaults"}:
        return 500
    return 409 if report.get("connected") else 400


def database_error_code(error):
    args = getattr(getattr(error, "orig", None), "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def _fail(report, code, *, object_name=None, error=None, stage="preflight"):
    report.update(
        success=False,
        schema_ready=False,
        code=code,
        message=code,
        stage=stage,
        retryable=True,
        failed_object=object_name,
        database_error_code=database_error_code(error),
    )
    return report


def _report(dialect):
    return {
        "success": True,
        "connected": True,
        "schema_ready": True,
        "initialized": False,
        "stage": "preflight",
        "code": "database_preflight_ready",
        "message": "database_preflight_ready",
        "dialect": dialect,
        "retryable": True,
        "checks": [],
        "warnings": [],
    }


def preflight_database(connection):
    """Verify the connection works and a default database is selected.

    This intentionally runs only ordinary queries that any valid application
    account can execute. No privilege inspection, metadata verification, probe
    objects, or global-settings checks belong here.
    """
    report = _report(connection.dialect.name)
    dialect = connection.dialect.name
    try:
        if dialect in {"mysql", "mariadb"}:
            database = connection.execute(text("SELECT DATABASE()")).scalar_one()
            version = connection.execute(text("SELECT VERSION()")).scalar_one()
        else:
            database = connection.execute(
                text("SELECT current_database()")
            ).scalar_one()
            version = connection.execute(text("SELECT version()")).scalar_one()
    except DBAPIError as exc:
        return _fail(report, "database_preflight_failed", error=exc)
    report["database"] = database
    report["version"] = version
    if not database:
        return _fail(report, "database_not_selected")
    report["checks"].append({"code": "connection", "status": "passed"})
    return report


@asynccontextmanager
async def setup_connection(database_url, *, username=None, password=None):
    """Use an unpooled connection; keep every secret scoped through disposal."""
    from backend.models.database import normalize_database_url

    if not isinstance(database_url, str) or not database_url:
        raise ValueError("Invalid database URL")
    try:
        url = make_url(normalize_database_url(database_url))
    except ArgumentError:
        raise ValueError("Invalid database URL") from None
    if url.get_backend_name() not in {"mysql", "mariadb", "postgresql"}:
        raise ValueError("Unsupported database type")
    if set(url.query).intersection(
        {
            "user",
            "username",
            "password",
            "host",
            "port",
            "db",
            "database",
            "init_command",
        }
    ):
        raise ValueError("Database URL query cannot override identity or target")
    if username is not None:
        url = url.set(username=username, password=password)
    with sensitive_log_scope(
        database_url, url.render_as_string(hide_password=False), url.password
    ):
        engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.connect() as connection:
                yield connection
        finally:
            await engine.dispose()


async def check_database(database_url):
    with sensitive_log_scope(database_url):
        try:
            async with setup_connection(database_url) as connection:
                result = await connection.run_sync(preflight_database)
            logger.bind(
                database_type=result["dialect"],
                setup_stage=result["stage"],
                database_error_code=result.get("database_error_code"),
            ).info("Setup database preflight completed: success={}", result["success"])
            return result
        except DatabaseSetupError as exc:
            return exc.report
        except Exception as exc:
            logger.opt(exception=exc).warning("Setup database preflight failed")
            report = _report("unknown")
            report["connected"] = False
            return _fail(report, "database_preflight_failed", error=exc)
