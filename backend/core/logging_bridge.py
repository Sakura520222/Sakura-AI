"""将标准库日志统一转发到 Loguru。"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from backend.core.time_service import get_time_service, now_utc

APP_LOG_DIRECTORY = Path("logs")
APP_LOG_RETENTION_DAYS = 10
_NOISY_LOGGER_PREFIXES = ("httpx", "httpcore", "telegram")
_URL_PASSWORD_PATTERN = re.compile(
    r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)[^@/\s]+(?P<suffix>@)"
)
_TELEGRAM_BOT_TOKEN_PATTERN = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def _cleanup_expired_app_logs(_log_paths: list[str] | None = None) -> None:
    """删除超过保留期限的应用日志，包含历史启动与轮转文件。"""
    retention_threshold = now_utc().timestamp() - APP_LOG_RETENTION_DAYS * 24 * 60 * 60
    for log_path in APP_LOG_DIRECTORY.glob("app_*.log"):
        try:
            if log_path.stat().st_mtime < retention_threshold:
                log_path.unlink()
        except OSError:
            # 其他进程可能已完成同一历史文件的清理，或仍占用该历史文件。
            continue


def _create_startup_log_file(
    log_directory: Path = APP_LOG_DIRECTORY,
    *,
    started_at: datetime | None = None,
    process_id: int | None = None,
) -> Path:
    """为本次进程启动原子地分配一个新的日志文件。"""
    log_directory.mkdir(parents=True, exist_ok=True)
    startup_time = started_at or now_utc()
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

    def emit(self, record: logging.LogRecord) -> None:
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
            target_logger = patcher(lambda log_record: log_record.update(time=event_time))
        target_logger.opt(exception=record.exc_info).log(
            level,
            "[{}] {}",
            record.name,
            message,
        )


def _is_noisy_library_record(record: logging.LogRecord) -> bool:
    """抑制高频 HTTP 传输和 Telegram 轮询的正常细节日志。"""
    return record.levelno < logging.WARNING and any(
        record.name == prefix or record.name.startswith(f"{prefix}.")
        for prefix in _NOISY_LOGGER_PREFIXES
    )


def _redact_standard_log_message(message: str) -> str:
    """防止标准日志中的 URL 密码和 Telegram Bot token 落盘。"""
    message = _URL_PASSWORD_PATTERN.sub(r"\g<prefix>***\g<suffix>", message)
    return _TELEGRAM_BOT_TOKEN_PATTERN.sub("/bot***", message)


def install_standard_logging_bridge() -> None:
    """捕获标准库、Uvicorn 与第三方库的日志到 Loguru。"""
    handler = InterceptHandler()
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


def configure_logging(*, started_at: datetime | None = None) -> None:
    """在导入应用依赖前配置完整的控制台与文件日志。"""
    _cleanup_expired_app_logs()
    app_log_path = _create_startup_log_file(started_at=started_at)
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS ZZ}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level="INFO",
    )
    logger.add(
        str(app_log_path),
        format="{time:YYYY-MM-DD HH:mm:ss.SSS ZZ} | {level: <8} | {name}:{function} - {message}",
        rotation="500 MB",
        retention=_cleanup_expired_app_logs,
        level="DEBUG",
    )
    logger.configure(patcher=_patch_loguru_time)
    install_standard_logging_bridge()
