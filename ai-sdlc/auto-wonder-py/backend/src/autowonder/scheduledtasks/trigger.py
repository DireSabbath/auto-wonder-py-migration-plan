"""创建定时任务运行。手动触发要求任务处于 ACTIVE，并冻结可执行小队成员。"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.artifacts.documents import TYPE
from autowonder.artifacts.models import Artifact
from autowonder.core.errors import BizError, ErrorCode
from autowonder.scheduledtasks.models import ScheduledTask, ScheduledTaskRun
from autowonder.squads.models import SquadMember
from autowonder.storage.objects import get_object_storage

SNAPSHOT_SCHEMA = "autowonder.scheduledTaskExecutionSnapshot.v1"
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED"})


def manual_key(task_id: int, request_id: str | None) -> str:
    """手动触发的幂等键。空白 requestId 直接拒绝。"""
    if request_id is None or request_id.strip() == "":
        raise BizError(ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED, "requestId is required")
    return "task:" + str(task_id) + ":manual:" + request_id.strip()


async def fire_manual(
    session: AsyncSession,
    workspace_id: int,
    task_id: int,
    request_id: str,
) -> ScheduledTaskRun:
    """按 requestId 创建或取回一次手动运行。"""
    task = await session.scalar(
        select(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == workspace_id,
            ScheduledTask.id == task_id,
            ScheduledTask.is_deleted == 0,
        )
        .limit(1)
    )
    if task is None or task.workspace_id != workspace_id:
        raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
    _require_runnable(task, False)
    decided = await _lock_for_overlap(session, task, False)
    skip_reason = None
    if await _has_active(session, decided) and (
        decided.overlap_policy == "SKIP" or decided.session_mode == "CONTINUOUS"
    ):
        skip_reason = "OVERLAP"
    now = datetime.now(UTC).replace(tzinfo=None)
    trigger_key = manual_key(task.id, request_id)
    run = await _base_run(session, decided, now, "MANUAL", trigger_key, skip_reason)
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError:
        recovered = await session.scalar(
            select(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.workspace_id == workspace_id,
                ScheduledTaskRun.trigger_key == trigger_key,
            )
            .limit(1)
        )
        if recovered is None:
            raise
        return recovered
    return run


async def _lock_for_overlap(
    session: AsyncSession,
    task: ScheduledTask,
    allow_exhausted: bool,
) -> ScheduledTask:
    if task.overlap_policy != "SKIP" and task.session_mode != "CONTINUOUS":
        return task
    locked = await session.scalar(
        select(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == task.workspace_id,
            ScheduledTask.id == task.id,
            ScheduledTask.is_deleted == 0,
        )
        .with_for_update()
        .limit(1)
    )
    if locked is None or locked.workspace_id != task.workspace_id:
        raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
    _require_runnable(locked, allow_exhausted)
    return locked


async def _has_active(session: AsyncSession, task: ScheduledTask) -> bool:
    rows = await session.scalars(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == task.workspace_id,
            ScheduledTaskRun.scheduled_task_id == task.id,
            ScheduledTaskRun.status.not_in(_TERMINAL),
        )
        .with_for_update()
    )
    return len(list(rows.all())) > 0


async def _base_run(
    session: AsyncSession,
    task: ScheduledTask,
    scheduled_at: datetime,
    trigger_type: str,
    trigger_key: str,
    skip_reason: str | None,
) -> ScheduledTaskRun:
    snapshot, sdlc_id, step_id = await _snapshot(session, task, scheduled_at, trigger_type)
    return ScheduledTaskRun(
        workspace_id=task.workspace_id,
        scheduled_task_id=task.id,
        trigger_key=trigger_key,
        trigger_type=trigger_type,
        scheduled_at=scheduled_at,
        status="SKIPPED" if skip_reason is not None else "QUEUED",
        skip_reason=skip_reason,
        squad_id=task.squad_id,
        initial_agent_id=task.initial_agent_id,
        current_agent_id=task.initial_agent_id,
        sdlc_id=sdlc_id,
        current_step_id=step_id,
        session_mode=task.session_mode,
        execution_snapshot_json=snapshot,
        owner_id=task.creator_id,
        creator_id=task.creator_id,
        modifier_id=task.creator_id,
    )


async def _snapshot(
    session: AsyncSession,
    task: ScheduledTask,
    scheduled_at: datetime,
    trigger_type: str,
) -> tuple[dict[str, object], int | None, int | None]:
    contexts, sdlc = await _agent_contexts(session, task)
    documents = _requirement_documents(session, task)
    snapshot: dict[str, object] = {
        "schemaVersion": SNAPSHOT_SCHEMA,
        "task": {"id": task.id, "name": task.name, "instructionMd": task.instruction_md},
        "assignment": {"squadId": task.squad_id, "initialAgentId": task.initial_agent_id},
        "sdlc": sdlc,
        "agentContexts": contexts,
        "requirementDocuments": await documents,
        "policies": {
            "sessionMode": task.session_mode,
            "overlapPolicy": task.overlap_policy,
            "misfirePolicy": task.misfire_policy,
            "startDeadlineSeconds": task.start_deadline_seconds,
            "affinityTimeoutSeconds": task.affinity_timeout_seconds,
            "scheduleType": task.schedule_type,
            "timezone": task.timezone,
        },
        "trigger": {"type": trigger_type, "scheduledAt": scheduled_at.isoformat()},
    }
    sdlc_id = None if sdlc is None else sdlc.get("id")
    step_id = None if sdlc is None else sdlc.get("currentStepId")
    return snapshot, _as_int(sdlc_id), _as_int(step_id)


async def _agent_contexts(
    session: AsyncSession,
    task: ScheduledTask,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    members = await session.scalars(
        select(SquadMember).where(SquadMember.squad_id == task.squad_id)
    )
    contexts: list[dict[str, object]] = []
    seen: set[int] = set()
    initial_found = False
    sdlc: dict[str, object] | None = None
    for member in members.all():
        if member.tenant_id != task.workspace_id or member.agent_id in seen:
            continue
        seen.add(member.agent_id)
        agent = await session.get(Agent, member.agent_id)
        if (
            agent is None
            or agent.tenant_id != task.workspace_id
            or agent.online_version_id is None
        ):
            continue
        version = await session.get(AgentVersion, agent.online_version_id)
        if version is None or version.tenant_id != task.workspace_id:
            continue
        identity = version.identity_json if isinstance(version.identity_json, dict) else {}
        identity = dict(identity)
        identity.setdefault("name", version.role_name)
        identity.setdefault("roleCode", version.role_code)
        contexts.append(
            {
                "agentId": agent.id,
                "agentVersionId": version.id,
                "identity": identity,
                "repos": [],
                "repoMap": {"boundRepoIds": [], "relations": []},
                "skills": [],
                "memory": {},
                "roster": {"digitalTeammates": [], "humanTeammates": []},
            }
        )
        if agent.id == task.initial_agent_id:
            initial_found = True
            if version.sdlc_id is not None:
                sdlc = {"id": version.sdlc_id, "currentStepId": None, "steps": []}
    if not initial_found:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "initial agent is not an executable squad member",
        )
    if len(contexts) == 0:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "squad has no executable online agents",
        )
    return contexts, sdlc


async def _requirement_documents(
    session: AsyncSession,
    task: ScheduledTask,
) -> list[dict[str, object]]:
    rows = await session.scalars(
        select(Artifact).where(
            Artifact.tenant_id == task.workspace_id,
            Artifact.source_type == "SCHEDULED_TASK",
            Artifact.workitem_id == task.id,
            Artifact.type == TYPE,
        )
    )
    storage = get_object_storage()
    documents: list[dict[str, object]] = []
    for artifact in rows.all():
        if (
            artifact.tenant_id != task.workspace_id
            or artifact.source_type != "SCHEDULED_TASK"
            or artifact.type != TYPE
            or artifact.workitem_id != task.id
            or artifact.oss_ref is None
        ):
            raise BizError(
                ErrorCode.SCHEDULED_TASK_INVALID_STATE,
                "requirement document does not belong to scheduled task: " + str(artifact.id),
            )
        payload = storage.get(artifact.oss_ref)
        if payload is None:
            raise BizError(
                ErrorCode.SCHEDULED_TASK_INVALID_STATE,
                "requirement document is unavailable: " + str(artifact.id),
            )
        digest = hashlib.sha256(payload).hexdigest()
        documents.append(
            {
                "artifactId": artifact.id,
                "name": artifact.name,
                "ossRef": artifact.oss_ref,
                "sha256": "sha256:" + digest,
            }
        )
    return documents


def _require_runnable(task: ScheduledTask, allow_exhausted: bool) -> None:
    allowed = task.status == "ACTIVE" or (allow_exhausted and task.status == "EXHAUSTED")
    if task.id is None or task.workspace_id is None or not allowed:
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def scheduled_trigger_key(task_id: int, scheduled_at: datetime) -> str:
    """计划触发的幂等键。时间格式与 Java ``Instant.toString()`` 一致。"""
    return "task:" + str(task_id) + ":scheduled:" + java_instant(scheduled_at)


def java_instant(value: datetime) -> str:
    """UTC 瞬间写成带 ``Z`` 的 ISO-8601，小数末尾的 0 去掉。"""
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        text = text + "." + fraction
    return text + "Z"


async def fire_scheduled(
    session: AsyncSession,
    task: ScheduledTask,
    scheduled_at: datetime,
) -> ScheduledTaskRun:
    """创建一次到期的计划运行。"""
    return await fire_occurrence(session, task, scheduled_at, "SCHEDULED", None, False)


async def fire_misfire(
    session: AsyncSession,
    task: ScheduledTask,
    scheduled_at: datetime,
    skip_reason: str | None,
    bypass_overlap: bool,
) -> ScheduledTaskRun:
    """补触发。跳过原因只接受空、策略跳过或超过启动期限。"""
    if skip_reason is not None and skip_reason not in {"MISFIRE_POLICY", "START_DEADLINE"}:
        raise ValueError("unsupported misfire skip reason: " + skip_reason)
    return await fire_occurrence(
        session,
        task,
        scheduled_at,
        "MISFIRE",
        skip_reason,
        bypass_overlap,
    )


async def fire_occurrence(
    session: AsyncSession,
    task: ScheduledTask,
    scheduled_at: datetime,
    trigger_type: str,
    skip_reason: str | None,
    bypass_overlap: bool,
) -> ScheduledTaskRun:
    """按触发键插入运行。重复键取回已有行。"""
    from autowonder.scheduledtasks.capability import require_scheduled_capability

    require_scheduled_capability()
    allow_exhausted = trigger_type != "MANUAL"
    decided = task
    if (
        not bypass_overlap
        and skip_reason is None
        and (task.overlap_policy == "SKIP" or task.session_mode == "CONTINUOUS")
    ):
        decided = await _lock_for_overlap(session, task, allow_exhausted)
    _require_runnable(decided, allow_exhausted)
    resolved_skip = skip_reason
    if (
        not bypass_overlap
        and resolved_skip is None
        and await _has_active(session, decided)
        and (decided.overlap_policy == "SKIP" or decided.session_mode == "CONTINUOUS")
    ):
        resolved_skip = "OVERLAP"
    trigger_key = scheduled_trigger_key(task.id, scheduled_at)
    run = await _base_run(session, decided, scheduled_at, trigger_type, trigger_key, resolved_skip)
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError:
        recovered = await session.scalar(
            select(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.workspace_id == task.workspace_id,
                ScheduledTaskRun.trigger_key == trigger_key,
            )
            .limit(1)
        )
        if recovered is None:
            raise
        return recovered
    return run
