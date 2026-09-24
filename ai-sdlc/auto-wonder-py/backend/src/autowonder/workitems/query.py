"""工单列表 SQL。源感知调度条件与 Java ``autowonder-source-aware`` 一致。"""

from sqlalchemy import ColumnElement, Select, and_, exists, func, not_, or_, select

from autowonder.dispatch.models import Dispatch
from autowonder.statemachines.models import StatusNode
from autowonder.workitems.models import Workitem, WorkitemWatcher
from autowonder.workitems.rules import (
    DECISION_PLAIN,
    DECISION_UPPER,
    NAME_DONE_PLAIN,
    NAME_DONE_UPPER,
    NOT_DONE_CODE_UPPER,
    NOT_DONE_NAME_PLAIN,
    NOT_DONE_NAME_UPPER,
    PROGRESS_PLAIN,
    PROGRESS_UPPER,
)


def list_statement(
    tenant_id: int,
    work_type: str | None,
    status_node_id: int | None,
    status_category: str | None,
    assignee_type: str | None,
    assignee_ref: int | None,
    pending_decision_only: bool,
    mine_scope: str | None,
    current_user_id: int,
    keyword: str | None,
    keyword_as_id: int | None,
    tag: str | None,
    scheduled_start: str | None,
    offset: int,
    limit: int,
) -> Select[tuple[Workitem]]:
    """按创建时间倒序列出一页工单。"""
    statement = (
        select(Workitem)
        .where(
            *_filters(
                tenant_id,
                work_type,
                status_node_id,
                status_category,
                assignee_type,
                assignee_ref,
                pending_decision_only,
                mine_scope,
                current_user_id,
                keyword,
                keyword_as_id,
                tag,
                scheduled_start,
            )
        )
        .order_by(Workitem.gmt_create.desc(), Workitem.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return statement


def count_statement(
    tenant_id: int,
    work_type: str | None,
    status_node_id: int | None,
    status_category: str | None,
    assignee_type: str | None,
    assignee_ref: int | None,
    pending_decision_only: bool,
    mine_scope: str | None,
    current_user_id: int,
    keyword: str | None,
    keyword_as_id: int | None,
    tag: str | None,
    scheduled_start: str | None,
) -> Select[tuple[int]]:
    """同一套过滤条件下的总数。"""
    return (
        select(func.count())
        .select_from(Workitem)
        .where(
            *_filters(
                tenant_id,
                work_type,
                status_node_id,
                status_category,
                assignee_type,
                assignee_ref,
                pending_decision_only,
                mine_scope,
                current_user_id,
                keyword,
                keyword_as_id,
                tag,
                scheduled_start,
            )
        )
    )


def _filters(
    tenant_id: int,
    work_type: str | None,
    status_node_id: int | None,
    status_category: str | None,
    assignee_type: str | None,
    assignee_ref: int | None,
    pending_decision_only: bool,
    mine_scope: str | None,
    current_user_id: int,
    keyword: str | None,
    keyword_as_id: int | None,
    tag: str | None,
    scheduled_start: str | None,
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [
        Workitem.tenant_id == tenant_id,
        Workitem.is_deleted == 0,
    ]
    if work_type is not None:
        clauses.append(Workitem.work_type == work_type)
    if status_node_id is not None:
        clauses.append(Workitem.status_node_id == status_node_id)
    if assignee_type is not None:
        clauses.append(Workitem.assignee_type == assignee_type)
    if assignee_ref is not None:
        clauses.append(Workitem.assignee_ref == assignee_ref)
    if pending_decision_only:
        clauses.append(Workitem.assignee_type == "HUMAN")
        clauses.append(Workitem.assignee_ref == current_user_id)
        clauses.append(_latest_succeeded())
        clauses.append(~_not_done_node_hit())
    if mine_scope == "CREATED":
        clauses.append(Workitem.creator_id == current_user_id)
    if mine_scope == "ASSIGNED":
        clauses.append(Workitem.assignee_type == "HUMAN")
        clauses.append(Workitem.assignee_ref == current_user_id)
    if mine_scope == "WATCHED":
        clauses.append(_watched_by(current_user_id))
    if keyword is not None:
        title_match = Workitem.title.like("%" + keyword + "%")
        if keyword_as_id is None:
            clauses.append(title_match)
        else:
            clauses.append(or_(title_match, Workitem.id == keyword_as_id))
    if status_category == "DONE":
        clauses.append(_name_done())
    if status_category == "PENDING_DECISION":
        clauses.append(~_name_done())
        clauses.append(_status_pending_decision())
    if status_category == "IN_PROGRESS":
        clauses.append(~_name_done())
        clauses.append(~_status_pending_decision())
        clauses.append(_name_in_progress())
    if status_category == "NEW":
        clauses.append(~_name_done())
        clauses.append(~_status_pending_decision())
        clauses.append(~_name_in_progress())
    if tag is not None:
        clauses.append(func.json_contains(Workitem.tags, func.json_quote(tag)))
    if scheduled_start == "ALL":
        clauses.append(
            or_(
                Workitem.scheduled_start_at.is_not(None),
                Workitem.scheduled_start_triggered_at.is_not(None),
            )
        )
    if scheduled_start == "PENDING":
        clauses.append(Workitem.scheduled_start_at.is_not(None))
    if scheduled_start == "TRIGGERED":
        clauses.append(Workitem.scheduled_start_triggered_at.is_not(None))
    return clauses


def _latest_id() -> ColumnElement[int]:
    return (
        select(func.max(Dispatch.id))
        .where(
            Dispatch.tenant_id == Workitem.tenant_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id == Workitem.id,
            Dispatch.is_deleted == 0,
        )
        .correlate(Workitem)
        .scalar_subquery()
    )


def _latest_succeeded() -> ColumnElement[bool]:
    return exists(
        select(Dispatch.id).where(
            Dispatch.id == _latest_id(),
            Dispatch.source_type == "WORKITEM",
            Dispatch.status == "SUCCEEDED",
            Dispatch.is_deleted == 0,
        )
    )


def _not_done_node_hit() -> ColumnElement[bool]:
    code = func.upper(func.coalesce(StatusNode.code, ""))
    name = func.coalesce(StatusNode.name, "")
    upper_name = func.upper(name)
    pieces: list[ColumnElement[bool]] = [
        func.upper(func.coalesce(StatusNode.category, "")) == "DONE",
    ]
    for token in NOT_DONE_CODE_UPPER:
        pieces.append(code.like("%" + token + "%"))
    for token in NOT_DONE_NAME_PLAIN:
        pieces.append(name.like("%" + token + "%"))
    for token in NOT_DONE_NAME_UPPER:
        pieces.append(upper_name.like("%" + token + "%"))
    return exists(
        select(StatusNode.id).where(StatusNode.id == Workitem.status_node_id, or_(*pieces))
    )


def _name_done() -> ColumnElement[bool]:
    return _name_exists(NAME_DONE_PLAIN, NAME_DONE_UPPER)


def _name_in_progress() -> ColumnElement[bool]:
    return _name_exists(PROGRESS_PLAIN, PROGRESS_UPPER)


def _name_decision() -> ColumnElement[bool]:
    return _name_exists(DECISION_PLAIN, DECISION_UPPER)


def _name_exists(plain: tuple[str, ...], upper: tuple[str, ...]) -> ColumnElement[bool]:
    pieces: list[ColumnElement[bool]] = []
    for token in plain:
        pieces.append(StatusNode.name.like("%" + token + "%"))
    folded = func.upper(StatusNode.name)
    for token in upper:
        pieces.append(folded.like("%" + token + "%"))
    return exists(
        select(StatusNode.id).where(StatusNode.id == Workitem.status_node_id, or_(*pieces))
    )


def _status_pending_decision() -> ColumnElement[bool]:
    handoff = and_(
        Workitem.assignee_type == "HUMAN",
        _latest_succeeded(),
        not_(_not_done_node_hit()),
    )
    return or_(handoff, _name_decision())


def _watched_by(user_id: int) -> ColumnElement[bool]:
    return exists(
        select(WorkitemWatcher.id).where(
            WorkitemWatcher.tenant_id == Workitem.tenant_id,
            WorkitemWatcher.workitem_id == Workitem.id,
            WorkitemWatcher.user_id == user_id,
        )
    )
