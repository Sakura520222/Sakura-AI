import logging
import sys

from backend.core.logging_bridge import InterceptHandler


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
