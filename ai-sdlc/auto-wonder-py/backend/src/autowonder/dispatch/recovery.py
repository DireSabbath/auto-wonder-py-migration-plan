"""调度取消意图、工单关闭围栏，以及评论投递的终态投影。

暂停帧发不出去时只记下 ``stop_pending``，由对账再送。``ready`` 和
``retry_packaging`` 由待派发主环调用，语义与 Java 一致。
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.audits.service import AuditRecord, record_required
from autowonder.config import get_settings
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.dispatch.models import Dispatch, DispatchRecovery
from autowonder.dispatch.transport import deliver_pause
from autowonder.executors.registry import DispatchSnapshot, current_snapshot, is_online
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.workitems.models import Workitem, WorkitemExecutionControl

logger = logging.getLogger(__name__)

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
QUIET_BEFORE_STOP = frozenset({"PENDING", "PACKAGING", "WAITING_FOR_PAUSE", "PAUSED"})
OPEN_GUIDANCE = frozenset({"QUEUED", "DELIVERED"})
RETRYABLE = frozenset({"FAILED", "TIMEOUT", "CANCELED", "PAUSED"})
RECONCILE_LIMIT = 200
STOP_RESEND_MS = 30_000
STOP_SELF_HEAL_GRACE_MS = 120_000
SUCCESS_ACK_WAIT_MS = 120_000
ORPHAN_GUIDANCE_MS = 30 * 60_000
REPLY_ACK_MISSING = "REPLY_ACK_MISSING: 执行已结束，回复确认缺失"
ORPHAN_GUIDANCE = "INTERACTION_DISPATCH_MISSING: 交互未关联有效执行，请重新发起"
CANCEL_REQUESTED = "CANCEL_REQUESTED: 正在取消"
STOP_UNCONFIRMED = "CANCELED_STOP_UNCONFIRMED: 平台已结束，执行器停止未确认"
USER_CANCELED = "USER_CANCELED"
PACKAGE_TRANSIENT = "TASK_PACKAGE_TRANSIENT_ERROR"

Pause = Callable[[Dispatch], Awaitable[None]]
Online = Callable[[int], bool]
SnapshotOf = Callable[[int], DispatchSnapshot | None]


@asynccontextmanager
async def transaction_unit(session: AsyncSession) -> AsyncIterator[None]:
    """一次提交。失败时保存点回滚，调用方看到原始异常。"""
    async with session.begin_nested():
        yield
    await session.commit()


def execution_source(dispatch: Dispatch) -> str:
    """空白来源按工单。其他值保持原样。"""
    source = dispatch.source_type
    if source is None or source.strip() == "":
        return "WORKITEM"
    return source


def is_terminal(status: str | None) -> bool:
    """成功、失败、超时和取消都不再向前推进。"""
    return status in TERMINAL


async def find_dispatch(session: AsyncSession, dispatch_id: int) -> Dispatch | None:
    """按主键读取未删除派发，并脱离会话，避免后续更新改写调用方手里的版本。"""
    row = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    if row is None:
        return None
    return _copy_dispatch(row)


async def update_status(
    session: AsyncSession,
    dispatch_id: int,
    tenant_id: int,
    status: str,
    agent_version_id: int | None,
    executor_id: int | None,
    package_oss_ref: str | None,
    result_summary: str | None,
    error: str | None,
    version: int,
    modifier_id: int,
) -> int:
    """乐观更新状态。空的可选字段不写，空字符串会清空错误。"""
    values: dict[str, object] = {
        "status": status,
        "version": Dispatch.version + 1,
        "modifier_id": modifier_id,
        "gmt_modified": now_local(),
    }
    if agent_version_id is not None:
        values["agent_version_id"] = agent_version_id
    if executor_id is not None:
        values["executor_id"] = executor_id
    if package_oss_ref is not None:
        values["package_oss_ref"] = package_oss_ref
    if result_summary is not None:
        values["result_summary"] = result_summary
    if error is not None:
        values["error"] = error
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch_id,
            Dispatch.tenant_id == tenant_id,
            Dispatch.version == version,
            Dispatch.is_deleted == 0,
        )
        .values(**values)
    )
    return rowcount(result)


async def closed(session: AsyncSession, tenant_id: int, workitem_id: int) -> bool:
    """工单交付是否已关闭。没有控制行时视为打开。"""
    row = await _control(session, tenant_id, workitem_id)
    return row is not None and row.closed == 1


async def require_open(session: AsyncSession, dispatch: Dispatch) -> None:
    """关闭后的工单不能再插入或推进非终态派发。"""
    if execution_source(dispatch) != "WORKITEM":
        return
    if await closed(session, dispatch.tenant_id, dispatch.workitem_id):
        raise BizError(ErrorCode.CONFLICT, "工单已关闭，请先重新打开交付")


async def insert_dispatch(session: AsyncSession, dispatch: Dispatch) -> None:
    """在工单锁内插入派发。继续交互时另起一条排队中的评论投递。"""
    async with transaction_unit(session):
        await _lock_subject(session, dispatch)
        await require_open(session, dispatch)
        session.add(dispatch)
        await session.flush()
        await _copy_continue_guidance(session, dispatch)


async def transition(
    session: AsyncSession,
    dispatch: Dispatch,
    status: str,
    agent_version_id: int | None,
    executor_id: int | None,
    package_oss_ref: str | None,
    result_summary: str | None,
    error: str | None,
) -> int:
    """状态和评论投影一起提交。取消意图会挡住非终态推进。"""
    changed = 0
    async with transaction_unit(session):
        await _lock_subject(session, dispatch)
        if not is_terminal(status):
            await require_open(session, dispatch)
            if await cancel_requested(session, dispatch.tenant_id, dispatch.id):
                changed = 0
            else:
                changed = await _apply_transition(
                    session,
                    dispatch,
                    status,
                    agent_version_id,
                    executor_id,
                    package_oss_ref,
                    result_summary,
                    error,
                )
        else:
            changed = await _apply_transition(
                session,
                dispatch,
                status,
                agent_version_id,
                executor_id,
                package_oss_ref,
                result_summary,
                error,
            )
    return changed


async def cancel_requested(session: AsyncSession, tenant_id: int, dispatch_id: int) -> bool:
    """是否已经写下取消意图。"""
    row = await _live_recovery(session, tenant_id, dispatch_id)
    return row is not None and row.cancel_requested == 1


async def fenced(session: AsyncSession, dispatch: Dispatch | None) -> bool:
    """取消或工单关闭之后，迟到的平台写入不再推进这条派发。"""
    if dispatch is None:
        return False
    if await cancel_requested(session, dispatch.tenant_id, dispatch.id):
        return True
    if execution_source(dispatch) != "WORKITEM":
        return False
    return await closed(session, dispatch.tenant_id, dispatch.workitem_id)


async def cancel(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    user_id: int,
    force: bool,
    pause: Pause = deliver_pause,
) -> dict[str, object]:
    """记下取消意图。执行器已经接手时先进入 PAUSING，强制取消直接结束。"""
    await _require_workitem(session, tenant_id, workitem_id, dispatch_id)
    async with transaction_unit(session):
        current = cast(Dispatch, await find_dispatch(session, dispatch_id))
        await _cancel_locked(session, current, user_id, force)
    await send_stop(session, await find_dispatch(session, dispatch_id), pause)
    return await state(session, tenant_id, workitem_id)


async def force_cancel_scheduled_run(
    session: AsyncSession,
    tenant_id: int,
    run_id: int,
    dispatch_id: int,
    user_id: int,
    pause: Pause = deliver_pause,
) -> None:
    """定时任务运行沿用同一条停止意图，但来源必须是这次运行。"""
    async with transaction_unit(session):
        current = await find_dispatch(session, dispatch_id)
        if (
            current is None
            or current.tenant_id != tenant_id
            or execution_source(current) != "SCHEDULED_TASK_RUN"
            or current.workitem_id != run_id
        ):
            raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
        await _cancel_locked(session, current, user_id, True)
    await send_stop(session, await find_dispatch(session, dispatch_id), pause)


async def on_stopped(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
) -> bool:
    """执行器确认停止后清掉 stop_pending。执行器对不上时保持原状。"""
    stopped = False
    async with transaction_unit(session):
        dispatch = await find_dispatch(session, dispatch_id)
        if (
            dispatch is None
            or dispatch.tenant_id != tenant_id
            or dispatch.executor_id != executor_id
        ):
            stopped = False
        else:
            await _lock_subject(session, dispatch)
            if not await cancel_requested(session, tenant_id, dispatch_id):
                stopped = False
            else:
                current = cast(Dispatch, await find_dispatch(session, dispatch_id))
                stopped = await _confirm_stop(session, current)
    return stopped


async def close(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    user_id: int,
    force: bool,
    pause: Pause = deliver_pause,
) -> dict[str, object]:
    """关闭交付并取消尚未结束的派发。关闭本身不自动再跑。"""
    await _require_workitem_row(session, tenant_id, workitem_id)
    async with transaction_unit(session):
        subject = _subject(tenant_id, workitem_id)
        await _lock_subject(session, subject)
        control = await _ensure_control(session, tenant_id, workitem_id)
        control.closed = 1
        control.modifier_id = user_id
        control.gmt_modified = now_local()
        for dispatch in await list_workitem_dispatches(session, tenant_id, workitem_id):
            await _cancel_locked(session, dispatch, user_id, force)
        await _audit(session, tenant_id, workitem_id, user_id, "CLOSE_DELIVERY", "WORKITEM")
    for dispatch in await list_workitem_dispatches(session, tenant_id, workitem_id):
        await send_stop(session, dispatch, pause)
    return await state(session, tenant_id, workitem_id)


async def reopen(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    user_id: int,
) -> dict[str, object]:
    """重新打开交付。仍有未结束派发时拒绝，也不会自动插入新派发。"""
    await _require_workitem_row(session, tenant_id, workitem_id)
    async with transaction_unit(session):
        subject = _subject(tenant_id, workitem_id)
        await _lock_subject(session, subject)
        for dispatch in await list_workitem_dispatches(session, tenant_id, workitem_id):
            if not is_terminal(dispatch.status):
                raise BizError(ErrorCode.CONFLICT, "请先结束未完成的取消操作")
        control = await _ensure_control(session, tenant_id, workitem_id)
        control.closed = 0
        control.modifier_id = user_id
        control.gmt_modified = now_local()
    await _audit(session, tenant_id, workitem_id, user_id, "REOPEN_DELIVERY", "WORKITEM")
    await session.commit()
    return await state(session, tenant_id, workitem_id)


async def state(session: AsyncSession, tenant_id: int, workitem_id: int) -> dict[str, object]:
    """恢复页：关闭标记，以及每条工单派发的阶段和可否重试。"""
    await _require_workitem_row(session, tenant_id, workitem_id)
    retries = _package_retries()
    executions: list[dict[str, object]] = []
    for dispatch in await list_workitem_dispatches(session, tenant_id, workitem_id):
        detail = await _recovery_detail(session, tenant_id, dispatch.id, retries)
        reason = dispatch.error
        if reason is None:
            stored = detail.get("reason")
            if isinstance(stored, str):
                reason = stored
        error_code = None
        if reason is not None:
            error_code = reason.split(":", 1)[0]
        executions.append(
            {
                "dispatchId": dispatch.id,
                "status": dispatch.status,
                "error": dispatch.error,
                "agentId": dispatch.agent_id,
                "attempt": dispatch.attempt,
                "updatedAt": dispatch.gmt_modified,
                "recovery": detail,
                "phase": dispatch.status,
                "errorCode": error_code,
                "retryable": dispatch.status in RETRYABLE,
            }
        )
    return {"closed": await closed(session, tenant_id, workitem_id), "executions": executions}


async def ready(session: AsyncSession, dispatch: Dispatch) -> bool:
    """打包退避还没到点时，主环先不取这条派发。"""
    row = await _live_recovery(session, dispatch.tenant_id, dispatch.id)
    if row is None or row.next_retry_at is None:
        return True
    return row.next_retry_at <= now_local()


async def waiting(
    session: AsyncSession,
    dispatch: Dispatch,
    reason: str | None,
    retry_delay_millis: int = 0,
) -> None:
    """记下等待原因。已经请求取消的行不改原因和下一次时间。"""
    row = await _ensure_recovery(session, dispatch.tenant_id, dispatch.id)
    if row.cancel_requested != 1:
        row.phase = "PENDING"
        row.reason = reason
        if retry_delay_millis > 0:
            row.next_retry_at = now_local() + timedelta(milliseconds=retry_delay_millis)
        else:
            row.next_retry_at = None
    else:
        row.phase = "PENDING"
    row.gmt_modified = now_local()
    await session.commit()


async def wake_capacity_waits(session: AsyncSession, agent_id: int) -> None:
    """新的心跳比过期的容量退避更近，清掉对应的下一次重试时间。"""
    rows = (
        await session.scalars(
            select(Dispatch).where(Dispatch.agent_id == agent_id, Dispatch.status == "PENDING")
        )
    ).all()
    for dispatch in rows:
        recovery = await _live_recovery(session, dispatch.tenant_id, dispatch.id)
        if recovery is None or recovery.cancel_requested == 1:
            continue
        if recovery.reason != "EXECUTOR_AT_CAPACITY":
            continue
        recovery.next_retry_at = None
        recovery.gmt_modified = now_local()
    await session.commit()


async def retry_packaging(session: AsyncSession, dispatch: Dispatch, reason: str | None) -> bool:
    """只重试还没下发的打包。次数和退避都写在恢复行上。"""
    accepted = False
    async with transaction_unit(session):
        await _lock_subject(session, dispatch)
        current = await find_dispatch(session, dispatch.id)
        if (
            current is None
            or current.status != "PACKAGING"
            or current.version != dispatch.version
            or await fenced(session, current)
        ):
            accepted = False
        else:
            row = await _ensure_recovery(session, dispatch.tenant_id, dispatch.id)
            if row.retry_count >= max(0, _package_retries()):
                accepted = False
            else:
                delay = max(1000, _retry_delay_ms()) * (1 << min(row.retry_count, 8))
                text = PACKAGE_TRANSIENT
                if reason is not None:
                    text = reason[:512]
                row.retry_count = row.retry_count + 1
                row.phase = "PACKAGING"
                row.reason = text
                row.next_retry_at = now_local() + timedelta(milliseconds=delay)
                row.gmt_modified = now_local()
                accepted = (
                    await _return_packaging(
                        session, dispatch.id, dispatch.tenant_id, dispatch.version
                    )
                    == 1
                )
    return accepted


async def reconcile(
    session: AsyncSession,
    pause: Pause = deliver_pause,
    online: Online = is_online,
    snapshot: SnapshotOf = current_snapshot,
) -> None:
    """补齐终态评论投影，并重送或自愈尚未确认的停止意图。"""
    await _reconcile_terminal_guidance(session)
    await _reconcile_missing_reply_ack(session)
    await _reconcile_orphan_guidance(session)
    await reconcile_stops(session, None, pause, online, snapshot)
    await session.commit()


async def reconcile_executor(
    session: AsyncSession,
    executor_id: int,
    pause: Pause = deliver_pause,
    online: Online = is_online,
    snapshot: SnapshotOf = current_snapshot,
) -> None:
    """一次心跳之后，只对账这台执行器上的停止意图。"""
    await reconcile_stops(session, executor_id, pause, online, snapshot)


async def send_stop(
    session: AsyncSession,
    dispatch: Dispatch | None,
    pause: Pause = deliver_pause,
) -> None:
    """停止意图是发件箱。30 秒内不重复发送，发送失败留给下一轮。"""
    if dispatch is None or dispatch.executor_id is None:
        return
    if not await cancel_requested(session, dispatch.tenant_id, dispatch.id):
        return
    row = await _live_recovery(session, dispatch.tenant_id, dispatch.id)
    cutoff = now_local() - timedelta(milliseconds=STOP_RESEND_MS)
    if row is None or row.stop_pending != 1:
        return
    if row.last_sent_at is not None and row.last_sent_at >= cutoff:
        return
    row.last_sent_at = now_local()
    row.gmt_modified = now_local()
    await session.commit()
    try:
        await pause(dispatch)
    except Exception:
        logger.warning(
            "cancel control delivery deferred dispatchId=%s",
            dispatch.id,
            exc_info=True,
        )


async def list_workitem_dispatches(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
) -> list[Dispatch]:
    """同一工单的工单来源派发，按创建时间从早到晚。"""
    rows = (
        await session.scalars(
            select(Dispatch).where(
                Dispatch.tenant_id == tenant_id,
                Dispatch.workitem_id == workitem_id,
                Dispatch.source_type == "WORKITEM",
                Dispatch.is_deleted == 0,
            )
        )
    ).all()
    ordered = sorted(rows, key=_dispatch_order)
    return [_copy_dispatch(row) for row in ordered]


async def reconcile_stops(
    session: AsyncSession,
    executor_id: int | None,
    pause: Pause,
    online: Online,
    snapshot: SnapshotOf,
) -> None:
    cutoff = now_local() - timedelta(milliseconds=STOP_RESEND_MS)
    pending = await _pending_stops(session, executor_id, cutoff)
    for recovery, dispatch in pending[:RECONCILE_LIMIT]:
        executor = None
        if dispatch is not None:
            executor = dispatch.executor_id
        if (
            dispatch is not None
            and executor is not None
            and _runtime_proves_stopped(dispatch, recovery.requested_at, online, snapshot)
            and await on_stopped(session, dispatch.tenant_id, executor, dispatch.id)
        ):
            logger.info(
                "stop intent self-healed from runtime running set dispatchId=%s executorId=%s",
                dispatch.id,
                dispatch.executor_id,
            )
            continue
        await send_stop(session, dispatch, pause)


def _runtime_proves_stopped(
    dispatch: Dispatch,
    requested_at: datetime | None,
    online: Online,
    snapshot: SnapshotOf,
) -> bool:
    if dispatch.executor_id is None:
        return False
    if requested_at is not None and requested_at > now_local() - timedelta(
        milliseconds=STOP_SELF_HEAL_GRACE_MS
    ):
        return False
    executor_id = dispatch.executor_id
    if not online(executor_id):
        return False
    current = snapshot(executor_id)
    if current is None or not current.inventory_ready or current.inventory_error is not None:
        return False
    return dispatch.id not in current.owned_dispatch_ids


async def _apply_transition(
    session: AsyncSession,
    dispatch: Dispatch,
    status: str,
    agent_version_id: int | None,
    executor_id: int | None,
    package_oss_ref: str | None,
    result_summary: str | None,
    error: str | None,
) -> int:
    changed = await update_status(
        session,
        dispatch.id,
        dispatch.tenant_id,
        status,
        agent_version_id,
        executor_id,
        package_oss_ref,
        result_summary,
        error,
        dispatch.version,
        0,
    )
    if changed == 1 and not is_terminal(status):
        row = await _live_recovery(session, dispatch.tenant_id, dispatch.id)
        if row is not None and row.cancel_requested == 0:
            row.phase = status
            row.reason = None
            row.next_retry_at = None
            row.gmt_modified = now_local()
    if changed == 1 and is_terminal(status):
        await project_terminal(session, dispatch, status, error)
        if status == "TIMEOUT" and dispatch.executor_id is not None:
            await _mark_timeout_stop(session, dispatch, error)
        await _audit(
            session,
            dispatch.tenant_id,
            dispatch.id,
            0,
            "DISPATCH_" + status,
            "DISPATCH",
        )
    return changed


async def project_terminal(
    session: AsyncSession,
    dispatch: Dispatch,
    status: str,
    error: str | None,
) -> None:
    """成功留给回复确认。其余终态把仍在排队或已送达的指引改成失败或取消。"""
    if status == "SUCCEEDED":
        return
    guidance_status = "FAILED"
    if status == "CANCELED":
        guidance_status = "CANCELED"
    message = status
    if error is not None:
        message = error
    await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.tenant_id == dispatch.tenant_id,
            WorkitemCommentDelivery.dispatch_id == dispatch.id,
            WorkitemCommentDelivery.status.in_(tuple(OPEN_GUIDANCE)),
        )
        .values(status=guidance_status, error=message, gmt_modified=now_local())
    )


async def _confirm_stop(session: AsyncSession, dispatch: Dispatch) -> bool:
    previous_status = dispatch.status
    previous_error = dispatch.error
    should_write = not is_terminal(previous_status) or (
        previous_status == "CANCELED" and previous_error != USER_CANCELED
    )
    if should_write:
        changed = await update_status(
            session,
            dispatch.id,
            dispatch.tenant_id,
            "CANCELED",
            None,
            None,
            None,
            None,
            USER_CANCELED,
            dispatch.version,
            0,
        )
        if changed != 1:
            return False
    row = await _live_recovery(session, dispatch.tenant_id, dispatch.id)
    if row is not None:
        row.stop_pending = 0
        row.reason = USER_CANCELED
        row.gmt_modified = now_local()
    if is_terminal(previous_status):
        await project_terminal(session, dispatch, previous_status, previous_error)
    else:
        await project_terminal(session, dispatch, "CANCELED", USER_CANCELED)
    return True


async def _cancel_locked(
    session: AsyncSession,
    dispatch: Dispatch,
    user_id: int,
    force: bool,
) -> None:
    if is_terminal(dispatch.status):
        await project_terminal(session, dispatch, dispatch.status, dispatch.error)
        return
    if not force and await cancel_requested(session, dispatch.tenant_id, dispatch.id):
        return
    pending_stop = (
        dispatch.executor_id is not None and dispatch.status not in QUIET_BEFORE_STOP
    )
    target = "CANCELED"
    if pending_stop and not force:
        target = "PAUSING"
    reason = USER_CANCELED
    if pending_stop and force:
        reason = STOP_UNCONFIRMED
    elif pending_stop:
        reason = CANCEL_REQUESTED
    changed = await update_status(
        session,
        dispatch.id,
        dispatch.tenant_id,
        target,
        None,
        None,
        None,
        None,
        reason,
        dispatch.version,
        user_id,
    )
    if changed != 1:
        raise BizError(ErrorCode.CONFLICT, "执行状态已变化，请重试取消")
    await _upsert_cancel(session, dispatch, user_id, pending_stop, force, reason)
    await project_terminal(session, dispatch, "CANCELED", reason)
    action = "CANCEL_DISPATCH"
    if force:
        action = "FORCE_CANCEL_DISPATCH"
    await _audit(session, dispatch.tenant_id, dispatch.id, user_id, action, "DISPATCH")


async def _upsert_cancel(
    session: AsyncSession,
    dispatch: Dispatch,
    user_id: int,
    pending_stop: bool,
    force: bool,
    reason: str,
) -> None:
    row = await _ensure_recovery(session, dispatch.tenant_id, dispatch.id)
    row.cancel_requested = 1
    row.stop_pending = 1 if pending_stop else 0
    forced = 1 if force else 0
    row.forced = max(row.forced, forced)
    if row.requested_at is None:
        row.requested_at = now_local()
    row.modifier_id = user_id
    row.reason = reason
    row.gmt_modified = now_local()


async def _mark_timeout_stop(
    session: AsyncSession,
    dispatch: Dispatch,
    error: str | None,
) -> None:
    row = await _ensure_recovery(session, dispatch.tenant_id, dispatch.id)
    row.cancel_requested = 1
    row.stop_pending = 1
    row.forced = 1
    if row.requested_at is None:
        row.requested_at = now_local()
    row.reason = error
    row.gmt_modified = now_local()


async def _copy_continue_guidance(session: AsyncSession, dispatch: Dispatch) -> None:
    key = dispatch.idempotency_key
    if dispatch.resume_mode != "CANONICAL_INTERACTION" or not key.startswith("continue:"):
        return
    if dispatch.resume_from_dispatch_id is None:
        return
    rows = (
        await session.scalars(
            select(WorkitemCommentDelivery).where(
                WorkitemCommentDelivery.tenant_id == dispatch.tenant_id,
                WorkitemCommentDelivery.dispatch_id == dispatch.resume_from_dispatch_id,
            )
        )
    ).all()
    for row in rows:
        if row.status not in {"FAILED", "CANCELED"} or row.reply_comment_id is not None:
            continue
        session.add(
            WorkitemCommentDelivery(
                tenant_id=row.tenant_id,
                source_type=row.source_type,
                workitem_id=row.workitem_id,
                comment_id=row.comment_id,
                target_agent_id=row.target_agent_id,
                dispatch_id=dispatch.id,
                status="QUEUED",
                retry_dispatch_id=dispatch.id,
            )
        )
    await session.flush()


async def _return_packaging(
    session: AsyncSession,
    dispatch_id: int,
    tenant_id: int,
    version: int,
) -> int:
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch_id,
            Dispatch.tenant_id == tenant_id,
            Dispatch.status == "PACKAGING",
            Dispatch.version == version,
            Dispatch.is_deleted == 0,
        )
        .values(
            status="PENDING",
            executor_id=None,
            package_oss_ref=None,
            version=Dispatch.version + 1,
            modifier_id=0,
            gmt_modified=now_local(),
        )
    )
    return rowcount(result)


async def _reconcile_terminal_guidance(session: AsyncSession) -> None:
    guidance_rows = await _open_guidance(session)
    dispatch_ids = sorted(
        {row.dispatch_id for row in guidance_rows if row.dispatch_id is not None}
    )
    repaired = 0
    for dispatch_id in dispatch_ids:
        if repaired >= RECONCILE_LIMIT:
            return
        dispatch = await find_dispatch(session, dispatch_id)
        if dispatch is None or dispatch.status not in {"FAILED", "TIMEOUT", "CANCELED"}:
            continue
        matched = [
            row
            for row in guidance_rows
            if row.dispatch_id == dispatch_id and row.tenant_id == dispatch.tenant_id
        ]
        if len(matched) == 0:
            continue
        await project_terminal(session, dispatch, dispatch.status, dispatch.error)
        repaired = repaired + 1


async def _reconcile_missing_reply_ack(session: AsyncSession) -> None:
    cutoff = now_local() - timedelta(milliseconds=SUCCESS_ACK_WAIT_MS)
    chosen: list[WorkitemCommentDelivery] = []
    for row in await _open_guidance(session):
        if row.dispatch_id is None:
            continue
        dispatch = await find_dispatch(session, row.dispatch_id)
        if (
            dispatch is None
            or dispatch.tenant_id != row.tenant_id
            or dispatch.status != "SUCCEEDED"
            or dispatch.gmt_modified is None
            or dispatch.gmt_modified >= cutoff
        ):
            continue
        chosen.append(row)
    chosen.sort(key=lambda row: row.id)
    now = now_local()
    for row in chosen[:RECONCILE_LIMIT]:
        if row.status not in OPEN_GUIDANCE:
            continue
        if row.reply_comment_id is None:
            row.status = "FAILED"
            row.error = REPLY_ACK_MISSING
        else:
            row.status = "APPLIED"
            row.error = None
        row.gmt_modified = now


async def _reconcile_orphan_guidance(session: AsyncSession) -> None:
    cutoff = now_local() - timedelta(milliseconds=ORPHAN_GUIDANCE_MS)
    rows = await _open_guidance(session)
    rows.sort(key=lambda row: row.id)
    repaired = 0
    now = now_local()
    for row in rows:
        if repaired >= RECONCILE_LIMIT:
            return
        if row.gmt_modified is None or row.gmt_modified >= cutoff:
            continue
        owner = None
        if row.dispatch_id is not None:
            owner = await find_dispatch(session, row.dispatch_id)
            if owner is not None and owner.tenant_id != row.tenant_id:
                owner = None
        if owner is not None:
            continue
        if row.gmt_modified >= cutoff or row.status not in OPEN_GUIDANCE:
            continue
        row.status = "FAILED"
        row.error = ORPHAN_GUIDANCE
        row.gmt_modified = now
        repaired = repaired + 1


async def _pending_stops(
    session: AsyncSession,
    executor_id: int | None,
    cutoff: datetime,
) -> list[tuple[DispatchRecovery, Dispatch | None]]:
    rows = (
        await session.scalars(
            select(DispatchRecovery).where(DispatchRecovery.stop_pending == 1)
        )
    ).all()
    chosen: list[tuple[DispatchRecovery, Dispatch | None]] = []
    for row in rows:
        if row.last_sent_at is not None and row.last_sent_at >= cutoff:
            continue
        dispatch = await find_dispatch(session, row.dispatch_id)
        if executor_id is not None and (
            dispatch is None or dispatch.executor_id != executor_id
        ):
            continue
        chosen.append((row, dispatch))
    chosen.sort(key=lambda item: _sent_order(item[0]))
    return chosen


async def _open_guidance(session: AsyncSession) -> list[WorkitemCommentDelivery]:
    rows = (await session.scalars(select(WorkitemCommentDelivery))).all()
    return [row for row in rows if row.status in OPEN_GUIDANCE]


async def _recovery_detail(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    retries: int,
) -> dict[str, object]:
    row = await _live_recovery(session, tenant_id, dispatch_id)
    detail: dict[str, object] = {}
    if row is not None:
        detail = {
            "cancel_requested": row.cancel_requested,
            "stop_pending": row.stop_pending,
            "forced": row.forced,
            "retry_count": row.retry_count,
            "next_retry_at": row.next_retry_at,
            "phase": row.phase,
            "reason": row.reason,
            "requested_at": row.requested_at,
        }
    detail["max_retries"] = retries
    return detail


async def _lock_subject(session: AsyncSession, dispatch: Dispatch) -> None:
    if execution_source(dispatch) != "WORKITEM":
        return
    await _ensure_control(session, dispatch.tenant_id, dispatch.workitem_id)


async def _ensure_control(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
) -> WorkitemExecutionControl:
    row = await _control(session, tenant_id, workitem_id)
    if row is None:
        row = WorkitemExecutionControl(
            tenant_id=tenant_id,
            workitem_id=workitem_id,
            closed=0,
            modifier_id=0,
            gmt_modified=now_local(),
        )
        session.add(row)
        await session.flush()
    return row


async def _control(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
) -> WorkitemExecutionControl | None:
    return await session.scalar(
        select(WorkitemExecutionControl).where(
            WorkitemExecutionControl.tenant_id == tenant_id,
            WorkitemExecutionControl.workitem_id == workitem_id,
        )
    )


async def _ensure_recovery(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> DispatchRecovery:
    row = await _live_recovery(session, tenant_id, dispatch_id)
    if row is None:
        row = DispatchRecovery(
            tenant_id=tenant_id,
            dispatch_id=dispatch_id,
            cancel_requested=0,
            stop_pending=0,
            forced=0,
            retry_count=0,
            modifier_id=0,
            gmt_modified=now_local(),
        )
        session.add(row)
        await session.flush()
    return row


async def _live_recovery(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> DispatchRecovery | None:
    return await session.scalar(
        select(DispatchRecovery).where(
            DispatchRecovery.tenant_id == tenant_id,
            DispatchRecovery.dispatch_id == dispatch_id,
        )
    )


async def _require_workitem(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
) -> Dispatch:
    dispatch = await find_dispatch(session, dispatch_id)
    if (
        dispatch is None
        or dispatch.tenant_id != tenant_id
        or execution_source(dispatch) != "WORKITEM"
        or dispatch.workitem_id != workitem_id
    ):
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    return dispatch


async def _require_workitem_row(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> Workitem:
    row = await session.scalar(select(Workitem).where(Workitem.id == workitem_id).limit(1))
    if row is None or row.tenant_id != tenant_id:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return row


async def _audit(
    session: AsyncSession,
    tenant_id: int,
    subject_id: int,
    user_id: int,
    action: str,
    target_type: str,
) -> None:
    actor_type = "HUMAN"
    if user_id == 0:
        actor_type = "SYSTEM"
    await record_required(
        session,
        AuditRecord(
            tenant_id=tenant_id,
            actor_id=user_id,
            actor_type=actor_type,
            module="dispatch",
            action=action,
            target_type=target_type,
            target_id=subject_id,
            event_type=action,
            trigger_type="RECOVERY",
        ),
    )


def _subject(tenant_id: int, workitem_id: int) -> Dispatch:
    return Dispatch(
        tenant_id=tenant_id,
        workitem_id=workitem_id,
        source_type="WORKITEM",
        agent_id=0,
        status="PENDING",
        idempotency_key="lock",
        is_deleted=0,
        version=0,
    )


def _copy_dispatch(row: Dispatch) -> Dispatch:
    return Dispatch(
        id=row.id,
        tenant_id=row.tenant_id,
        source_type=row.source_type,
        workitem_id=row.workitem_id,
        sdlc_step_id=row.sdlc_step_id,
        agent_id=row.agent_id,
        agent_version_id=row.agent_version_id,
        executor_id=row.executor_id,
        package_oss_ref=row.package_oss_ref,
        status=row.status,
        attempt=row.attempt,
        idempotency_key=row.idempotency_key,
        result_summary=row.result_summary,
        error=row.error,
        resume_from_dispatch_id=row.resume_from_dispatch_id,
        delivery_source_dispatch_id=row.delivery_source_dispatch_id,
        resume_mode=row.resume_mode,
        debug_log_enabled=row.debug_log_enabled,
        gmt_create=row.gmt_create,
        gmt_modified=row.gmt_modified,
        creator_id=row.creator_id,
        modifier_id=row.modifier_id,
        is_deleted=row.is_deleted,
        version=row.version,
    )


def _dispatch_order(row: Dispatch) -> tuple[datetime, int]:
    created = row.gmt_create
    if created is None:
        created = datetime.min
    return created, row.id


def _sent_order(row: DispatchRecovery) -> tuple[int, datetime]:
    if row.last_sent_at is None:
        return 0, datetime.min
    return 1, row.last_sent_at


def _package_retries() -> int:
    return get_settings().dispatch_recovery_package_retries


def _retry_delay_ms() -> int:
    return get_settings().dispatch_recovery_retry_delay_ms
