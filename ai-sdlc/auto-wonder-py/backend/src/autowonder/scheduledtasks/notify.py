"""定时任务运行的产物通知。Redis 频道只带运行 id。"""

import json
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.redis import redis_client
from autowonder.scheduledtasks.models import ScheduledTaskRun


async def scheduled_task_id(session: AsyncSession, workspace_id: int, run_id: int) -> int | None:
    """运行不存在时没有任务 id。"""
    run = await _run(session, workspace_id, run_id)
    if run is None:
        return None
    return run.scheduled_task_id


async def publish_artifact(session: AsyncSession, workspace_id: int, run_id: int) -> None:
    """向该运行的订阅频道发布 artifact 帧。运行不存在时不发布。"""
    run = await _run(session, workspace_id, run_id)
    if run is None:
        return
    channel = "scheduled-run:" + str(run.id)
    frame = {
        "channel": channel,
        "type": "artifact",
        "payload": {"runId": run.id},
        "timestamp": time.time_ns() // 1_000_000,
    }
    await redis_client().publish(channel, json.dumps(frame, separators=(",", ":")))


async def _run(session: AsyncSession, workspace_id: int, run_id: int) -> ScheduledTaskRun | None:
    return await session.scalar(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == workspace_id,
            ScheduledTaskRun.id == run_id,
        )
        .limit(1)
    )
