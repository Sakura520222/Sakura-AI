"""Exercise the actual Loguru sinks, bridge and hostile credential surfaces."""

import ast
import io
import json
import logging
import sys
from pathlib import Path

import pytest
from loguru import logger

from backend.core.config import get_settings
from backend.core.log_context import log_context
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
