"""启动一条已经入队的定时运行，并创建它的根派发。"""

import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.db.session import SessionLocal
from autowonder.dispatch.handoff_rules import (
    HandoffResult,
    agent_result,
    frozen_entry_step,
    rejected_result,
    scheduled_target_agent_id,
)
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.pending import run_pending
from autowonder.scheduledtasks.models import ScheduledTask, ScheduledTaskRun
from autowonder.users.models import User
from autowonder.workspaces.models import OrgMember

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = "autowonder.scheduledTaskExecutionSnapshot.v1"
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED"})
_STARTABLE = frozenset({"QUEUED", "STARTING", "WAITING_EXECUTOR"})


async def start_run(workspace_id: int, run_id: int, actor_id: int) -> None:
    """把 QUEUED、STARTING 或等待执行器的运行推进到等待执行器，并保证根派发存在。"""
    async with SessionLocal() as session:
        run = await _load(session, workspace_id, run_id)
        if run is None or run.status not in _STARTABLE:
            return
        try:
            if run.status == "WAITING_EXECUTOR":
                moved = await _transition(session, run, "WAITING_EXECUTOR", "QUEUED", actor_id)
                if not moved:
                    return
                run = await _load(session, workspace_id, run_id)
                if run is None or run.status != "QUEUED":
                    return
            if not await _owner_active(session, run):
                await _fail(session, run, actor_id, "OWNER_INACTIVE")
                await _pause_task(run.workspace_id, run.scheduled_task_id, actor_id)
                return
            snapshot = _snapshot(run)
            version_id = _frozen_version(snapshot, run.initial_agent_id)
            sdlc = snapshot.get("sdlc")
            sdlc_id = run.sdlc_id
            step_id = run.current_step_id
            if not isinstance(sdlc, dict):
                sdlc_id = None
                step_id = None
            started = await _initialize(
                session, run, sdlc_id, step_id, run.initial_agent_id, actor_id
            )
            if not started:
                return
            run = await _load(session, workspace_id, run_id)
            if run is None or run.status != "STARTING":
                raise BizError(
                    ErrorCode.SCHEDULED_TASK_INVALID_STATE,
                    "Run start state was not persisted",
                )
            dispatch = await _enqueue(session, run, step_id, actor_id)
            await _pin_version(session, dispatch, version_id)
            moved = await _transition(session, run, "STARTING", "WAITING_EXECUTOR", actor_id)
            if not moved:
                raise BizError(
                    ErrorCode.SCHEDULED_TASK_INVALID_STATE,
                    "Run waiting-executor transition was lost",
                )
            await session.commit()
            await run_pending(session, dispatch.id)
        except BizError as error:
            await session.rollback()
            if error.code == ErrorCode.SCHEDULED_TASK_INVALID_STATE.code:
                await _fail_detached(
                    workspace_id,
                    run_id,
                    actor_id,
                    error.code + ": " + str(error),
                )
                return
            raise
        except Exception as error:
            await session.rollback()
            message = ErrorCode.SCHEDULED_TASK_INVALID_STATE.code + ": " + str(error)
            await _fail_detached(workspace_id, run_id, actor_id, message)


async def _fail_detached(workspace_id: int, run_id: int, actor_id: int, error: str) -> None:
    async with SessionLocal() as session:
        run = await _load(session, workspace_id, run_id)
        if run is None or run.status in _TERMINAL:
            return
        await _fail(session, run, actor_id, error)


async def _load(session: AsyncSession, workspace_id: int, run_id: int) -> ScheduledTaskRun | None:
    run = await session.get(ScheduledTaskRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        return None
    return run


async def _owner_active(session: AsyncSession, run: ScheduledTaskRun) -> bool:
    user = await session.get(User, run.owner_id)
    if user is None or user.status != 0:
        return False
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == run.workspace_id,
            OrgMember.user_id == run.owner_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    return member is not None and member.status == 0


def _snapshot(run: ScheduledTaskRun) -> dict[str, object]:
    raw = run.execution_snapshot_json
    if not isinstance(raw, dict) or raw.get("schemaVersion") != SNAPSHOT_SCHEMA:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "execution snapshot schema is invalid",
        )
    task = raw.get("task")
    assignment = raw.get("assignment")
    policies = raw.get("policies")
    name = task.get("name") if isinstance(task, dict) else None
    instruction = task.get("instructionMd") if isinstance(task, dict) else None
    session_mode = policies.get("sessionMode") if isinstance(policies, dict) else None
    if (
        not isinstance(task, dict)
        or task.get("id") != run.scheduled_task_id
        or not isinstance(name, str)
        or name.strip() == ""
        or not isinstance(instruction, str)
        or instruction.strip() == ""
        or not isinstance(assignment, dict)
        or assignment.get("squadId") != run.squad_id
        or assignment.get("initialAgentId") != run.initial_agent_id
        or not isinstance(policies, dict)
        or not isinstance(session_mode, str)
        or session_mode.strip() == ""
        or not isinstance(raw.get("requirementDocuments"), list)
        or not isinstance(raw.get("agentContexts"), list)
    ):
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "execution snapshot is incomplete")
    return raw


def _frozen_version(snapshot: dict[str, object], agent_id: int) -> int:
    if agent_id <= 0:
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "initial agent is invalid")
    contexts = snapshot.get("agentContexts")
    matched: dict[str, object] | None = None
    if isinstance(contexts, list):
        for item in contexts:
            if isinstance(item, dict) and item.get("agentId") == agent_id:
                if matched is not None:
                    raise BizError(
                        ErrorCode.SCHEDULED_TASK_INVALID_STATE,
                        "agent context is duplicated",
                    )
                matched = item
    version_id = None if matched is None else matched.get("agentVersionId")
    if not isinstance(version_id, int) or isinstance(version_id, bool) or version_id <= 0:
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "frozen agent context is missing")
    return version_id


async def resume_paused(
    session: AsyncSession, workspace_id: int, run_id: int, user_id: int
) -> bool:
    """暂停派发存在时，用它的冻结版本开一条连续续跑。没有暂停派发时返回 False。"""
    run = await _load(session, workspace_id, run_id)
    if run is None or run.status != "QUEUED" or run.version is None:
        return False
    paused = await _latest_paused(session, workspace_id, run_id)
    if paused is None:
        return False
    snapshot = _snapshot(run)
    version_id = _frozen_version(snapshot, paused.agent_id)
    started = await _initialize(
        session,
        run,
        run.sdlc_id,
        paused.sdlc_step_id,
        paused.agent_id,
        user_id,
    )
    if not started:
        return False
    run = await _load(session, workspace_id, run_id)
    if run is None or run.status != "STARTING":
        return False
    from autowonder.scheduledtasks.notify import publish_status

    await publish_status(session, workspace_id, run_id)
    continuation = await _enqueue_scheduled_resume(session, run, paused, user_id)
    await _pin_handoff_version(session, continuation, version_id)
    moved = await _transition(session, run, "STARTING", "WAITING_EXECUTOR", user_id)
    await session.commit()
    if not moved:
        return False
    await run_pending(session, continuation.id)
    return True


async def _latest_paused(session: AsyncSession, workspace_id: int, run_id: int) -> Dispatch | None:
    rows = await session.scalars(
        select(Dispatch).where(
            Dispatch.tenant_id == workspace_id,
            Dispatch.source_type == "SCHEDULED_TASK_RUN",
            Dispatch.workitem_id == run_id,
            Dispatch.is_deleted == 0,
        )
    )
    paused: Dispatch | None = None
    for row in rows.all():
        if row.status == "PAUSED" and (paused is None or row.id > paused.id):
            paused = row
    return paused


async def _enqueue_scheduled_resume(
    session: AsyncSession,
    run: ScheduledTaskRun,
    source: Dispatch,
    user_id: int,
) -> Dispatch:
    key = "scheduled-resume:" + str(run.id) + ":" + str(source.id) + ":native"
    existing = await session.scalar(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == run.workspace_id,
            Dispatch.idempotency_key == key,
            Dispatch.is_deleted == 0,
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    dispatch = Dispatch(
        tenant_id=run.workspace_id,
        source_type="SCHEDULED_TASK_RUN",
        workitem_id=run.id,
        sdlc_step_id=source.sdlc_step_id,
        agent_id=source.agent_id,
        status="PENDING",
        attempt=source.attempt + 1,
        idempotency_key=key,
        resume_from_dispatch_id=source.id,
        resume_mode="CONTINUOUS",
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
        is_deleted=0,
        debug_log_enabled=0,
    )
    try:
        async with session.begin_nested():
            session.add(dispatch)
            await session.flush()
    except IntegrityError:
        winner = await session.scalar(
            select(Dispatch)
            .where(
                Dispatch.tenant_id == run.workspace_id,
                Dispatch.idempotency_key == key,
            )
            .limit(1)
        )
        if winner is None:
            raise
        return winner
    return dispatch


async def _initialize(
    session: AsyncSession,
    run: ScheduledTaskRun,
    sdlc_id: int | None,
    step_id: int | None,
    agent_id: int,
    actor_id: int,
) -> bool:
    started_at = run.started_at if run.started_at is not None else now_local()
    result = await session.execute(
        update(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == run.workspace_id,
            ScheduledTaskRun.id == run.id,
            ScheduledTaskRun.status == run.status,
            ScheduledTaskRun.status.in_(("QUEUED", "STARTING")),
            ScheduledTaskRun.version == run.version,
        )
        .values(
            status="STARTING",
            started_at=started_at,
            sdlc_id=sdlc_id,
            current_agent_id=agent_id,
            current_step_id=step_id,
            modifier_id=actor_id,
            version=ScheduledTaskRun.version + 1,
        )
    )
    return rowcount(result) == 1


async def _transition(
    session: AsyncSession,
    run: ScheduledTaskRun,
    expected: str,
    target: str,
    actor_id: int,
) -> bool:
    result = await session.execute(
        update(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == run.workspace_id,
            ScheduledTaskRun.id == run.id,
            ScheduledTaskRun.status == expected,
            ScheduledTaskRun.version == run.version,
        )
        .values(
            status=target,
            modifier_id=actor_id,
            version=ScheduledTaskRun.version + 1,
        )
    )
    if rowcount(result) != 1:
        return False
    run.status = target
    run.version = run.version + 1
    return True


async def _enqueue(
    session: AsyncSession,
    run: ScheduledTaskRun,
    step_id: int | None,
    actor_id: int,
) -> Dispatch:
    step_token = "root" if step_id is None else str(step_id)
    idempotency_key = "SCHEDULED_TASK_RUN:" + str(run.id) + ":" + step_token + ":1"
    existing = await session.scalar(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == run.workspace_id,
            Dispatch.idempotency_key == idempotency_key,
            Dispatch.is_deleted == 0,
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    dispatch = Dispatch(
        tenant_id=run.workspace_id,
        source_type="SCHEDULED_TASK_RUN",
        workitem_id=run.id,
        sdlc_step_id=step_id,
        agent_id=run.initial_agent_id,
        status="PENDING",
        attempt=1,
        idempotency_key=idempotency_key,
        creator_id=actor_id,
        modifier_id=actor_id,
        version=0,
        is_deleted=0,
        debug_log_enabled=0,
    )
    try:
        async with session.begin_nested():
            session.add(dispatch)
            await session.flush()
    except IntegrityError:
        winner = await session.scalar(
            select(Dispatch)
            .where(
                Dispatch.tenant_id == run.workspace_id,
                Dispatch.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        if winner is None:
            raise
        return winner
    return dispatch


async def _pin_version(session: AsyncSession, dispatch: Dispatch, agent_version_id: int) -> None:
    if dispatch.agent_version_id == agent_version_id:
        return
    if dispatch.agent_version_id is not None:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "scheduled dispatch cannot be pinned to its frozen agent version",
        )
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch.id,
            Dispatch.tenant_id == dispatch.tenant_id,
            Dispatch.source_type == "SCHEDULED_TASK_RUN",
            Dispatch.agent_id == dispatch.agent_id,
            Dispatch.agent_version_id.is_(None),
            Dispatch.is_deleted == 0,
        )
        .values(agent_version_id=agent_version_id, modifier_id=0, version=Dispatch.version + 1)
    )
    if rowcount(result) != 1:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "scheduled dispatch cannot be pinned to its frozen agent version",
        )
    dispatch.agent_version_id = agent_version_id


async def _pause_task(workspace_id: int, task_id: int, actor_id: int) -> None:
    """所有者不可用时把任务从当前状态改成 PAUSED。版本对不上就留给下一次。"""
    async with SessionLocal() as session:
        task = await session.get(ScheduledTask, task_id)
        if task is None or task.workspace_id != workspace_id or task.version is None:
            return
        applied = rowcount(
            await session.execute(
                update(ScheduledTask)
                .where(
                    ScheduledTask.workspace_id == workspace_id,
                    ScheduledTask.id == task_id,
                    ScheduledTask.status == task.status,
                    ScheduledTask.version == task.version,
                    ScheduledTask.is_deleted == 0,
                )
                .values(
                    status="PAUSED",
                    modifier_id=actor_id,
                    version=ScheduledTask.version + 1,
                )
            )
        )
        await session.commit()
        if applied == 1:
            from autowonder.scheduledtasks.notify import announce_task_paused

            await announce_task_paused(session, workspace_id, task_id)


async def _fail(session: AsyncSession, run: ScheduledTaskRun, actor_id: int, error: str) -> None:
    current = await _load(session, run.workspace_id, run.id)
    if current is None or current.status in _TERMINAL or current.version is None:
        return
    applied = rowcount(
        await session.execute(
            update(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.workspace_id == current.workspace_id,
                ScheduledTaskRun.id == current.id,
                ScheduledTaskRun.status == current.status,
                ScheduledTaskRun.version == current.version,
                ScheduledTaskRun.status.not_in(_TERMINAL),
            )
            .values(
                status="FAILED",
                error=error[:1024],
                finished_at=now_local(),
                modifier_id=actor_id,
                version=ScheduledTaskRun.version + 1,
            )
        )
    )
    await session.commit()
    logger.info(
        "scheduled run failed workspaceId=%s runId=%s error=%s",
        run.workspace_id,
        run.id,
        error,
    )
    if applied == 1:
        from autowonder.scheduledtasks.notify import announce_run

        await announce_run(session, current.workspace_id, current.id, "FAILED", actor_id, error)


async def handoff_scheduled(
    session: AsyncSession, source: Dispatch, target: str | None
) -> HandoffResult:
    """在同一次运行的冻结小队里交接。目标不在快照里就拒绝。"""
    if source.source_type != "SCHEDULED_TASK_RUN" or source.workitem_id is None:
        return rejected_result("DISPATCH_NOT_FOUND", "source dispatch is not a scheduled run")
    if target is None or target.strip() == "":
        return rejected_result("TARGET_UNRESOLVED", "scheduled handoff target is required")
    run = await _load(session, source.tenant_id, source.workitem_id)
    if run is None:
        return rejected_result("RUN_NOT_FOUND", "scheduled run not found")
    replay = await _handoff_replay(session, source)
    if replay is not None:
        await _drive_if_pending(session, replay)
        return agent_result(replay.agent_id, replay.id)
    try:
        snapshot = _snapshot(run)
        _frozen_version(snapshot, run.initial_agent_id)
    except BizError as invalid:
        return rejected_result("SCHEDULED_TASK_INVALID_STATE", str(invalid))
    agent_id = scheduled_target_agent_id(snapshot, target)
    if agent_id is None:
        return rejected_result("TARGET_UNRESOLVED", "target is not in the frozen scheduled squad")
    try:
        return await _handoff_frozen_agent(session, source, run, snapshot, agent_id)
    except BizError as invalid:
        await _fail(session, run, 0, invalid.code + ": " + str(invalid))
        return rejected_result("SCHEDULED_TASK_INVALID_STATE", str(invalid))


async def _handoff_frozen_agent(
    session: AsyncSession,
    source: Dispatch,
    run: ScheduledTaskRun,
    snapshot: dict[str, object],
    agent_id: int,
) -> HandoffResult:
    replay = await _handoff_replay(session, source)
    if replay is not None:
        await _drive_if_pending(session, replay)
        return agent_result(replay.agent_id, replay.id)
    if (
        source.status != "SUCCEEDED"
        or run.current_agent_id != source.agent_id
        or run.current_step_id != source.sdlc_step_id
    ):
        return rejected_result(
            "SOURCE_NOT_CURRENT",
            "scheduled handoff source is not the completed current assignment",
        )
    version_id = _frozen_version(snapshot, agent_id)
    sdlc_id, step_id = frozen_entry_step(snapshot, run.initial_agent_id, agent_id)
    if run.sdlc_id is not None and step_id is None:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "frozen target agent SDLC is missing",
        )
    moved = await session.execute(
        update(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == run.workspace_id,
            ScheduledTaskRun.id == run.id,
            ScheduledTaskRun.version == run.version,
            ScheduledTaskRun.status.not_in(_TERMINAL),
        )
        .values(
            sdlc_id=sdlc_id,
            current_agent_id=agent_id,
            current_step_id=step_id,
            modifier_id=0,
            version=ScheduledTaskRun.version + 1,
        )
    )
    if rowcount(moved) != 1:
        return rejected_result("RUN_VERSION_CONFLICT", "scheduled run changed during handoff")
    downstream = await _enqueue_scheduled_handoff(session, run, source, agent_id, step_id)
    await _pin_handoff_version(session, downstream, version_id)
    await session.commit()
    await run_pending(session, downstream.id)
    return agent_result(agent_id, downstream.id)


async def _drive_if_pending(session: AsyncSession, dispatch: Dispatch) -> None:
    if dispatch.status == "PENDING":
        await run_pending(session, dispatch.id)


async def _handoff_replay(session: AsyncSession, source: Dispatch) -> Dispatch | None:
    return await session.scalar(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == source.tenant_id,
            Dispatch.idempotency_key == "handoff:" + str(source.id),
            Dispatch.is_deleted == 0,
        )
        .limit(1)
    )


async def _enqueue_scheduled_handoff(
    session: AsyncSession,
    run: ScheduledTaskRun,
    source: Dispatch,
    agent_id: int,
    step_id: int | None,
) -> Dispatch:
    key = "handoff:" + str(source.id)
    existing = await _handoff_replay(session, source)
    if existing is not None:
        return existing
    dispatch = Dispatch(
        tenant_id=run.workspace_id,
        source_type="SCHEDULED_TASK_RUN",
        workitem_id=run.id,
        sdlc_step_id=step_id,
        agent_id=agent_id,
        status="PENDING",
        attempt=source.attempt + 1,
        idempotency_key=key,
        delivery_source_dispatch_id=source.id,
        creator_id=0,
        modifier_id=0,
        version=0,
        is_deleted=0,
        debug_log_enabled=0,
    )
    try:
        async with session.begin_nested():
            session.add(dispatch)
            await session.flush()
    except IntegrityError:
        winner = await _handoff_replay(session, source)
        if winner is None:
            raise
        return winner
    return dispatch


async def _pin_handoff_version(
    session: AsyncSession, dispatch: Dispatch, agent_version_id: int
) -> None:
    if (
        dispatch.source_type != "SCHEDULED_TASK_RUN"
        or dispatch.agent_id is None
        or agent_version_id <= 0
    ):
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "scheduled dispatch cannot be pinned to its frozen agent version",
        )
    if dispatch.agent_version_id == agent_version_id:
        return
    if dispatch.agent_version_id is not None:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "scheduled dispatch version pin was lost: dispatchId=" + str(dispatch.id),
        )
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch.id,
            Dispatch.tenant_id == dispatch.tenant_id,
            Dispatch.source_type == "SCHEDULED_TASK_RUN",
            Dispatch.agent_id == dispatch.agent_id,
            Dispatch.status == "PENDING",
            Dispatch.agent_version_id.is_(None),
            Dispatch.is_deleted == 0,
        )
        .values(agent_version_id=agent_version_id, modifier_id=0)
    )
    if rowcount(result) == 1:
        dispatch.agent_version_id = agent_version_id
        return
    session.expire(dispatch)
    current = await session.get(Dispatch, dispatch.id)
    if (
        current is not None
        and current.tenant_id == dispatch.tenant_id
        and current.source_type == "SCHEDULED_TASK_RUN"
        and current.agent_version_id == agent_version_id
    ):
        return
    raise BizError(
        ErrorCode.SCHEDULED_TASK_INVALID_STATE,
        "scheduled dispatch version pin was lost: dispatchId=" + str(dispatch.id),
    )
