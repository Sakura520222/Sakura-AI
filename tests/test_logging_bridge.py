import ast
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

import backend.models.database
from backend.core.logging_bridge import (
    CONSOLE_LOG_FORMAT,
    FILE_LOG_FORMAT,
    InterceptHandler,
    _create_startup_log_file,
    _patch_loguru_time,
    _redact_standard_log_message,
)

_REAL_TIMESTAMP_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{4} \| INFO"
)


class _FakeZoneService:
    def __init__(self, zone):
        self.zone = zone


def _render_line(make_patched_logger, log_format=FILE_LOG_FORMAT):
    lines = []

    def sink(message):
        lines.append(str(message))

    handler_id = logger.add(sink, format=log_format, catch=False)
    try:
        make_patched_logger(logger).info("hello")
    finally:
        logger.remove(handler_id)

    assert len(lines) == 1
    return lines[0]


def test_auto_migrate_standard_logging_messages_are_percent_formatted():
    source = Path(backend.models.database.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    migration_calls = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "info":
            continue
        if len(node.args) < 2:
            continue
        message = node.args[0]
        if isinstance(message, ast.Constant) and isinstance(message.value, str):
            if "auto-migrate" in message.value and any(
                marker in message.value
                for marker in ("扩展列为 LONGTEXT", "添加列", "迁移完成")
            ):
                migration_calls.append((message.value, len(node.args) - 1))

    assert len(migration_calls) == 3
    for message, argument_count in migration_calls:
        assert "{}" not in message
        assert message.count("%s") >= argument_count


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
    assert calls["args"] == (
        "backend.models.database",
        "database failure: retry exhausted",
    )
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


def test_intercept_handler_preserves_record_created_and_converts_to_app_zone(
    monkeypatch,
):
    calls = {}

    class FakeTimeService:
        zone = ZoneInfo("America/New_York")

    class LoguruLogger:
        def level(self, name):
            return type("Level", (), {"name": name})()

        def patch(self, patcher):
            calls["patcher"] = patcher
            return self

        def opt(self, **_kwargs):
            return self

        def log(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr("backend.core.logging_bridge.logger", LoguruLogger())
    monkeypatch.setattr(
        "backend.core.logging_bridge.get_time_service", lambda: FakeTimeService()
    )

    record = logging.LogRecord(
        name="example",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.created = datetime(2026, 8, 12, 16, 34, 56, tzinfo=UTC).timestamp()

    InterceptHandler().emit(record)
    patched = {}
    calls["patcher"](patched)

    assert patched["time"] == datetime(
        2026, 8, 12, 12, 34, 56, tzinfo=ZoneInfo("America/New_York")
    )


def test_loguru_patcher_keeps_bootstrap_diagnostics_utc_when_zone_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(
        "backend.core.logging_bridge.get_time_service",
        lambda: (_ for _ in ()).throw(ValueError("timezone unavailable")),
    )
    record = {"time": datetime(2026, 8, 12, 12, 34, 56, tzinfo=UTC)}

    _patch_loguru_time(record)

    assert record["time"].tzinfo is UTC


def test_startup_log_filename_is_utc_z_even_for_local_input(tmp_path):
    path = _create_startup_log_file(
        tmp_path,
        started_at=datetime(
            2026,
            8,
            12,
            20,
            34,
            56,
            123456,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        process_id=7,
    )

    assert path.name == "app_20260812_123456_123456Z_pid7.log"


def test_log_format_renders_zoneinfo_patched_time_without_handler_error(monkeypatch):
    """直连 loguru 记录：time 经 _patch_loguru_time 变为 ZoneInfo tzinfo 的子类实例。

    loguru 迷你语言的 Z/ZZ 会调 utcoffset(None)，ZoneInfo 返回 None 导致
    "--- Logging error ---" 并丢弃记录；strftime 规范必须能正常渲染。
    """

    monkeypatch.setattr(
        "backend.core.logging_bridge.get_time_service",
        lambda: _FakeZoneService(ZoneInfo("America/New_York")),
    )

    line = _render_line(lambda log: log.patch(_patch_loguru_time))

    assert _REAL_TIMESTAMP_PREFIX.match(line)
    assert "YYYY-MM-DD" not in line


def test_log_format_renders_plain_bridge_datetime_with_instant_offset():
    """标准库桥接记录：time 是普通 stdlib datetime，必须渲染出真实时间戳。

    夏季 EDT(-0400)、冬季 EST(-0500) 各自正确，证明 offset 按记录自身
    时刻计算，跨 DST 不漂移。
    """

    def bridge_patch(fixed_time):
        return lambda log: log.patch(lambda record: record.update(time=fixed_time))

    summer = _render_line(
        bridge_patch(
            datetime(
                2026, 8, 12, 12, 34, 56, 123456, tzinfo=ZoneInfo("America/New_York")
            )
        )
    )
    winter = _render_line(
        bridge_patch(
            datetime(
                2026, 1, 15, 12, 34, 56, 123456, tzinfo=ZoneInfo("America/New_York")
            )
        )
    )

    assert summer.startswith("2026-08-12 12:34:56.123456-0400 | INFO")
    assert winter.startswith("2026-01-15 12:34:56.123456-0500 | INFO")


def test_console_format_renders_real_timestamp():
    line = _render_line(
        lambda log: log.patch(
            lambda record: record.update(
                time=datetime(
                    2026, 8, 12, 12, 34, 56, 123456, tzinfo=ZoneInfo("Asia/Shanghai")
                )
            )
        ),
        log_format=CONSOLE_LOG_FORMAT,
    )

    assert line.startswith("2026-08-12 12:34:56.123456+0800")
