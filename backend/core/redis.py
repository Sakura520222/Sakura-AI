"""Redis 客户端模块"""

import atexit
import contextvars

import redis
import redis.asyncio as aioredis
from loguru import logger
from backend.core.config import get_settings

_client_context = contextvars.ContextVar("redis_client", default=None)


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
            _async_client_context.set(client)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.error(f"异步 Redis 连接失败: {e}")
            raise
    return client
