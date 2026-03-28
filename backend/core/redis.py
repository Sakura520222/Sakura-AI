"""Redis 客户端模块"""

import redis
from backend.core.config import get_settings


_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """获取 Redis 客户端单例"""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
    return _client
