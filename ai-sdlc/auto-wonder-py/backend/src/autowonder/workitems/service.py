"""工单创建、列表、流转、指派、定时、标签、内容和删除。"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.config import get_settings
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.page import PageResult
from autowonder.db.rows import rowcount
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.assignment import drive_queued, on_workitem_assigned
from autowonder.dispatch.models import Dispatch
from autowonder.integrations.models import ExternalWorkitemLink
from autowonder.scheduledtasks.models import ScheduledTask, ScheduledTaskRun
from autowonder.scheduledtasks.notify import publish_derived_workitem
from autowonder.sdlcs.models import Sdlc, SdlcStep
from autowonder.squads.models import SquadMember
from autowonder.statemachines.models import StatusNode, StatusTemplate, StatusTransition
from autowonder.users.models import User
from autowonder.workitems.events import (
    WorkitemAssigned,
    WorkitemContentUpdated,
    WorkitemHumanAssigned,
    WorkitemStatusChanged,
    publish_content_updated,
    publish_human_assigned,
    publish_status_changed,
    request_id_or_none,
)
from autowonder.workitems.models import Workitem, WorkitemEvent
from autowonder.workitems.query import count_statement, list_statement
from autowonder.workitems.rules import (
    ACTIVE_DISPATCH_STATUSES,
    WORK_TYPES,
    assignment_detail,
    is_after,
    keyword_id,
    keyword_text,
    normalize_scheduled_start,
    normalize_status_category,
    normalize_tags,
    page_bounds,
)
from autowonder.workitems.schemas import CreateWorkitemRequest, WorkitemView
from autowonder.workitems.view import (
    actor_display,
    person_name,
    render_workitem,
    shanghai_millis,
)
from autowonder.workitems.watchers import apply_watch, mark_watched

logger = logging.getLogger(__name__)
_SYSTEM_USER_ID = 0


@dataclass
class AssignmentActor:
    """指派动作的操作者。系统交接不能覆盖真人操作人。"""

    type: str
    ref: int
    display_name: str

    def is_human(self) -> bool:
        """真人操作者。"""
        return self.type == "HUMAN"


async def create(
    session: AsyncSession,
    request: CreateWorkitemRequest,
    tenant_id: int,
    user_id: int,
    scheduled_start_at: datetime | None,
) -> WorkitemView:
    """按默认状态创建工单。显式负责人走与单独指派相同的交付启动。"""
    return await create_with_origin(
        session,
        request,
        tenant_id,
        user_id,
        None,
        None,
        scheduled_start_at,
    )


async def create_with_origin(
    session: AsyncSession,
    request: CreateWorkitemRequest,
    tenant_id: int,
    user_id: int,
    origin_type: str | None,
    origin_id: int | None,
    scheduled_start_at: datetime | None,
) -> WorkitemView:
    """来源只接受服务端传入的类型和 id。"""
    if request.work_type is None or request.work_type not in WORK_TYPES:
        raise BizError(ErrorCode.WORK_TYPE_INVALID)
    template = await _default_template(session, request.work_type)
    if template is None:
        raise BizError(ErrorCode.STATUS_TEMPLATE_NOT_FOUND)
    init = await _init_node(session, template.id)
    if init is None:
        raise BizError(ErrorCode.STATUS_TEMPLATE_NOT_FOUND)
    priority = 2
    if request.priority is not None:
        priority = request.priority
    stored = Workitem(
        tenant_id=tenant_id,
        work_type=request.work_type,
        title=request.title,
        content_md=request.content_md,
        template_id=template.id,
        status_node_id=init.id,
        assignee_type="HUMAN",
        assignee_ref=user_id,
        priority=priority,
        creator_id=user_id,
        origin_type=origin_type,
        origin_id=origin_id,
        version=0,
    )
    session.add(stored)
    await session.flush()
    if origin_type == "SCHEDULED_TASK_RUN" and origin_id is not None:
        await publish_derived_workitem(session, tenant_id, origin_id)
    await _write_event(
        session,
        tenant_id,
        stored.id,
        "CREATE",
        None,
        init.code,
        "HUMAN",
        user_id,
        None,
    )
    if request.assignee_type is not None:
        return await assign_as(
            session,
            stored.id,
            request.assignee_type,
            request.assignee_ref,
            request.sdlc_id,
            request.squad_id,
            scheduled_start_at,
            tenant_id,
            user_id,
            AssignmentActor("HUMAN", user_id, await _human_label(session, user_id)),
        )
    await session.commit()
    return await _detail(session, stored.id)


async def get_workitem(
    session: AsyncSession, workitem_id: int, tenant_id: int, user_id: int
) -> WorkitemView:
    """读取未删除工单，并回填当前用户的关注状态。"""
    view = await _detail(session, workitem_id)
    await mark_watched(session, view, tenant_id, user_id)
    return view


async def list_workitems(
    session: AsyncSession,
    tenant_id: int,
    current_user_id: int,
    work_type: str | None,
    status_node_id: int | None,
    status_category: str | None,
    assignee_type: str | None,
    assignee_ref: int | None,
    pending_decision_only: bool,
    mine_scope: str | None,
    keyword: str | None,
    tag: str | None,
    scheduled_start: str | None,
    page: int,
    size: int,
) -> PageResult:
    """分页列表。过滤在 SQL 中完成，卡片上的健康和阶段在读出后派生。"""
    safe_page, safe_size, offset = page_bounds(page, size)
    effective_keyword = keyword_text(keyword)
    effective_tag = keyword_text(tag)
    effective_category = normalize_status_category(status_category)
    effective_scheduled = normalize_scheduled_start(scheduled_start)
    keyword_as_id = None
    if effective_keyword is not None:
        keyword_as_id = keyword_id(effective_keyword)
    total_value = await session.scalar(
        count_statement(
            tenant_id,
            work_type,
            status_node_id,
            effective_category,
            assignee_type,
            assignee_ref,
            pending_decision_only,
            mine_scope,
            current_user_id,
            effective_keyword,
            keyword_as_id,
            effective_tag,
            effective_scheduled,
        )
    )
    total = 0
    if total_value is not None:
        total = int(total_value)
    result = await session.execute(
        list_statement(
            tenant_id,
            work_type,
            status_node_id,
            effective_category,
            assignee_type,
            assignee_ref,
            pending_decision_only,
            mine_scope,
            current_user_id,
            effective_keyword,
            keyword_as_id,
            effective_tag,
            effective_scheduled,
            offset,
            safe_size,
        )
    )
    rows = list(result.scalars().all())
    views = await _decorate_page(session, tenant_id, rows)
    await apply_watch(session, views, tenant_id, current_user_id)
    return PageResult.model_validate(
        {
            "list": views,
            "total": total,
            "pageNum": safe_page,
            "pageSize": safe_size,
        }
    )


async def transition(
    session: AsyncSession,
    workitem_id: int,
    to_node_id: int,
    tenant_id: int,
    user_id: int,
    expected_from_node_id: int | None,
    expected_version: int | None,
) -> WorkitemView:
    """沿状态边流转。来源节点或版本不一致时拒绝。"""
    workitem = await _live_in_tenant(session, workitem_id, tenant_id)
    conflict = False
    if expected_from_node_id is not None and expected_from_node_id != workitem.status_node_id:
        conflict = True
    if expected_version is not None and expected_version != workitem.version:
        conflict = True
    if conflict:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    if workitem.template_id is None or workitem.status_node_id is None:
        raise BizError(ErrorCode.ILLEGAL_TRANSITION)
    from_node_id = workitem.status_node_id
    edge = await session.scalar(
        select(StatusTransition)
        .where(
            StatusTransition.template_id == workitem.template_id,
            StatusTransition.from_node_id == from_node_id,
            StatusTransition.to_node_id == to_node_id,
        )
        .limit(1)
    )
    if edge is None:
        raise BizError(ErrorCode.ILLEGAL_TRANSITION)
    from_node = await _find_node(session, from_node_id)
    to_node = await _find_node(session, to_node_id)
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {"status_node_id": to_node_id, "modifier_id": user_id},
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    from_code = None
    if from_node is not None:
        from_code = from_node.code
    to_code = None
    if to_node is not None:
        to_code = to_node.code
    await _write_event(
        session,
        tenant_id,
        workitem_id,
        "STATUS_CHANGE",
        from_code,
        to_code,
        "HUMAN",
        user_id,
        None,
    )
    publish_status_changed(
        WorkitemStatusChanged("HUMAN", tenant_id, workitem_id, to_node_id, user_id)
    )
    await session.commit()
    return await _detail(session, workitem_id)


async def agent_transition(
    session: AsyncSession,
    workitem_id: int,
    to_status_code: str,
    tenant_id: int,
    agent_id: int,
) -> WorkitemView:
    """按状态编码沿迁移边流转。操作者记为数字员工。"""
    workitem = await _live_in_tenant(session, workitem_id, tenant_id)
    to_node = await session.scalar(
        select(StatusNode)
        .where(
            StatusNode.template_id == workitem.template_id,
            StatusNode.code == to_status_code,
        )
        .limit(1)
    )
    if to_node is None or workitem.status_node_id is None:
        raise BizError(ErrorCode.ILLEGAL_TRANSITION)
    from_node_id = workitem.status_node_id
    edge = await session.scalar(
        select(StatusTransition)
        .where(
            StatusTransition.template_id == workitem.template_id,
            StatusTransition.from_node_id == from_node_id,
            StatusTransition.to_node_id == to_node.id,
        )
        .limit(1)
    )
    if edge is None:
        raise BizError(ErrorCode.ILLEGAL_TRANSITION)
    from_node = await _find_node(session, from_node_id)
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {"status_node_id": to_node.id, "modifier_id": agent_id},
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    from_code = None
    if from_node is not None:
        from_code = from_node.code
    await _write_event(
        session,
        tenant_id,
        workitem_id,
        "STATUS_CHANGE",
        from_code,
        to_node.code,
        "AGENT",
        agent_id,
        None,
    )
    publish_status_changed(
        WorkitemStatusChanged("AGENT", tenant_id, workitem_id, to_node.id, agent_id)
    )
    await session.commit()
    return await _detail(session, workitem_id)


async def assign(
    session: AsyncSession,
    workitem_id: int,
    assignee_type: str | None,
    assignee_ref: int | None,
    sdlc_id: int | None,
    squad_id: int | None,
    scheduled_start_at: datetime | None,
    tenant_id: int,
    user_id: int,
) -> WorkitemView:
    """HTTP 指派。操作者是当前真人。"""
    return await assign_as(
        session,
        workitem_id,
        assignee_type,
        assignee_ref,
        sdlc_id,
        squad_id,
        scheduled_start_at,
        tenant_id,
        user_id,
        AssignmentActor("HUMAN", user_id, await _human_label(session, user_id)),
    )


async def assign_as(
    session: AsyncSession,
    workitem_id: int,
    assignee_type: str | None,
    assignee_ref: int | None,
    sdlc_id: int | None,
    squad_id: int | None,
    scheduled_start_at: datetime | None,
    tenant_id: int,
    modifier_user_id: int,
    actor: AssignmentActor | None,
) -> WorkitemView:
    """更换负责人、绑定流程，并在未延期时发布指派事件。"""
    workitem = await _live_in_tenant(session, workitem_id, tenant_id)
    same_assignee = (
        workitem.assignee_type == assignee_type and workitem.assignee_ref == assignee_ref
    )
    if same_assignee and scheduled_start_at is None:
        await session.commit()
        return await _detail(session, workitem_id)
    effective = actor
    if effective is None:
        effective = AssignmentActor("SYSTEM", 0, "系统")
    assign_event = None
    if not same_assignee:
        previous_type = workitem.assignee_type
        previous_ref = workitem.assignee_ref
        previous_version = workitem.version
        previous_sdlc_id = workitem.sdlc_id
        previous_step_id = workitem.current_step_id
        previous_work_type = workitem.work_type
        await _validate_squad(session, assignee_type, assignee_ref, squad_id, tenant_id)
        changed = await _cas(
            session,
            workitem_id,
            tenant_id,
            previous_version,
            {
                "assignee_type": assignee_type,
                "assignee_ref": assignee_ref,
                "modifier_id": modifier_user_id,
            },
            scheduled_set=False,
        )
        if changed == 0:
            raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
        from_val = None
        if previous_ref is not None:
            from_val = str(previous_ref)
        to_val = None
        if assignee_ref is not None:
            to_val = str(assignee_ref)
        assign_event = await _write_event(
            session,
            tenant_id,
            workitem_id,
            "ASSIGN",
            from_val,
            to_val,
            effective.type,
            effective.ref,
            assignment_detail(previous_type, assignee_type),
        )
        if (
            assignee_type == "AGENT"
            and assignee_ref is not None
            and previous_sdlc_id is None
            and previous_step_id is None
        ):
            effective_sdlc = sdlc_id
            if effective_sdlc is None:
                effective_sdlc = await _resolve_agent_sdlc(
                    session, assignee_ref, tenant_id, previous_work_type
                )
            await _bind_sdlc(
                session,
                workitem_id,
                tenant_id,
                effective_sdlc,
                previous_version + 1,
                modifier_user_id,
            )
        reloaded = await _reload(session, workitem_id)
        if (
            effective.is_human()
            and modifier_user_id != _SYSTEM_USER_ID
            and reloaded.assign_operator_id != modifier_user_id
        ):
            await _cas(
                session,
                workitem_id,
                tenant_id,
                reloaded.version,
                {
                    "assign_operator_id": modifier_user_id,
                    "modifier_id": modifier_user_id,
                },
                scheduled_set=False,
            )
            reloaded = await _reload(session, workitem_id)
    else:
        reloaded = workitem
    now = now_local()
    if scheduled_start_at is not None and assignee_type == "AGENT" and assignee_ref is not None:
        if is_after(scheduled_start_at, now):
            changed = await _cas(
                session,
                workitem_id,
                tenant_id,
                reloaded.version,
                {
                    "scheduled_start_at": scheduled_start_at,
                    "modifier_id": modifier_user_id,
                },
                scheduled_set=False,
            )
            if changed == 0:
                raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
            reloaded = await _reload(session, workitem_id)
        elif reloaded.scheduled_start_at is not None:
            changed = await _cas(
                session,
                workitem_id,
                tenant_id,
                reloaded.version,
                {"scheduled_start_at": None, "modifier_id": 0},
                scheduled_set=True,
            )
            if changed == 0:
                raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
            reloaded = await _reload(session, workitem_id)
    queued_dispatch = None
    if assignee_type == "AGENT" and assignee_ref is not None:
        planned = reloaded.scheduled_start_at
        deferred = planned is not None and is_after(planned, now_local())
        if deferred:
            logger.info(
                "Workitem %s agent delivery deferred until %s",
                workitem_id,
                reloaded.scheduled_start_at,
            )
        else:
            queued_dispatch = await on_workitem_assigned(
                session,
                WorkitemAssigned(
                    tenant_id,
                    workitem_id,
                    reloaded.current_step_id,
                    assignee_ref,
                    reloaded.version,
                    modifier_user_id,
                ),
            )
    if (
        assignee_type == "HUMAN"
        and assignee_ref is not None
        and assign_event is not None
        and assign_event.id is not None
        and not (effective.is_human() and effective.ref == assignee_ref)
    ):
        publish_human_assigned(
            WorkitemHumanAssigned(
                tenant_id,
                workitem_id,
                reloaded.title,
                assign_event.id,
                assignee_ref,
                effective.type,
                effective.ref,
                effective.display_name,
                request_id_or_none(),
            )
        )
    await session.commit()
    await drive_queued(queued_dispatch)
    return await _detail(session, workitem_id)


async def update_scheduled_start(
    session: AsyncSession,
    workitem_id: int,
    scheduled_start_at: datetime | None,
    execute_now: bool,
    tenant_id: int,
    user_id: int,
) -> WorkitemView:
    """改期、取消或立即触发。立即触发会打上实际触发时间。"""
    workitem = await _live_in_tenant(session, workitem_id, tenant_id)
    if execute_now or scheduled_start_at is None:
        values: dict[str, Any] = {"scheduled_start_at": None, "modifier_id": 0}
        if execute_now:
            values["scheduled_start_triggered_at"] = now_local()
        changed = await _cas(
            session,
            workitem_id,
            tenant_id,
            workitem.version,
            values,
            scheduled_set=True,
        )
        if changed == 0:
            fresh = await _find_live(session, workitem_id)
            if fresh is not None and fresh.scheduled_start_at is not None:
                raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
            if fresh is None:
                raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
            return await _render_detail(session, fresh)
        fresh = await _reload(session, workitem_id)
        queued_dispatch = None
        if execute_now and fresh.assignee_type == "AGENT" and fresh.assignee_ref is not None:
            queued_dispatch = await on_workitem_assigned(
                session,
                WorkitemAssigned(
                    tenant_id,
                    workitem_id,
                    fresh.current_step_id,
                    fresh.assignee_ref,
                    fresh.version,
                    user_id,
                ),
            )
        await session.commit()
        await drive_queued(queued_dispatch)
        return await _detail(session, workitem_id)
    if workitem.assignee_type != "AGENT" or workitem.assignee_ref is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    if not is_after(scheduled_start_at, now_local()):
        raise BizError(ErrorCode.PARAM_INVALID)
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {
            "scheduled_start_at": scheduled_start_at,
            "modifier_id": user_id,
        },
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    await session.commit()
    return await _detail(session, workitem_id)


async def update_tags(
    session: AsyncSession,
    workitem_id: int,
    tags: list[str | None] | None,
    tenant_id: int,
    user_id: int,
) -> WorkitemView:
    """整表替换标签。空列表写成 null。"""
    workitem = await _live_in_tenant(session, workitem_id, tenant_id)
    normalized = normalize_tags(tags)
    stored: list[str] | None = normalized
    if len(normalized) == 0:
        stored = None
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {"tags": stored, "modifier_id": user_id},
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    await session.commit()
    return await _detail(session, workitem_id)


async def update_content(
    session: AsyncSession,
    workitem_id: int,
    title: str | None,
    content_md: str | None,
    tenant_id: int,
    user_id: int,
) -> WorkitemView:
    """外部工单的标题和正文只由来源平台维护。未变化时不写版本。"""
    workitem = await _find_live(session, workitem_id)
    if workitem is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    links = await _links_for(session, tenant_id, workitem_id)
    if len(links) > 0:
        raise BizError(ErrorCode.WORKITEM_EXTERNAL_CONTENT_READ_ONLY)
    effective_title = workitem.title
    if title is not None:
        effective_title = title
    effective_content = workitem.content_md
    if content_md is not None:
        effective_content = content_md
    if workitem.title == effective_title and workitem.content_md == effective_content:
        return await _render_detail(session, workitem)
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {
            "title": effective_title,
            "content_md": effective_content,
            "modifier_id": user_id,
        },
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    await _write_event(session, tenant_id, workitem_id, "EDIT", None, None, "HUMAN", user_id, None)
    publish_content_updated(
        WorkitemContentUpdated(tenant_id, workitem_id, effective_title, effective_content, user_id)
    )
    await session.commit()
    return await _detail(session, workitem_id)


async def delete_workitem(
    session: AsyncSession,
    workitem_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """外部工单和仍在执行的工单不能删除。"""
    workitem = await _live_in_tenant(session, workitem_id, tenant_id)
    links = await _links_for(session, tenant_id, workitem_id)
    if len(links) > 0:
        raise BizError(ErrorCode.WORKITEM_EXTERNAL_NO_DELETE)
    dispatches = await _dispatches_for(session, tenant_id, workitem_id)
    for item in dispatches:
        if item.status in ACTIVE_DISPATCH_STATUSES:
            raise BizError(ErrorCode.WORKITEM_RUNNING_NO_DELETE)
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        workitem.version,
        {"is_deleted": 1, "modifier_id": user_id},
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)
    await _write_event(
        session, tenant_id, workitem_id, "DELETE", None, None, "HUMAN", user_id, None
    )
    await session.commit()


async def _decorate_page(
    session: AsyncSession,
    tenant_id: int,
    rows: list[Workitem],
) -> list[WorkitemView]:
    human_ids: set[int] = set()
    agent_ids: set[int] = set()
    node_ids: set[int] = set()
    sdlc_ids: set[int] = set()
    workitem_ids: list[int] = []
    for workitem in rows:
        workitem_ids.append(workitem.id)
        if workitem.assignee_type == "HUMAN" and workitem.assignee_ref is not None:
            human_ids.add(workitem.assignee_ref)
        if workitem.assignee_type == "AGENT" and workitem.assignee_ref is not None:
            agent_ids.add(workitem.assignee_ref)
        if workitem.creator_id is not None:
            human_ids.add(workitem.creator_id)
        if workitem.status_node_id is not None:
            node_ids.add(workitem.status_node_id)
        if workitem.sdlc_id is not None:
            sdlc_ids.add(workitem.sdlc_id)
    users = await _users_by_ids(session, human_ids)
    agents = await _agents_by_ids(session, tenant_id, agent_ids)
    nodes = await _nodes_by_ids(session, node_ids)
    sdlcs = await _sdlcs_by_ids(session, sdlc_ids)
    latest = await _latest_by_workitem(session, tenant_id, workitem_ids)
    links = await _links_by_workitem(session, tenant_id, workitem_ids)
    dispatches = await _dispatches_by_workitem(session, tenant_id, workitem_ids)
    now = now_local()
    now_ms = shanghai_millis(now)
    stuck = get_settings().workitem_stuck_threshold_ms
    views: list[WorkitemView] = []
    for workitem in rows:
        status_name, status_code, status_category = _node_fields(nodes, workitem.status_node_id)
        sdlc_name = None
        if workitem.sdlc_id is not None:
            sdlc = sdlcs.get(workitem.sdlc_id)
            if sdlc is not None:
                sdlc_name = sdlc.name
        assignee_name, assignee_display_name = _named(
            workitem.assignee_type, workitem.assignee_ref, users, agents
        )
        creator_name, creator_display_name = _named("HUMAN", workitem.creator_id, users, agents)
        row_links: list[ExternalWorkitemLink] = []
        grouped_links = links.get(workitem.id)
        if grouped_links is not None:
            row_links = grouped_links
        row_dispatches: list[Dispatch] = []
        grouped_dispatches = dispatches.get(workitem.id)
        if grouped_dispatches is not None:
            row_dispatches = grouped_dispatches
        views.append(
            render_workitem(
                workitem,
                status_name=status_name,
                status_code=status_code,
                status_category=status_category,
                sdlc_name=sdlc_name,
                assignee_name=assignee_name,
                assignee_display_name=assignee_display_name,
                creator_name=creator_name,
                creator_display_name=creator_display_name,
                latest=latest.get(workitem.id),
                links=row_links,
                dispatches=row_dispatches,
                now=now,
                now_ms=now_ms,
                stuck_threshold_ms=stuck,
                include_runtime=True,
                origin=None,
            )
        )
    return views


def _node_fields(
    nodes: dict[int, StatusNode],
    status_node_id: int | None,
) -> tuple[str | None, str | None, str | None]:
    if status_node_id is None:
        return None, None, None
    node = nodes.get(status_node_id)
    if node is None:
        return None, None, None
    return node.name, node.code, node.category


def _named(
    actor_type: str | None,
    ref: int | None,
    users: dict[int, User],
    agents: dict[int, Agent],
) -> tuple[str | None, str | None]:
    if ref is None:
        return None, None
    name = _name_from_maps(actor_type, ref, users, agents)
    return name, actor_display(name, ref)


def _name_from_maps(
    actor_type: str | None,
    ref: int,
    users: dict[int, User],
    agents: dict[int, Agent],
) -> str | None:
    if actor_type == "AGENT":
        agent = agents.get(ref)
        if agent is None:
            return None
        return agent.name
    if actor_type == "HUMAN":
        user = users.get(ref)
        if user is None:
            return None
        return person_name(user.nickname, user.username)
    return None


async def _detail(session: AsyncSession, workitem_id: int) -> WorkitemView:
    workitem = await _find_live(session, workitem_id)
    if workitem is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return await _render_detail(session, workitem)


async def _render_detail(session: AsyncSession, workitem: Workitem) -> WorkitemView:
    status_name = None
    status_code = None
    status_category = None
    if workitem.status_node_id is not None:
        node = await _find_node(session, workitem.status_node_id)
        if node is not None:
            status_name = node.name
            status_code = node.code
            status_category = node.category
    sdlc_name = None
    if workitem.sdlc_id is not None:
        sdlc = await _find_sdlc(session, workitem.sdlc_id)
        if sdlc is not None:
            sdlc_name = sdlc.name
    assignee_name = None
    assignee_display_name = None
    if workitem.assignee_ref is not None:
        assignee_name = await _actor_name(session, workitem.assignee_type, workitem.assignee_ref)
        assignee_display_name = actor_display(assignee_name, workitem.assignee_ref)
    creator_name = await _actor_name(session, "HUMAN", workitem.creator_id)
    creator_display_name = actor_display(creator_name, workitem.creator_id)
    links: list[ExternalWorkitemLink] = []
    dispatches: list[Dispatch] = []
    if workitem.tenant_id is not None:
        links = await _links_for(session, workitem.tenant_id, workitem.id)
        dispatches = await _dispatches_for(session, workitem.tenant_id, workitem.id)
    now = now_local()
    return render_workitem(
        workitem,
        status_name=status_name,
        status_code=status_code,
        status_category=status_category,
        sdlc_name=sdlc_name,
        assignee_name=assignee_name,
        assignee_display_name=assignee_display_name,
        creator_name=creator_name,
        creator_display_name=creator_display_name,
        latest=None,
        links=links,
        dispatches=dispatches,
        now=now,
        now_ms=shanghai_millis(now),
        stuck_threshold_ms=get_settings().workitem_stuck_threshold_ms,
        include_runtime=False,
        origin=await _origin(session, workitem),
    )


async def _origin(session: AsyncSession, workitem: Workitem) -> Any:
    if workitem.origin_type is None or workitem.origin_id is None:
        return None
    from autowonder.workitems.schemas import WorkitemOriginView

    if workitem.origin_type != "SCHEDULED_TASK_RUN":
        return WorkitemOriginView(type=workitem.origin_type, id=workitem.origin_id)
    run = await session.scalar(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == workitem.tenant_id,
            ScheduledTaskRun.id == workitem.origin_id,
        )
        .limit(1)
    )
    if run is None or run.workspace_id != workitem.tenant_id:
        return WorkitemOriginView(type=workitem.origin_type, id=workitem.origin_id)
    task = await session.scalar(
        select(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == workitem.tenant_id,
            ScheduledTask.id == run.scheduled_task_id,
            ScheduledTask.is_deleted == 0,
        )
        .limit(1)
    )
    if task is None or task.workspace_id != workitem.tenant_id:
        return WorkitemOriginView(type=workitem.origin_type, id=workitem.origin_id)
    return WorkitemOriginView(
        type=workitem.origin_type,
        id=workitem.origin_id,
        scheduled_task_id=task.id,
        scheduled_task_name=task.name,
    )


async def rebind_for_interaction_rework(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    target_agent_id: int,
    target_sdlc_id: int,
    target_step_id: int,
    user_id: int,
) -> None:
    """把工单当前步骤和负责人改到评论触发的正式流程。不发布指派交付事件。"""
    attempt = 0
    while attempt < 3:
        current = await _find_live(session, workitem_id)
        if current is None or current.tenant_id != tenant_id:
            raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
        route_matches = (
            current.sdlc_id == target_sdlc_id and current.current_step_id == target_step_id
        )
        if not route_matches:
            changed = await _cas(
                session,
                workitem_id,
                tenant_id,
                current.version,
                {
                    "sdlc_id": target_sdlc_id,
                    "current_step_id": target_step_id,
                    "modifier_id": user_id,
                },
                scheduled_set=False,
            )
            if changed == 0:
                attempt += 1
                continue
            current = await _reload(session, workitem_id)
        if current.assignee_type == "AGENT" and current.assignee_ref == target_agent_id:
            return
        from_ref = current.assignee_ref
        from_type = current.assignee_type
        changed = await _cas(
            session,
            workitem_id,
            tenant_id,
            current.version,
            {
                "assignee_type": "AGENT",
                "assignee_ref": target_agent_id,
                "modifier_id": user_id,
            },
            scheduled_set=False,
        )
        if changed == 0:
            attempt += 1
            continue
        from_text = None
        if from_ref is not None:
            from_text = str(from_ref)
        await _write_event(
            session,
            tenant_id,
            workitem_id,
            "ASSIGN",
            from_text,
            str(target_agent_id),
            "HUMAN",
            user_id,
            assignment_detail(from_type, "AGENT"),
        )
        return
    raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)


async def _live_in_tenant(session: AsyncSession, workitem_id: int, tenant_id: int) -> Workitem:
    workitem = await _find_live(session, workitem_id)
    if workitem is None or workitem.tenant_id != tenant_id:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return workitem


async def _find_live(session: AsyncSession, workitem_id: int) -> Workitem | None:
    return await session.scalar(
        select(Workitem).where(Workitem.id == workitem_id, Workitem.is_deleted == 0).limit(1)
    )


async def _reload(session: AsyncSession, workitem_id: int) -> Workitem:
    session.expire_all()
    workitem = await _find_live(session, workitem_id)
    if workitem is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return workitem


async def _cas(
    session: AsyncSession,
    workitem_id: int,
    tenant_id: int,
    version: int,
    values: dict[str, Any],
    *,
    scheduled_set: bool,
) -> int:
    statement = update(Workitem).where(
        Workitem.id == workitem_id,
        Workitem.tenant_id == tenant_id,
        Workitem.version == version,
        Workitem.is_deleted == 0,
    )
    if scheduled_set:
        statement = statement.where(Workitem.scheduled_start_at.is_not(None))
    result = await session.execute(statement.values(version=Workitem.version + 1, **values))
    session.expire_all()
    return rowcount(result)


async def _write_event(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    event_type: str,
    from_val: str | None,
    to_val: str | None,
    actor_type: str,
    actor_ref: int,
    detail: dict[str, str] | None,
) -> WorkitemEvent:
    event = WorkitemEvent(
        tenant_id=tenant_id,
        workitem_id=workitem_id,
        event_type=event_type,
        from_val=from_val,
        to_val=to_val,
        actor_type=actor_type,
        actor_ref=actor_ref,
        detail_json=detail,
    )
    session.add(event)
    await session.flush()
    return event


async def _default_template(session: AsyncSession, work_type: str) -> StatusTemplate | None:
    return await session.scalar(
        select(StatusTemplate)
        .where(
            StatusTemplate.work_type == work_type,
            StatusTemplate.is_default == 1,
            StatusTemplate.is_deleted == 0,
        )
        .limit(1)
    )


async def _init_node(session: AsyncSession, template_id: int) -> StatusNode | None:
    return await session.scalar(
        select(StatusNode)
        .where(StatusNode.template_id == template_id, StatusNode.category == "INIT")
        .limit(1)
    )


async def _find_node(session: AsyncSession, node_id: int) -> StatusNode | None:
    return await session.scalar(select(StatusNode).where(StatusNode.id == node_id).limit(1))


async def _find_sdlc(session: AsyncSession, sdlc_id: int) -> Sdlc | None:
    return await session.scalar(
        select(Sdlc).where(Sdlc.id == sdlc_id, Sdlc.is_deleted == 0).limit(1)
    )


async def _find_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(
        select(User).where(User.id == user_id, User.is_deleted == 0).limit(1)
    )


async def _find_agent(session: AsyncSession, agent_id: int) -> Agent | None:
    return await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted == 0).limit(1)
    )


async def _actor_name(
    session: AsyncSession,
    actor_type: str | None,
    ref: int | None,
) -> str | None:
    if ref is None:
        return None
    if actor_type == "AGENT":
        agent = await _find_agent(session, ref)
        if agent is None:
            return None
        return agent.name
    if actor_type == "HUMAN":
        user = await _find_user(session, ref)
        if user is None:
            return None
        return person_name(user.nickname, user.username)
    return None


async def _human_label(session: AsyncSession, user_id: int) -> str:
    name = await _actor_name(session, "HUMAN", user_id)
    if name is None or java_is_blank(name):
        return "用户"
    return name


async def _validate_squad(
    session: AsyncSession,
    assignee_type: str | None,
    assignee_ref: int | None,
    squad_id: int | None,
    tenant_id: int,
) -> None:
    if assignee_type != "AGENT" or assignee_ref is None or squad_id is None:
        return
    member = await session.scalar(
        select(SquadMember)
        .where(SquadMember.squad_id == squad_id, SquadMember.agent_id == assignee_ref)
        .limit(1)
    )
    if member is None or member.tenant_id != tenant_id:
        raise BizError(ErrorCode.SQUAD_NOT_FOUND)


async def _resolve_agent_sdlc(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    work_type: str | None,
) -> int:
    agent = await _find_agent(session, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        raise BizError(ErrorCode.AGENT_NOT_FOUND)
    sdlc_id = await _version_sdlc(session, agent, tenant_id)
    if sdlc_id is not None:
        return sdlc_id
    if work_type is None or java_is_blank(work_type):
        raise BizError(ErrorCode.SDLC_NOT_FOUND)
    fallback = await session.scalar(
        select(Sdlc)
        .where(
            Sdlc.work_type == work_type,
            Sdlc.is_default == 1,
            Sdlc.status == "ENABLED",
            Sdlc.is_deleted == 0,
        )
        .limit(1)
    )
    if fallback is not None and fallback.tenant_id == tenant_id:
        return fallback.id
    raise BizError(ErrorCode.SDLC_NOT_FOUND)


async def _version_sdlc(session: AsyncSession, agent: Agent, tenant_id: int) -> int | None:
    if agent.online_version_id is not None:
        online = await session.scalar(
            select(AgentVersion)
            .where(AgentVersion.id == agent.online_version_id, AgentVersion.is_deleted == 0)
            .limit(1)
        )
        if online is not None and online.tenant_id == tenant_id and online.agent_id == agent.id:
            return online.sdlc_id
        return None
    result = await session.execute(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent.id,
            AgentVersion.is_deleted == 0,
        )
    )
    versions = list(result.scalars().all())
    versions.sort(key=lambda item: item.version_no, reverse=True)
    for version in versions:
        if version.tenant_id == tenant_id and version.sdlc_id is not None:
            return version.sdlc_id
    return None


async def _bind_sdlc(
    session: AsyncSession,
    workitem_id: int,
    tenant_id: int,
    sdlc_id: int,
    version: int,
    user_id: int,
) -> None:
    sdlc = await _find_sdlc(session, sdlc_id)
    if sdlc is None or sdlc.tenant_id != tenant_id:
        raise BizError(ErrorCode.SDLC_NOT_FOUND)
    result = await session.execute(
        select(SdlcStep).where(SdlcStep.sdlc_id == sdlc_id, SdlcStep.is_deleted == 0)
    )
    chosen: SdlcStep | None = None
    for step in result.scalars().all():
        if step.tenant_id != tenant_id:
            continue
        if chosen is None or step.step_order < chosen.step_order:
            chosen = step
    if chosen is None:
        raise BizError(ErrorCode.SDLC_STEP_NOT_FOUND)
    changed = await _cas(
        session,
        workitem_id,
        tenant_id,
        version,
        {
            "sdlc_id": sdlc_id,
            "current_step_id": chosen.id,
            "modifier_id": user_id,
        },
        scheduled_set=False,
    )
    if changed == 0:
        raise BizError(ErrorCode.WORKITEM_VERSION_CONFLICT)


async def _links_for(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
) -> list[ExternalWorkitemLink]:
    result = await session.execute(
        select(ExternalWorkitemLink)
        .where(
            ExternalWorkitemLink.tenant_id == tenant_id,
            ExternalWorkitemLink.workitem_id == workitem_id,
        )
        .order_by(ExternalWorkitemLink.gmt_create.asc(), ExternalWorkitemLink.id.asc())
    )
    return list(result.scalars().all())


async def _dispatches_for(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
) -> list[Dispatch]:
    result = await session.execute(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id == workitem_id,
            Dispatch.is_deleted == 0,
        )
        .order_by(Dispatch.gmt_create.asc())
    )
    return list(result.scalars().all())


async def _users_by_ids(session: AsyncSession, ids: set[int]) -> dict[int, User]:
    if len(ids) == 0:
        return {}
    result = await session.execute(select(User).where(User.is_deleted == 0, User.id.in_(ids)))
    mapped: dict[int, User] = {}
    for user in result.scalars().all():
        if user.id not in mapped:
            mapped[user.id] = user
    return mapped


async def _agents_by_ids(session: AsyncSession, tenant_id: int, ids: set[int]) -> dict[int, Agent]:
    if len(ids) == 0:
        return {}
    result = await session.execute(
        select(Agent).where(
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
            Agent.id.in_(ids),
        )
    )
    mapped: dict[int, Agent] = {}
    for agent in result.scalars().all():
        if agent.id not in mapped:
            mapped[agent.id] = agent
    return mapped


async def _nodes_by_ids(session: AsyncSession, ids: set[int]) -> dict[int, StatusNode]:
    if len(ids) == 0:
        return {}
    result = await session.execute(select(StatusNode).where(StatusNode.id.in_(ids)))
    mapped: dict[int, StatusNode] = {}
    for node in result.scalars().all():
        if node.id not in mapped:
            mapped[node.id] = node
    return mapped


async def _sdlcs_by_ids(session: AsyncSession, ids: set[int]) -> dict[int, Sdlc]:
    if len(ids) == 0:
        return {}
    result = await session.execute(select(Sdlc).where(Sdlc.is_deleted == 0, Sdlc.id.in_(ids)))
    mapped: dict[int, Sdlc] = {}
    for sdlc in result.scalars().all():
        if sdlc.id not in mapped:
            mapped[sdlc.id] = sdlc
    return mapped


async def _latest_by_workitem(
    session: AsyncSession,
    tenant_id: int,
    workitem_ids: list[int],
) -> dict[int, Dispatch]:
    if len(workitem_ids) == 0:
        return {}
    latest = (
        select(func.max(Dispatch.id).label("latest_id"))
        .where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.is_deleted == 0,
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id.in_(workitem_ids),
        )
        .group_by(Dispatch.workitem_id)
        .subquery()
    )
    result = await session.execute(
        select(Dispatch).where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.is_deleted == 0,
            Dispatch.id.in_(select(latest.c.latest_id)),
        )
    )
    mapped: dict[int, Dispatch] = {}
    for row in result.scalars().all():
        mapped[row.workitem_id] = row
    return mapped


async def _links_by_workitem(
    session: AsyncSession,
    tenant_id: int,
    workitem_ids: list[int],
) -> dict[int, list[ExternalWorkitemLink]]:
    if len(workitem_ids) == 0:
        return {}
    result = await session.execute(
        select(ExternalWorkitemLink)
        .where(
            ExternalWorkitemLink.tenant_id == tenant_id,
            ExternalWorkitemLink.workitem_id.in_(workitem_ids),
        )
        .order_by(ExternalWorkitemLink.gmt_create.asc(), ExternalWorkitemLink.id.asc())
    )
    grouped: dict[int, list[ExternalWorkitemLink]] = {}
    for link in result.scalars().all():
        bucket = grouped.get(link.workitem_id)
        if bucket is None:
            bucket = []
            grouped[link.workitem_id] = bucket
        bucket.append(link)
    return grouped


async def _dispatches_by_workitem(
    session: AsyncSession,
    tenant_id: int,
    workitem_ids: list[int],
) -> dict[int, list[Dispatch]]:
    if len(workitem_ids) == 0:
        return {}
    result = await session.execute(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.is_deleted == 0,
            Dispatch.workitem_id.in_(workitem_ids),
        )
        .order_by(Dispatch.gmt_create.asc())
    )
    grouped: dict[int, list[Dispatch]] = {}
    for item in result.scalars().all():
        bucket = grouped.get(item.workitem_id)
        if bucket is None:
            bucket = []
            grouped[item.workitem_id] = bucket
        bucket.append(item)
    return grouped
