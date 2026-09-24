"""执行器上报的忙、暂停成功和暂停失败。"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.db.rows import rowcount
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery import waiting

_SYSTEM_USER_ID = 0


async def on_busy(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
) -> bool:
    """已下发但执行器容量已满时退回 PENDING，并记下 2 秒后再试。"""
    dispatch = await _owned(session, tenant_id, executor_id, dispatch_id)
    if dispatch is None or dispatch.status != "DISPATCHED":
        return False
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch.id,
            Dispatch.tenant_id == tenant_id,
            Dispatch.executor_id == executor_id,
            Dispatch.status == "DISPATCHED",
            Dispatch.version == dispatch.version,
            Dispatch.is_deleted == 0,
        )
        .values(
            status="PENDING",
            executor_id=None,
            package_oss_ref=None,
            version=Dispatch.version + 1,
            modifier_id=_SYSTEM_USER_ID,
        )
    )
    if rowcount(result) != 1:
        return False
    dispatch.status = "PENDING"
    await waiting(session, dispatch, "EXECUTOR_AT_CAPACITY", 2_000)
    return True


async def on_paused(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
    durable: bool,
) -> bool:
    """暂停中的派发在检查点可对上时进入 PAUSED。已经暂停的再报一次也算成功。"""
    dispatch = await _owned(session, tenant_id, executor_id, dispatch_id)
    if dispatch is None:
        return False
    if dispatch.status == "PAUSED":
        return True
    if dispatch.status != "PAUSING" or not durable:
        return False
    return await _move(session, dispatch, "PAUSED", None)


async def on_pause_failed(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
    error: str | None,
) -> bool:
    """仍在暂停中的派发改成 PAUSE_FAILED，并记下执行器给出的原因。"""
    dispatch = await _owned(session, tenant_id, executor_id, dispatch_id)
    if dispatch is None or dispatch.status != "PAUSING":
        return False
    return await _move(session, dispatch, "PAUSE_FAILED", error)


async def _owned(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
) -> Dispatch | None:
    dispatch = await session.get(Dispatch, dispatch_id)
    if dispatch is None or dispatch.is_deleted != 0:
        return None
    if dispatch.tenant_id != tenant_id or dispatch.executor_id != executor_id:
        return None
    return dispatch


async def _move(
    session: AsyncSession,
    dispatch: Dispatch,
    status: str,
    error: str | None,
) -> bool:
    values: dict[str, object] = {
        "status": status,
        "version": Dispatch.version + 1,
        "modifier_id": _SYSTEM_USER_ID,
    }
    if error is not None:
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
    if rowcount(result) != 1:
        return False
    await session.commit()
    return True
