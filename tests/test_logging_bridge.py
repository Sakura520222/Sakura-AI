import logging
import sys

from backend.core.logging_bridge import InterceptHandler
from backend.core.logging_bridge import _redact_standard_log_message


def test_intercept_handler_preserves_logger_name_message_and_exception(monkeypatch):
    calls = {}

    class LoguruLogger:
        def level(self, name):
            assert name == "ERROR"
            return type("Level", (), {"name": name})()

        def opt(self, **kwargs):
            calls["exception"] = kwargs["exception"]
            return self

        def log(self, level, message, *args):
            calls["level"] = level
            calls["message"] = message
            calls["args"] = args

    monkeypatch.setattr("backend.core.logging_bridge.logger", LoguruLogger())

    try:
        raise RuntimeError("database unavailable")
    except RuntimeError:
        record = logging.LogRecord(
            name="backend.models.database",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="database failure: %s",
            args=("retry exhausted",),
            exc_info=sys.exc_info(),
        )

    InterceptHandler().emit(record)

    assert calls["level"] == "ERROR"
    assert calls["message"] == "[{}] {}"
    assert calls["args"] == ("backend.models.database", "database failure: retry exhausted")
    assert calls["exception"][0] is RuntimeError


def test_intercept_handler_suppresses_http_noise_but_keeps_warnings(monkeypatch):
    calls = []

    class LoguruLogger:
        def level(self, name):
            return type("Level", (), {"name": name})()

        def opt(self, **_kwargs):
            return self

        def log(self, level, message, *args):
            calls.append((level, message, args))

    monkeypatch.setattr("backend.core.logging_bridge.logger", LoguruLogger())
    handler = InterceptHandler()

    handler.emit(
        logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="HTTP Request: GET https://example.test",
            args=(),
            exc_info=None,
        )
    )
    handler.emit(
        logging.LogRecord(
            name="httpcore.connection",
            level=logging.DEBUG,
            pathname=__file__,
            lineno=1,
            msg="connect_tcp.started",
            args=(),
            exc_info=None,
        )
    )
    handler.emit(
        logging.LogRecord(
            name="httpx",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="request retrying",
            args=(),
            exc_info=None,
        )
    )

    assert calls == [
        ("WARNING", "[{}] {}", ("httpx", "request retrying")),
    ]


def test_redact_standard_log_message_masks_url_passwords_and_bot_tokens():
    message = (
        "mysql+asyncmy://sakura:database-password@db.local/sakura "
        "https://api.telegram.org/bot123456:telegram-token/getMe"
    )

    assert _redact_standard_log_message(message) == (
        "mysql+asyncmy://sakura:***@db.local/sakura "
        "https://api.telegram.org/bot***/getMe"
    )
