"""执行器对评论指引的投递、确认、退回和失败。"""

import json
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError
from autowonder.db.rows import rowcount
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery import fenced
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.workitems.comments import add_agent_comment
from autowonder.workitems.models import WorkitemComment
from autowonder.ws.mailbox import deliver_executor_frame

logger = logging.getLogger(__name__)

_ACCEPTED = frozenset({"APPLIED", "FAILED"})
_INTERACTION = frozenset({"SIDE_INTERACTION", "CANONICAL_INTERACTION"})
_BLOCKED = frozenset({"PAUSING", "PAUSED", "PAUSE_FAILED", "CANCELED"})
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
_MAX_ERROR = 512


async def binding_for_inbound(
    session: AsyncSession, workspace_id: int, executor_id: int, guidance_id: int
) -> tuple[int, str]:
    """确认这条指引确实绑在当前执行器的调度上。对不上就是无权限。"""
    guidance = await session.get(WorkitemCommentDelivery, guidance_id)
    if guidance is None or not _guidance_owned(guidance, workspace_id, executor_id):
        raise BizError(ErrorCode.NO_PERMISSION)
    dispatch_id = guidance.dispatch_id
    if dispatch_id is None:
        raise BizError(ErrorCode.NO_PERMISSION)
    dispatch = await session.get(Dispatch, dispatch_id)
    if dispatch is None or dispatch.tenant_id != workspace_id:
        raise BizError(ErrorCode.NO_PERMISSION)
    if dispatch.executor_id != executor_id or dispatch.executor_id != guidance.executor_id:
        raise BizError(ErrorCode.NO_PERMISSION)
    if dispatch.workitem_id is None or dispatch.workitem_id <= 0:
        raise BizError(ErrorCode.NO_PERMISSION)
    if dispatch.workitem_id != guidance.workitem_id:
        raise BizError(ErrorCode.NO_PERMISSION)
    if dispatch.agent_id != guidance.target_agent_id:
        raise BizError(ErrorCode.NO_PERMISSION)
    if dispatch.is_deleted != 0:
        raise BizError(ErrorCode.NO_PERMISSION)
    return dispatch.id, dispatch.source_type


async def acknowledge(
    session: AsyncSession,
    workspace_id: int,
    executor_id: int,
    guidance_id: int,
    status: str | None,
    error: str | None,
    reply_markdown: str | None,
) -> None:
    """只接受 APPLIED 或 FAILED。失败会结束交互调度，成功且有回复则绑定评论。"""
    if status not in _ACCEPTED:
        raise IllegalArgumentError("invalid guidance status")
    result = await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.id == guidance_id,
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.executor_id == executor_id,
            WorkitemCommentDelivery.status == "DELIVERED",
        )
        .values(status=status, error=error, gmt_modified=now_local(), **_applied(status))
    )
    if rowcount(result) != 1:
        return
    session.expire_all()
    guidance = await session.get(WorkitemCommentDelivery, guidance_id)
    if guidance is None or guidance.tenant_id != workspace_id:
        raise RuntimeError("acknowledged guidance is missing")
    if status == "FAILED":
        await _fail_interaction(session, workspace_id, executor_id, guidance, error)
        await session.commit()
        return
    if reply_markdown is not None and not java_is_blank(reply_markdown):
        reply_id = await _reply(session, guidance, reply_markdown)
        bound = await _bind_reply(session, guidance_id, workspace_id, reply_id)
        if bound != 1:
            raise RuntimeError("failed to bind side interaction reply comment")
    await session.commit()


async def deliver_queued_for_dispatch(
    session: AsyncSession, workspace_id: int, dispatch_id: int
) -> None:
    """调度确认或出现进度后，把还排着队的指引发给执行器。"""
    dispatch = await session.get(Dispatch, dispatch_id)
    if dispatch is not None and dispatch.tenant_id == workspace_id:
        await deliver_queued(session, dispatch)


async def deliver_queued(session: AsyncSession, dispatch: Dispatch) -> None:
    """交互调度把 QUEUED 改成 DELIVERED，并发送 TASK_GUIDANCE。"""
    if dispatch.executor_id is None or dispatch.agent_id is None:
        return
    if dispatch.resume_mode not in _INTERACTION:
        return
    result = await session.scalars(
        select(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.tenant_id == dispatch.tenant_id,
            WorkitemCommentDelivery.dispatch_id == dispatch.id,
            WorkitemCommentDelivery.status == "QUEUED",
        )
        .order_by(WorkitemCommentDelivery.id.asc())
    )
    frames: list[str] = []
    for guidance in result.all():
        bound = await _bind_executor(session, guidance.id, dispatch)
        if bound != 1:
            continue
        await _mark_delivered(session, guidance.id, dispatch.tenant_id)
        comment = await _require_comment(session, guidance)
        frames.append(_frame(guidance, dispatch.executor_id, comment.content_md))
    for frame in frames:
        await deliver_executor_frame(dispatch.executor_id, frame)
    await session.commit()


async def requeue_delivered_for_dispatch(
    session: AsyncSession, workspace_id: int, dispatch_id: int
) -> None:
    """暂停后把已投递的指引退回队列，方便恢复后重发。"""
    await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.dispatch_id == dispatch_id,
            WorkitemCommentDelivery.status == "DELIVERED",
        )
        .values(
            status="QUEUED",
            dispatch_id=None,
            executor_id=None,
            delivered_at=None,
            error=None,
            gmt_modified=now_local(),
        )
    )
    await session.commit()


async def requeue_for_executor_failover(
    session: AsyncSession, workspace_id: int, dispatch_id: int
) -> None:
    """执行器故障后保留调度 id，清掉执行器，等下一个执行器再投递。"""
    await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.dispatch_id == dispatch_id,
            WorkitemCommentDelivery.status.in_(("DELIVERED", "FAILED")),
        )
        .values(
            status="QUEUED",
            dispatch_id=dispatch_id,
            executor_id=None,
            delivered_at=None,
            applied_at=None,
            error=None,
            gmt_modified=now_local(),
        )
    )
    await session.commit()


async def fail_for_dispatch(
    session: AsyncSession, workspace_id: int, dispatch_id: int, error: str | None
) -> None:
    """调度失败时，还没被确认的指引一起失败。"""
    await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.dispatch_id == dispatch_id,
            WorkitemCommentDelivery.status.in_(("QUEUED", "DELIVERED")),
        )
        .values(status="FAILED", error=error, gmt_modified=now_local())
    )
    await session.commit()


def _guidance_owned(
    guidance: WorkitemCommentDelivery | None, workspace_id: int, executor_id: int
) -> bool:
    if guidance is None or guidance.tenant_id != workspace_id:
        return False
    if guidance.executor_id != executor_id:
        return False
    if guidance.workitem_id is None or guidance.workitem_id <= 0:
        return False
    if guidance.dispatch_id is None or guidance.dispatch_id <= 0:
        return False
    return True


def _applied(status: str) -> dict[str, object]:
    if status == "APPLIED":
        return {"applied_at": now_local()}
    return {}


async def _fail_interaction(
    session: AsyncSession,
    workspace_id: int,
    executor_id: int,
    guidance: WorkitemCommentDelivery,
    error: str | None,
) -> None:
    dispatch = None
    if guidance.dispatch_id is not None:
        dispatch = await session.get(Dispatch, guidance.dispatch_id)
    if dispatch is None or dispatch.tenant_id != workspace_id:
        raise RuntimeError("guidance interaction dispatch is missing")
    if dispatch.resume_mode not in _INTERACTION:
        raise RuntimeError("guidance interaction dispatch is missing")
    if not await _mark_failed(session, workspace_id, executor_id, dispatch, error):
        raise RuntimeError("failed to terminate guidance interaction dispatch")


async def _mark_failed(
    session: AsyncSession,
    workspace_id: int,
    executor_id: int,
    dispatch: Dispatch,
    error: str | None,
) -> bool:
    if dispatch.executor_id != executor_id:
        return False
    if dispatch.status in _BLOCKED or await fenced(session, dispatch):
        return False
    if dispatch.status in _TERMINAL:
        return dispatch.status == "FAILED"
    stored = None
    if error is not None:
        stored = error[:_MAX_ERROR]
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch.id,
            Dispatch.tenant_id == workspace_id,
            Dispatch.version == dispatch.version,
            Dispatch.is_deleted == 0,
        )
        .values(
            status="FAILED",
            error=stored,
            version=Dispatch.version + 1,
            modifier_id=0,
        )
    )
    if rowcount(result) == 1:
        return True
    session.expire(dispatch)
    refreshed = await session.get(Dispatch, dispatch.id)
    return (
        refreshed is not None
        and refreshed.tenant_id == workspace_id
        and refreshed.executor_id == executor_id
        and refreshed.status == "FAILED"
    )


async def _reply(
    session: AsyncSession, guidance: WorkitemCommentDelivery, reply_markdown: str
) -> int:
    if guidance.source_type == "SCHEDULED_TASK_RUN":
        return await _scheduled_reply(session, guidance, reply_markdown)
    comment, _notices = await add_agent_comment(
        session,
        guidance.workitem_id,
        reply_markdown,
        [],
        guidance.tenant_id,
        guidance.target_agent_id,
        None,
    )
    return comment.id


async def _scheduled_reply(
    session: AsyncSession, guidance: WorkitemCommentDelivery, reply_markdown: str
) -> int:
    require_scheduled_capability()
    run = await session.get(ScheduledTaskRun, guidance.workitem_id)
    if run is None or run.workspace_id != guidance.tenant_id:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    comment = WorkitemComment(
        tenant_id=guidance.tenant_id,
        source_type="SCHEDULED_TASK_RUN",
        workitem_id=guidance.workitem_id,
        author_type="AGENT",
        author_ref=guidance.target_agent_id,
        content_md=reply_markdown,
    )
    session.add(comment)
    await session.flush()
    return comment.id


async def _bind_reply(
    session: AsyncSession, guidance_id: int, workspace_id: int, reply_id: int
) -> int:
    result = await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.id == guidance_id,
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.reply_comment_id.is_(None),
        )
        .values(reply_comment_id=reply_id, gmt_modified=now_local())
    )
    return rowcount(result)


async def _bind_executor(session: AsyncSession, guidance_id: int, dispatch: Dispatch) -> int:
    result = await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.id == guidance_id,
            WorkitemCommentDelivery.tenant_id == dispatch.tenant_id,
            WorkitemCommentDelivery.status == "QUEUED",
        )
        .values(
            dispatch_id=dispatch.id,
            executor_id=dispatch.executor_id,
            gmt_modified=now_local(),
        )
    )
    return rowcount(result)


async def _mark_delivered(session: AsyncSession, guidance_id: int, workspace_id: int) -> None:
    await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.id == guidance_id,
            WorkitemCommentDelivery.tenant_id == workspace_id,
        )
        .values(status="DELIVERED", error=None, delivered_at=now_local(), gmt_modified=now_local())
    )


async def _require_comment(
    session: AsyncSession, guidance: WorkitemCommentDelivery
) -> WorkitemComment:
    statement = select(WorkitemComment).where(
        WorkitemComment.tenant_id == guidance.tenant_id,
        WorkitemComment.id == guidance.comment_id,
    )
    if guidance.source_type == "WORKITEM":
        statement = statement.where(WorkitemComment.source_type == "WORKITEM")
    else:
        statement = statement.where(
            WorkitemComment.source_type == guidance.source_type,
            WorkitemComment.workitem_id == guidance.workitem_id,
        )
    comment = await session.scalar(statement.limit(1))
    if comment is None or comment.workitem_id != guidance.workitem_id:
        raise IllegalArgumentError("guidance comment does not belong to source")
    return comment


def _frame(guidance: WorkitemCommentDelivery, executor_id: int, content: str | None) -> str:
    if guidance.dispatch_id is None:
        raise IllegalArgumentError("guidance must be bound to a dispatch and executor")
    return json.dumps(
        {
            "type": "TASK_GUIDANCE",
            "executorId": executor_id,
            "guidanceId": guidance.id,
            "dispatchId": guidance.dispatch_id,
            "workitemId": guidance.workitem_id,
            "targetAgentId": guidance.target_agent_id,
            "content": content,
            "mode": "SAFE_STEER",
        },
        separators=(",", ":"),
    )
