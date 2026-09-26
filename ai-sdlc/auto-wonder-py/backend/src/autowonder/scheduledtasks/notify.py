"""定时任务运行的产物通知。Redis 频道只带运行 id。"""

import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.redis import redis_client
from autowonder.scheduledtasks.models import ScheduledTask, ScheduledTaskRun
from autowonder.workspaces.models import OrgMember

logger = logging.getLogger(__name__)

_CONTENT_LIMIT = 1024
_DETAIL_LIMIT = 180
_ATTENTION = {
    "FAILED": ("SCHEDULED_RUN_FAILED", "定时任务运行失败"),
    "TIMED_OUT": ("SCHEDULED_RUN_FAILED", "定时任务运行超时"),
    "PAUSED": ("SCHEDULED_RUN_PAUSED", "定时任务运行已暂停"),
    "WAITING_HUMAN": ("SCHEDULED_RUN_NEEDS_HUMAN", "定时任务需要人工处理"),
    "NEEDS_HUMAN": ("SCHEDULED_RUN_NEEDS_HUMAN", "定时任务需要人工处理"),
}


async def scheduled_task_id(session: AsyncSession, workspace_id: int, run_id: int) -> int | None:
    """运行不存在时没有任务 id。"""
    run = await _run(session, workspace_id, run_id)
    if run is None:
        return None
    return run.scheduled_task_id


def attention_event(status: str) -> tuple[str, str] | None:
    """失败、暂停和需要人工处理才发站内通知。"""
    return _ATTENTION.get(status)


def notify_owner(status: str, actor_id: int, owner_id: int) -> bool:
    """负责人自己暂停运行时不给自己发通知。失败和需要人工处理仍通知。"""
    if attention_event(status) is None:
        return False
    if status == "PAUSED" and actor_id == owner_id:
        return False
    return True


def attention_copy(task_name: str, headline: str, detail: str | None) -> str:
    """拼出不超过通知正文字段的摘要。"""
    text = "「" + task_name + "」" + headline
    if detail is not None and detail.strip() != "":
        clipped = detail.strip()
        if len(clipped) > _DETAIL_LIMIT:
            clipped = clipped[:_DETAIL_LIMIT]
        text = text + "：" + clipped
    if len(text) > _CONTENT_LIMIT:
        return text[:_CONTENT_LIMIT]
    return text


async def announce_run(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    status: str,
    actor_id: int,
    detail: str | None,
) -> None:
    """先发运行状态帧。需要人工关注时再给任务负责人发站内和 IM 通知。"""
    try:
        await publish_status(session, workspace_id, run_id)
    except Exception:
        logger.exception(
            "scheduled status frame failed workspaceId=%s runId=%s",
            workspace_id,
            run_id,
        )
    chosen = attention_event(status)
    if chosen is None:
        return
    event_type, headline = chosen
    try:
        run = await _run(session, workspace_id, run_id)
        if run is None:
            raise RuntimeError("scheduled run disappeared before attention notice")
        task = await session.get(ScheduledTask, run.scheduled_task_id)
        if task is None:
            raise RuntimeError("scheduled task disappeared before attention notice")
        if not notify_owner(status, actor_id, task.creator_id):
            return
        from autowonder.notifications.service import publish

        await publish(
            session,
            workspace_id,
            event_type,
            headline,
            attention_copy(task.name, headline, detail),
            "/scheduled-task-runs/" + str(run_id),
            "SCHEDULED_TASK_RUN",
            run_id,
            [task.creator_id],
        )
    except Exception:
        logger.exception(
            "scheduled attention notice failed workspaceId=%s runId=%s status=%s",
            workspace_id,
            run_id,
            status,
        )
        await session.rollback()


async def announce_task_paused(session: AsyncSession, workspace_id: int, task_id: int) -> None:
    """负责人不可用导致任务被暂停时，通知负责人和空间管理员。"""
    try:
        task = await session.get(ScheduledTask, task_id)
        if task is None:
            raise RuntimeError("scheduled task disappeared before pause notice")
        from autowonder.notifications.service import publish

        await publish(
            session,
            workspace_id,
            "SCHEDULED_TASK_PAUSED",
            "定时任务已暂停",
            attention_copy(task.name, "负责人已不可用，任务已暂停，需要人工重新启用", None),
            "/scheduled-tasks/" + str(task_id),
            "SCHEDULED_TASK",
            task_id,
            await _pause_recipients(session, workspace_id, task.creator_id),
        )
    except Exception:
        logger.exception(
            "scheduled task pause notice failed workspaceId=%s taskId=%s",
            workspace_id,
            task_id,
        )
        await session.rollback()


async def _pause_recipients(
    session: AsyncSession,
    workspace_id: int,
    owner_id: int,
) -> list[int]:
    ids = [owner_id]
    admins = await session.scalars(
        select(OrgMember.user_id).where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.status == 0,
            OrgMember.is_deleted == 0,
            OrgMember.access_level == "ADMIN",
        )
    )
    for user_id in admins:
        if user_id not in ids:
            ids.append(user_id)
    return ids


async def publish_status(session: AsyncSession, workspace_id: int, run_id: int) -> None:
    """运行进入新状态后，向订阅频道发布 status 帧。运行不存在时不发布。"""
    await _publish_run_frame(session, workspace_id, run_id, "status")


async def publish_runtime(session: AsyncSession, workspace_id: int, run_id: int) -> None:
    """执行器故障转移后，向该运行的订阅频道发布 runtime 帧。运行不存在时不发布。"""
    await _publish_run_frame(session, workspace_id, run_id, "runtime")


async def publish_artifact(session: AsyncSession, workspace_id: int, run_id: int) -> None:
    """向该运行的订阅频道发布 artifact 帧。运行不存在时不发布。"""
    await _publish_run_frame(session, workspace_id, run_id, "artifact")


async def publish_derived_workitem(session: AsyncSession, workspace_id: int, run_id: int) -> None:
    """定时任务派生工单后发布 derived-workitem 帧。运行不存在时不发布。"""
    await _publish_run_frame(session, workspace_id, run_id, "derived-workitem")


async def publish_comment(
    session: AsyncSession, workspace_id: int, run_id: int, comment_id: int
) -> None:
    """先发评论服务的原始帧，再发订阅通道上的 comment 帧。"""
    channel = "scheduled-run:" + str(run_id)
    await redis_client().publish(
        channel,
        json.dumps(
            {"type": "comment", "runId": run_id, "commentId": comment_id},
            separators=(",", ":"),
        ),
    )
    await _publish_run_frame(session, workspace_id, run_id, "comment")


async def _publish_run_frame(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    frame_type: str,
) -> None:
    run = await _run(session, workspace_id, run_id)
    if run is None:
        return
    channel = "scheduled-run:" + str(run.id)
    frame = {
        "channel": channel,
        "type": frame_type,
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
