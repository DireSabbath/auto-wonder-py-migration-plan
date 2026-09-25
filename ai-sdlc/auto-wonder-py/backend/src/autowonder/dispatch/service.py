"""工作空间删除后，停掉仍在执行的派发。

暂停帧发给已分配的执行器。发送失败时按 Java 把状态写成 PAUSE_FAILED。
"""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.transport import deliver_pause
from autowonder.scheduledtasks.service import DELETION_REASON

logger = logging.getLogger(__name__)

DISPATCH_LINKAGE_LIMIT = 500
CANCELABLE_STATUSES = frozenset({"PENDING", "PACKAGING", "WAITING_FOR_PAUSE"})
IN_FLIGHT_STATUSES = (
    "PENDING",
    "PACKAGING",
    "DISPATCHED",
    "ACKED",
    "RUNNING",
    "PAUSING",
    "PAUSE_FAILED",
    "WAITING_FOR_PAUSE",
)
PAUSE_SEND_FAILURE = "暂停请求发送失败，请重试暂停"


async def stop_deleted_workspace_dispatches(
    session: AsyncSession,
    workspace_id: int,
    operator_id: int,
) -> int:
    """取消尚未下发的派发，并向已下发的派发请求暂停。返回成功停下的条数。"""
    rows = (
        await session.scalars(
            select(Dispatch)
            .where(
                Dispatch.tenant_id == workspace_id,
                Dispatch.is_deleted == 0,
                Dispatch.status.in_(IN_FLIGHT_STATUSES),
            )
            .order_by(Dispatch.id.asc())
            .limit(DISPATCH_LINKAGE_LIMIT)
        )
    ).all()
    stopped = 0
    for dispatch in rows:
        if await _stop(session, dispatch, operator_id):
            stopped += 1
    if stopped < len(rows):
        logger.warning(
            "Stopped %s of %s in-flight dispatch(es) of deleted workspace %s",
            stopped,
            len(rows),
            workspace_id,
        )
    return stopped


async def _stop(session: AsyncSession, dispatch: Dispatch, operator_id: int) -> bool:
    try:
        if dispatch.status in CANCELABLE_STATUSES:
            return await _cancel(session, dispatch, operator_id)
        await _request_pause(session, dispatch, operator_id)
    except Exception:
        logger.warning(
            "Failed to stop dispatch %s of deleted workspace %s",
            dispatch.id,
            dispatch.tenant_id,
            exc_info=True,
        )
        return False
    return True


async def _send_pause(dispatch: Dispatch) -> None:
    """向这条派发的执行器发送暂停帧。"""
    await deliver_pause(dispatch)


async def _request_pause(session: AsyncSession, dispatch: Dispatch, operator_id: int) -> None:
    source = dispatch.source_type
    if source is None or source.strip() == "":
        source = "WORKITEM"
    if source != "SCHEDULED_TASK_RUN" and source != "WORKITEM":
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    if dispatch.status == "PAUSED":
        return
    if dispatch.status == "PAUSING":
        await _send_pause(dispatch)
        return
    if dispatch.status not in {"DISPATCHED", "ACKED", "RUNNING", "PAUSE_FAILED"}:
        raise BizError(ErrorCode.CONFLICT, "当前执行状态不能暂停")
    error: str | None = "" if dispatch.status == "PAUSE_FAILED" else None
    updated = await _set_status(
        session,
        dispatch,
        "PAUSING",
        operator_id,
        error,
        write_error=error is not None,
    )
    if updated != 1:
        raise BizError(ErrorCode.CONFLICT, "执行状态已变化，请刷新后重试")
    dispatch.status = "PAUSING"
    dispatch.version = dispatch.version + 1
    await session.commit()
    try:
        await _send_pause(dispatch)
    except RuntimeError as send_failed:
        await _mark_pause_failed(session, dispatch, operator_id, send_failed)
        raise


async def _mark_pause_failed(
    session: AsyncSession,
    dispatch: Dispatch,
    operator_id: int,
    send_failed: BaseException,
) -> None:
    message = str(send_failed).strip()
    if message == "":
        message = PAUSE_SEND_FAILURE
    updated = await _set_status(
        session,
        dispatch,
        "PAUSE_FAILED",
        operator_id,
        message,
        write_error=True,
    )
    if updated == 1:
        dispatch.status = "PAUSE_FAILED"
        dispatch.error = message
        dispatch.version = dispatch.version + 1
        await session.commit()


async def _cancel(session: AsyncSession, dispatch: Dispatch, operator_id: int) -> bool:
    updated = await _set_status(
        session,
        dispatch,
        "CANCELED",
        operator_id,
        DELETION_REASON,
        write_error=True,
    )
    if updated == 1:
        await session.commit()
    return updated == 1


async def _set_status(
    session: AsyncSession,
    dispatch: Dispatch,
    status: str,
    operator_id: int,
    error: str | None,
    write_error: bool,
) -> int:
    values: dict[str, object] = {
        "status": status,
        "version": Dispatch.version + 1,
        "modifier_id": operator_id,
    }
    if write_error:
        values["error"] = error
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch.id,
            Dispatch.tenant_id == dispatch.tenant_id,
            Dispatch.version == dispatch.version,
            Dispatch.is_deleted == 0,
        )
        .values(**values)
    )
    return rowcount(result)
