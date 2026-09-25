"""把工单行收成与 ``WorkitemVO`` 相同的字段。"""

from datetime import datetime

from autowonder.core.clock import SHANGHAI
from autowonder.core.errors import ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.models import Dispatch
from autowonder.integrations.models import ExternalWorkitemLink
from autowonder.workitems.models import Workitem
from autowonder.workitems.rules import (
    ACTIVE_DISPATCH_STATUSES,
    contains_done_token,
    evaluate_health,
    is_done_node,
    parse_tags,
    pending_decision,
    scheduled_phase,
)
from autowonder.workitems.schemas import (
    ExternalCollaborationView,
    ExternalPrincipalView,
    WorkitemOriginView,
    WorkitemView,
)


def person_name(nickname: str | None, username: str | None) -> str | None:
    """昵称优先，否则登录名。"""
    if nickname is not None and not java_is_blank(nickname):
        return nickname
    return username


def actor_display(name: str | None, ref: int | None) -> str | None:
    """没有名字时退回数字 id，并带上括号里的 id。"""
    if ref is None:
        return name
    if name is None or java_is_blank(name):
        return str(ref)
    return name + "(" + str(ref) + ")"


def shanghai_millis(value: datetime) -> int:
    """naive 时间按上海本地钟换成毫秒。"""
    aware = value
    if value.tzinfo is None:
        aware = value.replace(tzinfo=SHANGHAI)
    else:
        aware = value.astimezone(SHANGHAI)
    return int(aware.timestamp() * 1000)


def render_workitem(
    workitem: Workitem,
    *,
    status_name: str | None,
    status_code: str | None,
    status_category: str | None,
    sdlc_name: str | None,
    assignee_name: str | None,
    assignee_display_name: str | None,
    creator_name: str | None,
    creator_display_name: str | None,
    latest: Dispatch | None,
    links: list[ExternalWorkitemLink],
    dispatches: list[Dispatch],
    now: datetime,
    now_ms: int,
    stuck_threshold_ms: int,
    include_runtime: bool,
    origin: WorkitemOriginView | None,
    external_collaboration: ExternalCollaborationView | None = None,
    source_creator: ExternalPrincipalView | None = None,
) -> WorkitemView:
    """详情带协作快照；列表带来源创建者、健康和定时阶段。"""
    source_type, deletable, deletable_reason, provider, url = _eligibility(
        workitem, links, dispatches
    )
    health: str | None = None
    health_reason: str | None = None
    execution_status: str | None = None
    phase: str | None = None
    decision = False
    source_provider: str | None = None
    source_url: str | None = None
    shown_collaboration: ExternalCollaborationView | None = None
    shown_creator: ExternalPrincipalView | None = None
    if include_runtime:
        if latest is not None:
            execution_status = latest.status
        health, health_reason = _health(
            workitem.status_node_id,
            status_category,
            latest,
            now_ms,
            stuck_threshold_ms,
        )
        done = _done(status_category, status_code, status_name)
        decision = pending_decision(workitem.assignee_type, execution_status, done)
        phase = scheduled_phase(
            workitem.scheduled_start_at,
            workitem.scheduled_start_triggered_at,
            done,
            execution_status,
            now,
        )
        if source_type == "EXTERNAL":
            source_provider = provider
            source_url = url
            shown_creator = source_creator
    else:
        shown_collaboration = external_collaboration
    return WorkitemView(
        id=workitem.id,
        work_type=workitem.work_type,
        title=workitem.title,
        execution_status=execution_status,
        content_md=workitem.content_md,
        template_id=workitem.template_id,
        status_node_id=workitem.status_node_id,
        status_name=status_name,
        sdlc_id=workitem.sdlc_id,
        sdlc_name=sdlc_name,
        assignee_type=workitem.assignee_type,
        assignee_ref=workitem.assignee_ref,
        assignee_name=assignee_name,
        assignee_display_name=assignee_display_name,
        creator_id=workitem.creator_id,
        creator_name=creator_name,
        creator_display_name=creator_display_name,
        priority=workitem.priority,
        version=workitem.version,
        gmt_create=workitem.gmt_create,
        gmt_modified=workitem.gmt_modified,
        health=health,
        health_reason=health_reason,
        pending_decision=decision,
        source_type=source_type,
        source_provider=source_provider,
        source_url=source_url,
        deletable=deletable,
        deletable_reason=deletable_reason,
        origin=origin,
        external_collaboration=shown_collaboration,
        source_creator=shown_creator,
        scheduled_start_at=workitem.scheduled_start_at,
        scheduled_start_triggered_at=workitem.scheduled_start_triggered_at,
        scheduled_phase=phase,
        tags=parse_tags(workitem.tags),
    )


def _done(category: str | None, code: str | None, status_name: str | None) -> bool:
    if is_done_node(category, code, status_name):
        return True
    return contains_done_token(status_name)


def _health(
    status_node_id: int | None,
    category: str | None,
    latest: Dispatch | None,
    now_ms: int,
    stuck_threshold_ms: int,
) -> tuple[str, str | None]:
    if latest is None or status_node_id is None:
        return "OK", None
    modified_ms = None
    if latest.gmt_modified is not None:
        modified_ms = shanghai_millis(latest.gmt_modified)
    return evaluate_health(
        category,
        latest.status,
        modified_ms,
        now_ms,
        stuck_threshold_ms,
    )


def _eligibility(
    workitem: Workitem,
    links: list[ExternalWorkitemLink],
    dispatches: list[Dispatch],
) -> tuple[str, bool, str | None, str | None, str | None]:
    present = [link for link in links if link is not None]
    if len(present) > 0:
        preferred = prefer_link(present)
        provider = None
        url = None
        if preferred is not None:
            provider = preferred.provider
            url = preferred.external_url
        return "EXTERNAL", False, ErrorCode.WORKITEM_EXTERNAL_NO_DELETE.message, provider, url
    for item in dispatches:
        if item is not None and item.status in ACTIVE_DISPATCH_STATUSES:
            return "NATIVE", False, ErrorCode.WORKITEM_RUNNING_NO_DELETE.message, None, None
    return "NATIVE", True, None, None, None


def prefer_link(links: list[ExternalWorkitemLink]) -> ExternalWorkitemLink | None:
    """AONE 优先，其次更早创建、更小 id。"""
    best: ExternalWorkitemLink | None = None
    best_key: tuple[int, int, datetime, int, int] | None = None
    for link in links:
        aone = 1
        if link.provider == "AONE":
            aone = 0
        created_missing = 1
        created = datetime.max
        if link.gmt_create is not None:
            created_missing = 0
            created = link.gmt_create
        ident_missing = 1
        ident = 0
        if link.id is not None:
            ident_missing = 0
            ident = link.id
        key = (aone, created_missing, created, ident_missing, ident)
        if best_key is None:
            best = link
            best_key = key
        elif key < best_key:
            best = link
            best_key = key
    return best
