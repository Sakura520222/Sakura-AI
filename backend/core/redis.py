"""Redis 客户端模块"""

import atexit
import contextvars

import redis
import redis.asyncio as aioredis
from loguru import logger
from redis.exceptions import ResponseError

from backend.core.config import get_settings

_client_context = contextvars.ContextVar("redis_client", default=None)
_MIN_REDIS_GETDEL_VERSION = (6, 2, 0)
_getdel_version_warning_logged = False

# Redis 6.0 and earlier do not provide GETDEL.  Keep the fallback script
# deliberately single-key so Redis executes GET+DEL atomically on the server.
_ATOMIC_GETDEL_LUA = """
local value = redis.call('GET', KEYS[1])
if value then
    redis.call('DEL', KEYS[1])
end
return value
"""


def _parse_redis_version(version: str) -> tuple[int, int, int] | None:
    """Parse Redis server version text into a comparable tuple."""
    try:
        parts = version.split("-", 1)[0].split(".")
        numbers = [int(part) for part in parts[:3]]
    except AttributeError, TypeError, ValueError:
        return None
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def _redis_version_supports_getdel(version: str) -> bool:
    parsed = _parse_redis_version(version)
    return bool(parsed and parsed >= _MIN_REDIS_GETDEL_VERSION)


def _warn_if_getdel_unsupported(version: str | None) -> None:
    """Warn once when Redis Server is too old for native atomic GETDEL."""
    global _getdel_version_warning_logged
    if not version or _redis_version_supports_getdel(version):
        return
    if _getdel_version_warning_logged:
        return
    _getdel_version_warning_logged = True
    logger.warning(
        "Redis Server 6.2+ provides native atomic GETDEL; current Redis Server "
        "version is {}. Telegram binding token consumption will use an atomic "
        "Lua GET+DEL compatibility path on older servers (EVAL must be enabled). "
        "TOTP/Passkey challenge paths may still fall back to in-memory storage "
        "if native GETDEL is unavailable.",
        version,
    )


def _is_unknown_getdel_command(error: ResponseError) -> bool:
    """Return whether a Redis error specifically rejects the GETDEL command.

    Redis reports disabled commands, ACL denials, and scripting failures using
    response errors too.  Only the server's *unknown command GETDEL* response
    authorizes the Lua compatibility path; all other response errors must be
    propagated to the caller.
    """

    message = str(error)
    marker = message.lower().find("unknown command")
    if marker < 0:
        return False
    command_text = message[marker + len("unknown command") :].lstrip()
    if not command_text:
        return False
    if command_text[0] in "'\"":
        quote = command_text[0]
        command_text = command_text[1:]
        command = command_text.split(quote, 1)[0]
    else:
        command = command_text.split(None, 1)[0].rstrip(",;")
    return command.upper() == "GETDEL"


async def atomic_getdel(client: aioredis.Redis, key: str) -> object:
    """Atomically get and delete one Redis key.

    Native ``GETDEL`` is preferred.  Redis versions before 6.2 use a server
    side Lua transaction instead of an unsafe client-side GET followed by DEL.
    The fallback is intentionally gated on the precise unknown-command error
    for GETDEL, so ACL, connection, timeout, and scripting errors remain
    visible to callers.
    """

    try:
        return await client.execute_command("GETDEL", key)
    except ResponseError as error:
        if not _is_unknown_getdel_command(error):
            raise
        return await client.eval(_ATOMIC_GETDEL_LUA, 1, key)


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
