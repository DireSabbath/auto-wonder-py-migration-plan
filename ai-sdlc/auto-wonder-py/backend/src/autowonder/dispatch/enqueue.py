"""把调度行写成 PENDING。真正拉起执行器仍留给调度主环。"""

import logging
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.models import Dispatch, DispatchRecoveryCheckpoint

logger = logging.getLogger(__name__)

ACTIVE_TURN_STATUSES = frozenset(
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
INTERACTION_MODES = frozenset({"SIDE_INTERACTION", "CANONICAL_INTERACTION", "COMMENT_INTERACTION"})


def workitem_idempotency_key(workitem_id: int, sdlc_step_id: int | None, attempt: int) -> str:
    """工单幂等键是 ``工单:步骤:尝试``。步骤为空时用 root。"""
    step = "root"
    if sdlc_step_id is not None:
        step = str(sdlc_step_id)
    return str(workitem_id) + ":" + step + ":" + str(attempt)


def is_interaction(dispatch: Dispatch | None) -> bool:
    """旁路、正式会话和旧的评论交互都不算正式交付。"""
    if dispatch is None or dispatch.resume_mode is None:
        return False
    return dispatch.resume_mode in INTERACTION_MODES


async def enqueue_workitem(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    sdlc_step_id: int,
    agent_id: int,
    attempt: int,
    user_id: int,
) -> Dispatch:
    """按工单、步骤和尝试次数幂等插入一条 PENDING 调度。"""
    key = workitem_idempotency_key(workitem_id, sdlc_step_id, attempt)
    existing = await _by_key(session, workspace_id, key)
    if existing is None:
        existing = await _by_key(session, workspace_id, "WORKITEM:" + key)
    if existing is not None:
        logger.info("dispatch enqueue idempotent hit key=%s", key)
        return existing
    row = Dispatch(
        tenant_id=workspace_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        sdlc_step_id=sdlc_step_id,
        agent_id=agent_id,
        status="PENDING",
        attempt=attempt,
        idempotency_key=key,
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
    )
    winner = await _insert(session, workspace_id, key, row)
    if winner is row:
        logger.info(
            "dispatch enqueued dispatchId=%s workitemId=%s stepId=%s agentId=%s attempt=%s",
            row.id,
            workitem_id,
            sdlc_step_id,
            agent_id,
            attempt,
        )
    return winner


async def enqueue_assignment(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    sdlc_step_id: int,
    agent_id: int,
    assignment_version: int,
    user_id: int,
) -> Dispatch:
    """按指派版本幂等插入 PENDING。尝试次数取该步骤当前最大值加一。"""
    key = (
        "assignment:"
        + str(workitem_id)
        + ":"
        + str(sdlc_step_id)
        + ":"
        + str(agent_id)
        + ":"
        + str(assignment_version)
    )
    existing = await _by_key(session, workspace_id, key)
    if existing is not None:
        logger.info("assignment enqueue idempotent hit key=%s", key)
        return existing
    attempt = await _next_attempt(session, workspace_id, workitem_id, sdlc_step_id)
    row = Dispatch(
        tenant_id=workspace_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        sdlc_step_id=sdlc_step_id,
        agent_id=agent_id,
        status="PENDING",
        attempt=attempt,
        idempotency_key=key,
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
        is_deleted=0,
    )
    previous = await _latest_formal(session, workspace_id, workitem_id)
    if previous is not None:
        row.delivery_source_dispatch_id = effective_delivery_source(previous)
    created = await _insert(session, workspace_id, key, row)
    if created is row:
        logger.info(
            "assignment dispatch enqueued dispatchId=%s workitemId=%s stepId=%s "
            "agentId=%s attempt=%s",
            created.id,
            workitem_id,
            sdlc_step_id,
            agent_id,
            attempt,
        )
    return created


async def enqueue_comment_interaction(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    agent_id: int,
    source_dispatch_id: int | None,
    fork_source_session: bool,
    sdlc_step_id: int | None,
    guidance_id: int,
    user_id: int,
) -> Dispatch:
    """为一条评论指引插入交互调度。幂等键是 ``guidance:{id}``。"""
    key = "guidance:" + str(guidance_id)
    existing = await _by_key(session, workspace_id, key)
    if existing is not None:
        return existing
    attempt = await _next_attempt(session, workspace_id, workitem_id, sdlc_step_id)
    source = None
    if source_dispatch_id is not None:
        source = await _require_workitem_dispatch(
            session, workspace_id, workitem_id, source_dispatch_id
        )
    delivery_source = source
    if delivery_source is None:
        delivery_source = await _latest_formal(session, workspace_id, workitem_id)
    mode = "CANONICAL_INTERACTION"
    if fork_source_session:
        mode = "SIDE_INTERACTION"
    row = Dispatch(
        tenant_id=workspace_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        sdlc_step_id=sdlc_step_id,
        agent_id=agent_id,
        status="PENDING",
        attempt=attempt,
        idempotency_key=key,
        resume_from_dispatch_id=source_dispatch_id,
        resume_mode=mode,
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
    )
    if delivery_source is not None:
        row.delivery_source_dispatch_id = effective_delivery_source(delivery_source)
    return await _insert(session, workspace_id, key, row)


def effective_delivery_source(source: Dispatch) -> int:
    """交互行继承前序正式交付；成功的正式行用自己的 id。"""
    if is_interaction(source) and source.delivery_source_dispatch_id is not None:
        return source.delivery_source_dispatch_id
    if source.status == "SUCCEEDED":
        return source.id
    if source.delivery_source_dispatch_id is not None:
        return source.delivery_source_dispatch_id
    return source.id


async def has_resumable_session(session: AsyncSession, tenant_id: int, dispatch_id: int) -> bool:
    """沿恢复来源查找仍带着提供者会话的检查点。"""
    current = dispatch_id
    visited: list[int] = []
    while len(visited) < 16 and current not in visited:
        visited.append(current)
        if await _provider_session(session, tenant_id, current):
            return True
        dispatch = await session.scalar(
            select(Dispatch).where(Dispatch.id == current, Dispatch.is_deleted == 0).limit(1)
        )
        if dispatch is None or dispatch.tenant_id != tenant_id:
            return False
        if dispatch.resume_from_dispatch_id is None:
            return False
        current = dispatch.resume_from_dispatch_id
    return False


async def list_workitem_dispatches(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> list[Dispatch]:
    """该工单下未删除的 WORKITEM 调度。"""
    result = await session.execute(
        select(Dispatch).where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id == workitem_id,
            Dispatch.is_deleted == 0,
        )
    )
    return list(result.scalars().all())


async def _provider_session(session: AsyncSession, tenant_id: int, dispatch_id: int) -> bool:
    result = await session.execute(
        select(DispatchRecoveryCheckpoint).where(
            DispatchRecoveryCheckpoint.tenant_id == tenant_id,
            DispatchRecoveryCheckpoint.dispatch_id == dispatch_id,
        )
    )
    for row in result.scalars().all():
        if row.provider_session_id is not None and not java_is_blank(row.provider_session_id):
            return True
    return False


async def _latest_formal(
    session: AsyncSession, workspace_id: int, workitem_id: int
) -> Dispatch | None:
    rows = await list_workitem_dispatches(session, workspace_id, workitem_id)
    chosen: Dispatch | None = None
    for row in rows:
        if is_interaction(row) or row.status != "SUCCEEDED" or row.id is None:
            continue
        if chosen is None or row.id > chosen.id:
            chosen = row
    return chosen


async def _next_attempt(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    sdlc_step_id: int | None,
) -> int:
    """步骤为空时 ``= NULL`` 匹配不到行，下一次尝试从 1 开始。"""
    if sdlc_step_id is None:
        return 1
    result = await session.execute(
        select(Dispatch).where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.workitem_id == workitem_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.sdlc_step_id == sdlc_step_id,
            Dispatch.is_deleted == 0,
        )
    )
    highest = 0
    found = False
    for row in result.scalars().all():
        found = True
        if row.attempt > highest:
            highest = row.attempt
    if not found:
        return 1
    return highest + 1


async def _require_workitem_dispatch(
    session: AsyncSession, workspace_id: int, workitem_id: int, dispatch_id: int
) -> Dispatch:
    row = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    if row is None or row.tenant_id != workspace_id or row.workitem_id != workitem_id:
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    if row.source_type != "WORKITEM":
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    return row


async def _by_key(session: AsyncSession, tenant_id: int, key: str) -> Dispatch | None:
    return await session.scalar(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.idempotency_key == key,
            Dispatch.is_deleted == 0,
        )
        .limit(1)
    )


async def _insert(session: AsyncSession, tenant_id: int, key: str, row: Dispatch) -> Dispatch:
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        winner = await _by_key(session, tenant_id, key)
        if winner is None:
            raise
        return winner
    return row


def handoff_idempotency_key(source_dispatch_id: int) -> str:
    """交接幂等键。同一来源调度只产生一条下游。"""
    return "handoff:" + str(source_dispatch_id)


async def find_handoff_by_source(
    session: AsyncSession, workspace_id: int, source_dispatch_id: int
) -> Dispatch | None:
    """按来源调度查找已经写下的交接。"""
    return await _by_key(session, workspace_id, handoff_idempotency_key(source_dispatch_id))


async def enqueue_handoff(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    sdlc_step_id: int,
    agent_id: int,
    source_dispatch_id: int,
    user_id: int,
) -> Dispatch:
    """为一次 Worker 交接插入 PENDING。幂等键是 ``handoff:{来源}``。"""
    await _require_workitem_dispatch(session, workspace_id, workitem_id, source_dispatch_id)
    key = handoff_idempotency_key(source_dispatch_id)
    existing = await _by_key(session, workspace_id, key)
    if existing is not None:
        logger.info("handoff enqueue idempotent hit key=%s", key)
        return existing
    attempt = await _next_attempt(session, workspace_id, workitem_id, sdlc_step_id)
    prior = await _latest_resumable_worker(session, workspace_id, workitem_id, agent_id)
    row = Dispatch(
        tenant_id=workspace_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        sdlc_step_id=sdlc_step_id,
        agent_id=agent_id,
        status="PENDING",
        attempt=attempt,
        idempotency_key=key,
        delivery_source_dispatch_id=source_dispatch_id,
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
    )
    if prior is not None:
        row.resume_from_dispatch_id = prior.id
        row.resume_mode = "RETURNING_WORKER"
    winner = await _insert(session, workspace_id, key, row)
    if winner is row:
        logger.info(
            "handoff dispatch enqueued dispatchId=%s sourceDispatchId=%s attempt=%s",
            row.id,
            source_dispatch_id,
            attempt,
        )
    return winner


async def enqueue_interaction_rework(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    agent_id: int,
    sdlc_step_id: int,
    resume_from_dispatch_id: int | None,
    source_interaction_dispatch_id: int,
    wait_for_dispatch_id: int | None,
    user_id: int,
) -> Dispatch:
    """评论触发的正式返工先停在 WAITING_FOR_PAUSE，等主调度暂停后再放行。"""
    resume_source = None
    if resume_from_dispatch_id is not None:
        resume_source = await _require_workitem_dispatch(
            session, workspace_id, workitem_id, resume_from_dispatch_id
        )
    await _require_workitem_dispatch(
        session, workspace_id, workitem_id, source_interaction_dispatch_id
    )
    if wait_for_dispatch_id is not None:
        await _require_workitem_dispatch(session, workspace_id, workitem_id, wait_for_dispatch_id)
    key = "interaction-rework:" + str(source_interaction_dispatch_id)
    existing = await _by_key(session, workspace_id, key)
    if existing is not None:
        return existing
    summary = None
    if wait_for_dispatch_id is not None:
        summary = "waitForDispatchId=" + str(wait_for_dispatch_id)
    row = Dispatch(
        tenant_id=workspace_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        sdlc_step_id=sdlc_step_id,
        agent_id=agent_id,
        status="WAITING_FOR_PAUSE",
        attempt=await _next_attempt(session, workspace_id, workitem_id, sdlc_step_id),
        idempotency_key=key,
        resume_from_dispatch_id=resume_from_dispatch_id,
        resume_mode="COMMENT_REWORK",
        result_summary=summary,
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
    )
    if resume_source is not None:
        row.delivery_source_dispatch_id = effective_delivery_source(resume_source)
    return await _insert(session, workspace_id, key, row)


async def _latest_resumable_worker(
    session: AsyncSession, workspace_id: int, workitem_id: int, agent_id: int
) -> Dispatch | None:
    result = await session.scalars(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == workspace_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id == workitem_id,
            Dispatch.agent_id == agent_id,
            Dispatch.is_deleted == 0,
            or_(Dispatch.resume_mode.is_(None), Dispatch.resume_mode != "SIDE_INTERACTION"),
        )
        .order_by(Dispatch.id.desc())
        .limit(20)
    )
    for candidate in result.all():
        if await has_resumable_session(session, workspace_id, candidate.id):
            return candidate
    return None


def formal_succeeded(rows: Sequence[Dispatch]) -> Dispatch | None:
    """测试和调用方共用的正式成功调度选择。"""
    chosen: Dispatch | None = None
    for row in rows:
        if is_interaction(row) or row.status != "SUCCEEDED":
            continue
        if chosen is None or row.id > chosen.id:
            chosen = row
    return chosen
