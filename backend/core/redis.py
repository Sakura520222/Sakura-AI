"""Redis 客户端模块"""

import atexit
import contextvars

import redis
import redis.asyncio as aioredis
from loguru import logger
from backend.core.config import get_settings

_client_context = contextvars.ContextVar("redis_client", default=None)
_MIN_REDIS_GETDEL_VERSION = (6, 2, 0)
_getdel_version_warning_logged = False


def _parse_redis_version(version: str) -> tuple[int, int, int] | None:
    """Parse Redis server version text into a comparable tuple."""
    try:
        parts = version.split("-", 1)[0].split(".")
        numbers = [int(part) for part in parts[:3]]
    except (AttributeError, TypeError, ValueError):
        return None
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def _redis_version_supports_getdel(version: str) -> bool:
    parsed = _parse_redis_version(version)
    return bool(parsed and parsed >= _MIN_REDIS_GETDEL_VERSION)


def _warn_if_getdel_unsupported(version: str | None) -> None:
    """Warn once when Redis Server is too old for atomic GETDEL."""
    global _getdel_version_warning_logged
    if not version or _redis_version_supports_getdel(version):
        return
    if _getdel_version_warning_logged:
        return
    _getdel_version_warning_logged = True
    logger.warning(
        "Redis Server 6.2+ is required for atomic GETDEL challenge consumption; "
        "current Redis Server version is {}. One-time TOTP/Passkey secrets may "
        "fall back to in-memory storage if GETDEL is unsupported.",
        version,
    )


def _check_getdel_support(client) -> None:
    try:
        info = client.info("server")
        _warn_if_getdel_unsupported(info.get("redis_version"))
    except Exception as e:
        logger.debug(f"检查 Redis GETDEL 兼容性失败（可忽略）: {e}")


async def _check_async_getdel_support(client) -> None:
    try:
        info = await client.info("server")
        _warn_if_getdel_unsupported(info.get("redis_version"))
    except Exception as e:
        logger.debug(f"检查异步 Redis GETDEL 兼容性失败（可忽略）: {e}")


def _cleanup_client(client):
    """安全关闭 Redis 客户端连接"""
    try:
        client.close()
    except Exception:
        pass


def get_redis() -> redis.Redis:
    """获取 Redis 客户端（协程隔离，带连接池和异常处理）"""
    client = _client_context.get()
    if client is None:
        try:
            settings = get_settings()
            client = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=50,
            )
            client.ping()
            _check_getdel_support(client)
            _client_context.set(client)
            atexit.register(_cleanup_client, client)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.error(f"Redis 连接失败: {e}")
            raise
    return client


_async_client_context = contextvars.ContextVar("async_redis_client", default=None)


def _cleanup_async_client(client):
    """安全关闭异步 Redis 客户端连接"""
    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # 事件循环正在运行，创建关闭任务并保存引用防止 GC
            task = loop.create_task(client.aclose())
            _cleanup_async_client._pending_tasks = getattr(
                _cleanup_async_client, "_pending_tasks", []
            )
            _cleanup_async_client._pending_tasks.append(task)
        else:
            # 没有运行中的事件循环
            asyncio.run(client.aclose())
    except Exception as e:
        logger.debug(f"清理异步 Redis 客户端时出错（通常可忽略）: {e}")


async def get_async_redis() -> aioredis.Redis:
    """获取异步 Redis 客户端（协程隔离，带连接池和异常处理）"""
    client = _async_client_context.get()
    if client is None:
        try:
            settings = get_settings()
            client = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=50,
            )
            await client.ping()
            await _check_async_getdel_support(client)
            _async_client_context.set(client)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.error(f"异步 Redis 连接失败: {e}")
            raise
    return client
