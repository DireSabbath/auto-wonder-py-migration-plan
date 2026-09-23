"""定时任务中工作空间删除会用到的暂停。"""

import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.db.rows import rowcount
from autowonder.scheduledtasks.models import ScheduledTask

logger = logging.getLogger(__name__)

DELETION_REASON = "工作空间已删除"


async def pause_active_by_workspace(
    session: AsyncSession,
    workspace_id: int,
    operator_id: int,
) -> int:
    """把该工作空间仍在运行的定时任务改为 PAUSED，并返回变更行数。"""
    result = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == workspace_id,
            ScheduledTask.status == "ACTIVE",
            ScheduledTask.is_deleted == 0,
        )
        .values(
            status="PAUSED",
            modifier_id=operator_id,
            version=ScheduledTask.version + 1,
        )
    )
    paused = rowcount(result)
    if paused > 0:
        logger.info(
            "Paused %s scheduled task(s) of workspace %s: %s",
            paused,
            workspace_id,
            DELETION_REASON,
        )
    return paused
