"""定时任务运行的状态变更、评论、参与者和交付进度。"""

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.artifacts.models import Artifact
from autowonder.artifacts.schemas import ArtifactView
from autowonder.audits.service import AuditRecord, record_required
from autowonder.core.clock import now_local
from autowonder.core.context import current, current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.rows import rowcount
from autowonder.db.session import get_session
from autowonder.dispatch.models import Dispatch, DispatchRuntimeEvent
from autowonder.dispatch.pause_request import PAUSEABLE
from autowonder.dispatch.recovery import TERMINAL as DISPATCH_TERMINAL
from autowonder.dispatch.recovery import (
    execution_source,
    force_cancel_scheduled_run,
    update_status,
)
from autowonder.executors.models import Executor
from autowonder.executors.registry import is_online
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.scheduledtasks.comments import (
    add_human_comment,
    list_run_comments,
    publish_run_mentions,
)
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.scheduledtasks.schemas import (
    ScheduledRunMentionCandidateView,
    ScheduledTaskRunDetailView,
)
from autowonder.scheduledtasks.service import _run_view
from autowonder.users.models import User
from autowonder.workitems.models import Workitem
from autowonder.workitems.schemas import (
    AddCommentRequest,
    AgentDeliveryProgressView,
    DeliveryProgressView,
    DeliveryStepView,
    DispatchAttemptView,
    ParticipantView,
    ProcessGraphEdgeView,
    ProcessGraphNodeView,
    ProcessGraphView,
)
from autowonder.workitems.service import get_workitem
from autowonder.workspaces.models import OrgMember

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED"})
_NON_FROZEN = "不在本次运行的冻结快照中，无法 @ 触发执行"
_RUN_TARGETS = frozenset(
    {"QUEUED", "STARTING", "WAITING_EXECUTOR", "RUNNING", "WAITING_HUMAN", "PAUSED"}
)
_FINISH_TARGETS = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED"})

router = APIRouter(
    prefix="/api/scheduled-task-runs",
    tags=["scheduled-task-runs"],
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看定时任务运行记录")),
        Depends(require_scheduled_capability),
    ],
)


def can_transition(source: str | None, target: str) -> bool:
    """人机操作允许的运行状态迁移。"""
    if target == "PAUSED":
        return source in {"QUEUED", "WAITING_EXECUTOR", "RUNNING", "WAITING_HUMAN"}
    if target == "QUEUED":
        return source == "PAUSED"
    if target == "CANCELED":
        return not _terminal(source)
    return False


def determine_step_status(
    step_id: int | None,
    events: list[DispatchRuntimeEvent],
    run_status: str | None,
) -> str:
    """按运行时事件和运行状态决定步骤展示态。"""
    has_started = False
    has_completed = False
    has_failed = False
    for event in events:
        if not _step_matches(step_id, event):
            continue
        event_type = event.event_type or ""
        if "step.completed" in event_type:
            has_completed = True
        elif "step.started" in event_type:
            has_started = True
        elif "step.failed" in event_type:
            has_failed = True
    if has_completed:
        return "done"
    if has_failed:
        return "failed"
    if run_status == "CANCELED":
        return "cancelled"
    if has_started:
        if run_status == "FAILED":
            return "failed"
        if run_status == "SUCCEEDED":
            return "done"
        if run_status == "PAUSED":
            return "paused"
        return "active"
    return "pending"


async def transition_run(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    version: int | None,
    target: str,
    user_id: int,
) -> ScheduledTaskRun:
    """按界面带来的版本迁移运行状态。取消走终态更新。"""
    if workspace_id <= 0 or run_id <= 0 or user_id <= 0 or version is None:
        raise BizError(ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED)
    run = await _require_run(session, workspace_id, run_id)
    if version != run.version or not can_transition(run.status, target):
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE)
    if target == "CANCELED":
        if not await _finish(session, run, target, None, "CANCELED", user_id):
            raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
        await _audit(session, run, user_id, target)
        return run
    changed = await session.execute(
        update(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == workspace_id,
            ScheduledTaskRun.id == run_id,
            ScheduledTaskRun.status == run.status,
            ScheduledTaskRun.status.not_in(_TERMINAL),
            ScheduledTaskRun.version == version,
        )
        .values(
            status=target,
            modifier_id=user_id,
            version=ScheduledTaskRun.version + 1,
            gmt_modified=now_local(),
        )
    )
    if target not in _RUN_TARGETS or rowcount(changed) != 1:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    run.status = target
    run.version = version + 1
    run.modifier_id = user_id
    if _terminal(target):
        run.finished_at = now_local()
    await _audit(session, run, user_id, target)
    from autowonder.scheduledtasks.notify import announce_run, notify_owner, publish_status

    if notify_owner(target, user_id, run.owner_id):
        await session.commit()
        await announce_run(session, workspace_id, run_id, target, user_id, None)
    elif target == "PAUSED":
        try:
            await publish_status(session, workspace_id, run_id)
        except Exception:
            logger.exception(
                "scheduled status frame failed workspaceId=%s runId=%s",
                workspace_id,
                run_id,
            )
    return run


async def mark_cancel_intent(session: AsyncSession, run: ScheduledTaskRun, user_id: int) -> bool:
    """非终态运行记下取消意图，并推进版本。"""
    if run.version is None or _terminal(run.status):
        return False
    changed = await session.execute(
        update(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == run.workspace_id,
            ScheduledTaskRun.id == run.id,
            ScheduledTaskRun.status.not_in(_TERMINAL),
            ScheduledTaskRun.version == run.version,
        )
        .values(error="CANCEL_PENDING", modifier_id=user_id, version=ScheduledTaskRun.version + 1)
    )
    if rowcount(changed) != 1:
        return False
    run.error = "CANCEL_PENDING"
    run.version = run.version + 1
    return True


async def complete_cancel_if_quiescent(
    session: AsyncSession,
    tenant_id: int,
    source_type: str | None,
    run_id: int | None,
) -> None:
    """取消意图已写下，且这次运行的派发都已结束或暂停时，把运行收成 CANCELED。"""
    if source_type != "SCHEDULED_TASK_RUN" or run_id is None:
        return
    run = await session.scalar(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.workspace_id == tenant_id, ScheduledTaskRun.id == run_id)
        .limit(1)
    )
    if run is None or run.error != "CANCEL_PENDING":
        return
    for dispatch in await _dispatches(session, tenant_id, run_id):
        if dispatch.status not in DISPATCH_TERMINAL and dispatch.status != "PAUSED":
            return
    await _finish(session, run, "CANCELED", None, "CANCELED", 0)
    await session.commit()


async def pause_active(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    user_id: int,
    cancel_pending: bool,
) -> bool:
    """暂停仍在执行的派发。待派发且带取消意图时直接取消。"""
    awaiting = False
    for dispatch in await _dispatches(session, workspace_id, run_id):
        if dispatch.status in DISPATCH_TERMINAL:
            continue
        if dispatch.status == "PENDING" and cancel_pending:
            await update_status(
                session,
                dispatch.id,
                workspace_id,
                "CANCELED",
                None,
                None,
                None,
                None,
                "scheduled run canceled",
                dispatch.version,
                user_id,
            )
        elif dispatch.status in PAUSEABLE or dispatch.status == "PAUSE_FAILED":
            await update_status(
                session,
                dispatch.id,
                workspace_id,
                "PAUSING",
                None,
                None,
                None,
                None,
                "" if dispatch.status == "PAUSE_FAILED" else None,
                dispatch.version,
                user_id,
            )
            awaiting = True
        elif dispatch.status == "PAUSING":
            awaiting = True
    return awaiting


async def _finish(
    session: AsyncSession,
    run: ScheduledTaskRun,
    target: str,
    summary: str | None,
    error: str | None,
    user_id: int,
) -> bool:
    if _terminal(run.status) or target not in _FINISH_TARGETS:
        return False
    changed = await session.execute(
        update(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == run.workspace_id,
            ScheduledTaskRun.id == run.id,
            ScheduledTaskRun.status == run.status,
            ScheduledTaskRun.status.not_in(_TERMINAL),
            ScheduledTaskRun.version == run.version,
        )
        .values(
            status=target,
            result_summary=summary,
            error=error,
            finished_at=now_local(),
            modifier_id=user_id,
            version=ScheduledTaskRun.version + 1,
            gmt_modified=now_local(),
        )
    )
    if rowcount(changed) != 1:
        return False
    run.status = target
    run.result_summary = summary
    run.error = error
    run.finished_at = now_local()
    run.version = (run.version or 0) + 1
    return True


async def complete_from_dispatch(
    session: AsyncSession,
    dispatch: Dispatch,
    success: bool,
    summary: str | None,
    error: str | None,
) -> None:
    """调度终态回写定时运行。非运行来源、缺失或已终态的运行保持原状。"""
    if execution_source(dispatch) != "SCHEDULED_TASK_RUN":
        return
    run = await session.scalar(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == dispatch.tenant_id,
            ScheduledTaskRun.id == dispatch.workitem_id,
        )
        .limit(1)
    )
    if run is None or _terminal(run.status):
        return
    target = "SUCCEEDED" if success else "FAILED"
    finished = await _finish(session, run, target, summary, error, 0)
    await session.commit()
    if finished and target in {"FAILED", "TIMED_OUT"}:
        from autowonder.scheduledtasks.notify import announce_run

        await announce_run(session, dispatch.tenant_id, run.id, target, 0, error)


async def _require_run(session: AsyncSession, workspace_id: int, run_id: int) -> ScheduledTaskRun:
    run = await session.scalar(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.workspace_id == workspace_id, ScheduledTaskRun.id == run_id)
        .limit(1)
    )
    if run is None or run.workspace_id != workspace_id:
        raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
    return run


async def _dispatches(session: AsyncSession, workspace_id: int, run_id: int) -> list[Dispatch]:
    rows = await session.scalars(
        select(Dispatch).where(
            Dispatch.tenant_id == workspace_id,
            Dispatch.source_type == "SCHEDULED_TASK_RUN",
            Dispatch.workitem_id == run_id,
            Dispatch.is_deleted == 0,
        )
    )
    return list(rows.all())


async def _events(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
) -> list[DispatchRuntimeEvent]:
    values: list[DispatchRuntimeEvent] = []
    for dispatch in await _dispatches(session, workspace_id, run_id):
        rows = await session.scalars(
            select(DispatchRuntimeEvent).where(
                DispatchRuntimeEvent.tenant_id == workspace_id,
                DispatchRuntimeEvent.dispatch_id == dispatch.id,
            )
        )
        values.extend(rows.all())
    return values


def _terminal(status: str | None) -> bool:
    return status in _TERMINAL


def _step_matches(step_id: int | None, event: DispatchRuntimeEvent) -> bool:
    if event.step_id is not None and event.step_id == step_id:
        return True
    detail = event.detail_json
    if isinstance(detail, dict):
        raw = detail.get("stepId")
        return isinstance(raw, int) and not isinstance(raw, bool) and raw == step_id
    return False


async def _audit(session: AsyncSession, run: ScheduledTaskRun, user_id: int, target: str) -> None:
    await record_required(
        session,
        AuditRecord(
            tenant_id=run.workspace_id,
            actor_id=user_id,
            actor_type="HUMAN",
            module="SCHEDULED_TASK",
            action="RUN_" + target,
            target_type="SCHEDULED_TASK_RUN",
            target_id=run.id,
            trigger_type="EVENT",
            trigger_source="WEB",
        )
        .add("scheduledTaskId", run.scheduled_task_id)
        .add("status", target)
        .add("version", run.version),
    )


def _detail(run: ScheduledTaskRun, executor_id: int | None) -> ScheduledTaskRunDetailView:
    base = _run_view(run)
    return ScheduledTaskRunDetailView(**base.model_dump(), executor_id=executor_id)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


def _require_owner(owner_id: int | None) -> None:
    user_id = _user_id()
    if user_id != owner_id and current().access_level != WorkspaceAccessLevel.ADMIN.name:
        raise BizError(ErrorCode.UNAUTHORIZED)


@router.get("/{runId}")
async def get_run(runId: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """运行详情。执行器取最后一条派发。"""
    workspace_id = _workspace_id()
    run = await _require_run(session, workspace_id, runId)
    dispatches = await _dispatches(session, workspace_id, runId)
    executor_id = None if len(dispatches) == 0 else dispatches[-1].executor_id
    return ok(_detail(run, executor_id))


@router.post(
    "/{runId}/pause",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "暂停定时任务运行"))],
)
async def pause_run(
    runId: int,
    version: Annotated[int, Query()],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """暂停运行，并请求执行器暂停活动派发。"""
    workspace_id = _workspace_id()
    run = await _require_run(session, workspace_id, runId)
    _require_owner(run.owner_id)
    await pause_active(session, workspace_id, runId, _user_id(), False)
    updated = await transition_run(session, workspace_id, runId, version, "PAUSED", _user_id())
    await session.commit()
    return ok(_run_view(updated))


@router.post(
    "/{runId}/resume",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "恢复定时任务运行"))],
)
async def resume_run(
    runId: int,
    version: Annotated[int, Query()],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把暂停的运行改回排队，再按冻结版本续跑。没有暂停派发时重新启动。"""
    workspace_id = _workspace_id()
    user_id = _user_id()
    existing = await _require_run(session, workspace_id, runId)
    _require_owner(existing.owner_id)
    updated = await transition_run(session, workspace_id, runId, version, "QUEUED", user_id)
    await session.commit()
    from autowonder.scheduledtasks.orchestrator import resume_paused, start_run

    continued = await resume_paused(session, workspace_id, runId, user_id)
    if not continued:
        await start_run(workspace_id, runId, user_id)
        session.expire_all()
    current_run = await _require_run(session, workspace_id, runId)
    return ok(_run_view(current_run if current_run is not None else updated))


@router.post(
    "/{runId}/cancel",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "取消定时任务运行"))],
)
async def cancel_run(
    runId: int,
    version: Annotated[int, Query()],
    force: Annotated[bool, Query()] = False,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """取消运行。强制取消不等执行器确认。"""
    workspace_id = _workspace_id()
    user_id = _user_id()
    existing = await _require_run(session, workspace_id, runId)
    _require_owner(existing.owner_id)
    if version != existing.version or not await mark_cancel_intent(session, existing, user_id):
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    if force:
        for dispatch in await _dispatches(session, workspace_id, runId):
            if dispatch.status not in DISPATCH_TERMINAL:
                await force_cancel_scheduled_run(
                    session, workspace_id, runId, dispatch.id, user_id
                )
        canceled = await transition_run(
            session, workspace_id, runId, existing.version, "CANCELED", user_id
        )
        await session.commit()
        return ok(_run_view(canceled))
    awaiting = await pause_active(session, workspace_id, runId, user_id, True)
    current_run = await _require_run(session, workspace_id, runId)
    if current_run.status == "CANCELED":
        await session.commit()
        return ok(_run_view(current_run))
    target = "PAUSED" if awaiting else "CANCELED"
    updated = await transition_run(session, workspace_id, runId, existing.version, target, user_id)
    await session.commit()
    return ok(_run_view(updated))


@router.get("/{runId}/comments")
async def run_comments(runId: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出运行评论。"""
    return ok(await list_run_comments(session, _workspace_id(), runId))


@router.post(
    "/{runId}/comments",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "评论定时任务运行"))],
)
async def add_run_comment(
    runId: int,
    body: AddCommentRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """写真人评论，并通知被 @ 的人。缺少正文时是参数不合法。"""
    agents: list[int] = []
    if body.target_agent_ids is not None:
        agents = [item for item in body.target_agent_ids if isinstance(item, int)]
    humans: list[int] = []
    if body.target_human_ids is not None:
        humans = [item for item in body.target_human_ids if isinstance(item, int)]
    view, notices = await add_human_comment(
        session,
        _workspace_id(),
        runId,
        _user_id(),
        body.content_md,
        agents,
        humans,
    )
    await session.commit()
    await publish_run_mentions(session, notices)
    return ok(view)


@router.get("/{runId}/mention-candidates")
async def mention_candidates(
    runId: int,
    q: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 50,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """@ 候选。不可提及的数字人也返回。"""
    workspace_id = _workspace_id()
    run = await _require_run(session, workspace_id, runId)
    return ok(await _mention_candidates(session, workspace_id, run, q, limit))


@router.get("/{runId}/artifacts")
async def run_artifacts(
    runId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """运行产物。"""
    workspace_id = _workspace_id()
    await _require_run(session, workspace_id, runId)
    rows = await session.scalars(
        select(Artifact).where(
            Artifact.tenant_id == workspace_id,
            Artifact.source_type == "SCHEDULED_TASK_RUN",
            Artifact.workitem_id == runId,
        )
    )
    return ok(
        [
            ArtifactView(
                id=row.id,
                workitem_id=row.workitem_id,
                dispatch_id=row.dispatch_id,
                name=row.name,
                type=row.type,
                size=row.size,
                gmt_create=row.gmt_create,
            )
            for row in rows.all()
        ]
    )


@router.get("/{runId}/events")
async def run_events(runId: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """汇总这次运行全部分发的运行时事件。"""
    workspace_id = _workspace_id()
    await _require_run(session, workspace_id, runId)
    return ok([_event_view(event) for event in await _events(session, workspace_id, runId)])


@router.get("/{runId}/derived-workitems")
async def derived_workitems(
    runId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """由这次运行创建的工单。"""
    workspace_id = _workspace_id()
    await _require_run(session, workspace_id, runId)
    rows = await session.scalars(
        select(Workitem).where(
            Workitem.tenant_id == workspace_id,
            Workitem.origin_type == "SCHEDULED_TASK_RUN",
            Workitem.origin_id == runId,
            Workitem.is_deleted == 0,
        )
    )
    views = []
    for row in rows.all():
        views.append(await get_workitem(session, row.id, workspace_id, _user_id()))
    return ok(views)


@router.get("/{runId}/participants")
async def participants(runId: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """负责人和参与这次运行的数字人。"""
    workspace_id = _workspace_id()
    run = await _require_run(session, workspace_id, runId)
    return ok(await _participants(session, workspace_id, run))


@router.get("/{runId}/delivery-progress")
async def delivery_progress(
    runId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按冻结步骤和派发事件组装交付进度。"""
    workspace_id = _workspace_id()
    run = await _require_run(session, workspace_id, runId)
    return ok(await _delivery(session, workspace_id, run))


async def _participants(
    session: AsyncSession,
    workspace_id: int,
    run: ScheduledTaskRun,
) -> list[ParticipantView]:
    people: list[ParticipantView] = []
    if run.owner_id is not None:
        user = await session.get(User, run.owner_id)
        name = None
        display = None
        if user is not None:
            name = user.username if user.nickname is None else user.nickname
            display = user.username
        people.append(
            ParticipantView(
                user_id=run.owner_id,
                target_type="HUMAN",
                role="OWNER",
                role_name="Owner",
                agent=False,
                online=True,
                name=name,
                display_id=display,
            )
        )
    agent_ids: list[int] = []
    if run.initial_agent_id is not None:
        agent_ids.append(run.initial_agent_id)
    for dispatch in await _dispatches(session, workspace_id, run.id):
        if dispatch.agent_id is not None and dispatch.agent_id not in agent_ids:
            agent_ids.append(dispatch.agent_id)
    for agent_id in agent_ids:
        agent = await session.get(Agent, agent_id)
        if agent is None:
            continue
        executor_status = await _executor_status(session, workspace_id, agent_id)
        people.append(
            ParticipantView(
                user_id=agent_id,
                target_type="AGENT",
                role="AGENT",
                role_name="Agent",
                agent=True,
                name=agent.name,
                status=agent.status,
                online=executor_status != "OFFLINE",
                executor_status=executor_status,
            )
        )
    return people


async def _mention_candidates(
    session: AsyncSession,
    workspace_id: int,
    run: ScheduledTaskRun,
    query: str | None,
    limit: int,
) -> list[ScheduledRunMentionCandidateView]:
    effective = 50 if limit <= 0 else min(limit, 100)
    frozen = _frozen_ids(run.execution_snapshot_json)
    found: dict[str, ScheduledRunMentionCandidateView] = {}
    for participant in await _participants(session, workspace_id, run):
        is_agent = participant.agent or participant.target_type == "AGENT"
        candidate = ScheduledRunMentionCandidateView(
            user_id=participant.user_id,
            target_type="AGENT" if is_agent else "HUMAN",
            name=participant.name,
            display_id=participant.display_id,
            agent=is_agent,
            online=participant.online,
            executor_status=participant.executor_status,
            mentionable=True,
        )
        if is_agent:
            mentionable = participant.user_id in frozen
            candidate.mentionable = mentionable
            if not mentionable:
                candidate.mention_disabled_reason = _NON_FROZEN
        _add_candidate(found, candidate, query, effective)
    for agent_id in frozen:
        agent = await session.get(Agent, agent_id)
        if agent is None:
            continue
        executor_status = await _executor_status(session, workspace_id, agent_id)
        _add_candidate(
            found,
            ScheduledRunMentionCandidateView(
                user_id=agent_id,
                target_type="AGENT",
                name=agent.name,
                agent=True,
                online=executor_status != "OFFLINE",
                executor_status=executor_status,
                mentionable=True,
            ),
            query,
            effective,
        )
    members = await session.scalars(
        select(OrgMember).where(OrgMember.tenant_id == workspace_id, OrgMember.is_deleted == 0)
    )
    for member in members.all():
        if member.user_id is None or member.status != 0:
            continue
        user = await session.get(User, member.user_id)
        if user is None:
            continue
        if user.nickname is None or user.nickname.strip() == "":
            name = user.username
        else:
            name = user.nickname
        _add_candidate(
            found,
            ScheduledRunMentionCandidateView(
                user_id=user.id,
                target_type="HUMAN",
                name=name,
                display_id=user.username,
                agent=False,
                online=True,
                mentionable=True,
            ),
            query,
            effective,
        )
    return list(found.values())


def _add_candidate(
    found: dict[str, ScheduledRunMentionCandidateView],
    candidate: ScheduledRunMentionCandidateView,
    query: str | None,
    limit: int,
) -> None:
    if (
        candidate.user_id is None
        or candidate.name is None
        or candidate.name.strip() == ""
        or len(found) >= limit
    ):
        return
    if query is not None and query.strip() != "":
        needle = query.lower()
        display = "" if candidate.display_id is None else candidate.display_id.lower()
        if needle not in candidate.name.lower() and needle not in display:
            return
    found.setdefault((candidate.target_type or "") + ":" + str(candidate.user_id), candidate)


def _frozen_ids(snapshot: object) -> set[int]:
    if not isinstance(snapshot, dict):
        return set()
    contexts = snapshot.get("agentContexts")
    if not isinstance(contexts, list):
        return set()
    agent_ids: set[int] = set()
    for context in contexts:
        if not isinstance(context, dict):
            continue
        agent_id = context.get("agentId")
        if isinstance(agent_id, bool) or not isinstance(agent_id, int):
            continue
        agent_ids.add(agent_id)
    return agent_ids


async def _executor_status(session: AsyncSession, workspace_id: int, agent_id: int) -> str:
    rows = await session.scalars(
        select(Executor).where(
            Executor.tenant_id == workspace_id,
            Executor.agent_id == agent_id,
            Executor.is_deleted == 0,
        )
    )
    online = False
    busy = False
    for executor in rows.all():
        if not is_online(executor.id):
            continue
        online = True
        if executor.status == "BUSY":
            busy = True
    if not online:
        return "OFFLINE"
    if busy:
        return "BUSY"
    return "ONLINE"


async def _delivery(
    session: AsyncSession,
    workspace_id: int,
    run: ScheduledTaskRun,
) -> DeliveryProgressView:
    steps_def = _step_defs(run)
    dispatches = await _dispatches(session, workspace_id, run.id)
    events = await _events(session, workspace_id, run.id)
    steps: list[DeliveryStepView] = []
    for step_id, code, name in steps_def:
        attempts = []
        for dispatch in dispatches:
            if step_id is None or dispatch.sdlc_step_id != step_id:
                continue
            agent = None
            if dispatch.agent_id is not None:
                agent = await session.get(Agent, dispatch.agent_id)
            attempts.append(
                DispatchAttemptView(
                    dispatch_id=dispatch.id,
                    status=dispatch.status,
                    error=dispatch.error,
                    started_at=dispatch.gmt_create,
                    duration_ms=_duration(dispatch.gmt_create, dispatch.gmt_modified),
                    can_continue=dispatch.status not in DISPATCH_TERMINAL,
                    can_pause=dispatch.status in PAUSEABLE,
                    executor_name=None if agent is None else agent.name,
                )
            )
        steps.append(
            DeliveryStepView(
                step_id=step_id,
                step_key=code,
                name=name,
                status=determine_step_status(step_id, events, run.status),
                attempts=attempts,
                duration_ms=_step_duration(step_id, events),
            )
        )
    agent_id = run.current_agent_id if run.current_agent_id is not None else run.initial_agent_id
    agent = None if agent_id is None else await session.get(Agent, agent_id)
    status = {
        "CANCELED": "cancelled",
        "FAILED": "failed",
        "SUCCEEDED": "finished",
    }.get(run.status or "", _agent_status(steps))
    total = 0
    has_duration = False
    for dispatch in dispatches:
        duration = _duration(dispatch.gmt_create, dispatch.gmt_modified)
        if duration is not None:
            total += duration
            has_duration = True
    total_ms = total if has_duration else None
    nodes = []
    edges = []
    names = {step_id: name for step_id, _code, name in steps_def if step_id is not None}
    for dispatch in dispatches:
        agent_row = None
        if dispatch.agent_id is not None:
            agent_row = await session.get(Agent, dispatch.agent_id)
        step_name = None
        if dispatch.sdlc_step_id is not None:
            step_name = names.get(dispatch.sdlc_step_id)
        nodes.append(
            ProcessGraphNodeView(
                key="dispatch-" + str(dispatch.id),
                dispatch_id=dispatch.id,
                agent_id=dispatch.agent_id,
                agent_name=None if agent_row is None else agent_row.name,
                step_id=dispatch.sdlc_step_id,
                step_name=step_name,
                status=dispatch.status,
                started_at=dispatch.gmt_create,
            )
        )
        if dispatch.resume_from_dispatch_id is not None:
            edges.append(
                ProcessGraphEdgeView(
                    source_key="dispatch-" + str(dispatch.resume_from_dispatch_id),
                    target_key="dispatch-" + str(dispatch.id),
                    type="CONTINUE",
                    source_dispatch_id=dispatch.resume_from_dispatch_id,
                    target_dispatch_id=dispatch.id,
                    label="CONTINUE",
                )
            )
    progress = AgentDeliveryProgressView.model_construct(
        agent_id=agent_id,
        agent_name=None if agent is None else agent.name,
        status=status,
        duration_ms=total_ms,
        steps=steps,
    )
    return DeliveryProgressView(
        steps=steps,
        agents=[progress],
        process_graph=ProcessGraphView(nodes=nodes, edges=edges),
        total_duration_ms=total_ms,
    )


def _step_defs(run: ScheduledTaskRun) -> list[tuple[int | None, str | None, str | None]]:
    snapshot = run.execution_snapshot_json
    if not isinstance(snapshot, dict):
        return []
    contexts = snapshot.get("agentContexts")
    if not isinstance(contexts, list) or len(contexts) == 0 or not isinstance(contexts[0], dict):
        return []
    sdlc = contexts[0].get("sdlc")
    if not isinstance(sdlc, dict):
        return []
    steps = sdlc.get("steps")
    if not isinstance(steps, list):
        return []
    result: list[tuple[int | None, str | None, str | None]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        raw_id = step.get("id")
        step_id = int(raw_id) if isinstance(raw_id, int | str) and str(raw_id).isdigit() else None
        code = step.get("kind") if isinstance(step.get("kind"), str) else None
        name = step.get("name") if isinstance(step.get("name"), str) else None
        result.append((step_id, code, name))
    return result


def _agent_status(steps: list[DeliveryStepView]) -> str:
    if any(step.status == "paused" for step in steps):
        return "paused"
    if any(step.status == "active" for step in steps):
        return "active"
    if any(step.status == "failed" for step in steps):
        return "failed"
    if any(step.status == "done" for step in steps):
        return "finished"
    return "pending"


def _duration(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _step_duration(step_id: int | None, events: list[DispatchRuntimeEvent]) -> int | None:
    start = None
    end = None
    for event in events:
        if not _step_matches(step_id, event) or event.event_time is None:
            continue
        event_type = event.event_type or ""
        if "step.started" in event_type and (start is None or event.event_time < start):
            start = event.event_time
        if ("step.completed" in event_type or "step.failed" in event_type) and (
            end is None or event.event_time > end
        ):
            end = event.event_time
    return _duration(start, end)


def _event_view(event: DispatchRuntimeEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tenantId": event.tenant_id,
        "workitemId": event.workitem_id,
        "dispatchId": event.dispatch_id,
        "agentId": event.agent_id,
        "eventId": event.event_id,
        "seq": event.seq,
        "eventType": event.event_type,
        "stepId": event.step_id,
        "stepKey": event.step_key,
        "stepOrder": event.step_order,
        "detailJson": event.detail_json,
        "eventTime": event.event_time,
    }
