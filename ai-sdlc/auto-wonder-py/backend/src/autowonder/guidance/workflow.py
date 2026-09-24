"""旁路交互提出的正式流程。主调度还在跑时，返工先等它暂停。"""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.db.rows import rowcount
from autowonder.dispatch.enqueue import enqueue_interaction_rework, is_interaction
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.pause_request import request_workitem_pause
from autowonder.dispatch.pending import drive_remembered, remember_pending
from autowonder.guidance.service import _first_step, _sdlc_id
from autowonder.guidance.steps import resolve_step
from autowonder.sdlcs.models import SdlcStep
from autowonder.workitems.models import Workitem
from autowonder.workitems.service import rebind_for_interaction_rework

logger = logging.getLogger(__name__)

_PAUSEABLE = frozenset(
    {
        "PENDING",
        "PACKAGING",
        "DISPATCHED",
        "ACKED",
        "RUNNING",
        "PAUSING",
        "PAUSE_FAILED",
    }
)
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
_REWORK_ERROR = "COMMENT_REWORK"


async def apply_from_executor(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    side_dispatch_id: int,
    plan: dict[str, object],
) -> Dispatch | None:
    """只有持有这条交互调度的执行器可以把它转成正式流程。"""
    side = await session.get(Dispatch, side_dispatch_id)
    if side is None or side.tenant_id != tenant_id or side.executor_id != executor_id:
        return None
    return await apply_plan(session, tenant_id, side_dispatch_id, plan)


async def apply_plan(
    session: AsyncSession, tenant_id: int, side_dispatch_id: int, plan: dict[str, object]
) -> Dispatch | None:
    """按交互调度上的员工创建评论返工。计划里的其他员工 id 只记日志。"""
    side = await session.get(Dispatch, side_dispatch_id)
    if (
        side is None
        or side.tenant_id != tenant_id
        or side.resume_mode
        not in {
            "SIDE_INTERACTION",
            "CANONICAL_INTERACTION",
        }
    ):
        return None
    proposed = plan.get("targetAgentId")
    if isinstance(proposed, int) and not isinstance(proposed, bool) and proposed != side.agent_id:
        logger.warning(
            "interaction plan target ignored sideDispatchId=%s proposedAgentId=%s "
            "authoritativeAgentId=%s",
            side_dispatch_id,
            proposed,
            side.agent_id,
        )
    target_agent_id = side.agent_id
    sdlc_id = await _sdlc_id(session, tenant_id, target_agent_id)
    if sdlc_id is None:
        logger.warning(
            "interaction plan SDLC unresolved sideDispatchId=%s targetAgentId=%s",
            side_dispatch_id,
            target_agent_id,
        )
        return None
    step = await _target_step(session, tenant_id, sdlc_id, side, plan)
    if step is None or step.id is None:
        logger.warning(
            "interaction plan target unresolved sideDispatchId=%s targetAgentId=%s",
            side_dispatch_id,
            target_agent_id,
        )
        return None
    workitem = await _lock_workitem(session, tenant_id, side.workitem_id)
    if workitem is None:
        logger.info(
            "interaction plan ignored because workitem is missing sideDispatchId=%s workitemId=%s",
            side_dispatch_id,
            side.workitem_id,
        )
        return None
    rows = await _workitem_rows(session, tenant_id, side.workitem_id)
    decision = await _queue_rework(session, tenant_id, side, rows, target_agent_id, step.id)
    if decision is None:
        return None
    rework, wait_for = decision
    await session.commit()
    await drive_remembered(session)
    if wait_for is not None and rework.status == "WAITING_FOR_PAUSE":
        await _pause_predecessor(session, tenant_id, side.workitem_id, wait_for, rework.id)
    return rework


async def on_paused(session: AsyncSession, tenant_id: int, paused_dispatch_id: int) -> None:
    """主调度已经暂停时，放行等着它的最新返工。"""
    paused = await session.get(Dispatch, paused_dispatch_id)
    if paused is not None and paused.tenant_id == tenant_id:
        await activate_latest_waiting(session, tenant_id, paused.workitem_id, paused_dispatch_id)


async def _target_step(
    session: AsyncSession,
    tenant_id: int,
    sdlc_id: int,
    side: Dispatch,
    plan: dict[str, object],
) -> SdlcStep | None:
    steps = list(
        (
            await session.scalars(
                select(SdlcStep).where(SdlcStep.sdlc_id == sdlc_id, SdlcStep.is_deleted == 0)
            )
        ).all()
    )
    requested = resolve_step(
        steps,
        tenant_id,
        _plan_text(plan, "targetStepId"),
        _plan_text(plan, "targetStepHint"),
    )
    if isinstance(requested, SdlcStep):
        return requested
    if side.sdlc_step_id is not None:
        requested = resolve_step(steps, tenant_id, str(side.sdlc_step_id), None)
        if isinstance(requested, SdlcStep):
            return requested
    fallback = await _first_step(session, tenant_id, sdlc_id)
    return fallback


async def _queue_rework(
    session: AsyncSession,
    tenant_id: int,
    side: Dispatch,
    rows: list[Dispatch],
    target_agent_id: int,
    step_id: int,
) -> tuple[Dispatch, int | None] | None:
    target_source = _target_in_delivery(rows, target_agent_id)
    if target_source is None and side.agent_id == target_agent_id:
        target_source = side
    if target_source is None:
        logger.info(
            "interaction plan ignored for worker outside current delivery "
            "sideDispatchId=%s targetAgentId=%s",
            side.id,
            target_agent_id,
        )
        return None
    active = _latest_main(rows)
    wait_for = await _fence_active(session, tenant_id, active)
    rework = await enqueue_interaction_rework(
        session,
        tenant_id,
        side.workitem_id,
        target_agent_id,
        step_id,
        target_source.id,
        side.id,
        wait_for,
        0,
    )
    if wait_for is not None:
        await _supersede_older_waiters(session, tenant_id, rows, rework.id, wait_for)
    if wait_for is None and rework.status == "WAITING_FOR_PAUSE":
        await _activate(session, tenant_id, rework)
        session.expire(rework)
        refreshed = await session.get(Dispatch, rework.id)
        if refreshed is not None:
            rework = refreshed
    return rework, wait_for


async def _pause_predecessor(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    wait_for: int,
    rework_id: int,
) -> None:
    try:
        state = await request_workitem_pause(session, tenant_id, workitem_id, wait_for, 0)
    except Exception as pause_failure:
        await _recover_pause(session, tenant_id, workitem_id, wait_for, rework_id, pause_failure)
        return
    if state.status == "PAUSED":
        await activate_latest_waiting(session, tenant_id, workitem_id, wait_for)


async def _recover_pause(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    wait_for: int,
    rework_id: int,
    pause_failure: Exception,
) -> None:
    session.expire_all()
    current = await session.get(Dispatch, wait_for)
    if current is not None and (current.status == "PAUSED" or current.status in _TERMINAL):
        await activate_latest_waiting(session, tenant_id, workitem_id, wait_for)
        return
    if current is not None and current.status == "PAUSING":
        raise pause_failure
    await _cancel_waiting(session, tenant_id, rework_id)
    await session.commit()
    raise pause_failure


async def activate_latest_waiting(
    session: AsyncSession, tenant_id: int, workitem_id: int, paused_dispatch_id: int
) -> None:
    """同一暂停只放行 id 最大的等待返工，其余取消。"""
    if await _lock_workitem(session, tenant_id, workitem_id) is None:
        logger.info(
            "waiting interaction rework ignored because workitem is missing "
            "workitemId=%s pausedDispatchId=%s",
            workitem_id,
            paused_dispatch_id,
        )
        return
    rows = await _workitem_rows(session, tenant_id, workitem_id)
    summary = "waitForDispatchId=" + str(paused_dispatch_id)
    waiting = [
        row for row in rows if row.status == "WAITING_FOR_PAUSE" and row.result_summary == summary
    ]
    waiting.sort(key=lambda row: row.id, reverse=True)
    if len(waiting) == 0:
        return
    for older in waiting[1:]:
        await _cancel_waiting(session, tenant_id, older.id)
    await _activate(session, tenant_id, waiting[0])
    await session.commit()
    await drive_remembered(session)


async def _activate(session: AsyncSession, tenant_id: int, rework: Dispatch) -> None:
    sdlc_id = await _sdlc_id(session, tenant_id, rework.agent_id)
    if sdlc_id is None or rework.sdlc_step_id is None:
        raise RuntimeError("comment rework target is no longer valid")
    steps = list(
        (
            await session.scalars(
                select(SdlcStep).where(SdlcStep.sdlc_id == sdlc_id, SdlcStep.is_deleted == 0)
            )
        ).all()
    )
    if resolve_step(steps, tenant_id, str(rework.sdlc_step_id), None) is None:
        raise RuntimeError("comment rework target is no longer valid")
    await rebind_for_interaction_rework(
        session,
        tenant_id,
        rework.workitem_id,
        rework.agent_id,
        sdlc_id,
        rework.sdlc_step_id,
        0,
    )
    if not await _release(session, tenant_id, rework.id):
        raise RuntimeError("comment rework release lost optimistic-lock race")


async def _fence_active(
    session: AsyncSession, tenant_id: int, active: Dispatch | None
) -> int | None:
    if active is None:
        return None
    if active.status in {"PENDING", "PACKAGING"}:
        if await _cancel_undelivered(session, tenant_id, active.id):
            return None
        session.expire_all()
        active = await session.get(Dispatch, active.id)
    if active is None or active.status in _TERMINAL or active.status == "PAUSED":
        return None
    return active.id


async def _supersede_older_waiters(
    session: AsyncSession,
    tenant_id: int,
    rows: list[Dispatch],
    rework_id: int,
    wait_for: int,
) -> None:
    summary = "waitForDispatchId=" + str(wait_for)
    for row in rows:
        if row.id == rework_id:
            continue
        if row.status == "WAITING_FOR_PAUSE" and row.result_summary == summary:
            await _cancel_waiting(session, tenant_id, row.id)


async def _cancel_undelivered(session: AsyncSession, tenant_id: int, dispatch_id: int) -> bool:
    for _retry in range(3):
        current = await session.get(Dispatch, dispatch_id)
        if current is None or current.tenant_id != tenant_id or current.source_type != "WORKITEM":
            return False
        if current.status in _TERMINAL or current.status == "PAUSED":
            return True
        if current.status not in {"PENDING", "PACKAGING"}:
            return False
        result = await session.execute(
            update(Dispatch)
            .where(
                Dispatch.id == current.id,
                Dispatch.tenant_id == tenant_id,
                Dispatch.version == current.version,
                Dispatch.is_deleted == 0,
            )
            .values(
                status="CANCELED",
                error=_REWORK_ERROR,
                version=Dispatch.version + 1,
                modifier_id=0,
            )
        )
        if rowcount(result) == 1:
            return True
        session.expire(current)
    return False


async def _cancel_waiting(session: AsyncSession, tenant_id: int, dispatch_id: int) -> bool:
    current = await session.get(Dispatch, dispatch_id)
    if (
        current is None
        or current.tenant_id != tenant_id
        or current.source_type != "WORKITEM"
        or current.status != "WAITING_FOR_PAUSE"
    ):
        return False
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == current.id,
            Dispatch.tenant_id == tenant_id,
            Dispatch.version == current.version,
            Dispatch.is_deleted == 0,
        )
        .values(
            status="CANCELED",
            error=_REWORK_ERROR,
            version=Dispatch.version + 1,
            modifier_id=0,
        )
    )
    return rowcount(result) == 1


async def _release(session: AsyncSession, tenant_id: int, dispatch_id: int) -> bool:
    current = await session.get(Dispatch, dispatch_id)
    if (
        current is None
        or current.tenant_id != tenant_id
        or current.source_type != "WORKITEM"
        or current.status != "WAITING_FOR_PAUSE"
    ):
        return False
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == current.id,
            Dispatch.tenant_id == tenant_id,
            Dispatch.version == current.version,
            Dispatch.is_deleted == 0,
        )
        .values(status="PENDING", version=Dispatch.version + 1, modifier_id=0)
    )
    released = rowcount(result) == 1
    if released:
        remember_pending(session, dispatch_id)
    return released


async def _lock_workitem(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> Workitem | None:
    return await session.scalar(
        select(Workitem)
        .where(
            Workitem.id == workitem_id,
            Workitem.tenant_id == tenant_id,
            Workitem.is_deleted == 0,
        )
        .limit(1)
        .with_for_update()
    )


async def _workitem_rows(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> list[Dispatch]:
    result = await session.scalars(
        select(Dispatch).where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id == workitem_id,
            Dispatch.is_deleted == 0,
        )
    )
    return list(result.all())


def _target_in_delivery(rows: list[Dispatch], target_agent_id: int) -> Dispatch | None:
    formal = [row for row in rows if not is_interaction(row)]
    formal.sort(key=lambda row: row.id, reverse=True)
    for row in formal:
        if row.agent_id == target_agent_id:
            return row
        if row.resume_mode == "COMMENT_REWORK":
            return None
    return None


def _latest_main(rows: list[Dispatch]) -> Dispatch | None:
    chosen: Dispatch | None = None
    for row in rows:
        if is_interaction(row) or row.status not in _PAUSEABLE:
            continue
        if chosen is None or row.id > chosen.id:
            chosen = row
    return chosen


def _plan_text(plan: dict[str, object], key: str) -> str | None:
    value = plan.get(key)
    if isinstance(value, str):
        return value
    return None
