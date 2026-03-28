"""Redis 客户端模块"""

import threading

import redis
from loguru import logger
from backend.core.config import get_settings

_client_local = threading.local()


def get_redis() -> redis.Redis:
    """获取 Redis 客户端单例（线程安全，带连接池和异常处理）"""
    client = getattr(_client_local, 'client', None)
    if client is None:
        try:
            settings = get_settings()
            client = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=50,
            )
            client.ping()
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.error(f"Redis 连接失败: {e}")
            raise
        _client_local.client = client
    return client
