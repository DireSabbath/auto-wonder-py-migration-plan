"""debug_log 超过 24 小时仍为 PENDING 时对账，默认每小时一次。"""

import logging
import uuid

from autowonder.core.locks import release_lock, try_acquire_lock
from autowonder.db.session import SessionLocal
from autowonder.debuglogs.issue import reconcile_pending_once

logger = logging.getLogger(__name__)

LOCK_KEY = "debuglog:reconciliation:lock"
LOCK_TTL_MILLIS = 5 * 60_000


async def reconcile_debug_logs() -> None:
    """拿到集群锁后扫一轮。扫失败也要释放自己的锁。"""
    owner = str(uuid.uuid4())
    locked = await try_acquire_lock(LOCK_KEY, owner, LOCK_TTL_MILLIS)
    if not locked:
        return
    try:
        async with SessionLocal() as session:
            await reconcile_pending_once(session)
    except Exception:
        logger.warning("debug log reconciliation failed", exc_info=True)
    finally:
        await release_lock(LOCK_KEY, owner)
