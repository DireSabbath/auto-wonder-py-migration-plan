"""集群锁：SET NX PX，释放时只删除自己持有的锁。"""

from typing import Any, cast

RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


async def try_acquire_lock(lock_key: str, owner_token: str, ttl_millis: int) -> bool:
    """尝试获取锁。成功返回 True。"""
    from autowonder.core.redis import redis_client

    result = await redis_client().set(lock_key, owner_token, nx=True, px=ttl_millis)
    return result is True or result == "OK"


async def release_lock(lock_key: str, owner_token: str) -> bool:
    """仅当锁的值仍是 owner_token 时删除。"""
    from autowonder.core.redis import redis_client

    result = await cast(Any, redis_client().eval(RELEASE_SCRIPT, 1, lock_key, owner_token))
    return int(result) == 1
