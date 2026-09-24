"""用户请求暂停一条工单派发。发送失败时把状态写成 PAUSE_FAILED。"""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery import (
    execution_source,
    find_dispatch,
    transaction_unit,
    update_status,
)
from autowonder.dispatch.transport import PAUSE_SEND_FAILURE, deliver_pause

PAUSEABLE = frozenset({"DISPATCHED", "ACKED", "RUNNING"})


async def request_workitem_pause(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    user_id: int,
    pause: Callable[[Dispatch], None] = deliver_pause,
) -> Dispatch:
    """把可暂停的工单派发改成 PAUSING，并向执行器发暂停帧。"""
    dispatch = await _require(session, tenant_id, workitem_id, dispatch_id)
    if dispatch.status == "PAUSED":
        return dispatch
    if dispatch.status == "PAUSING":
        pause(dispatch)
        return dispatch
    if dispatch.status not in PAUSEABLE and dispatch.status != "PAUSE_FAILED":
        raise BizError(ErrorCode.CONFLICT, "当前执行状态不能暂停")
    cleared: str | None = None
    if dispatch.status == "PAUSE_FAILED":
        cleared = ""
    async with transaction_unit(session):
        changed = await update_status(
            session,
            dispatch.id,
            tenant_id,
            "PAUSING",
            None,
            None,
            None,
            None,
            cleared,
            dispatch.version,
            user_id,
        )
    if changed != 1:
        raise BizError(ErrorCode.CONFLICT, "执行状态已变化，请刷新后重试")
    dispatch.status = "PAUSING"
    dispatch.error = None
    dispatch.version = dispatch.version + 1
    try:
        pause(dispatch)
    except Exception as send_failed:
        await _mark_failed(session, dispatch, user_id, send_failed)
        raise
    return dispatch


async def _mark_failed(
    session: AsyncSession,
    dispatch: Dispatch,
    user_id: int,
    send_failed: BaseException,
) -> None:
    message = str(send_failed).strip()
    if message == "":
        message = PAUSE_SEND_FAILURE
    async with transaction_unit(session):
        await update_status(
            session,
            dispatch.id,
            dispatch.tenant_id,
            "PAUSE_FAILED",
            None,
            None,
            None,
            None,
            message,
            dispatch.version,
            user_id,
        )


async def _require(
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
