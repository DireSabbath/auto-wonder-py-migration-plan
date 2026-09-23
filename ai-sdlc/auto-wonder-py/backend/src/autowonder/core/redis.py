"""redis.asyncio 客户端。"""

from redis.asyncio import Redis

from autowonder.config import get_settings

_CLIENT: Redis | None = None


def redis_client() -> Redis:
    """进程内复用的异步 Redis 连接。"""
    global _CLIENT
    if _CLIENT is None:
        settings = get_settings()
        _CLIENT = Redis.from_url(settings.redis_url, decode_responses=True)
    return _CLIENT
