"""定时任务拿锁。没拿到就跳过这一轮，结束时只释放自己的锁。"""

import uuid
from collections.abc import Awaitable, Callable

from autowonder.core.locks import release_lock, try_acquire_lock


async def under_lock(
    lock_key: str,
    ttl_millis: int,
    body: Callable[[], Awaitable[None]],
) -> bool:
    """拿到锁才执行。执行失败也释放当前持有者的锁。"""
    owner = str(uuid.uuid4())
    locked = await try_acquire_lock(lock_key, owner, ttl_millis)
    if not locked:
        return False
    try:
        await body()
    finally:
        await release_lock(lock_key, owner)
    return True
