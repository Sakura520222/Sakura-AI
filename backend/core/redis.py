"""Redis 客户端模块"""

import atexit
import contextvars

import redis
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
