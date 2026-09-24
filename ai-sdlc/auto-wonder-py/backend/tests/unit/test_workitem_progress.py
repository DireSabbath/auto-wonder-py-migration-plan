"""工单交付进度。向量来自 Java ``WorkitemServiceTest`` 的 delivery progress。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autowonder.agents.models import Agent, AgentVersion
from autowonder.aiusage.models import DispatchAiUsage
from autowonder.core.clock import SHANGHAI
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch, DispatchRuntimeEvent
from autowonder.executors.registry import drop_session, register_session
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.sdlcs.models import SdlcStep
from autowonder.users.models import User
from autowonder.workitems.models import Workitem
from autowonder.workitems.progress import get_delivery_progress
from tests.unit.test_workitems import MemorySession

_NOW = datetime(2026, 9, 24, 8, 0, 0)


def _at(millis: int) -> datetime:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=millis)
    return moment.astimezone(SHANGHAI).replace(tzinfo=None)


class _FailingUsageSession(MemorySession):
    """用量查询失败时，交付进度仍要返回，只是不带 credits。"""

    async def scalars(self, statement: object) -> object:
        entity = statement.column_descriptions[0]["entity"]
        if entity is DispatchAiUsage:
            raise RuntimeError("db down")
        return await MemorySession.scalars(self, statement)


def _workitem(**extra: object) -> Workitem:
    values: dict[str, object] = {
        "id": 100,
        "tenant_id": 7,
        "work_type": "TASK",
        "title": "交付",
        "priority": 0,
        "version": 0,
        "is_deleted": 0,
        "gmt_create": _NOW,
        "gmt_modified": _NOW,
        "sdlc_id": 10,
        "current_step_id": 20,
    }
    values.update(extra)
    return Workitem(**values)


def _step(step_id: int, sdlc_id: int, order: int, name: str, code: str | None = None) -> SdlcStep:
    return SdlcStep(
        id=step_id,
        tenant_id=7,
        sdlc_id=sdlc_id,
        step_order=order,
        name=name,
        code=code,
        is_deleted=0,
        gmt_create=_NOW,
        gmt_modified=_NOW,
    )


def _agent(agent_id: int, name: str) -> Agent:
    return Agent(
        id=agent_id,
        tenant_id=7,
        name=name,
        is_deleted=0,
        gmt_create=_NOW,
        gmt_modified=_NOW,
    )


def _bind(agent: Agent, sdlc_id: int) -> AgentVersion:
    version_id = agent.id + 1000
    agent.online_version_id = version_id
    return AgentVersion(
        id=version_id,
        tenant_id=7,
        agent_id=agent.id,
        version_no=1,
        sdlc_id=sdlc_id,
        is_deleted=0,
        version=0,
        gmt_create=_NOW,
        gmt_modified=_NOW,
    )


def _dispatch(
    dispatch_id: int,
    step_id: int,
    agent_id: int | None,
    status: str,
    create_ms: int,
    modified_ms: int,
    error: str | None = None,
    *,
    resume_mode: str | None = None,
    idempotency_key: str | None = None,
    result_summary: str | None = None,
    resume_from: int | None = None,
    executor_id: int | None = None,
) -> Dispatch:
    key = str(dispatch_id)
    if idempotency_key is not None:
        key = idempotency_key
    return Dispatch(
        id=dispatch_id,
        tenant_id=7,
        source_type="WORKITEM",
        workitem_id=100,
        sdlc_step_id=step_id,
        agent_id=agent_id,
        executor_id=executor_id,
        status=status,
        attempt=1,
        idempotency_key=key,
        result_summary=result_summary,
        error=error,
        resume_from_dispatch_id=resume_from,
        resume_mode=resume_mode,
        is_deleted=0,
        version=0,
        gmt_create=_at(create_ms),
        gmt_modified=_at(modified_ms),
    )


def _event(
    event_id: int,
    dispatch_id: int,
    agent_id: int,
    event_type: str,
    step_order: int | None,
    step_name: str | None,
    create_ms: int,
    message: str | None,
    *,
    error: str | None = None,
    detail_json: object | None = None,
) -> DispatchRuntimeEvent:
    return DispatchRuntimeEvent(
        id=event_id,
        tenant_id=7,
        workitem_id=100,
        dispatch_id=dispatch_id,
        agent_id=agent_id,
        event_type=event_type,
        step_order=step_order,
        step_name=step_name,
        message=message,
        error=error,
        detail_json=detail_json,
        gmt_create=_at(create_ms),
    )


async def _load(session: MemorySession, *rows: object) -> None:
    for row in rows:
        session.add(row)
    await session.flush()


async def test_pending_dispatch_substeps() -> None:
    session = MemorySession()
    await _load(
        session,
        _workitem(),
        _step(20, 10, 1, "编码实现"),
        _agent(40, "worker"),
        _dispatch(30, 20, 40, "PENDING", 1_000, 2_000),
    )
    progress = await get_delivery_progress(session, 100, 7)
    step = progress.steps[0]
    assert step.status == "active"
    assert step.executor_name == "worker"
    assert step.sub_steps is not None
    assert step.sub_steps[0].name == "启动交付"
    assert step.sub_steps[0].status == "done"
    assert step.sub_steps[1].name == "等待调度执行"
    assert step.sub_steps[1].status == "active"


async def test_unassigned_failure_does_not_claim_client_accepted() -> None:
    session = MemorySession()
    await _load(
        session,
        _workitem(),
        _step(20, 10, 1, "需求分析"),
        _agent(40, "worker"),
        _dispatch(30, 20, 40, "FAILED", 1_000, 2_000, "AGENT_NOT_PUBLISHED: 未发布"),
    )
    progress = await get_delivery_progress(session, 100, 7)
    names = [item.name for item in progress.steps[0].sub_steps or []]
    assert "未派发到客户端" in names
    assert "客户端已接单" not in names


async def test_assigned_failure_does_not_invent_acknowledgement() -> None:
    session = MemorySession()
    await _load(
        session,
        _workitem(),
        _step(20, 10, 1, "需求分析"),
        _agent(40, "worker"),
        _dispatch(
            30,
            20,
            40,
            "FAILED",
            1_000,
            2_000,
            "EXECUTOR_PROTOCOL_INCOMPATIBLE: dispatch_inventory_v1 is required",
            executor_id=10067,
        ),
    )
    progress = await get_delivery_progress(session, 100, 7)
    names = [item.name for item in progress.steps[0].sub_steps or []]
    assert "已分配执行器" in names
    assert "客户端已接单" not in names


async def test_attempt_duration_without_runtime_events() -> None:
    session = MemorySession()
    await _load(
        session,
        _workitem(),
        _step(20, 10, 1, "编码实现"),
        _agent(40, "worker"),
        _dispatch(30, 20, 40, "FAILED", 1_000_000, 1_161_000),
        _dispatch(31, 20, 40, "PENDING", 2_000_000, 2_030_000),
    )
    step = (await get_delivery_progress(session, 100, 7)).steps[0]
    assert step.duration_ms is None
    assert len(step.attempts) == 2
    assert step.attempts[0].duration_ms == 161_000
    assert step.attempts[0].executor_name == "worker"
    assert step.attempts[1].duration_ms == 30_000


async def test_each_step_gets_its_own_runtime_duration() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与评论"),
        _step(102, 10, 2, "编码实现"),
        _step(103, 10, 3, "自测与交付"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 0, 300_000),
        _event(1, 301, 41, "step.started", 1, "需求分析与评论", 1_000, None),
        _event(2, 301, 41, "step.completed", 1, "需求分析与评论", 61_000, None),
        _event(3, 301, 41, "step.started", 2, "编码实现", 62_000, None),
        _event(4, 301, 41, "step.completed", 2, "编码实现", 182_000, None),
        _event(5, 301, 41, "step.started", 3, "自测与交付", 183_000, None),
        _event(6, 301, 41, "step.completed", 3, "自测与交付", 213_000, None),
    )
    steps = (await get_delivery_progress(session, 100, 7)).agents[0].steps
    assert steps[0].duration_ms == 60_000
    assert steps[1].duration_ms == 120_000
    assert steps[2].duration_ms == 30_000


async def test_step_duration_stays_null_without_runtime_events() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与评论"),
        _step(102, 10, 2, "编码实现"),
        _step(103, 10, 3, "自测与交付"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 0, 300_000),
    )
    steps = (await get_delivery_progress(session, 100, 7)).agents[0].steps
    assert [step.duration_ms for step in steps] == [None, None, None]


async def test_total_duration_sums_formal_dispatches() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 0, 60_000),
        _dispatch(302, 101, 41, "SUCCEEDED", 90_000, 200_000),
    )
    assert (await get_delivery_progress(session, 100, 7)).total_duration_ms == 170_000


async def test_total_duration_excludes_interaction_dispatches() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 0, 60_000),
        _dispatch(302, 101, 41, "SUCCEEDED", 60_000, 120_000, resume_mode="CANONICAL_INTERACTION"),
        _dispatch(303, 101, 41, "SUCCEEDED", 120_000, 180_000),
    )
    assert (await get_delivery_progress(session, 100, 7)).total_duration_ms == 120_000


async def test_running_total_matches_displayed_agent_durations() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 0, 60_000),
        _dispatch(302, 101, 41, "RUNNING", 120_000, 200_000),
    )
    progress = await get_delivery_progress(session, 100, 7)
    displayed = 0
    for agent in progress.agents:
        if agent.duration_ms is not None:
            displayed = displayed + agent.duration_ms
    assert progress.total_duration_ms == 140_000
    assert progress.total_duration_ms == displayed


async def test_total_duration_is_null_without_dispatches() -> None:
    session = MemorySession()
    await _load(session, _workitem(sdlc_id=None, current_step_id=None))
    progress = await get_delivery_progress(session, 100, 7)
    assert progress.total_duration_ms is None
    assert progress.total_usage is None


async def test_groups_by_agent_and_keeps_handoff_history() -> None:
    session = MemorySession()
    dev = _agent(41, "Agent Dev")
    review = _agent(42, "Agent CR")
    await _load(
        session,
        _workitem(sdlc_id=20, current_step_id=202, assignee_type="AGENT", assignee_ref=42),
        _step(101, 10, 1, "需求分析与建分支"),
        _step(102, 10, 2, "编码实现"),
        _step(201, 20, 1, "Code Review"),
        _step(202, 20, 2, "修复意见"),
        dev,
        review,
        _bind(dev, 10),
        _bind(review, 20),
        _dispatch(301, 101, 41, "SUCCEEDED", 1_000, 121_000),
        _dispatch(
            302,
            201,
            42,
            "FAILED",
            200_000,
            240_000,
            "execute dispatch: load skills: skill name is required.",
        ),
        _dispatch(303, 202, 42, "RUNNING", 250_000, 550_000),
    )
    progress = await get_delivery_progress(session, 100, 7)
    assert len(progress.agents) == 2
    first = progress.agents[0]
    assert first.agent_id == 41
    assert first.agent_name == "Agent Dev"
    assert first.status == "finished"
    assert first.steps[0].status == "done"
    assert first.steps[0].duration_ms is None
    second = progress.agents[1]
    assert second.agent_id == 42
    assert second.agent_name == "Agent CR"
    assert second.status == "active"
    assert second.steps[0].status == "failed"
    assert second.steps[0].error == "execute dispatch: load skills: skill name is required."
    assert second.steps[1].status == "active"
    assert second.steps[1].executor_name == "Agent CR"


async def test_graph_handoff_edge() -> None:
    session = MemorySession()
    dev = _agent(41, "开发")
    review = _agent(42, "评审")
    await _load(
        session,
        _workitem(current_step_id=201, assignee_type="AGENT", assignee_ref=42),
        _step(101, 10, 1, "编码实现"),
        _step(201, 20, 1, "代码评审"),
        dev,
        review,
        _bind(dev, 10),
        _bind(review, 20),
        _dispatch(301, 101, 41, "SUCCEEDED", 1_000, 121_000),
        _dispatch(302, 201, 42, "RUNNING", 130_000, 160_000, idempotency_key="handoff:301"),
    )
    graph = (await get_delivery_progress(session, 100, 7)).process_graph
    assert len(graph.nodes) == 2
    assert len(graph.edges) == 1
    assert graph.edges[0].type == "HANDOFF"
    assert graph.edges[0].source_dispatch_id == 301
    assert graph.edges[0].target_dispatch_id == 302
    assert graph.edges[0].label == "交接"


async def test_comment_rework_excludes_side_interaction() -> None:
    session = MemorySession()
    dev = _agent(41, "开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "PAUSED", 1_000, 61_000),
        _dispatch(
            302,
            101,
            41,
            "SUCCEEDED",
            70_000,
            80_000,
            resume_mode="SIDE_INTERACTION",
            idempotency_key="guidance:900",
        ),
        _dispatch(
            303,
            101,
            41,
            "RUNNING",
            90_000,
            120_000,
            resume_mode="COMMENT_REWORK",
            idempotency_key="interaction-rework:302",
            resume_from=301,
            result_summary="waitForDispatchId=301",
        ),
        WorkitemCommentDelivery(
            id=900,
            tenant_id=7,
            source_type="WORKITEM",
            workitem_id=100,
            comment_id=11599,
            target_agent_id=41,
            dispatch_id=302,
            status="APPLIED",
            retry_dispatch_id=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        ),
    )
    graph = (await get_delivery_progress(session, 100, 7)).process_graph
    assert [node.dispatch_id for node in graph.nodes] == [301, 303]
    assert graph.nodes[1].trigger_comment_id == 11599
    edge = graph.edges[0]
    assert edge.type == "COMMENT_REWORK"
    assert edge.source_key == "dispatch:301"
    assert edge.target_key == "dispatch:303"
    assert edge.comment_id == 11599
    assert edge.label == "用户返工（评论 #11599）"


async def test_resume_lineage_does_not_invent_edges() -> None:
    session = MemorySession()
    dev = _agent(41, "开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(300, 101, 41, "SUCCEEDED", 1_000, 31_000),
        _dispatch(301, 101, 41, "PAUSED", 40_000, 60_000),
        _dispatch(302, 101, 41, "RUNNING", 70_000, 90_000, resume_from=301),
    )
    graph = (await get_delivery_progress(session, 100, 7)).process_graph
    assert len(graph.nodes) == 3
    assert len(graph.edges) == 1
    assert graph.edges[0].type == "CONTINUE"
    assert graph.edges[0].source_dispatch_id == 301
    assert graph.edges[0].target_dispatch_id == 302


async def test_single_success_marks_internal_sdlc_done() -> None:
    session = MemorySession()
    dev = _agent(41, "AutoWonder前后端1号开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与建分支"),
        _step(102, 10, 2, "编码实现"),
        _step(103, 10, 3, "自测"),
        _step(104, 10, 4, "推送分支并交接"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 1_000, 121_000),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.status == "finished"
    assert [step.status for step in agent.steps] == ["done", "done", "done", "done"]


async def test_keeps_runtime_details_after_single_success() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与评论"),
        _step(102, 10, 2, "编码实现"),
        _step(103, 10, 3, "自测"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 1_000, 301_000),
        _event(1, 301, 41, "step.started", 1, "需求分析与评论", 1_000, "读取工单"),
        _event(2, 301, 41, "step.completed", 1, "需求分析与评论", 61_000, "发布分析评论"),
        _event(3, 301, 41, "step.started", 2, "编码实现", 62_000, "开始编码"),
        _event(4, 301, 41, "agent.progress", 2, "编码实现", 120_000, "提交修复代码"),
        _event(5, 301, 41, "step.completed", 2, "编码实现", 181_000, "编码完成"),
        _event(6, 301, 41, "step.started", 3, "自测", 182_000, "运行测试"),
        _event(7, 301, 41, "step.completed", 3, "自测", 240_000, "测试通过"),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.status == "finished"
    assert [step.status for step in agent.steps] == ["done", "done", "done"]
    coding = agent.steps[1]
    assert coding.executor_name == "AW全栈开发"
    assert coding.sub_steps is not None
    assert any(item.name == "提交修复代码" for item in coding.sub_steps)
    assert all(item.status == "done" for item in coding.sub_steps)


async def test_runtime_events_inside_one_running_dispatch() -> None:
    session = MemorySession()
    dev = _agent(41, "AutoWonder前后端1号开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与建分支"),
        _step(102, 10, 2, "编码实现"),
        _step(103, 10, 3, "自测"),
        _step(104, 10, 4, "推送分支并交接"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "RUNNING", 1_000, 301_000),
        _event(1, 301, 41, "step.started", 1, "需求分析与建分支", 1_000, None),
        _event(2, 301, 41, "step.completed", 1, "需求分析与建分支", 61_000, None),
        _event(3, 301, 41, "step.started", 2, "编码实现", 62_000, None),
        _event(4, 301, 41, "step.completed", 2, "编码实现", 181_000, None),
        _event(5, 301, 41, "step.started", 3, "自测", 182_000, None),
        _event(6, 301, 41, "agent.progress", 3, "自测", 240_000, "运行测试"),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.status == "active"
    assert agent.current_activity == "运行测试"
    assert [step.status for step in agent.steps] == ["done", "done", "active", "pending"]
    substeps = agent.steps[2].sub_steps or []
    assert any(item.name == "运行测试" for item in substeps)
    assert substeps[len(substeps) - 1].status == "active"


async def test_hides_mojibake_and_labels_gate_events() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "分析与分支准备"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "RUNNING", 1_000, 301_000),
        _event(1, 301, 41, "step.started", 1, "分析与分支准备", 1_000, "\ufffd乱码进度"),
        _event(2, 301, 41, "step.completion_requested", 1, "分析与分支准备", 2_000, None),
        _event(3, 301, 41, "step.gate_started", 1, "分析与分支准备", 3_000, None),
        _event(4, 301, 41, "step.gate_finished", 1, "分析与分支准备", 4_000, None),
        _event(5, 301, 41, "agent.progress", 1, "分析与分支准备", 5_000, "正常进度"),
    )
    names = [
        item.name
        for item in (await get_delivery_progress(session, 100, 7)).agents[0].steps[0].sub_steps
        or []
    ]
    assert names == ["开始执行", "请求完成", "开始校验", "校验完成", "正常进度"]
    assert all("\ufffd" not in name and not name.startswith("step.") for name in names)


async def test_hides_mojibake_current_activity() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "RUNNING", 1_000, 301_000),
        _event(1, 301, 41, "agent.progress", 1, "编码实现", 5_000, "正在运行测试"),
        _event(
            2,
            301,
            41,
            "agent.progress",
            1,
            "编码实现",
            6_000,
            "运行测试\ufffd\ufffd\ufffd\ufffd",
        ),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.current_activity == "正在运行测试"


async def test_current_activity_null_when_only_mojibake() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "RUNNING", 1_000, 301_000),
        _event(
            1,
            301,
            41,
            "agent.progress",
            1,
            "编码实现",
            5_000,
            "\ufffd\ufffd\ufffd\ufffd\u0432\ufffd\ufffd\ufffd",
        ),
        _event(
            2,
            301,
            41,
            "agent.progress",
            1,
            "编码实现",
            6_000,
            "\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd",
        ),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.current_activity is None


async def test_rerun_worker_stays_active_when_assignee_is_human() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=102, assignee_type="HUMAN", assignee_ref=9),
        _step(101, 10, 1, "需求分析与评论"),
        _step(102, 10, 2, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 102, 41, "RUNNING", 1_000, 61_000),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.agent_name == "AW全栈开发"
    assert agent.status == "active"
    assert agent.steps[1].status == "active"


async def test_latest_workflow_plan_does_not_overwrite_execution_status() -> None:
    session = MemorySession()
    dev = _agent(41, "开发 Dev")
    plan = (
        '{"revision":2,"targetStepId":"coding","reason":"实现方式变化",'
        '"sourceGuidanceIds":[184],"steps":['
        '{"stepKey":"analysis","name":"需求分析","planStatus":"REUSED","sourceAttempt":1},'
        '{"stepKey":"coding","name":"编码实现","planStatus":"RUN"}]}'
    )
    await _load(
        session,
        _workitem(current_step_id=102, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析", code="analysis"),
        _step(102, 10, 2, "编码实现", code="coding"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 102, 41, "RUNNING", 1_000, 30_000),
        _event(
            1,
            301,
            41,
            "workflow.plan_applied",
            None,
            None,
            1_000,
            None,
            detail_json='{"revision":1,"targetStepId":"analysis","steps":[{"stepKey":"analysis","name":"需求分析","planStatus":"RUN"}]}',
        ),
        _event(2, 301, 41, "workflow.plan_applied", None, None, 2_000, None, detail_json=plan),
        _event(3, 301, 41, "step.started", 2, "编码实现", 3_000, None),
    )
    progress = await get_delivery_progress(session, 100, 7)
    assert progress.workflow_plan is not None
    assert progress.workflow_plan.revision == 2
    assert progress.workflow_plan.reason == "实现方式变化"
    assert progress.workflow_plan.source_guidance_ids == [184]
    assert [step.plan_status for step in progress.workflow_plan.steps] == ["REUSED", "RUN"]
    agent = progress.agents[0]
    assert agent.steps[0].plan_status == "REUSED"
    assert agent.steps[0].source_attempt == 1
    assert agent.steps[1].plan_status == "RUN"
    assert agent.steps[1].status == "active"


async def test_retry_success_marks_internal_sdlc_done() -> None:
    session = MemorySession()
    dev = _agent(41, "AutoWonder前后端1号开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与建分支"),
        _step(102, 10, 2, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "FAILED", 1_000, 61_000, "transient"),
        _dispatch(302, 101, 41, "SUCCEEDED", 70_000, 121_000),
        _event(1, 301, 41, "step.started", 1, "需求分析与建分支", 1_000, "旧 attempt 开始"),
        _event(2, 301, 41, "step.failed", 1, "需求分析与建分支", 61_000, "旧 attempt 失败"),
        _event(3, 302, 41, "step.started", 1, "需求分析与建分支", 70_000, "重试开始"),
        _event(4, 302, 41, "step.completed", 1, "需求分析与建分支", 91_000, "重试分析完成"),
        _event(5, 302, 41, "step.started", 2, "编码实现", 92_000, "重试编码开始"),
        _event(6, 302, 41, "step.completed", 2, "编码实现", 121_000, "重试编码完成"),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.status == "finished"
    assert [step.status for step in agent.steps] == ["done", "done"]
    assert len(agent.steps[0].attempts) == 2
    first = agent.steps[0].sub_steps or []
    assert all(item.name != "旧 attempt 失败" and item.status != "failed" for item in first)
    second = agent.steps[1].sub_steps or []
    assert any(item.name == "重试编码完成" for item in second)
    assert all(item.status == "done" for item in second)


async def test_latest_running_attempt_can_pause_when_online() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与评论"),
        _step(102, 10, 2, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "FAILED", 1_000, 61_000, "transient"),
        _dispatch(302, 101, 41, "RUNNING", 70_000, 90_000, executor_id=51),
        _event(1, 301, 41, "step.started", 1, "需求分析与评论", 1_000, "旧 attempt 开始"),
        _event(2, 301, 41, "step.failed", 1, "需求分析与评论", 61_000, "旧 attempt 失败"),
        _event(3, 302, 41, "step.started", 1, "需求分析与评论", 70_000, "本轮重跑"),
    )
    register_session(51)
    try:
        agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    finally:
        drop_session(51)
    assert agent.status == "active"
    assert agent.steps[0].status == "active"
    substeps = agent.steps[0].sub_steps or []
    assert all(item.name != "旧 attempt 失败" and item.status != "failed" for item in substeps)
    attempts = agent.steps[0].attempts
    assert attempts[0].can_pause is False
    assert attempts[1].can_pause is True
    assert attempts[1].can_continue is False


async def test_pending_attempt_stays_authoritative_after_failover() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与评论"),
        _step(102, 10, 2, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(302, 101, 41, "PENDING", 70_000, 90_000, executor_id=51),
        _event(1, 302, 41, "step.started", 1, "需求分析与评论", 70_000, "开始"),
        _event(2, 302, 41, "agent.message", 1, "需求分析与评论", 80_000, "准备上下文"),
        _event(
            3,
            302,
            41,
            "dispatch.executor_failover",
            1,
            "需求分析与评论",
            90_000,
            "Runtime 51 失败，正在切换其他 Runtime",
            error="Runtime 51 · agent_error.provider_quota_limit · quota exhausted",
        ),
    )
    register_session(51)
    try:
        agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    finally:
        drop_session(51)
    assert agent.status == "active"
    assert agent.steps[0].status == "active"
    attempt = agent.steps[0].attempts[0]
    assert attempt.status == "PENDING"
    assert attempt.error == "Runtime 51 · agent_error.provider_quota_limit · quota exhausted"
    assert attempt.can_pause is False
    assert attempt.can_continue is False


async def test_stuck_running_attempt_becomes_failed_from_runtime() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与评论"),
        dev,
        _bind(dev, 10),
        _dispatch(302, 101, 41, "RUNNING", 70_000, 90_000, executor_id=51),
        _event(1, 302, 41, "step.started", 1, "需求分析与评论", 70_000, "开始"),
        _event(
            2,
            302,
            41,
            "step.failed",
            1,
            "需求分析与评论",
            90_000,
            "missing completion request",
        ),
    )
    register_session(51)
    try:
        agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    finally:
        drop_session(51)
    assert agent.status == "failed"
    assert agent.steps[0].status == "failed"
    attempt = agent.steps[0].attempts[0]
    assert attempt.status == "FAILED"
    assert attempt.error == "missing completion request"
    assert attempt.can_pause is False


async def test_restart_uses_latest_started_substep() -> None:
    session = MemorySession()
    dev = _agent(41, "AW测试工程师")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "获取上下文与评论"),
        _step(102, 10, 2, "测试验证"),
        dev,
        _bind(dev, 10),
        _dispatch(302, 101, 41, "RUNNING", 70_000, 90_000, executor_id=51),
        _event(1, 302, 41, "step.started", 1, "获取上下文与评论", 70_000, "第一次启动"),
        _event(2, 302, 41, "step.failed", 1, "获取上下文与评论", 75_000, "旧执行失败"),
        _event(3, 302, 41, "step.started", 1, "获取上下文与评论", 80_000, "恢复后重新启动"),
    )
    register_session(51)
    try:
        agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    finally:
        drop_session(51)
    assert agent.status == "active"
    assert agent.steps[0].status == "active"
    substeps = agent.steps[0].sub_steps or []
    assert substeps[len(substeps) - 1].name == "恢复后重新启动"
    assert substeps[len(substeps) - 1].status == "active"


async def test_paused_dispatch_overrides_started_runtime_event() -> None:
    session = MemorySession()
    dev = _agent(41, "AW全栈开发")
    paused = _dispatch(302, 101, 41, "PAUSED", 70_000, 90_000, executor_id=51)
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "需求分析与评论"),
        _step(102, 10, 2, "编码实现"),
        dev,
        _bind(dev, 10),
        paused,
        _event(3, 302, 41, "step.started", 2, "编码实现", 70_000, "正在编码"),
    )
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.status == "paused"
    assert agent.steps[1].status == "paused"
    attempt = agent.steps[0].attempts[0]
    assert attempt.can_pause is False
    assert attempt.can_continue is True
    paused.status = "FAILED"
    paused.error = "runtime failed after last progress event"
    agent = (await get_delivery_progress(session, 100, 7)).agents[0]
    assert agent.status == "failed"
    assert agent.steps[1].status == "failed"
    attempt = agent.steps[0].attempts[0]
    assert attempt.can_pause is False
    assert attempt.can_continue is True


async def test_continue_only_for_latest_failed_attempt() -> None:
    session = MemorySession()
    dev = _agent(41, "AutoWonder前后端1号开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "FAILED", 1_000, 61_000, "quota"),
        _dispatch(302, 101, 41, "FAILED", 70_000, 121_000, "quota"),
    )
    attempts = (await get_delivery_progress(session, 100, 7)).agents[0].steps[0].attempts
    assert attempts[0].can_continue is False
    assert attempts[1].can_continue is True


async def test_legacy_steps_expose_resume_mode_when_agent_is_missing() -> None:
    session = MemorySession()
    await _load(
        session,
        _workitem(current_step_id=101),
        _step(101, 10, 1, "编码实现"),
        _dispatch(303, 101, None, "RUNNING", 1_000, 61_000, resume_mode="SIDE_INTERACTION"),
    )
    progress = await get_delivery_progress(session, 100, 7)
    assert progress.steps[0].attempts[0].resume_mode == "SIDE_INTERACTION"


async def test_compat_steps_prefer_formal_step_over_interaction_worker() -> None:
    cases = (("RUNNING", "active"), ("PAUSED", "paused"), ("FAILED", "failed"))
    for formal_status, expected in cases:
        session = MemorySession()
        formal = _agent(41, "Formal Worker")
        mentioned = _agent(42, "Mentioned Worker")
        await _load(
            session,
            _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
            _step(101, 10, 1, "编码实现"),
            formal,
            mentioned,
            _bind(formal, 10),
            _bind(mentioned, 10),
            _dispatch(303, 101, 42, "RUNNING", 70_000, 121_000, resume_mode="SIDE_INTERACTION"),
            _dispatch(302, 101, 41, formal_status, 1_000, 61_000),
        )
        step = (await get_delivery_progress(session, 100, 7)).steps[0]
        assert step.executor_name == "Formal Worker"
        assert step.status == expected


async def test_pause_stays_available_while_executor_is_online() -> None:
    session = MemorySession()
    dev = _agent(41, "worker")
    pausing = _dispatch(301, 101, 41, "PAUSING", 1_000, 61_000, executor_id=51)
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        pausing,
    )
    register_session(51)
    try:
        attempt = (await get_delivery_progress(session, 100, 7)).agents[0].steps[0].attempts[0]
        assert attempt.can_pause is True
        assert attempt.can_continue is False
        pausing.status = "PAUSE_FAILED"
        attempt = (await get_delivery_progress(session, 100, 7)).agents[0].steps[0].attempts[0]
        assert attempt.can_pause is True
        assert attempt.can_continue is False
    finally:
        drop_session(51)


async def test_continue_when_pause_is_stuck_and_executor_is_offline() -> None:
    session = MemorySession()
    dev = _agent(41, "worker")
    pausing = _dispatch(301, 101, 41, "PAUSING", 1_000, 61_000, executor_id=51)
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="AGENT", assignee_ref=41),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        pausing,
    )
    attempt = (await get_delivery_progress(session, 100, 7)).agents[0].steps[0].attempts[0]
    assert attempt.can_pause is False
    assert attempt.can_continue is True
    pausing.status = "PAUSE_FAILED"
    attempt = (await get_delivery_progress(session, 100, 7)).agents[0].steps[0].attempts[0]
    assert attempt.can_pause is False
    assert attempt.can_continue is True


async def test_human_handoff_node_follows_latest_success() -> None:
    session = MemorySession()
    dev = _agent(41, "开发")
    await _load(
        session,
        _workitem(current_step_id=101, assignee_type="HUMAN", assignee_ref=9),
        _step(101, 10, 1, "编码实现"),
        dev,
        _bind(dev, 10),
        _dispatch(301, 101, 41, "SUCCEEDED", 1_000, 61_000),
        User(
            id=9,
            username="ada",
            nickname="艾达",
            password_hash="hash",
            is_deleted=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        ),
    )
    graph = (await get_delivery_progress(session, 100, 7)).process_graph
    assert graph.nodes[1].key == "human:9"
    assert graph.nodes[1].agent_name == "艾达"
    assert graph.nodes[1].status == "HUMAN"
    assert graph.edges[0].type == "HUMAN_HANDOFF"
    assert graph.edges[0].label == "交接真人"


async def test_failed_handoff_label() -> None:
    session = MemorySession()
    dev = _agent(41, "开发")
    review = _agent(42, "评审")
    await _load(
        session,
        _workitem(assignee_type="AGENT", assignee_ref=42),
        _step(101, 10, 1, "编码实现"),
        _step(201, 20, 1, "代码评审"),
        dev,
        review,
        _bind(dev, 10),
        _bind(review, 20),
        _dispatch(301, 101, 41, "FAILED", 1_000, 61_000, "boom"),
        _dispatch(302, 201, 42, "RUNNING", 70_000, 90_000, idempotency_key="handoff:301"),
    )
    edge = (await get_delivery_progress(session, 100, 7)).process_graph.edges[0]
    assert edge.label == "失败后交接"


async def test_credits_accumulate_across_agent_runs() -> None:
    session = MemorySession()
    dev = _agent(40, "DEV")
    review = _agent(41, "CR")
    await _load(
        session,
        _workitem(),
        _step(20, 10, 1, "编码实现"),
        dev,
        review,
        _dispatch(30, 20, 40, "SUCCEEDED", 1_000, 1_000),
        _dispatch(31, 20, 41, "SUCCEEDED", 2_000, 2_000),
        _dispatch(32, 20, 40, "SUCCEEDED", 3_000, 3_000),
        _usage(30, 40, "20"),
        _usage(31, 41, "20"),
        _usage(32, 40, "30"),
    )
    progress = await get_delivery_progress(session, 100, 7)
    assert progress.total_usage is not None
    assert progress.total_usage.credits == Decimal("70")
    labels = [run.label for run in progress.total_usage.runs]
    assert labels == ["DEV run-1", "CR run-1", "DEV run-2"]
    dev_agent = next(agent for agent in progress.agents if agent.agent_id == 40)
    assert dev_agent.usage is not None
    assert dev_agent.usage.credits == Decimal("50")


async def test_credits_summary_omitted_when_nothing_recorded() -> None:
    session = MemorySession()
    await _load(
        session,
        _workitem(),
        _step(20, 10, 1, "编码实现"),
        _agent(40, "DEV"),
        _dispatch(30, 20, 40, "SUCCEEDED", 1_000, 1_000),
    )
    assert (await get_delivery_progress(session, 100, 7)).total_usage is None


async def test_credits_summary_is_null_when_usage_query_fails() -> None:
    session = _FailingUsageSession()
    await _load(
        session,
        _workitem(),
        _step(20, 10, 1, "编码实现"),
        _agent(40, "DEV"),
        _dispatch(30, 20, 40, "SUCCEEDED", 1_000, 1_000),
        _usage(30, 40, "20"),
    )
    progress = await get_delivery_progress(session, 100, 7)
    assert progress.total_usage is None
    assert all(agent.usage is None for agent in progress.agents)


async def test_missing_workitem_raises_not_found() -> None:
    session = MemorySession()
    try:
        await get_delivery_progress(session, 100, 7)
    except BizError as error:
        assert error.error_code == ErrorCode.WORKITEM_NOT_FOUND
    else:
        raise AssertionError("expected missing workitem")


def _usage(dispatch_id: int, agent_id: int, credits: str) -> DispatchAiUsage:
    return DispatchAiUsage(
        id=dispatch_id,
        tenant_id=7,
        workitem_id=100,
        dispatch_id=dispatch_id,
        agent_id=agent_id,
        step_id="20",
        provider="test",
        model="model",
        credits=Decimal(credits),
        gmt_create=_NOW,
        gmt_modified=_NOW,
        usage_at=_NOW,
    )
