"""从失败、取消或暂停的工单派发再开一条，并立刻尝试打包下发。"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.locks import release_lock, try_acquire_lock
from autowonder.dispatch.enqueue import is_interaction
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.pending import run_pending
from autowonder.dispatch.recovery import (
    _copy_dispatch,
    execution_source,
    find_dispatch,
    insert_dispatch,
    is_terminal,
    list_workitem_dispatches,
    require_open,
    transition,
)
from autowonder.executors.registry import is_online

logger = logging.getLogger(__name__)

LOCK_TTL_MS = 30_000
STALE_MS = 120_000
MAX_ERROR_CHARS = 512

Acquire = Callable[[str, str, int], Awaitable[bool]]
Release = Callable[[str, str], Awaitable[bool]]
Online = Callable[[int], bool]


async def continue_workitem(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    user_id: int,
    online: Online = is_online,
    acquire: Acquire = try_acquire_lock,
    release: Release = release_lock,
) -> Dispatch:
    """拒绝定时任务运行，并在原执行器仍在线时不并行恢复。"""
    source = await find_dispatch(session, dispatch_id)
    if source is None:
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    if (
        source.tenant_id != tenant_id
        or execution_source(source) != "WORKITEM"
        or source.workitem_id != workitem_id
    ):
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    if (
        not is_terminal(source.status)
        and source.status != "PAUSED"
        and source.executor_id is not None
        and online(source.executor_id)
    ):
        raise BizError(ErrorCode.CONFLICT, "原执行器仍在线，不能创建并行恢复执行")
    return await continue_dispatch(
        session,
        tenant_id,
        workitem_id,
        dispatch_id,
        user_id,
        acquire,
        release,
    )


async def continue_dispatch(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    user_id: int,
    acquire: Acquire = try_acquire_lock,
    release: Release = release_lock,
) -> Dispatch:
    """按来源派发创建下一次尝试。同一继续键重复调用返回已有行。"""
    owner = str(uuid.uuid4())
    key = "dispatch:continue:" + str(tenant_id) + ":" + str(workitem_id)
    if not await acquire(key, owner, LOCK_TTL_MS):
        raise BizError(ErrorCode.CONFLICT, "恢复请求正在处理中")
    try:
        return await _continue_locked(session, tenant_id, workitem_id, dispatch_id, user_id)
    finally:
        await release(key, owner)


def can_continue(dispatch: Dispatch | None, now: datetime) -> bool:
    """成功不能继续。暂停和终态失败可以。仍在执行的只有超过两分钟没有更新才可以。"""
    if dispatch is None or dispatch.status == "SUCCEEDED":
        return False
    if dispatch.status in {"PAUSED", "PAUSING", "PAUSE_FAILED", "FAILED", "TIMEOUT", "CANCELED"}:
        return True
    if dispatch.status not in {"PACKAGING", "DISPATCHED", "ACKED", "RUNNING"}:
        return False
    if dispatch.gmt_modified is None:
        return False
    return dispatch.gmt_modified < now - timedelta(milliseconds=STALE_MS)


async def _continue_locked(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    user_id: int,
) -> Dispatch:
    source = await find_dispatch(session, dispatch_id)
    if (
        source is None
        or source.tenant_id != tenant_id
        or execution_source(source) != "WORKITEM"
        or source.workitem_id != workitem_id
    ):
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    await require_open(session, source)
    early = await _by_key(session, tenant_id, "continue:" + str(dispatch_id))
    if early is not None:
        return early
    workers = [
        row
        for row in await list_workitem_dispatches(session, tenant_id, workitem_id)
        if row.agent_id == source.agent_id
    ]
    latest = None
    if len(workers) > 0:
        latest = workers[-1]
    if is_interaction(source):
        if latest is None or latest.id != source.id:
            raise BizError(ErrorCode.CONFLICT, "只能继续该 Worker 的最新一次执行")
        target = source
    else:
        if latest is not None and is_interaction(latest) and not is_terminal(latest.status):
            raise BizError(ErrorCode.CONFLICT, "该 Worker 正在处理评论交互，请稍后再试")
        formal = [row for row in workers if not is_interaction(row)]
        if len(formal) == 0:
            raise BizError(ErrorCode.CONFLICT, "只能继续该 Worker 的最新一次执行")
        target = formal[-1]
        if target.id != source.id and target.sdlc_step_id != source.sdlc_step_id:
            step_text = "null"
            if target.sdlc_step_id is not None:
                step_text = str(target.sdlc_step_id)
            raise BizError(
                ErrorCode.CONFLICT,
                "该 Worker 有更新的执行（dispatchId="
                + str(target.id)
                + ", stepId="
                + step_text
                + "），请刷新后对最新执行操作",
            )
    idem = "continue:" + str(target.id)
    existing = await _by_key(session, tenant_id, idem)
    if existing is not None:
        return existing
    if not is_terminal(target.status) and target.status != "PAUSED":
        changed = await transition(
            session,
            target,
            "CANCELED",
            None,
            None,
            None,
            None,
            "MANUAL_CONTINUE"[:MAX_ERROR_CHARS],
        )
        if changed != 1:
            raise BizError(ErrorCode.CONFLICT, "执行状态已变化，请刷新后重试")
        target.status = "CANCELED"
        target.version = target.version + 1
    if not can_continue(target, now_local()):
        raise BizError(ErrorCode.CONFLICT, "当前执行仍在线或已成功，不能继续")
    recovery = Dispatch(
        tenant_id=tenant_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        sdlc_step_id=target.sdlc_step_id,
        agent_id=target.agent_id,
        status="PENDING",
        attempt=await _next_attempt(session, tenant_id, workitem_id, target.sdlc_step_id),
        idempotency_key=idem,
        resume_from_dispatch_id=target.id,
        resume_mode="RECOVERY",
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
        is_deleted=0,
    )
    if is_interaction(target):
        recovery.resume_mode = "CANONICAL_INTERACTION"
    try:
        await insert_dispatch(session, recovery)
    except IntegrityError:
        winner = await _by_key(session, tenant_id, idem)
        if winner is None:
            raise
        return winner
    logger.info(
        "dispatch continue created dispatchId=%s sourceId=%s",
        recovery.id,
        target.id,
    )
    await run_pending(session, recovery.id)
    return recovery


async def _next_attempt(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    sdlc_step_id: int | None,
) -> int:
    if sdlc_step_id is None:
        return 1
    rows = (
        await session.scalars(
            select(Dispatch).where(
                Dispatch.tenant_id == tenant_id,
                Dispatch.workitem_id == workitem_id,
                Dispatch.source_type == "WORKITEM",
                Dispatch.sdlc_step_id == sdlc_step_id,
                Dispatch.is_deleted == 0,
            )
        )
    ).all()
    highest = 0
    for row in rows:
        if row.attempt > highest:
            highest = row.attempt
    return highest + 1


async def _by_key(session: AsyncSession, tenant_id: int, key: str) -> Dispatch | None:
    row = await session.scalar(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.idempotency_key == key,
            Dispatch.is_deleted == 0,
        )
        .limit(1)
    )
    if row is None:
        return None
    return _copy_dispatch(row)
