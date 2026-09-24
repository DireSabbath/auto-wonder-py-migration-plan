"""工单创建、流转、指派和派生字段。这些检查不连接 MySQL。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList, Null

from autowonder.agents.models import Agent, AgentVersion
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.dispatch.assignment import on_workitem_assigned
from autowonder.dispatch.models import Dispatch
from autowonder.integrations.models import ExternalWorkitemLink
from autowonder.main import create_app
from autowonder.sdlcs.models import Sdlc, SdlcStep
from autowonder.squads.models import SquadMember
from autowonder.statemachines.models import StatusNode, StatusTemplate, StatusTransition
from autowonder.users.models import User
from autowonder.workitems.events import WorkitemAssigned
from autowonder.workitems.models import Workitem
from autowonder.workitems.query import list_statement
from autowonder.workitems.rules import (
    assignment_detail,
    evaluate_health,
    keyword_id,
    normalize_status_category,
    normalize_tags,
    page_bounds,
    parse_tags,
    scheduled_phase,
)
from autowonder.workitems.schemas import CreateWorkitemRequest, parse_java_date
from autowonder.workitems.service import (
    assign,
    create,
    delete_workitem,
    transition,
    update_content,
    update_tags,
)
from autowonder.workitems.view import render_workitem, shanghai_millis


class MemorySession:
    """按等值条件回放内存行，供工单写路径使用。"""

    def __init__(self) -> None:
        self.rows: list[object] = []
        self._pending: list[object] = []
        self._next_id = 1
        self.commits = 0

    def add(self, row: object) -> None:
        self._pending.append(row)

    async def flush(self) -> None:
        while len(self._pending) > 0:
            row = self._pending.pop(0)
            _apply_defaults(row)
            if getattr(row, "id", None) is None:
                row.id = self._next_id
                self._next_id += 1
            if row not in self.rows:
                self.rows.append(row)

    async def commit(self) -> None:
        await self.flush()
        self.commits += 1

    def expire_all(self) -> None:
        return None

    async def scalar(self, statement: object) -> object:
        rows = self._select(statement)
        if len(rows) == 0:
            return None
        return rows[0]

    async def execute(self, statement: object) -> object:
        if statement.__class__.__name__ == "Update":
            return _Result([], self._update(statement))
        return _Result(self._select(statement), None)

    async def scalars(self, statement: object) -> object:
        return _Result(self._select(statement), None)

    async def delete(self, row: object) -> None:
        if row in self._pending:
            self._pending.remove(row)
        if row in self.rows:
            self.rows.remove(row)

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        yield


class _Result:
    def __init__(self, rows: list[object], rowcount: int | None) -> None:
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[object]:
        return list(self._rows)


def _apply_defaults(row: object) -> None:
    mapper = getattr(row, "__mapper__", None)
    if mapper is None:
        return
    for prop in mapper.column_attrs:
        if getattr(row, prop.key) is not None:
            continue
        column = prop.columns[0]
        default = column.default
        if default is None:
            continue
        if getattr(default, "is_callable", False):
            arg = default.arg
            code = getattr(arg, "__code__", None)
            if code is not None and code.co_argcount == 0:
                setattr(row, prop.key, arg())
            elif code is not None and code.co_argcount == 1:
                setattr(row, prop.key, arg(None))
            continue
        if getattr(default, "is_scalar", False):
            setattr(row, prop.key, default.arg)


def _select(self: MemorySession, statement: object) -> list[object]:
    entity = statement.column_descriptions[0]["entity"]
    rows = [row for row in self.rows if isinstance(row, entity)]
    matched = [row for row in rows if _matches(row, _comparisons(statement.whereclause))]
    limit = _limit(statement)
    if limit is not None:
        return matched[:limit]
    return matched


MemorySession._select = _select  # type: ignore[method-assign]


def _update(self: MemorySession, statement: object) -> int:
    entity = statement.entity_description["entity"]
    comps = _comparisons(statement.whereclause)
    count = 0
    for row in self.rows:
        if not isinstance(row, entity) or not _matches(row, comps):
            continue
        for column, clause in statement._values.items():
            setattr(row, column.key, _assigned(row, column.key, clause))
        count += 1
    return count


MemorySession._update = _update  # type: ignore[method-assign]


def _assigned(row: object, key: str, clause: object) -> object:
    if isinstance(clause, BindParameter):
        return clause.value
    left = getattr(clause, "left", None)
    right = getattr(clause, "right", None)
    operator = getattr(getattr(clause, "operator", None), "__name__", "")
    same_column = getattr(left, "key", None) == key
    if operator == "add" and same_column and isinstance(right, BindParameter):
        return getattr(row, key) + right.value
    raise AssertionError(repr(clause))


def _comparisons(clause: object) -> list[tuple[str, str, object]]:
    found: list[tuple[str, str, object]] = []
    _walk(clause, found)
    return found


def _walk(node: object, found: list[tuple[str, str, object]]) -> None:
    if node is None:
        return
    if isinstance(node, BooleanClauseList):
        for child in node.clauses:
            _walk(child, found)
        return
    left = getattr(node, "left", None)
    key = getattr(left, "key", None)
    operator = getattr(getattr(node, "operator", None), "__name__", "")
    right = getattr(node, "right", None)
    if isinstance(key, str) and isinstance(right, BindParameter):
        found.append((key, operator, right.value))
        return
    if isinstance(key, str) and isinstance(right, Null):
        found.append((key, operator, None))
        return
    element = getattr(node, "element", None)
    if element is not None and element is not node:
        _walk(element, found)


def _matches(row: object, comps: list[tuple[str, str, object]]) -> bool:
    for key, operator, value in comps:
        current = getattr(row, key)
        if operator == "eq" and current != value:
            return False
        if operator == "is_" and value is None and current is not None:
            return False
        if operator in {"is_not", "isnot"} and value is None and current is None:
            return False
        if operator == "in_op" and current not in value:
            return False
    return True


def _limit(statement: object) -> int | None:
    clause = getattr(statement, "_limit_clause", None)
    if isinstance(clause, BindParameter) and isinstance(clause.value, int):
        return clause.value
    return None


def test_page_health_tags_and_dates() -> None:
    """分页、卡住判定、标签和 Java 日期与现有向量一致。"""
    assert page_bounds(0, 0) == (1, 20, 0)
    assert page_bounds(2, 250) == (2, 200, 200)
    assert normalize_status_category(" in_progress ") == "IN_PROGRESS"
    assert normalize_status_category("其他") is None
    assert keyword_id("007") == 7
    assert keyword_id("9223372036854775808") is None
    assert keyword_id("12a") is None
    assert evaluate_health("NEW", "FAILED", 0, 10, 5) == ("OK", None)
    health, reason = evaluate_health("IN_PROGRESS", "TIMEOUT", 0, 10, 5)
    assert health == "STUCK"
    assert reason is not None
    assert "超时" in reason
    stuck, stuck_reason = evaluate_health("IN_PROGRESS", "RUNNING", 0, 120_000, 60_000)
    assert stuck == "STUCK"
    assert stuck_reason == "执行已卡住超过 2 分钟无进展，请人工介入"
    assert evaluate_health("IN_PROGRESS", "RUNNING", 90_000, 120_000, 60_000) == ("OK", None)
    now = datetime(2026, 9, 24, 12, 0, 0)
    assert scheduled_phase(None, None, False, None, now) is None
    assert scheduled_phase(now + timedelta(minutes=5), None, True, "RUNNING", now) == "PENDING"
    assert scheduled_phase(now - timedelta(minutes=1), now, True, "RUNNING", now) == "DONE"
    assert scheduled_phase(None, now, False, "RUNNING", now) == "RUNNING"
    assert scheduled_phase(None, now, False, "SUCCEEDED", now) == "READY"
    assert normalize_tags([" 甲 ", None, "甲", "乙"]) == ["甲", "乙"]
    try:
        normalize_tags(["甲" * 33])
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected long tag")
    assert parse_tags("{") == []
    assert parse_tags(["甲"]) == ["甲"]
    assert assignment_detail("HUMAN", "NOPE") == {"fromType": "HUMAN"}
    assert assignment_detail("NOPE", "NOPE") is None
    parsed = parse_java_date("2026-09-24T04:00:00Z")
    assert parsed == datetime(2026, 9, 24, 12, 0, 0)
    assert parse_java_date(0) == datetime(1970, 1, 1, 8, 0)


def test_list_sql_uses_source_aware_dispatch_and_tag_contains() -> None:
    """列表 SQL 带上来源类型、关注关系和标签包含。"""
    statement = list_statement(
        1,
        "TASK",
        4,
        "PENDING_DECISION",
        "HUMAN",
        7,
        True,
        "WATCHED",
        7,
        "12",
        12,
        "后端",
        "PENDING",
        0,
        20,
    )
    sql = str(statement.compile(dialect=mysql.dialect())).lower()
    assert "source_type" in sql
    assert "workitem" in sql
    assert "json_contains" in sql
    assert "workitem_watcher" in sql
    assert "scheduled_start_at" in sql


def test_preferred_external_source_and_running_delete_block() -> None:
    """Aone 链接优先；执行中的本员工单不可删。"""
    workitem = Workitem(
        id=3,
        tenant_id=1,
        work_type="TASK",
        title="交付",
        priority=2,
        version=0,
        gmt_create=datetime(2026, 9, 24, 8, 0, 0),
        gmt_modified=datetime(2026, 9, 24, 8, 0, 0),
        assignee_type="HUMAN",
        assignee_ref=7,
        status_node_id=11,
    )
    jira = _link(1, "JIRA", datetime(2026, 1, 1, 0, 0, 0), "https://jira")
    aone = _link(2, "AONE", datetime(2026, 2, 1, 0, 0, 0), "https://aone")
    running = _dispatch(9, "RUNNING")
    now = datetime(2026, 9, 24, 12, 0, 0)
    view = render_workitem(
        workitem,
        status_name="开发中",
        status_code="dev",
        status_category="IN_PROGRESS",
        sdlc_name=None,
        assignee_name="艾达",
        assignee_display_name="艾达(7)",
        creator_name="艾达",
        creator_display_name="艾达(7)",
        latest=running,
        links=[jira, aone],
        dispatches=[running],
        now=now,
        now_ms=shanghai_millis(now),
        stuck_threshold_ms=3_600_000,
        include_runtime=True,
        origin=None,
    )
    body = dump_data(view)
    assert body["sourceType"] == "EXTERNAL"
    assert body["sourceProvider"] == "AONE"
    assert body["sourceUrl"] == "https://aone"
    assert body["deletable"] is False
    assert body["deletableReason"] == "外部平台集成工单不可删除"
    assert body["executionStatus"] == "RUNNING"
    assert body["health"] == "STUCK"


async def test_create_assigns_creator_and_init_status() -> None:
    """缺省负责人是创建人，并写下创建事件。"""
    session = await _ready_session()
    view = await create(
        session,
        CreateWorkitemRequest(work_type="TASK", title="补测试", content_md="正文"),
        1,
        7,
        None,
    )
    assert view.assignee_type == "HUMAN"
    assert view.assignee_ref == 7
    assert view.status_name == "新建"
    assert view.priority == 2
    assert view.version == 0
    assert view.creator_name == "艾达"
    events = [row for row in session.rows if row.__class__.__name__ == "WorkitemEvent"]
    assert events[0].event_type == "CREATE"
    assert events[0].to_val == "new"
    assert session.commits == 1


async def test_create_rejects_unknown_type_and_missing_template() -> None:
    """非法类型和没有默认模版时返回对应错误码。"""
    session = await _ready_session()
    try:
        await create(session, CreateWorkitemRequest(work_type="EPIC", title="x"), 1, 7, None)
    except BizError as error:
        assert error.error_code == ErrorCode.WORK_TYPE_INVALID
    else:
        raise AssertionError("expected invalid work type")
    empty = MemorySession()
    try:
        await create(empty, CreateWorkitemRequest(work_type="TASK", title="x"), 1, 7, None)
    except BizError as error:
        assert error.error_code == ErrorCode.STATUS_TEMPLATE_NOT_FOUND
    else:
        raise AssertionError("expected missing template")


async def test_agent_assign_binds_sdlc_and_skips_enqueue_without_step() -> None:
    """首次指派数字员工时绑定流程入口步骤，并交出指派事件。"""
    session = await _ready_session()
    created = await create(
        session,
        CreateWorkitemRequest(
            work_type="TASK",
            title="交给数字员工",
            assignee_type="AGENT",
            assignee_ref=4,
        ),
        1,
        7,
        None,
    )
    assert created.assignee_type == "AGENT"
    assert created.assignee_ref == 4
    assert created.sdlc_id == 20
    stored = _workitem(session)
    assert stored.current_step_id == 21
    assert stored.assign_operator_id == 7
    events = [row for row in session.rows if row.__class__.__name__ == "WorkitemEvent"]
    assert [row.event_type for row in events] == ["CREATE", "ASSIGN"]
    assert events[1].detail_json == {"fromType": "HUMAN", "toType": "AGENT"}
    on_workitem_assigned(WorkitemAssigned(1, stored.id, None, 4, stored.version, 7))


async def test_assign_same_person_is_noop_and_other_tenant_is_hidden() -> None:
    """同一负责人不写事件；其他空间的工单视为不存在。"""
    session = await _ready_session()
    created = await create(
        session,
        CreateWorkitemRequest(work_type="TASK", title="我的"),
        1,
        7,
        None,
    )
    again = await assign(session, created.id, "HUMAN", 7, None, None, None, 1, 7)
    assert again.version == created.version
    events = [row for row in session.rows if row.__class__.__name__ == "WorkitemEvent"]
    assert len(events) == 1
    try:
        await assign(session, created.id, "HUMAN", 8, None, None, None, 2, 7)
    except BizError as error:
        assert error.error_code == ErrorCode.WORKITEM_NOT_FOUND
    else:
        raise AssertionError("expected foreign tenant")


async def test_transition_requires_edge_and_matching_version() -> None:
    """没有边或版本不一致时拒绝，成功后版本加一。"""
    session = await _ready_session()
    created = await create(
        session,
        CreateWorkitemRequest(work_type="TASK", title="流转"),
        1,
        7,
        None,
    )
    try:
        await transition(session, created.id, 99, 1, 7, None, None)
    except BizError as error:
        assert error.error_code == ErrorCode.ILLEGAL_TRANSITION
    else:
        raise AssertionError("expected illegal transition")
    try:
        await transition(session, created.id, 12, 1, 7, None, 4)
    except BizError as error:
        assert error.error_code == ErrorCode.WORKITEM_VERSION_CONFLICT
    else:
        raise AssertionError("expected version conflict")
    moved = await transition(session, created.id, 12, 1, 7, 11, 0)
    assert moved.status_node_id == 12
    assert moved.version == 1


async def test_tags_content_and_delete_follow_java_guards() -> None:
    """标签去重，外部正文只读，执行中不能删除。"""
    session = await _ready_session()
    created = await create(
        session,
        CreateWorkitemRequest(work_type="TASK", title="旧标题"),
        1,
        7,
        None,
    )
    tagged = await update_tags(session, created.id, [" 甲 ", "甲", "乙"], 1, 7)
    assert tagged.tags == ["甲", "乙"]
    same = await update_content(session, created.id, "旧标题", None, 1, 7)
    assert same.version == tagged.version
    edited = await update_content(session, created.id, "新标题", "正文", 1, 7)
    assert edited.title == "新标题"
    link = _link(1, "AONE", datetime(2026, 1, 1), "https://aone")
    link.workitem_id = created.id
    session.add(link)
    await session.flush()
    try:
        await update_content(session, created.id, "再改", None, 1, 7)
    except BizError as error:
        assert error.error_code == ErrorCode.WORKITEM_EXTERNAL_CONTENT_READ_ONLY
    else:
        raise AssertionError("expected external content lock")
    try:
        await delete_workitem(session, created.id, 1, 7)
    except BizError as error:
        assert error.error_code == ErrorCode.WORKITEM_EXTERNAL_NO_DELETE
    else:
        raise AssertionError("expected external delete lock")
    session.rows.remove(link)
    session.add(_dispatch_for(created.id))
    await session.flush()
    try:
        await delete_workitem(session, created.id, 1, 7)
    except BizError as error:
        assert error.error_code == ErrorCode.WORKITEM_RUNNING_NO_DELETE
    else:
        raise AssertionError("expected running delete lock")


def test_workitem_routes_require_login() -> None:
    """已迁移的工单路径未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/workitems" in paths
    assert "/api/workitems/{id}/assignee" in paths
    assert "/api/workitems/{id}/scheduled-start" in paths
    assert "/api/workitems/{id}/tags" in paths
    assert "/api/workitems/{id}/comments" in paths
    assert "/api/workitems/{id}/timeline" in paths
    assert "/api/workitems/{id}/unified-timeline" in paths
    assert "/api/workitems/{id}/participants" in paths
    assert "/api/workitems/{id}/mention-candidates" in paths
    assert "/api/workitems/{id}/watch" in paths
    assert "/api/workitems/{id}/watchers" in paths
    assert "/api/workitems/{id}/delivery-progress" in paths
    response = client.get("/api/workitems")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    assert client.get("/api/workitems/1/comments").status_code == 401
    assert client.get("/api/workitems/1/delivery-progress").status_code == 401
    assert client.post("/api/workitems/1/watch").status_code == 401


async def _ready_session() -> MemorySession:
    session = MemorySession()
    now = datetime(2026, 9, 24, 8, 0, 0)
    session.add(
        User(
            id=7,
            username="ada",
            nickname="艾达",
            password_hash="hash",
            is_deleted=0,
            gmt_create=now,
            gmt_modified=now,
        )
    )
    session.add(
        StatusTemplate(
            id=10,
            tenant_id=1,
            work_type="TASK",
            name="默认任务",
            is_default=1,
            is_deleted=0,
            version=0,
            gmt_create=now,
            gmt_modified=now,
        )
    )
    session.add(
        StatusNode(
            id=11,
            tenant_id=1,
            template_id=10,
            code="new",
            name="新建",
            category="INIT",
            sort=0,
            gmt_create=now,
        )
    )
    session.add(
        StatusNode(
            id=12,
            tenant_id=1,
            template_id=10,
            code="dev",
            name="开发中",
            category="IN_PROGRESS",
            sort=1,
            gmt_create=now,
        )
    )
    session.add(
        StatusTransition(
            id=13,
            tenant_id=1,
            template_id=10,
            from_node_id=11,
            to_node_id=12,
            name="开始",
            gmt_create=now,
        )
    )
    session.add(
        Agent(
            id=4,
            tenant_id=1,
            name="构建员",
            is_deleted=0,
            version=0,
            gmt_create=now,
            gmt_modified=now,
        )
    )
    session.add(
        AgentVersion(
            id=40,
            tenant_id=1,
            agent_id=4,
            version_no=1,
            status="APPROVED",
            sdlc_id=20,
            is_deleted=0,
            version=0,
            gmt_create=now,
            gmt_modified=now,
        )
    )
    session.add(
        Sdlc(
            id=20,
            tenant_id=1,
            name="交付流程",
            work_type="TASK",
            status="ENABLED",
            is_default=1,
            is_deleted=0,
            version=0,
            gmt_create=now,
            gmt_modified=now,
        )
    )
    session.add(
        SdlcStep(
            id=21,
            tenant_id=1,
            sdlc_id=20,
            step_order=1,
            name="实现",
            is_deleted=0,
            gmt_create=now,
            gmt_modified=now,
        )
    )
    session.add(SquadMember(id=30, tenant_id=1, squad_id=8, agent_id=4, gmt_create=now))
    await session.flush()
    return session


def _workitem(session: MemorySession) -> Workitem:
    for row in session.rows:
        if isinstance(row, Workitem):
            return row
    raise AssertionError("missing workitem")


def _link(link_id: int, provider: str, created: datetime, url: str) -> ExternalWorkitemLink:
    return ExternalWorkitemLink(
        id=link_id,
        tenant_id=1,
        provider=provider,
        binding_id=1,
        external_project_id="p",
        external_workitem_id="e" + str(link_id),
        workitem_id=3,
        external_url=url,
        source_lifecycle="ACTIVE",
        sync_status="HEALTHY",
        comment_sync_cursor="0",
        gmt_create=created,
        gmt_modified=created,
    )


def _dispatch(dispatch_id: int, status: str) -> Dispatch:
    moment = datetime(2026, 9, 24, 8, 0, 0)
    return Dispatch(
        id=dispatch_id,
        tenant_id=1,
        source_type="WORKITEM",
        workitem_id=3,
        agent_id=4,
        status=status,
        idempotency_key="k" + str(dispatch_id),
        gmt_create=moment,
        gmt_modified=moment - timedelta(hours=2),
        is_deleted=0,
    )


def _dispatch_for(workitem_id: int) -> Dispatch:
    item = _dispatch(50, "RUNNING")
    item.workitem_id = workitem_id
    return item
