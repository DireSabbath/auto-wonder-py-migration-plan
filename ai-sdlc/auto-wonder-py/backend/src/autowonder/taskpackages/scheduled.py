"""定时运行只从冻结快照装配任务包，不回查同号工单。"""

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import AgentVersion
from autowonder.artifacts.documents import TYPE as REQUIREMENT_DOC
from autowonder.artifacts.models import Artifact
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.taskpackages.context import PackageContext, TaskArtifactRef

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = "autowonder.scheduledTaskExecutionSnapshot.v1"
_SHA256 = re.compile(r"sha256:[0-9a-fA-F]{64}")


async def assemble_scheduled(
    session: AsyncSession, dispatch: Dispatch, version: AgentVersion
) -> PackageContext:
    """按运行上的 ``execution_snapshot_json`` 生成包。快照损坏时拒绝派发。"""
    run = await session.scalar(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == dispatch.tenant_id,
            ScheduledTaskRun.id == dispatch.workitem_id,
        )
        .limit(1)
    )
    if run is None or run.workspace_id != dispatch.tenant_id or run.id != dispatch.workitem_id:
        raise _invalid("scheduled task run not found")
    snapshot = _snapshot(run.execution_snapshot_json)
    _validate(run, snapshot)
    if (
        dispatch.tenant_id != run.workspace_id
        or dispatch.source_type != "SCHEDULED_TASK_RUN"
        or dispatch.workitem_id != run.id
    ):
        raise _invalid("dispatch does not match scheduled run")
    interaction = dispatch.resume_mode in ("CANONICAL_INTERACTION", "SIDE_INTERACTION")
    current_agent_id = (
        _positive(dispatch.agent_id, "dispatch.agentId")
        if interaction
        else _positive(run.current_agent_id, "run.currentAgentId")
    )
    agent_context = _agent_context(snapshot, current_agent_id)
    frozen_version_id = _positive_field(agent_context, "agentVersionId")
    if (
        version.id != frozen_version_id
        or dispatch.agent_version_id != frozen_version_id
        or version.tenant_id != run.workspace_id
        or version.agent_id != current_agent_id
        or dispatch.agent_id != current_agent_id
    ):
        raise _invalid("selected agent version does not match frozen assignment")
    task = _object(snapshot, "task")
    identity = _object(agent_context, "identity")
    context = PackageContext(
        tenant_id=run.workspace_id,
        dispatch_id=dispatch.id,
        source_dispatch_id=(
            dispatch.delivery_source_dispatch_id
            if dispatch.delivery_source_dispatch_id is not None
            else dispatch.resume_from_dispatch_id
        ),
        workitem_id=run.id,
        agent_id=current_agent_id,
        agent_version_id=frozen_version_id,
        executor_id=dispatch.executor_id,
        attempt=dispatch.attempt,
        idempotency_key=dispatch.idempotency_key,
        workitem_title=_string(task, "name"),
        workitem_content_md=_string(task, "instructionMd"),
        work_type="TASK",
        identity=dict(identity),
        role_code=_string(identity, "roleCode"),
        role_name=_string(identity, "name"),
        repos=_object_list(agent_context.get("repos"), "repos"),
        repo_map=_optional_map(agent_context.get("repoMap")),
        skills=_object_list(agent_context.get("skills"), "skills"),
        memory=_string_map(agent_context.get("memory")),
        roster=dict(_object(agent_context, "roster")),
        requirement_documents=await _documents(session, run, snapshot),
        comments=[],
        teammates=[],
        source_revision_artifacts=[],
    )
    if not interaction and dispatch.sdlc_step_id != run.current_step_id:
        raise _invalid("dispatch step does not match scheduled run assignment")
    if interaction:
        context.omit_sdlc_file_when_absent = True
        return context
    _apply_sdlc(context, run, _frozen_sdlc(snapshot, run, agent_context))
    return context


def _snapshot(raw: object) -> dict[str, object]:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        raise _invalid("scheduled task execution snapshot is missing")
    if isinstance(raw, dict):
        return raw
    raise _invalid("scheduled task execution snapshot is damaged")


def _validate(run: ScheduledTaskRun, snapshot: dict[str, object]) -> None:
    if _string(snapshot, "schemaVersion") != SNAPSHOT_SCHEMA:
        raise _invalid("unsupported scheduled task snapshot schema")
    task = _object(snapshot, "task")
    if _positive_field(task, "id") != _positive(run.scheduled_task_id, "run.scheduledTaskId"):
        raise _invalid("snapshot task id mismatch")
    _string(task, "name")
    _string(task, "instructionMd")
    assignment = _object(snapshot, "assignment")
    if _positive_field(assignment, "squadId") != _positive(run.squad_id, "run.squadId"):
        raise _invalid("snapshot assignment mismatch")
    if _positive_field(assignment, "initialAgentId") != _positive(
        run.initial_agent_id, "run.initialAgentId"
    ):
        raise _invalid("snapshot assignment mismatch")
    _validate_contexts(snapshot, run.initial_agent_id)
    policies = _object(snapshot, "policies")
    if _string(policies, "sessionMode") != run.session_mode:
        raise _invalid("snapshot session policy mismatch")
    _string(policies, "overlapPolicy")
    trigger = _object(snapshot, "trigger")
    if _string(trigger, "type") != run.trigger_type:
        raise _invalid("snapshot trigger type mismatch")
    _string(trigger, "scheduledAt")
    documents = snapshot.get("requirementDocuments")
    if not isinstance(documents, list):
        raise _invalid("snapshot requirementDocuments is required")
    sdlc = snapshot.get("sdlc") if "sdlc" in snapshot else None
    if "sdlc" in snapshot and sdlc is not None and not isinstance(sdlc, dict):
        raise _invalid("snapshot sdlc is invalid")


def _validate_contexts(snapshot: dict[str, object], initial_agent_id: int) -> None:
    contexts = snapshot.get("agentContexts")
    if not isinstance(contexts, list) or len(contexts) == 0:
        raise _invalid("snapshot agentContexts must not be empty")
    agent_ids: set[int] = set()
    assignments: set[str] = set()
    found = False
    for context in contexts:
        if not isinstance(context, dict):
            raise _invalid("snapshot agentContexts is invalid")
        agent_id = _positive_field(context, "agentId")
        version_id = _positive_field(context, "agentVersionId")
        token = str(agent_id) + ":" + str(version_id)
        if agent_id in agent_ids or token in assignments:
            raise _invalid("snapshot agent context is duplicated for agent " + str(agent_id))
        agent_ids.add(agent_id)
        assignments.add(token)
        identity = _object(context, "identity")
        _string(identity, "name")
        _string(identity, "roleCode")
        if not isinstance(context.get("repos"), list):
            raise _invalid("snapshot repos is required")
        repo_map = context.get("repoMap") if "repoMap" in context else None
        if "repoMap" not in context or (repo_map is not None and not isinstance(repo_map, dict)):
            raise _invalid("snapshot agent context repoMap is required")
        if not isinstance(context.get("skills"), list):
            raise _invalid("snapshot skills is required")
        _object(context, "memory")
        _object(context, "roster")
        if initial_agent_id == agent_id:
            found = True
    if not found:
        raise _invalid("snapshot initial agent context is missing")


def _agent_context(snapshot: dict[str, object], agent_id: int) -> dict[str, object]:
    contexts = snapshot.get("agentContexts")
    if not isinstance(contexts, list):
        raise _invalid("snapshot agentContexts is required")
    match: dict[str, object] | None = None
    for candidate in contexts:
        if not isinstance(candidate, dict):
            continue
        raw = candidate.get("agentId")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw == agent_id:
            if match is not None:
                raise _invalid("snapshot agent context is duplicated for agent " + str(agent_id))
            match = candidate
    if match is None:
        raise _invalid("snapshot agent context is missing for agent " + str(agent_id))
    return match


async def _documents(
    session: AsyncSession, run: ScheduledTaskRun, snapshot: dict[str, object]
) -> list[TaskArtifactRef]:
    documents = snapshot.get("requirementDocuments")
    if not isinstance(documents, list):
        raise _invalid("snapshot requirementDocuments is required")
    result: list[TaskArtifactRef] = []
    for frozen in documents:
        if not isinstance(frozen, dict):
            raise _invalid("frozen requirement document is invalid")
        artifact_id = _positive_field(frozen, "artifactId")
        name = _string(frozen, "name")
        oss_ref = _string(frozen, "ossRef")
        digest = _sha256(frozen, "sha256")
        artifact = await session.get(Artifact, artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != run.workspace_id
            or artifact.source_type != "SCHEDULED_TASK"
            or artifact.workitem_id != run.scheduled_task_id
            or artifact.type != REQUIREMENT_DOC
            or artifact.name != name
            or artifact.oss_ref != oss_ref
        ):
            raise _invalid(
                "frozen requirement document no longer matches artifact " + str(artifact_id)
            )
        result.append(TaskArtifactRef(name=name, oss_ref=oss_ref, expected_sha256=digest))
    return result


def _apply_sdlc(
    context: PackageContext, run: ScheduledTaskRun, frozen: dict[str, object] | None
) -> None:
    if frozen is None:
        if run.sdlc_id is not None or run.current_step_id is not None:
            raise _invalid("run SDLC assignment is not present in snapshot")
        context.omit_sdlc_file_when_absent = True
        return
    sdlc_id = _positive_field(frozen, "id")
    current_step_id = _positive(run.current_step_id, "run.currentStepId")
    if run.sdlc_id != sdlc_id or not _contains_step(frozen.get("steps"), current_step_id):
        raise _invalid("run SDLC assignment does not match frozen workflow")
    sdlc = dict(frozen)
    sdlc["sdlcId"] = str(sdlc_id)
    sdlc["currentStepId"] = str(current_step_id)
    context.sdlc_id = sdlc_id
    context.sdlc_step_id = current_step_id
    context.sdlc = sdlc
    context.omit_sdlc_file_when_absent = False


def _frozen_sdlc(
    snapshot: dict[str, object], run: ScheduledTaskRun, agent_context: dict[str, object]
) -> dict[str, object] | None:
    current = agent_context.get("sdlc")
    if isinstance(current, dict):
        return current
    if run.current_agent_id == run.initial_agent_id:
        legacy = snapshot.get("sdlc")
        if isinstance(legacy, dict):
            return legacy
        return None
    if run.sdlc_id is not None or run.current_step_id is not None:
        raise _invalid("current agent frozen SDLC is missing")
    return None


def _contains_step(steps: object, step_id: int) -> bool:
    if not isinstance(steps, list) or len(steps) == 0:
        return False
    for step in steps:
        if isinstance(step, dict) and str(step.get("id")) == str(step_id):
            return True
    return False


def _object(parent: dict[str, object], key: str) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise _invalid("snapshot " + key + " is required")
    return value


def _object_list(raw: object, name: str) -> list[dict[str, object]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid("snapshot " + name + " is required")
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("snapshot " + name + " is invalid")
        result.append(dict(item))
    return result


def _optional_map(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    return None


def _string_map(raw: object) -> dict[str, str]:
    source = _object({"memory": raw}, "memory") if not isinstance(raw, dict) else raw
    result: dict[str, str] = {}
    for key, value in source.items():
        if value is None:
            raise _invalid("snapshot memory is invalid")
        result[str(key)] = str(value)
    return result


def _string(parent: dict[str, object], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise _invalid("snapshot " + key + " is required")
    return value


def _sha256(parent: dict[str, object], key: str) -> str:
    value = _string(parent, key)
    if _SHA256.fullmatch(value) is None:
        raise _invalid("snapshot " + key + " is invalid")
    return value.lower()


def _positive_field(parent: dict[str, object], key: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("snapshot." + key + " must be positive")
    return _positive(value, "snapshot." + key)


def _positive(value: int | None, name: str) -> int:
    if value is None or value <= 0:
        raise _invalid(name + " must be positive")
    return value


def _invalid(message: str) -> BizError:
    logger.info("scheduled package rejected reason=%s", message)
    return BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, message)
