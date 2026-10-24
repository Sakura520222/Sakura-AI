"""Setup-only capability checks and explicitly requested MySQL guard repair.

Connections here are short lived and never replace the application's engine.
Only disposable probe objects may be changed by a preflight. MySQL DDL commits
implicitly; recovery uses idempotent creation, never rollback or destructive DDL.
"""

import re
import sys
import uuid
from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from backend.core.log_sanitizer import sensitive_log_scope
from backend.models.billing_schema import (
    IMMUTABLE_TABLES,
    BillingSchemaPermissionError,
    _ensure_mysql_guard,
    _validate_mysql_guard,
    mysql_guard_ddl,
    mysql_probe_guard_ddl,
    read_mysql_guard,
)

_PERMISSION_ERRORS = {1044, 1045, 1142, 1143, 1227, 1370, 1419}
_METADATA_ERRORS = _PERMISSION_ERRORS | {1193}


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
    if report.get("stage") in {"ddl", "migration", "defaults", "repair"}:
        return 500
    return 409 if report.get("connected") else 400


def database_error_code(error):
    if isinstance(error, BillingSchemaPermissionError):
        return error.database_error_code
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
        "guards": [],
        "warnings": [],
    }


def _quote(connection, value):
    # Produce SQL source, not a DBAPI-formatted identifier. The MySQL preparer
    # doubles percent signs, and wrapping that in text() would escape them twice
    # (notably the host in an account such as runtime@%).
    quote = chr(96)
    return quote + value.replace(quote, quote + quote) + quote


def _account_sql(connection, identity):
    user, separator, host = identity.rpartition("@")
    if not separator or not user or not host:
        raise ValueError("Invalid database account identity")
    # Account names are identifiers, not SQL string literals. Backtick quoting
    # stays correct with either NO_BACKSLASH_ESCAPES or the default sql_mode.
    return f"{_quote(connection, user)}@{_quote(connection, host)}"


def _schema_statements(connection):
    """Compile the same metadata used by create_all, with inline MySQL indexes.

    Putting indexes inside CREATE TABLE IF NOT EXISTS makes a DBA script safely
    repeatable, including the tables pre-created before the application migrates.
    """
    from backend.models.database import Base, _ensure_model_modules_imported

    _ensure_model_modules_imported()
    for table in Base.metadata.sorted_tables:
        ddl = str(
            CreateTable(table, if_not_exists=True).compile(dialect=connection.dialect)
        )
        indexes = []
        for index in sorted(table.indexes, key=lambda item: item.name):
            sql = str(CreateIndex(index).compile(dialect=connection.dialect))
            prefix, separator, tail = sql.partition(" ON ")
            table_sql = connection.dialect.identifier_preparer.format_table(table)
            if not separator or not tail.startswith(table_sql + " "):
                raise ValueError("Unsupported bootstrap index definition")
            indexes.append(prefix.removeprefix("CREATE ") + tail[len(table_sql) :])
        if indexes:
            before, separator, after = ddl.rpartition("\n)")
            if not separator:
                raise ValueError("Unsupported bootstrap table definition")
            ddl = before + ",\n\t" + ",\n\t".join(indexes) + separator + after
        yield ddl.strip()


def _repair_sql(connection, report):
    database = report.get("database")
    identity = report.get("identity")
    if not database or not identity:
        return ""
    statements = [
        "-- Review with the database administrator; run only in this deployment database.",
        "-- The installer becomes DEFINER: retain that account and its TRIGGER privileges.",
        "-- DDL commits independently. Re-run after interruption; existing data is retained.",
        "-- No global settings, SUPER grants, DROP, or changes to business rows.",
        f"USE {_quote(connection, database)};",
    ]
    # A first-run failure must be repairable before any application DDL has run.
    # Schema definitions and guard bodies come from the authoritative metadata.
    statements.extend(ddl + ";" for ddl in _schema_statements(connection))
    for table in IMMUTABLE_TABLES:
        statements.extend(
            mysql_guard_ddl(table, action, if_not_exists=True) + ";"
            for action in ("UPDATE", "DELETE")
        )
        statements.append(
            f"GRANT TRIGGER ON {_quote(connection, database)}.{_quote(connection, table)} "
            f"TO {_account_sql(connection, identity)};"
        )
    statements.append(
        "-- Conflicting guards are never overwritten. Review them with the DBA, then recheck Setup."
    )
    return "\n\n".join(statements) + "\n"


def _optional_variable(connection, report, key):
    try:
        row = connection.execute(
            text("SHOW GLOBAL VARIABLES WHERE Variable_name = :name"), {"name": key}
        ).one_or_none()
        if row is not None:
            value = str(row[1]).upper()
            if value in {"ON", "TRUE", "1"}:
                return 1
            if value in {"OFF", "FALSE", "0"}:
                return 0
        report["warnings"].append({"code": "metadata_unavailable", "object": key})
        return None
    except DBAPIError as exc:
        if database_error_code(exc) not in _METADATA_ERRORS:
            raise
        report["warnings"].append({"code": "metadata_unavailable", "object": key})
        return None


def _inspect_mysql(connection, report):
    report["database"] = connection.execute(text("SELECT DATABASE()")).scalar_one()
    report["identity"] = connection.execute(text("SELECT CURRENT_USER()")).scalar_one()
    report["version"] = connection.execute(text("SELECT VERSION()")).scalar_one()
    for key in ("log_bin", "log_bin_trust_function_creators"):
        report[key] = _optional_variable(connection, report, key)
    # Never return SHOW GRANTS verbatim: older servers can include auth material.
    try:
        grants = connection.execute(text("SHOW GRANTS")).scalars().all()
        report["privileges"] = sorted(
            {
                privilege
                for grant in grants
                for privilege in ("CREATE", "ALTER", "INDEX", "TRIGGER", "DROP")
                if re.search(rf"\b(?:{privilege}|ALL PRIVILEGES)\b.*\bON\b", grant)
            }
        )
    except DBAPIError as exc:
        if database_error_code(exc) not in _METADATA_ERRORS:
            raise
        report["privileges"] = None
        report["warnings"].append(
            {"code": "metadata_unavailable", "object": "privileges"}
        )
    if not report["database"]:
        return _fail(report, "database_not_selected")
    inspector = inspect(connection)
    views = set(inspector.get_view_names())
    tables = set(inspector.get_table_names())
    report["existing_billing_tables"] = sorted(tables.intersection(IMMUTABLE_TABLES))
    report["partial_schema"] = bool(tables)
    checked_definers = {}
    for table in IMMUTABLE_TABLES:
        if table in views:
            return _fail(report, "database_object_conflict", object_name=table)
        for action in ("UPDATE", "DELETE"):
            name = f"{table}_no_{action.lower()}"
            installed = read_mysql_guard(connection, table, action)
            guard = {
                "name": name,
                "table": table,
                "action": action,
                "status": "missing",
            }
            report["guards"].append(guard)
            if installed is None:
                continue
            try:
                _validate_mysql_guard(installed, table, action)
            except RuntimeError:
                guard["status"] = "conflict"
                return _fail(report, "billing_guard_conflict", object_name=name)
            definer = str(installed.get("DEFINER") or "")
            guard["definer"] = definer
            if not definer or "@" not in definer:
                guard["status"] = "invalid_definer"
                return _fail(report, "billing_guard_definer_invalid", object_name=name)
            if definer not in checked_definers:
                user, _, host = definer.rpartition("@")
                try:
                    exists = connection.execute(
                        text(
                            "SELECT COUNT(*) FROM mysql.user WHERE User = :user AND Host = :host"
                        ),
                        {"user": user, "host": host},
                    ).scalar_one()
                    checked_definers[definer] = bool(exists)
                except DBAPIError as exc:
                    if database_error_code(exc) not in _METADATA_ERRORS:
                        raise
                    # Runtime accounts need not read mysql.user. Be explicit
                    # about this limit instead of granting instance privileges.
                    checked_definers[definer] = None
                    report["warnings"].append(
                        {"code": "definer_dba_verification", "object": definer}
                    )
            if checked_definers[definer] is False:
                guard["status"] = "invalid_definer"
                return _fail(report, "billing_guard_definer_invalid", object_name=name)
            guard["status"] = "valid"
    return report


def _cleanup_probe(connection, table):
    sql = text(f"DROP TABLE {_quote(connection, table)}")
    try:
        connection.execute(sql)
    except SQLAlchemyError, RuntimeError:
        # A disconnect/cancel can invalidate the first connection. Do not lose
        # the cleanup obligation with the failed transaction.
        with connection.engine.begin() as fresh:
            fresh.execute(sql)


def _probe_mysql(connection, report):
    table = "sakura_setup_probe_" + uuid.uuid4().hex
    quoted = _quote(connection, table)
    created = False
    try:
        connection.execute(
            text(f"CREATE TABLE {quoted} (id INT PRIMARY KEY) ENGINE=InnoDB")
        )
        created = True
        connection.execute(text(f"ALTER TABLE {quoted} ADD COLUMN checked INT NULL"))
        connection.execute(text(f"CREATE INDEX checked_index ON {quoted} (checked)"))
        connection.execute(text(f"INSERT INTO {quoted} (id) VALUES (1)"))
        for action in ("UPDATE", "DELETE"):
            connection.execute(text(mysql_probe_guard_ddl(table, action)))
            metadata = read_mysql_guard(connection, table, action)
            if metadata is None:
                raise BillingSchemaPermissionError(table, 1142)
            _validate_mysql_guard(metadata, table, action)
            mutation = (
                f"UPDATE {quoted} SET id = id WHERE id = 1"
                if action == "UPDATE"
                else f"DELETE FROM {quoted} WHERE id = 1"
            )
            try:
                connection.execute(text(mutation))
            except DBAPIError as exc:
                if database_error_code(exc) != 1644:
                    raise
            else:
                return _fail(report, "billing_guard_probe_failed", object_name=action)
        report["checks"].append(
            {"code": "guard_creation_and_rejection", "status": "passed"}
        )
        return report
    except (DBAPIError, BillingSchemaPermissionError) as exc:
        code = database_error_code(exc)
        return _fail(
            report,
            "billing_schema_privilege_required"
            if code in _PERMISSION_ERRORS
            else "database_probe_failed",
            object_name=next(
                (
                    guard["name"]
                    for guard in report["guards"]
                    if guard["status"] == "missing"
                ),
                "schema_ddl",
            ),
            error=exc,
        )
    finally:
        original_error = sys.exception()
        if created:
            try:
                _cleanup_probe(connection, table)
            except Exception:
                _fail(report, "database_probe_cleanup_required", object_name=table)
                report["cleanup_sql"] = f"DROP TABLE {quoted};"
                logger.exception("Setup database probe cleanup failed")
                if original_error is not None and not isinstance(
                    original_error, Exception
                ):
                    original_error.add_note(
                        "Setup probe cleanup requires administrator attention"
                    )
                    raise original_error
                raise DatabaseSetupError(report) from None


def preflight_database(connection, *, probe=True):
    report = _report(connection.dialect.name)
    if connection.dialect.name not in {"mysql", "mariadb"}:
        report["checks"].append(
            {"code": "mysql_guards_not_applicable", "status": "skipped"}
        )
        return report
    try:
        _inspect_mysql(connection, report)
        if report["success"] and any(
            item["status"] == "missing" for item in report["guards"]
        ):
            report["needs_guard_installation"] = True
            if probe:
                _probe_mysql(connection, report)
        else:
            report["needs_guard_installation"] = False
        report["repair_sql"] = _repair_sql(connection, report)
        return report
    except DBAPIError as exc:
        return _fail(report, "database_preflight_failed", error=exc)


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


def _repair_mysql(connection, runtime_report):
    lock_name = "sakura_setup_" + str(runtime_report["database"])
    # Named locks survive MySQL DDL commits. NullPool closes this physical
    # connection and releases its lock on every exit, including cancellation.
    acquired = connection.execute(
        text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name[:64]}
    ).scalar_one()
    if acquired != 1:
        raise DatabaseSetupError(
            _fail(runtime_report, "database_setup_busy", stage="repair")
        )
    report = preflight_database(connection, probe=False)
    if not report["success"]:
        raise DatabaseSetupError(report)
    if report["database"] != runtime_report["database"]:
        raise DatabaseSetupError(
            _fail(report, "database_target_mismatch", stage="repair")
        )
    # Preflight already rejected conflicting definitions. Never replace guards
    # or alter existing data; bootstrap is the same schema-only create_all DDL.
    runtime_report["schema_changes_started"] = True
    for ddl in _schema_statements(connection):
        connection.execute(text(ddl))
    for table in IMMUTABLE_TABLES:
        for action in ("UPDATE", "DELETE"):
            _ensure_mysql_guard(connection, table, action)
        if any(
            item["table"] == table and item["status"] == "missing"
            for item in runtime_report["guards"]
        ):
            connection.execute(
                text(
                    f"GRANT TRIGGER ON {_quote(connection, report['database'])}.{_quote(connection, table)} "
                    f"TO {_account_sql(connection, runtime_report['identity'])}"
                )
            )
    connection.commit()


async def repair_database(database_url, username, password, *, confirmed=False):
    if confirmed is not True:
        raise ValueError("Explicit database repair confirmation is required")
    from backend.models.database import normalize_database_url

    try:
        url = make_url(normalize_database_url(database_url))
    except ArgumentError:
        raise ValueError("Invalid database URL") from None
    if url.get_backend_name() not in {"mysql", "mariadb"} or not username.strip():
        raise ValueError("MySQL and a persistent administrator account are required")
    report = _report(url.get_backend_name())
    with sensitive_log_scope(database_url, url.password, password):
        try:
            # Read the runtime identity from the server, never from a browser-
            # supplied GRANT target. Check the same host/port/database using a
            # separate, temporary admin connection.
            async with setup_connection(database_url) as connection:
                runtime_report = await connection.run_sync(
                    lambda sync: preflight_database(sync, probe=False)
                )
            if not runtime_report["success"]:
                return runtime_report
            report = runtime_report
            async with setup_connection(
                database_url, username=username, password=password
            ) as connection:
                await connection.run_sync(
                    lambda sync: _repair_mysql(sync, runtime_report)
                )
            result = await check_database(database_url)
            result["stage"] = "repair"
            result["repaired"] = result["success"]
            logger.bind(
                database_type=url.get_backend_name(),
                setup_stage="repair",
                database_error_code=result.get("database_error_code"),
            ).info("Setup MySQL guard repair completed: success={}", result["success"])
            return result
        except DatabaseSetupError as exc:
            return exc.report
        except Exception as exc:
            logger.opt(exception=exc).warning("Setup MySQL guard repair failed")
            if report.get("schema_changes_started"):
                report["partial_schema"] = True
            code = (
                "database_admin_authentication_failed"
                if database_error_code(exc) == 1045
                else "billing_schema_privilege_required"
                if database_error_code(exc) in _PERMISSION_ERRORS
                else "database_repair_failed"
            )
            return _fail(report, code, stage="repair", error=exc)
