"""将标准库日志统一转发到 Loguru。"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from loguru import logger

from backend.core.log_context import CORRELATION_FIELDS, get_log_context
from backend.core.log_sanitizer import (
    sanitize,
    sanitize_text,
    sensitive_key,
    url_password_secrets,
)
from backend.core.time_service import SystemClock, get_time_service

APP_LOG_DIRECTORY = Path("logs")
APP_LOG_RETENTION_DAYS = 10
APP_LOG_ROTATION = "50 MB"
APP_LOG_MAX_BYTES = 500 * 1024 * 1024
_active_app_log_paths: set[Path] = set()
# apscheduler.executors：每个 job 触发都会发 "Running job"/"executed successfully"
# 两条 INFO；star_aid tick 即使功能关闭也保持注册（多副本观察远端开启），
# 不过滤就会形成 Issue #650 的周期性心跳噪音。executor 的 WARNING（missed
# run / max instances）与 ERROR（job 异常）不受影响。
_NOISY_LOGGER_PREFIXES = ("httpx", "httpcore", "telegram", "apscheduler.executors")

# 时间占位符必须用 strftime 规范，不能用 loguru 迷你语言（YYYY-MM-DD ... ZZ）：
# 其 Z/ZZ token 依赖 tzinfo.utcoffset(None)，ZoneInfo 返回 None 会令格式化崩溃、
# 记录被丢弃；而标准库桥接写入的 time 是普通 stdlib datetime，也解释不了迷你语言。
# %z 按记录自身时刻取偏移，跨 DST 不漂移。
CONSOLE_LOG_FORMAT = (
    "<green>{time:%Y-%m-%d %H:%M:%S.%f%z}</green>"
    " | <level>{level: <8}</level>"
    " | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)
FILE_LOG_FORMAT = (
    "{time:%Y-%m-%d %H:%M:%S.%f%z} | {level: <8} | {name}:{function}:{line} - {message}"
)


def _cleanup_expired_app_logs(_log_paths: list[str] | None = None) -> None:
    """删除超过保留期限的应用日志，包含历史启动与轮转文件。"""
    retention_threshold = (
        SystemClock.now_utc().timestamp() - APP_LOG_RETENTION_DAYS * 24 * 60 * 60
    )
    files = []
    for log_path in APP_LOG_DIRECTORY.glob("app_*.log"):
        if log_path in _active_app_log_paths:
            continue
        try:
            stat = log_path.stat()
            if stat.st_mtime < retention_threshold:
                log_path.unlink()
            else:
                files.append((stat.st_mtime, stat.st_size, log_path))
        except OSError:
            # 其他进程可能已完成同一历史文件的清理，或仍占用该历史文件。
            continue
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= APP_LOG_MAX_BYTES:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            continue


def _create_startup_log_file(
    log_directory: Path | None = None,
    *,
    started_at: datetime | None = None,
    process_id: int | None = None,
) -> Path:
    """为本次进程启动原子地分配一个新的日志文件。"""
    log_directory = log_directory or APP_LOG_DIRECTORY
    log_directory.mkdir(parents=True, exist_ok=True)
    startup_time = started_at or SystemClock.now_utc()
    if startup_time.tzinfo is None or startup_time.utcoffset() is None:
        raise ValueError("日志文件启动时间必须是 aware datetime")
    startup_time = startup_time.astimezone(UTC)
    pid = process_id if process_id is not None else os.getpid()
    file_stem = f"app_{startup_time:%Y%m%d_%H%M%S_%f}Z_pid{pid}"

    collision_index = 0
    while True:
        collision_suffix = "" if collision_index == 0 else f"_{collision_index}"
        log_path = log_directory / f"{file_stem}{collision_suffix}.log"
        try:
            log_path.touch(exist_ok=False)
            return log_path
        except FileExistsError:
            collision_index += 1


class InterceptHandler(logging.Handler):
    """把标准 ``logging`` 记录写入配置好的 Loguru sinks。"""

    def __init__(self) -> None:
        super().__init__()
        # 重入哨兵（thread-local）：TimeService 初始化经 tzlocal 探测时区
        # 时会再次写标准日志；不拦截会形成 bridge -> get_time_service() ->
        # std logging -> bridge 的无限递归（RecursionError，表现为启动期
        # "--- Logging error in Loguru Handler #2 ---" 刷屏）。处理期间的
        # 再入记录直接丢弃。
        self._reentrant = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._reentrant, "active", False):
            return
        self._reentrant.active = True
        try:
            self._forward(record)
        finally:
            self._reentrant.active = False

    def _forward(self, record: logging.LogRecord) -> None:
        if _is_noisy_library_record(record):
            return

        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        message = record.getMessage()
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        message = _redact_standard_log_message(message)

        event_time = datetime.fromtimestamp(record.created, tz=UTC)
        target_logger = logger
        patcher = getattr(target_logger, "patch", None)
        if patcher is not None:
            try:
                event_time = event_time.astimezone(get_time_service().zone)
            except Exception:
                pass

            def original_source(log_record):
                # LogRecord is authoritative even for QueueHandler, stacklevel,
                # adapters, or manually emitted records with no caller stack.
                log_record.setdefault("message", message)
                log_record.update(
                    time=event_time,
                    name=record.name,
                    module=record.module,
                    function=record.funcName,
                    line=record.lineno,
                    file=SimpleNamespace(name=record.filename, path=record.pathname),
                )
                log_record.setdefault("extra", {}).update(
                    getattr(record, "_sakura_context", {})
                )
                log_record["extra"].update(
                    {
                        key: value
                        for key, value in record.__dict__.items()
                        if key not in _STANDARD_RECORD_FIELDS
                    }
                )
                _sanitize_record(log_record)

            target_logger = patcher(original_source)
        target_logger.opt(exception=record.exc_info).log(
            level,
            "[{}] {}",
            record.name,
            message,
        )


def _is_noisy_library_record(record: logging.LogRecord) -> bool:
    """抑制高频 HTTP 传输、Telegram 轮询与 APScheduler 逐跳执行的正常细节日志。"""
    return record.levelno < logging.WARNING and any(
        record.name == prefix or record.name.startswith(f"{prefix}.")
        for prefix in _NOISY_LOGGER_PREFIXES
    )


def _redact_standard_log_message(message: str) -> str:
    """防止标准日志中的 URL 密码和 Telegram Bot token 落盘。"""
    return sanitize_text(message)


_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "_sakura_context",
}


def install_standard_logging_bridge() -> None:
    """捕获标准库、Uvicorn 与第三方库的日志到 Loguru。"""
    handler = InterceptHandler()
    previous_factory = logging.getLogRecordFactory()
    if not getattr(previous_factory, "_sakura_context_factory", False):

        def contextual_record(*args, **kwargs):
            record = previous_factory(*args, **kwargs)
            record._sakura_context = get_log_context()
            return record

        contextual_record._sakura_context_factory = True
        logging.setLogRecordFactory(contextual_record)
    logging.basicConfig(handlers=[handler], level=logging.DEBUG, force=True)
    logging.captureWarnings(True)

    # Uvicorn 默认关闭传播并自行写 stdout/stderr，需要显式替换其 handlers。
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        standard_logger = logging.getLogger(logger_name)
        standard_logger.handlers = [handler]
        standard_logger.setLevel(logging.DEBUG)
        standard_logger.propagate = False


def _patch_loguru_time(record: dict) -> None:
    """Render direct Loguru records in the frozen application timezone."""

    try:
        zone = get_time_service().zone
        record["time"] = record["time"].astimezone(zone)
    except Exception:
        # Bootstrap diagnostics still remain valid UTC if timezone discovery is
        # unavailable; lifespan will fail closed before normal startup.
        record["time"] = record["time"].astimezone(UTC)


def _configured_secrets() -> tuple[str, ...]:
    from backend.core.config import get_settings

    try:
        settings = get_settings()
        values = vars(settings)
    except Exception:
        # Logging the original startup/configuration error must remain possible.
        values = os.environ
    secrets = []
    for key, value in values.items():
        if not isinstance(value, str) or not value:
            continue
        if sensitive_key(key):
            secrets.append(value)
        elif key.lower() == "redis_url":
            # Redis errors may echo only the decoded password, or an encoded URI.
            secrets.extend(url_password_secrets(value))
    return tuple(secrets)


def _sanitize_record(record: dict) -> None:
    secrets = _configured_secrets()
    extra = {**get_log_context(), **record.get("extra", {})}
    exception = record.get("exception")
    if exception:
        seen = set()
        pending = [exception.value]
        while pending:
            error = pending.pop()
            if error is None or id(error) in seen:
                continue
            seen.add(id(error))
            for key, value in getattr(error, "_log_correlation", {}).items():
                if key in CORRELATION_FIELDS and extra.get(key) is None:
                    extra[key] = value
            secrets += tuple(getattr(error, "_log_redaction_secrets", ()))
            pending.extend((error.__cause__, error.__context__))
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
        # Render before any sink/queue can see exception objects or frame locals.
        # stdlib preserves cause/context, notes and ExceptionGroup branches.
        rendered = "".join(traceback.format_exception(*exception))
        extra["exception"] = sanitize_text(rendered, secrets=secrets)
        record["message"] += "\n" + extra["exception"]
        record["exception"] = None
    record["message"] = sanitize_text(record["message"], secrets=secrets)
    record["extra"] = sanitize(extra, secrets=secrets)


def _patch_record(record: dict) -> None:
    _patch_loguru_time(record)
    _sanitize_record(record)


def _sink_filter(record: dict) -> bool:
    from backend.core.config import Settings, effective_log_level, get_settings

    # Read the cached Settings singleton on each record: DB loading, WebUI saves
    # and backup restores all take effect without sink replacement/file churn.
    try:
        settings = get_settings()
    except Exception:
        settings = None
    default_level = Settings.model_fields["log_level"].default
    level = effective_log_level(settings)
    try:
        minimum = logger.level(level).no
    except ValueError:
        minimum = logger.level(default_level).no
    if record["level"].no < minimum:
        return False
    # Per-logger patchers run after the global patcher. Sanitize their additions
    # as well before formatting, output or queueing.
    _sanitize_record(record)
    return True


def _format_record(record: dict, *, console: bool = False) -> str:
    extra = {key: value for key, value in record["extra"].items() if key != "exception"}
    record["context"] = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
    return (
        CONSOLE_LOG_FORMAT if console else FILE_LOG_FORMAT
    ) + " | {context}\n{exception}"


def configure_logging(*, started_at: datetime | None = None) -> None:
    """在导入应用依赖前配置完整的控制台与文件日志。"""
    logger.remove()
    _active_app_log_paths.clear()
    _cleanup_expired_app_logs()
    app_log_path = _create_startup_log_file(started_at=started_at)
    _active_app_log_paths.add(app_log_path)
    logger.add(
        sys.stdout,
        format=lambda record: _format_record(record, console=True),
        level=0,
        filter=_sink_filter,
        diagnose=False,
        backtrace=False,
    )
    logger.add(
        str(app_log_path),
        format=_format_record,
        serialize=True,
        rotation=APP_LOG_ROTATION,
        retention=_cleanup_expired_app_logs,
        level=0,
        filter=_sink_filter,
        diagnose=False,
        backtrace=False,
    )
    logger.configure(patcher=_patch_record)
    install_standard_logging_bridge()
