"""Exercise the actual Loguru sinks, bridge and hostile credential surfaces."""

import ast
import asyncio
import io
import json
import logging
import sys
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote

import h11
import pytest
from loguru import logger
from redis.asyncio.connection import parse_url
from starlette.middleware.errors import ServerErrorMiddleware
from uvicorn.protocols.http.flow_control import FlowControl
from uvicorn.protocols.http.h11_impl import RequestResponseCycle

from backend.core.config import get_settings
from backend.core.log_context import (
    RequestLogContextMiddleware,
    get_log_context,
    log_context,
)
from backend.core.log_sanitizer import sanitize, sanitize_text, sensitive_log_scope
from backend.core.logging_bridge import (
    InterceptHandler,
    _format_record,
    _patch_record,
    _sink_filter,
)
from backend.services.system_config_service import SystemConfigService


@pytest.fixture
def secured_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(logger._core, "patcher", _patch_record)
    settings = get_settings()
    monkeypatch.setattr(settings, "log_level", "DEBUG")
    records = []
    console = io.StringIO()
    path = tmp_path / "app_test.log"
    ids = [
        logger.add(
            console, format=_format_record, filter=_sink_filter, level=0, catch=False
        ),
        logger.add(
            path,
            format=_format_record,
            filter=_sink_filter,
            level=0,
            serialize=True,
            catch=False,
        ),
        logger.add(lambda msg: records.append(msg.record), level=0, catch=False),
    ]
    try:
        yield console, path, records
    finally:
        for handler in ids:
            logger.remove(handler)


@pytest.mark.parametrize(
    "payload",
    [
        "Authorization: Bearer super-secret\nContent-Type: application/json",
        "{'Authorization': 'Basic super-secret', 'Accept': 'application/json'}",
        "{'api_key': 'super-secret'}",
        "Headers([(b'x-api-key', b'super-secret')])",
        "Cookie: sid=super-secret; csrf=super-secret",
        "https://provider.test/?access_token=super-secret&page=3",
        "/oauth/callback?code=super-secret&state=super-secret",
        "/oauth?%63ode=super-secret&%2573tate=super-secret",
        "https://provider.test/?key=super-secret",
        "mysql://user:super-secret@host/db",
        '{"client_secret":"super-secret","code_verifier":"super-secret"}',
        "-----BEGIN PRIVATE KEY-----\nsuper-secret\n-----END PRIVATE KEY-----",
        "password='super-secret with whitespace'",
    ],
)
def test_credentials_are_redacted_without_erasing_diagnostics(payload):
    assert "super-secret" not in sanitize_text(payload)
    assert sanitize_text("HTTP 429 provider=example timeout=30 input_tokens=128") == (
        "HTTP 429 provider=example timeout=30 input_tokens=128"
    )


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("redis://:opaque-redis-pw@host:6379/3", "redis://:***@host:6379/3"),
        ("rediss://:opaque-redis-pw@[::1]:6380/3", "rediss://:***@[::1]:6380/3"),
        (
            "redis://:opaque%40redis%3Apw@host/3?socket_timeout=2",
            "redis://:***@host/3?socket_timeout=2",
        ),
        ("redis://default:opaque-redis-pw@host/3", "redis://default:***@host/3"),
        ("redis://app@team:opaque-redis-pw@host/3", "redis://app@team:***@host/3"),
        ("redis://:opaque@redis@pw@host/3", "redis://:***@host/3"),
        ("redis://default:opaque@redis@pw@host/3", "redis://default:***@host/3"),
        (
            "redis://:opaque@pw@host?name=user@example.test#details@support",
            "redis://:***@host?name=user@example.test#details@support",
        ),
        ("unix://:opaque-redis-pw@/tmp/redis.sock", "unix://:***@/tmp/redis.sock"),
        (
            "redis://host:6379/3?socket_timeout=2",
            "redis://host:6379/3?socket_timeout=2",
        ),
        ("rediss://[::1]:6380/3", "rediss://[::1]:6380/3"),
        ("redis://default@host/3", "redis://default@host/3"),
        ("redis://default:@host/3", "redis://default:@host/3"),
        ("redis://app@team:@host/3", "redis://app@team:@host/3"),
        (
            "redis://host:6379?name=user@example.test#details@support",
            "redis://host:6379?name=user@example.test#details@support",
        ),
    ],
)
def test_redis_uri_redaction_preserves_connection_diagnostics(uri, expected):
    assert sanitize_text(uri) == expected
    assert sanitize({"endpoint": uri}) == {"endpoint": expected}


@pytest.mark.parametrize("standard", [False, True])
@pytest.mark.parametrize("scheme", ["redis", "rediss"])
def test_password_only_redis_url_is_safe_in_exception_and_persisted_logs(
    secured_logs, monkeypatch, standard, scheme
):
    console, path, records = secured_logs
    password = "opaque-redis-password-718"
    uri = f"{scheme}://:{password}@host:6379/3?socket_timeout=2"
    monkeypatch.setattr(get_settings(), "redis_url", uri)
    try:
        raise RuntimeError(f"connection failed for {uri}")
    except RuntimeError as error:
        if standard:
            record = logging.LogRecord(
                "redis.client",
                logging.ERROR,
                "/redis/client.py",
                42,
                "Redis error at %s",
                (uri,),
                sys.exc_info(),
                func="connect",
            )
            record.endpoint = uri
            InterceptHandler().emit(record)
        else:
            logger.bind(endpoint=uri).opt(exception=error).error(
                "Redis error at {}", uri
            )

    output = console.getvalue() + path.read_text(encoding="utf-8")
    assert password not in output and password not in str(records)
    clean = f"{scheme}://:***@host:6379/3?socket_timeout=2"
    stored = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert stored["record"]["extra"]["endpoint"] == clean
    assert "RuntimeError" in stored["text"] and "Traceback" in stored["text"]
    assert clean in stored["text"]


@pytest.mark.parametrize("standard", [False, True])
@pytest.mark.parametrize("config_available", [True, False])
@pytest.mark.parametrize(
    ("uri", "clean"),
    [
        ("redis://:opaque redis password@host/3", "redis://:***@host/3"),
        ("redis://:opaque\tredis password@host/3", "redis://:***@host/3"),
        ("redis://:opaque%40redis%3Apw@host/3", "redis://:***@host/3"),
        ("redis://:opaque%2540redis%253Apw@host/3", "redis://:***@host/3"),
        (
            "unix://:opaque-redis-password@/tmp/redis.sock",
            "unix://:***@/tmp/redis.sock",
        ),
        (
            "redis://host:6379/3?password=opaque+redis%40pw",
            "redis://host:6379/3?password=***",
        ),
    ],
)
def test_configured_redis_password_echo_is_redacted(
    secured_logs, monkeypatch, uri, clean, config_available, standard
):
    console, path, records = secured_logs
    if config_available:
        monkeypatch.setattr(get_settings(), "redis_url", uri)
    else:

        def unavailable_settings():
            raise ValueError("configuration could not be loaded")

        monkeypatch.setattr("backend.core.config.get_settings", unavailable_settings)
        monkeypatch.setenv("REDIS_URL", uri)

    # Use the installed driver's interpretation without making a Redis connection.
    password = parse_url(uri)["password"]
    encoded_password = quote(password, safe="")
    try:
        raise RuntimeError(f"Redis rejected {password}; endpoint {uri}")
    except RuntimeError as error:
        fields = {
            "endpoint": uri,
            "echoed_value": password,
            "encoded_value": encoded_password,
            "encoded_endpoint": quote(uri, safe=""),
            "diagnostic": "connection failed at host:6379 database 3",
        }
        if standard:
            record = logging.LogRecord(
                "redis.client",
                logging.ERROR,
                "/redis/client.py",
                42,
                "Redis connection failed",
                (),
                sys.exc_info(),
                func="connect",
            )
            record.__dict__.update(fields)
            InterceptHandler().emit(record)
        else:
            logger.bind(**fields).opt(exception=error).error("Redis connection failed")

    output = console.getvalue() + path.read_text(encoding="utf-8") + str(records)
    assert password not in output and encoded_password not in output
    stored = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    extra = stored["record"]["extra"]
    assert extra["endpoint"] == clean
    assert extra["echoed_value"] == "***" and extra["encoded_value"] == "***"
    assert "***" in extra["encoded_endpoint"]
    assert extra["diagnostic"] == "connection failed at host:6379 database 3"
    assert "Traceback" in extra["exception"] and "RuntimeError" in extra["exception"]


@pytest.mark.parametrize(
    "uri",
    [
        "redis://host:6379/3?name=user@example.test#details@support",
        "rediss://[::1]:6380/3?socket_timeout=2",
        "redis://default@host/3",
        "redis://default:@host/3",
        "redis://app@team:@host/3",
    ],
)
def test_configured_redis_url_without_password_keeps_diagnostics(
    secured_logs, monkeypatch, uri
):
    console, path, records = secured_logs
    monkeypatch.setattr(get_settings(), "redis_url", uri)
    logger.bind(endpoint=uri).info("Redis connection to {}", uri)
    assert records[-1]["extra"]["endpoint"] == uri
    assert uri in console.getvalue()
    assert uri in path.read_text(encoding="utf-8")


def test_invalid_redis_config_does_not_break_error_logging(secured_logs, monkeypatch):
    console, path, records = secured_logs
    uri = "redis://:opaque-invalid-redis-password@[broken"
    monkeypatch.setattr(get_settings(), "redis_url", uri)
    try:
        parse_url(uri)
    except ValueError as error:
        logger.bind(endpoint=uri).opt(exception=error).error(
            "Invalid Redis URL: {}", uri
        )
    output = console.getvalue() + path.read_text(encoding="utf-8") + str(records)
    assert "opaque-invalid-redis-password" not in output
    assert records[-1]["extra"]["endpoint"] == "redis://:***@[broken"
    assert "ValueError: Invalid IPv6 URL" in output


def test_structured_redaction_is_recursive_and_does_not_mutate():
    original = {
        "headers": [(b"x-api-key", b"secret-value")],
        "nested": {"accessToken": "token-value"},
        "input_tokens": 42,
        "code": 401,
    }
    clean = sanitize(original)
    assert "secret-value" not in str(clean)
    assert "token-value" not in str(clean)
    assert clean["input_tokens"] == 42
    assert clean["code"] == 401
    assert original["headers"][0][1] == b"secret-value"
    recursive = {}
    recursive["nested"] = recursive
    assert sanitize(recursive) == {"nested": "<recursive>"}


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_escaped_nested_json_credentials_are_redacted(depth):
    payload = {"api_key": "nested-opaque-secret", "status": 429}
    for _ in range(depth):
        payload = {"body": json.dumps(payload)}
    message = "provider error " + json.dumps(payload)
    result = sanitize_text(message)
    assert "nested-opaque-secret" not in result
    assert "429" in result


@pytest.mark.parametrize(
    "value", ['b"opaque secret with spaces"', '"opaque \\"secret\\" with spaces"']
)
def test_quoted_secret_values_are_consumed_completely(value):
    result = sanitize_text("api_key=" + value + " status=429")
    assert "opaque" not in result
    assert "with spaces" not in result
    assert "status=429" in result


def test_exception_chain_notes_group_and_bound_fields_are_sanitized(secured_logs):
    console, path, records = secured_logs
    credential = "opaque-provider-value"
    try:
        with sensitive_log_scope(credential):
            try:
                raise ValueError(f"provider rejected {credential}")
            except ValueError as cause:
                error = ExceptionGroup(
                    "batch failure", [cause, RuntimeError("api_key=another-secret")]
                )
                error.add_note("OAuth https://example.test/?state=state-secret")
                raise error from cause
    except ExceptionGroup as error:
        with sensitive_log_scope(credential), log_context(request_id="request-1"):
            logger.bind(headers={"Authorization": "Bearer header-secret"}).opt(
                exception=error
            ).error("Provider failed")
    output = console.getvalue() + path.read_text(encoding="utf-8")
    for secret in (credential, "another-secret", "state-secret", "header-secret"):
        assert secret not in output
        assert secret not in str(records)
    assert "ValueError" in output and "ExceptionGroup" in output
    assert "test_exception_chain_notes_group_and_bound_fields_are_sanitized" in output
    assert "request-1" in output
    assert records[-1]["exception"] is None
    assert "Traceback" in records[-1]["extra"]["exception"]


def test_bridge_uses_logrecord_origin_not_the_forwarding_stack(secured_logs):
    _, path, records = secured_logs
    try:
        raise RuntimeError("failed with access_token=hidden-token")
    except RuntimeError:
        record = logging.LogRecord(
            "third.party.client",
            logging.ERROR,
            "/actual/client.py",
            123,
            "request %s",
            ("failed",),
            sys.exc_info(),
            func="send_request",
        )
    record.request_id = "request-origin"
    record.headers = {"x-api-key": "opaque-secret"}
    InterceptHandler().emit(record)
    actual = records[-1]
    assert (actual["name"], actual["module"], actual["function"], actual["line"]) == (
        "third.party.client",
        "client",
        "send_request",
        123,
    )
    assert actual["file"].path == "/actual/client.py"
    assert actual["extra"]["request_id"] == "request-origin"
    stored = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert stored["record"]["file"]["path"] == "/actual/client.py"
    assert "RuntimeError" in stored["text"]
    assert "hidden-token" not in str(stored)
    assert "opaque-secret" not in str(stored)


@pytest.mark.asyncio
async def test_webui_live_level_changes_both_sinks_without_replacing_them(secured_logs):
    console, path, _ = secured_logs
    service = SystemConfigService()
    await service.apply_live_settings({"log_level": {"new": "ERROR"}})
    logger.info("filtered-info-marker")
    logger.error("visible-error-marker")
    await service.apply_live_settings({"log_level": {"new": "DEBUG"}})
    logger.debug("visible-debug-marker")
    standard = logging.LogRecord(
        "example", logging.DEBUG, __file__, 1, "visible-standard-debug", (), None
    )
    InterceptHandler().emit(standard)
    for output in (console.getvalue(), path.read_text(encoding="utf-8")):
        assert "filtered-info-marker" not in output
        assert "visible-error-marker" in output
        assert "visible-debug-marker" in output
        assert "visible-standard-debug" in output


def test_configured_secret_is_removed_even_without_a_field_name(
    secured_logs, monkeypatch
):
    console, path, _ = secured_logs
    monkeypatch.setattr(get_settings(), "github_webhook_secret", "opaque-config-secret")
    logger.error("upstream echoed {}", "opaque-config-secret")
    assert "opaque-config-secret" not in console.getvalue() + path.read_text(
        encoding="utf-8"
    )


def test_no_loguru_exc_info_regression():
    for path in Path("backend").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {
            a.asname or a.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "loguru"
            for a in node.names
            if a.name == "logger"
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in aliases
            ):
                assert not any(kw.arg == "exc_info" for kw in node.keywords), str(path)


@pytest.mark.parametrize(
    "environment,explicit,visible",
    [
        ("production", None, False),
        ("development", None, True),
        ("development", "WARNING", False),
    ],
)
def test_environment_defaults_respect_explicit_log_level(
    monkeypatch, environment, explicit, visible
):
    from backend.core.config import Settings

    monkeypatch.delenv("LOG_LEVEL", raising=False)
    values = {"sakura_env": environment}
    if explicit:
        values["log_level"] = explicit
    settings = Settings(**values)
    monkeypatch.setattr("backend.core.config.get_settings", lambda: settings)
    output = io.StringIO()
    sink = logger.add(output, level=0, filter=_sink_filter, catch=False)
    try:
        logger.debug("development-detail")
        logger.warning("warning-visible")
    finally:
        logger.remove(sink)
    assert ("development-detail" in output.getvalue()) is visible
    assert "warning-visible" in output.getvalue()


def test_standard_logging_stacklevel_preserves_the_real_caller(
    secured_logs, monkeypatch
):
    import inspect

    _, _, records = secured_logs
    standard = logging.getLogger("native.origin")
    monkeypatch.setattr(standard, "handlers", [InterceptHandler()])
    monkeypatch.setattr(standard, "level", logging.DEBUG)
    monkeypatch.setattr(standard, "propagate", False)

    def wrapper():
        standard.warning("wrapper warning", stacklevel=2)

    expected_line = inspect.currentframe().f_lineno + 1
    wrapper()
    record = records[-1]
    assert record["name"] == "native.origin"
    assert record["file"].path == __file__
    assert (
        record["function"]
        == "test_standard_logging_stacklevel_preserves_the_real_caller"
    )
    assert record["line"] == expected_line


def test_local_variables_are_never_dumped_even_in_debug_mode(secured_logs):
    console, path, _ = secured_logs

    def failing_function():
        private_value = "must-not-dump-locals"
        assert private_value
        raise RuntimeError("a normal failure")

    try:
        failing_function()
    except RuntimeError:
        logger.exception("failure with traceback")
    output = console.getvalue() + path.read_text(encoding="utf-8")
    assert "failing_function" in output and "RuntimeError" in output
    assert "must-not-dump-locals" not in output


async def _run_request_server(app, scope, monkeypatch):
    """Exercise real server exception handling without a network listener."""
    standard = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(standard, "handlers", [InterceptHandler()])
    monkeypatch.setattr(standard, "level", logging.ERROR)
    monkeypatch.setattr(standard, "propagate", False)
    connection = h11.Connection(h11.SERVER)
    connection.receive_data(b"GET /failure HTTP/1.1\r\nHost: testserver\r\n\r\n")
    connection.next_event()
    connection.next_event()
    transport = Mock(spec=asyncio.Transport)
    cycle = RequestResponseCycle(
        scope=scope,
        conn=connection,
        transport=transport,
        flow=FlowControl(transport),
        logger=standard,
        access_logger=standard,
        access_log=False,
        default_headers=[],
        message_event=asyncio.Event(),
        on_response=Mock(),
    )
    await cycle.run_asgi(ServerErrorMiddleware(RequestLogContextMiddleware(app)))
    return cycle, transport


def _request_scope(headers=(), query=b""):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": "/failure",
        "raw_path": b"/failure",
        "root_path": "",
        "query_string": query,
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["code", "state", "encoded", "x-api-key", "cookie"])
@pytest.mark.parametrize("kind", ["error", "cause", "group", "cancelled", "base_group"])
async def test_request_secret_survives_outer_server_exception_logging(
    secured_logs, monkeypatch, source, kind
):
    console, path, records = secured_logs
    secret = "opaque-request-value-713"
    headers = []
    query = b""
    if source == "encoded":
        secret += "+/with space"
        query = ("CoDe=" + quote(secret, safe="")).encode()
    elif source in {"code", "state"}:
        query = f"{source}={secret}".encode()
    elif source == "cookie":
        secret = "session=" + secret
        headers = [(b"cookie", secret.encode())]
    else:
        headers = [(source.encode(), secret.encode())]

    message = f"transport rejected {secret}; HTTP 503 timeout=30 input_tokens=42"
    if kind == "error":
        error = RuntimeError(message)
    elif kind == "cause":
        error = RuntimeError(
            "request validation failed; HTTP 503 timeout=30 input_tokens=42"
        )
        error.__cause__ = ValueError(message)
    elif kind == "group":
        error = ExceptionGroup("request batch " + secret, [ValueError(message)])
        error.add_note("private note " + secret)
    elif kind == "cancelled":
        error = asyncio.CancelledError(message)
    else:
        error = BaseExceptionGroup(
            "cancelled request " + secret,
            [asyncio.CancelledError(message), RuntimeError(message)],
        )
    original_args = error.args
    observed = []

    async def failing_app(scope, receive, send):
        observed.append(get_log_context()["request_id"])
        raise error

    cycle, transport = await _run_request_server(
        failing_app, _request_scope(headers, query), monkeypatch
    )
    assert error.args == original_args
    assert get_log_context() == {}
    assert sanitize_text(secret) == secret  # no process-global credential cache
    assert cycle.response_complete
    wire = b"".join(call.args[0] for call in transport.write.call_args_list)
    assert b"500 Internal Server Error" in wire
    assert secret.encode() not in wire
    outer = [record for record in records if record["name"] == "uvicorn.error"]
    assert len(outer) == 1
    assert "HTTP 503 timeout=30 input_tokens=42" in outer[0]["extra"]["exception"]
    if isinstance(error, Exception):
        assert outer[0]["extra"]["request_id"] == observed[0]
    inner_failures = [
        record for record in records if "HTTP request failed" in record["message"]
    ]
    assert len(inner_failures) == (1 if isinstance(error, Exception) else 0)
    output = console.getvalue() + path.read_text(encoding="utf-8") + str(records)
    assert secret not in output and quote(secret, safe="") not in output
    stored = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert stored[-1]["record"]["name"] == "uvicorn.error"
    assert "Traceback" in stored[-1]["text"] and "***" in stored[-1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["leaf", "subgroup", "split_match", "split_rest"])
async def test_request_group_derivatives_keep_scoped_secrets(secured_logs, operation):
    console, path, _ = secured_logs
    secret = "opaque-group-request-921"
    group = ExceptionGroup(
        "request batch " + secret,
        [ValueError("leaf " + secret), RuntimeError("other " + secret)],
    )
    group._log_redaction_secrets = ("earlier-provider-secret",)

    async def failing_app(scope, receive, send):
        raise group

    with pytest.raises(ExceptionGroup) as caught:
        await RequestLogContextMiddleware(failing_app)(
            _request_scope(query=f"code={secret}".encode()), None, None
        )
    assert caught.value is group
    if operation == "leaf":
        error = group.exceptions[0]
    elif operation == "subgroup":
        error = group.subgroup(ValueError)
    else:
        match, rest = group.split(ValueError)
        error = match if operation == "split_match" else rest
    logger.opt(exception=error).error("derived request failure")
    output = console.getvalue() + path.read_text(encoding="utf-8")
    assert secret not in output and "earlier-provider-secret" not in output
    assert "***" in output and "ValueError" in output


@pytest.mark.asyncio
async def test_request_failure_secrets_are_isolated_and_preserve_existing_metadata(
    secured_logs,
):
    barrier = asyncio.Barrier(2)
    saved = []

    async def run(number):
        secret = f"opaque-request-isolation-{number}"
        inherited = f"opaque-caller-secret-{number}"
        error = RuntimeError(f"echo {secret} and {inherited}")
        error._log_redaction_secrets = ("earlier-provider-secret",)
        error._log_correlation = {
            "session_id": number,
            "logical_call_id": f"provider-{number}",
        }

        async def app(scope, receive, send):
            await barrier.wait()
            raise error

        with (
            log_context(clear=True, request_id=f"caller-{number}"),
            sensitive_log_scope(inherited),
        ):
            with pytest.raises(RuntimeError) as caught:
                await RequestLogContextMiddleware(app)(
                    _request_scope(query=f"code={secret}".encode()), None, None
                )
            assert caught.value is error
            assert get_log_context() == {"request_id": f"caller-{number}"}
            assert sanitize_text(inherited) == "***" and sanitize_text(secret) == secret
        assert set(error._log_redaction_secrets) == {
            secret,
            inherited,
            "earlier-provider-secret",
        }
        assert error._log_correlation == {
            "session_id": number,
            "logical_call_id": f"provider-{number}",
        }
        saved.append(error)

    await asyncio.gather(run(1), run(2))
    assert get_log_context() == {}
    console, path, _ = secured_logs
    for error in saved:
        logger.opt(exception=error).error("deferred request failure")
    output = console.getvalue() + path.read_text(encoding="utf-8")
    assert "opaque-request-isolation" not in output
    assert "opaque-caller-secret" not in output


@pytest.mark.asyncio
async def test_successful_request_and_non_http_passthrough_remain_compatible(
    secured_logs,
):
    secret = "opaque-success-control-852"
    messages = []

    async def app(scope, receive, send):
        if scope["type"] == "http":
            assert sanitize_text(secret) == "***"
        else:
            assert get_log_context() == {"request_id": "caller-control"}
            assert "request_id" not in scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        messages.append(message)

    middleware = RequestLogContextMiddleware(app)
    with log_context(clear=True, request_id="caller-control"):
        await middleware(_request_scope(query=f"code={secret}".encode()), None, send)
        assert get_log_context() == {"request_id": "caller-control"}
        assert messages[0]["status"] == 200
        assert b"x-request-id" in dict(messages[0]["headers"])
        await middleware({"type": "websocket"}, None, send)
    assert sanitize_text(secret) == secret


def test_scoped_exception_retention_handles_cycles_without_mutating_errors(
    secured_logs,
):
    console, path, _ = secured_logs
    secret = "opaque-cycle-request-623"
    error = RuntimeError("root " + secret)
    cause = ValueError("cause " + secret)
    error.__cause__ = cause
    cause.__context__ = error
    cause._log_redaction_secrets = ("earlier-provider-secret",)
    error.add_note("a diagnostic note")
    with pytest.raises(RuntimeError) as caught:
        with sensitive_log_scope(secret):
            raise error
    assert caught.value is error and error.__cause__ is cause
    assert cause.__context__ is error and error.__notes__ == ["a diagnostic note"]
    assert set(cause._log_redaction_secrets) == {secret, "earlier-provider-secret"}
    logger.opt(exception=cause).error("isolated cause failure")
    output = console.getvalue() + path.read_text(encoding="utf-8")
    assert secret not in output
    assert "ValueError" in output and "diagnostic note" in output


def test_empty_or_handled_scopes_do_not_add_unrelated_exception_metadata():
    empty_error = RuntimeError("normal error")
    with pytest.raises(RuntimeError) as caught:
        with sensitive_log_scope():
            raise empty_error
    assert caught.value is empty_error
    assert not hasattr(empty_error, "_log_redaction_secrets")
    handled = ValueError("already handled")
    with sensitive_log_scope("opaque-handled-control"):
        try:
            raise handled
        except ValueError:
            pass
    assert not hasattr(handled, "_log_redaction_secrets")
    assert sanitize_text("opaque-handled-control") == "opaque-handled-control"
