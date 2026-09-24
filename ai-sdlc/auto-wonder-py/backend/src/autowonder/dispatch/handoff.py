"""执行器上报的下一跳。工单交给在线员工或真人，定时运行只在冻结小队里交接。"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.enqueue import (
    _require_workitem_dispatch,
    enqueue_handoff,
    find_handoff_by_source,
    list_workitem_dispatches,
)
from autowonder.dispatch.handoff_rules import (
    HandoffResult,
    agent_result,
    automatic_handoff_limited,
    human_result,
    parse_human_ref,
    rejected_result,
    superseded_by_interaction_rework,
)
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery import fenced
from autowonder.guidance.service import _first_step, _sdlc_id
from autowonder.scheduledtasks.orchestrator import handoff_scheduled
from autowonder.users.models import User
from autowonder.workitems.events import (
    WorkitemHumanAssigned,
    publish_human_assigned,
    request_id_or_none,
)
from autowonder.workitems.models import Workitem, WorkitemEvent
from autowonder.workitems.rules import assignment_detail
from autowonder.workitems.service import AssignmentActor, _cas, _write_event
from autowonder.workspaces.models import Org, OrgMember

logger = logging.getLogger(__name__)

_SYSTEM_USER_ID = 0
_MAX_AUTOMATIC_REPEATS = 5


async def handle(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    target: str | None,
    target_type: str | None,
) -> HandoffResult:
    """处理一帧交接。来源不存在或被更新的评论返工取代时拒绝。"""
    source = await session.get(Dispatch, dispatch_id)
    if source is not None and source.source_type == "SCHEDULED_TASK_RUN":
        if source.tenant_id != tenant_id or source.workitem_id is None:
            return rejected_result("DISPATCH_NOT_FOUND", "source scheduled dispatch not found")
        return await handoff_scheduled(session, source, target)
    try:
        await _require_workitem_dispatch(session, tenant_id, workitem_id, dispatch_id)
    except BizError:
        logger.info(
            "handoff source dispatch not found or not a workitem dispatch dispatchId=%s",
            dispatch_id,
        )
        return rejected_result("DISPATCH_NOT_FOUND", "source dispatch not found for work item")
    workitem = await _lock_workitem(session, tenant_id, workitem_id)
    if workitem is None or workitem.tenant_id != tenant_id:
        logger.info("handoff workitem not found or cross-tenant workitemId=%s", workitem_id)
        return rejected_result("WORKITEM_NOT_FOUND", "work item not found in tenant")
    rows = await list_workitem_dispatches(session, tenant_id, workitem_id)
    if superseded_by_interaction_rework(rows, dispatch_id):
        logger.info(
            "handoff rejected because source was superseded workitemId=%s dispatchId=%s",
            workitem_id,
            dispatch_id,
        )
        return rejected_result(
            "SOURCE_SUPERSEDED", "source dispatch was superseded by comment rework"
        )
    existing = await find_handoff_by_source(session, tenant_id, dispatch_id)
    if existing is not None:
        return agent_result(existing.agent_id, existing.id)
    if target_type is not None and target_type.upper() == "HUMAN":
        return await _human(
            session, tenant_id, workitem_id, dispatch_id, target, workitem, "REQUESTED_HUMAN", True
        )
    agent_id = None
    if target is not None and not java_is_blank(target):
        agent_id = await _online_agent(session, tenant_id, target)
    if agent_id is not None:
        if automatic_handoff_limited(rows, dispatch_id, agent_id, _MAX_AUTOMATIC_REPEATS):
            logger.warning(
                "automatic handoff limit reached workitemId=%s sourceDispatchId=%s "
                "targetAgentId=%s limit=%s",
                workitem_id,
                dispatch_id,
                agent_id,
                _MAX_AUTOMATIC_REPEATS,
            )
            return await _human(
                session,
                tenant_id,
                workitem_id,
                dispatch_id,
                None,
                workitem,
                "AUTOMATIC_HANDOFF_LIMIT",
                True,
            )
        return await _agent(
            session, tenant_id, workitem_id, dispatch_id, target, agent_id, workitem
        )
    logger.info(
        "handoff agent target unavailable; falling back to human tenantId=%s workitemId=%s "
        "target=%s",
        tenant_id,
        workitem_id,
        target,
    )
    return await _human(
        session,
        tenant_id,
        workitem_id,
        dispatch_id,
        None,
        workitem,
        "UNKNOWN_AGENT_FALLBACK_HUMAN",
        False,
    )


async def may_route_handoff(
    session: AsyncSession, workspace_id: int, executor_id: int, dispatch_id: int
) -> bool:
    """只有当前执行器刚完成的正式调度可以带出交接。"""
    dispatch = await session.get(Dispatch, dispatch_id)
    if dispatch is None or dispatch.tenant_id != workspace_id:
        return False
    if dispatch.executor_id != executor_id or dispatch.is_deleted != 0:
        return False
    if dispatch.status != "SUCCEEDED" or await fenced(session, dispatch):
        return False
    return dispatch.resume_mode not in {
        "SIDE_INTERACTION",
        "CANONICAL_INTERACTION",
        "COMMENT_INTERACTION",
    }


async def _agent(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    target: str | None,
    agent_id: int,
    workitem: Workitem,
) -> HandoffResult:
    sdlc_id = await _sdlc_id(session, tenant_id, agent_id)
    if sdlc_id is None:
        logger.info("handoff target agent has no sdlc tenantId=%s agentId=%s", tenant_id, agent_id)
        return rejected_result("TARGET_AGENT_HAS_NO_SDLC", "target agent has no SDLC")
    first = await _first_step(session, tenant_id, sdlc_id)
    if first is None or first.id is None:
        logger.info("handoff target sdlc has no steps sdlcId=%s", sdlc_id)
        return rejected_result("TARGET_SDLC_HAS_NO_STEPS", "target SDLC has no steps")
    await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {"sdlc_id": sdlc_id, "current_step_id": first.id, "modifier_id": _SYSTEM_USER_ID},
        scheduled_set=False,
    )
    session.expire_all()
    reloaded = await session.get(Workitem, workitem_id)
    next_version = workitem.version
    if reloaded is not None:
        next_version = reloaded.version
    await _cas(
        session,
        workitem_id,
        tenant_id,
        next_version,
        {
            "assignee_type": "AGENT",
            "assignee_ref": agent_id,
            "modifier_id": _SYSTEM_USER_ID,
        },
        scheduled_set=False,
    )
    actor = await _source_actor(session, tenant_id, workitem_id, dispatch_id, "AGENT_HANDOFF")
    await _assign_event(
        session,
        tenant_id,
        workitem_id,
        workitem.assignee_ref,
        agent_id,
        actor,
        workitem.assignee_type,
        "AGENT",
    )
    logger.info(
        "handoff to AGENT workitemId=%s target=%s targetAgentId=%s sdlcId=%s firstStepId=%s",
        workitem_id,
        target,
        agent_id,
        sdlc_id,
        first.id,
    )
    created = await enqueue_handoff(
        session, tenant_id, workitem_id, first.id, agent_id, dispatch_id, _SYSTEM_USER_ID
    )
    await session.commit()
    return agent_result(agent_id, created.id)


async def _human(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    target: str | None,
    workitem: Workitem,
    fallback_reason: str,
    allow_owner: bool,
) -> HandoffResult:
    resolved = parse_human_ref(target)
    if resolved is None:
        resolved = workitem.assign_operator_id
    if resolved is None and allow_owner:
        resolved = await _owner_id(session, tenant_id)
    if resolved is None or resolved <= 0:
        logger.info(
            "handoff human target unresolved and no fallback tenantId=%s workitemId=%s to=%s",
            tenant_id,
            workitem_id,
            target,
        )
        return rejected_result("TARGET_UNRESOLVED", "no agent or human fallback resolved")
    user = await session.get(User, resolved)
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == tenant_id,
            OrgMember.user_id == resolved,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    if user is None or user.status != 0 or member is None or member.status != 0:
        return rejected_result(
            "TARGET_NOT_WORKSPACE_MEMBER", "human target is not an active workspace member"
        )
    if workitem.assignee_type is not None and workitem.assignee_type.upper() == "HUMAN":
        if workitem.assignee_ref == resolved:
            return human_result(resolved, fallback_reason)
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {
            "assignee_type": "HUMAN",
            "assignee_ref": resolved,
            "modifier_id": _SYSTEM_USER_ID,
        },
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    actor = await _source_actor(session, tenant_id, workitem_id, dispatch_id, fallback_reason)
    event = await _assign_event(
        session,
        tenant_id,
        workitem_id,
        workitem.assignee_ref,
        resolved,
        actor,
        workitem.assignee_type,
        "HUMAN",
    )
    if event.id is not None:
        publish_human_assigned(
            WorkitemHumanAssigned(
                tenant_id,
                workitem_id,
                workitem.title,
                event.id,
                resolved,
                actor.type,
                actor.ref,
                actor.display_name,
                request_id_or_none(),
            )
        )
    logger.info(
        "handoff to HUMAN workitemId=%s target=%s resolvedUserId=%s",
        workitem_id,
        target,
        resolved,
    )
    await session.commit()
    return human_result(resolved, fallback_reason)


async def _assign_event(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    from_ref: int | None,
    to_ref: int,
    actor: AssignmentActor,
    from_type: str | None,
    to_type: str,
) -> WorkitemEvent:
    from_val = None
    if from_ref is not None:
        from_val = str(from_ref)
    return await _write_event(
        session,
        tenant_id,
        workitem_id,
        "ASSIGN",
        from_val,
        str(to_ref),
        actor.type,
        actor.ref,
        assignment_detail(from_type, to_type),
    )


async def _source_actor(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    fallback_reason: str,
) -> AssignmentActor:
    try:
        source = await session.get(Dispatch, dispatch_id)
        if (
            source is None
            or source.tenant_id != tenant_id
            or source.source_type != "WORKITEM"
            or source.workitem_id != workitem_id
            or source.agent_id <= 0
        ):
            logger.warning(
                "handoff source actor resolution failed tenantId=%s workitemId=%s "
                "sourceDispatchId=%s reason=%s",
                tenant_id,
                workitem_id,
                dispatch_id,
                fallback_reason,
            )
            return AssignmentActor("SYSTEM", 0, "系统")
        agent = await session.get(Agent, source.agent_id)
        if (
            agent is None
            or (agent.tenant_id is not None and agent.tenant_id != tenant_id)
            or agent.name is None
            or java_is_blank(agent.name)
        ):
            logger.warning(
                "handoff source agent resolution failed tenantId=%s workitemId=%s "
                "sourceDispatchId=%s sourceAgentId=%s reason=%s",
                tenant_id,
                workitem_id,
                dispatch_id,
                source.agent_id,
                fallback_reason,
            )
            return AssignmentActor("SYSTEM", 0, "系统")
        return AssignmentActor("AGENT", source.agent_id, agent.name)
    except Exception:
        logger.warning(
            "handoff source actor resolution exception tenantId=%s workitemId=%s "
            "sourceDispatchId=%s reason=%s",
            tenant_id,
            workitem_id,
            dispatch_id,
            fallback_reason,
            exc_info=True,
        )
        return AssignmentActor("SYSTEM", 0, "系统")


async def _online_agent(session: AsyncSession, tenant_id: int, role_code: str) -> int | None:
    code = role_code.strip()
    return await session.scalar(
        select(Agent.id)
        .join(AgentVersion, Agent.online_version_id == AgentVersion.id)
        .where(
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
            AgentVersion.status == "APPROVED",
            AgentVersion.is_deleted == 0,
            AgentVersion.role_code == code,
        )
        .order_by(Agent.id.asc())
        .limit(1)
    )


async def _owner_id(session: AsyncSession, tenant_id: int) -> int | None:
    workspace = await session.get(Org, tenant_id)
    if workspace is None or workspace.is_deleted != 0:
        return None
    return workspace.owner_id


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
