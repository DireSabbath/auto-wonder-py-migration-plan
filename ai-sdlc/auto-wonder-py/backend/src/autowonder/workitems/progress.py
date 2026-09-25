"""工单交付进度。

步骤耗时只来自运行时区间。数字员工和总耗时是非交互派发的创建到更新时间之和。
用量查询失败时进度仍返回，只是不带 credits。
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.aiusage.models import DispatchAiUsage
from autowonder.core.clock import now_local
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.action_text import looks_like_mojibake
from autowonder.dispatch.models import Dispatch, DispatchRuntimeEvent
from autowonder.evolution.jsontext import java_trim
from autowonder.executors.registry import is_online
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.sdlcs.models import SdlcStep
from autowonder.users.models import User
from autowonder.workitems.models import Workitem
from autowonder.workitems.participants import _resolve_squad_members
from autowonder.workitems.runtime_timeline import RuntimeStepTimeline
from autowonder.workitems.schemas import (
    AgentDeliveryProgressView,
    DeliveryProgressView,
    DeliveryStepView,
    DispatchAttemptView,
    ProcessGraphEdgeView,
    ProcessGraphNodeView,
    ProcessGraphView,
    StepUsageView,
    SubStepView,
    WorkflowPlanStepView,
    WorkflowPlanView,
    WorkitemUsageRunView,
    WorkitemUsageView,
)
from autowonder.workitems.service import _find_agent, _live_in_tenant, _version_sdlc
from autowonder.workitems.view import shanghai_millis

_STUCK_MS = 120_000
_PLAN_STATUSES = frozenset({"RUN", "REUSED", "SKIPPED"})
_DISPATCH_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
_PAUSEABLE = frozenset({"DISPATCHED", "ACKED", "RUNNING"})
_INTERACTION = frozenset({"SIDE_INTERACTION", "CANONICAL_INTERACTION", "COMMENT_INTERACTION"})
_IN_FLIGHT = frozenset({"PACKAGING", "DISPATCHED", "ACKED", "RUNNING"})
_NO_AGENT = object()


async def get_delivery_progress(
    session: AsyncSession, workitem_id: int, tenant_id: int
) -> DeliveryProgressView:
    """读取一个工单的交付进度。工单不存在时抛出 ``WORKITEM_NOT_FOUND``。"""
    workitem = await _live_in_tenant(session, workitem_id, tenant_id)
    now = now_local()
    now_ms = shanghai_millis(now)
    dispatches = await _workitem_dispatches(session, tenant_id, workitem_id)
    dispatches.sort(key=_dispatch_order)
    events = await _runtime_events(session, tenant_id, workitem_id, dispatches)
    guidance_rows = await _guidance_rows(session, tenant_id, workitem_id)
    names: dict[int, str | None] = {}
    step_lists: dict[int, list[SdlcStep]] = {}
    agents: list[AgentDeliveryProgressView] = []
    for agent_id in await _progress_agent_ids(session, tenant_id, workitem, dispatches):
        agent_view = await _build_agent(
            session,
            names,
            step_lists,
            workitem,
            tenant_id,
            agent_id,
            dispatches,
            events,
            now,
            now_ms,
        )
        if agent_view is not None:
            agents.append(agent_view)
    total_usage = await _enrich_usage(session, names, tenant_id, dispatches, agents)
    plan = await _workflow_plan(session, names, events)
    if not _apply_plan(plan, agents):
        plan = None
    steps = _compat_steps(workitem, agents)
    if len(steps) == 0 and workitem.sdlc_id is not None:
        steps = await _legacy_steps(session, names, step_lists, workitem, dispatches, now_ms)
    graph = await _process_graph(session, names, workitem, dispatches, guidance_rows)
    return DeliveryProgressView(
        steps=steps,
        agents=agents,
        workflow_plan=plan,
        process_graph=graph,
        total_duration_ms=_sum_agent_durations(agents),
        total_usage=total_usage,
    )


async def _build_agent(
    session: AsyncSession,
    names: dict[int, str | None],
    step_lists: dict[int, list[SdlcStep]],
    workitem: Workitem,
    tenant_id: int,
    agent_id: int,
    dispatches: list[Dispatch],
    events: list[DispatchRuntimeEvent],
    now: datetime,
    now_ms: int,
) -> AgentDeliveryProgressView | None:
    sdlc_id = await _progress_sdlc(session, tenant_id, agent_id, workitem.sdlc_id)
    if sdlc_id is None:
        return None
    sdlc_steps = await _steps_of(session, step_lists, sdlc_id)
    all_agent = [row for row in dispatches if row.agent_id == agent_id]
    formal = [row for row in all_agent if not _is_interaction(row)]
    latest_formal = _latest(formal)
    completed = _completed_workflow(formal)
    by_step: dict[int | None, list[Dispatch]] = {}
    for row in formal:
        _append_group(by_step, row.sdlc_step_id, row)
    display_events: list[DispatchRuntimeEvent] = []
    if latest_formal is not None:
        display_events = _events_for_dispatch(agent_id, latest_formal, events)
    timeline = RuntimeStepTimeline.from_events(display_events, sdlc_steps, now)
    resumable_id = None
    if latest_formal is not None:
        resumable_id = latest_formal.id
    step_views: list[DeliveryStepView] = []
    for step in sdlc_steps:
        step_views.append(
            await _formal_step(
                session,
                names,
                step,
                by_step.get(step.id),
                formal,
                latest_formal,
                completed,
                timeline,
                events,
                resumable_id,
                agent_id,
                now_ms,
            )
        )
    latest_any = _latest(all_agent)
    status = _agent_status(step_views)
    if latest_any is not None and _is_interaction(latest_any):
        if not _workitem_terminal(latest_any.status):
            status = "active"
    active_rows = [row for row in all_agent if not _workitem_terminal(row.status)]
    activity = None
    if len(active_rows) > 0:
        activity = _latest_activity(_events_for_agent(agent_id, active_rows, events))
    return AgentDeliveryProgressView(
        agent_id=agent_id,
        agent_name=await _agent_name(session, names, agent_id),
        status=status,
        duration_ms=_total_duration(formal),
        current_activity=activity,
        steps=step_views,
    )


async def _formal_step(
    session: AsyncSession,
    names: dict[int, str | None],
    step: SdlcStep,
    step_dispatches: list[Dispatch] | None,
    formal: list[Dispatch],
    latest_formal: Dispatch | None,
    completed: bool,
    timeline: RuntimeStepTimeline,
    events: list[DispatchRuntimeEvent],
    resumable_id: int | None,
    agent_id: int,
    now_ms: int,
) -> DeliveryStepView:
    latest_step = _latest(step_dispatches)
    runtime_status = timeline.status_of(step)
    if runtime_status is not None and timeline.is_current(step) and latest_formal is not None:
        runtime_status = _overlay_runtime(runtime_status, latest_formal)
    runtime_substeps: list[SubStepView] = []
    if runtime_status is not None:
        runtime_substeps = timeline.sub_steps_of(step, runtime_status)
    status = _formal_step_status(runtime_status, latest_step, step_dispatches, completed)
    error = None
    executor_name = None
    sub_steps = None
    if latest_step is not None:
        error = latest_step.error
        executor_name = await _agent_name(session, names, latest_step.agent_id)
        sub_steps = _dispatch_substeps(latest_step)
    last_event = None
    if runtime_status is not None:
        last_event = timeline.last_event_of(step)
    if last_event is not None and last_event.error is not None:
        if not looks_like_mojibake(last_event.error):
            error = last_event.error
    if runtime_status is not None and latest_step is None and len(formal) > 0:
        executor_name = await _agent_name(session, names, agent_id)
    if len(runtime_substeps) > 0:
        sub_steps = runtime_substeps
    return DeliveryStepView(
        step_id=step.id,
        step_key=step.code,
        name=step.name,
        status=status,
        executor_name=executor_name,
        error=error,
        sub_steps=sub_steps,
        duration_ms=timeline.duration_of(step),
        attempts=await _attempts(session, names, step_dispatches, resumable_id, events, now_ms),
    )


def _formal_step_status(
    runtime_status: str | None,
    latest_step: Dispatch | None,
    step_dispatches: list[Dispatch] | None,
    completed: bool,
) -> str:
    if runtime_status is not None and not completed:
        return runtime_status
    if latest_step is not None and latest_step.status == "CANCELED":
        return "cancelled"
    if latest_step is not None and latest_step.status == "PAUSED":
        return "paused"
    if latest_step is not None and _is_failed(latest_step.status):
        return "failed"
    if latest_step is not None and not _workitem_terminal(latest_step.status):
        return "active"
    succeeded = False
    if step_dispatches is not None:
        for row in step_dispatches:
            if row.status == "SUCCEEDED":
                succeeded = True
    if succeeded or completed:
        return "done"
    return "pending"


def _overlay_runtime(runtime_status: str, dispatch: Dispatch) -> str | None:
    if dispatch.status == "CANCELED":
        return "cancelled"
    if dispatch.status == "PAUSED":
        return "paused"
    if dispatch.status == "PENDING" and runtime_status == "failed":
        return None
    if _is_failed(dispatch.status):
        return "failed"
    return runtime_status


def _agent_status(steps: list[DeliveryStepView]) -> str:
    for step in steps:
        if step.status == "paused":
            return "paused"
    for step in steps:
        if step.status == "active":
            return "active"
    for step in steps:
        if step.status == "cancelled":
            return "cancelled"
    failed = False
    done = False
    for step in steps:
        if step.status == "failed":
            failed = True
        if step.status == "done":
            done = True
    if failed:
        return "failed"
    if done:
        return "finished"
    return "pending"


async def _legacy_steps(
    session: AsyncSession,
    names: dict[int, str | None],
    step_lists: dict[int, list[SdlcStep]],
    workitem: Workitem,
    dispatches: list[Dispatch],
    now_ms: int,
) -> list[DeliveryStepView]:
    sdlc_steps = await _steps_of(session, step_lists, workitem.sdlc_id)
    by_step: dict[int | None, list[Dispatch]] = {}
    for row in dispatches:
        _append_group(by_step, row.sdlc_step_id, row)
    latest_any = _latest(dispatches)
    resumable_id = None
    if latest_any is not None:
        resumable_id = latest_any.id
    views: list[DeliveryStepView] = []
    for step in sdlc_steps:
        step_dispatches = by_step.get(step.id)
        latest_step = _latest(step_dispatches)
        error = None
        executor_name = None
        sub_steps = None
        if latest_step is not None:
            error = latest_step.error
            executor_name = await _agent_name(session, names, latest_step.agent_id)
            sub_steps = _dispatch_substeps(latest_step)
        views.append(
            DeliveryStepView(
                step_id=step.id,
                step_key=step.code,
                name=step.name,
                status=_legacy_status(workitem, step, latest_step, step_dispatches),
                executor_name=executor_name,
                error=error,
                sub_steps=sub_steps,
                attempts=await _attempts(
                    session, names, step_dispatches, resumable_id, [], now_ms
                ),
            )
        )
    return views


def _legacy_status(
    workitem: Workitem,
    step: SdlcStep,
    latest_step: Dispatch | None,
    step_dispatches: list[Dispatch] | None,
) -> str:
    if latest_step is not None and latest_step.status == "CANCELED":
        return "cancelled"
    if latest_step is not None and _is_failed(latest_step.status):
        return "failed"
    if step.id is not None and step.id == workitem.current_step_id:
        return "active"
    if step_dispatches is not None:
        for row in step_dispatches:
            if row.status == "SUCCEEDED":
                return "done"
    return "pending"


async def _attempts(
    session: AsyncSession,
    names: dict[int, str | None],
    step_dispatches: list[Dispatch] | None,
    resumable_id: int | None,
    events: list[DispatchRuntimeEvent],
    now_ms: int,
) -> list[DispatchAttemptView]:
    if step_dispatches is None:
        return []
    latest_by_dispatch = _latest_event_by_dispatch(events)
    views: list[DispatchAttemptView] = []
    for row in step_dispatches:
        latest_event = None
        if row.id is not None:
            latest_event = latest_by_dispatch.get(row.id)
        runtime_failed = _attempt_runtime_failed(row, latest_event)
        status = row.status
        if runtime_failed and row.status != "CANCELED":
            status = "FAILED"
        error = row.error
        if runtime_failed or _executor_failover(row, latest_event):
            error = _runtime_failure_message(latest_event)
        offline = _executor_offline(row)
        same = row.id == resumable_id
        views.append(
            DispatchAttemptView(
                dispatch_id=row.id,
                executor_name=await _agent_name(session, names, row.agent_id),
                status=status,
                resume_mode=row.resume_mode,
                error=error,
                started_at=row.gmt_create,
                duration_ms=_duration_ms(row),
                can_continue=_can_continue(row, same, offline, now_ms),
                can_pause=_can_pause(row, same, offline, runtime_failed),
            )
        )
    return views


def _attempt_runtime_failed(row: Dispatch, event: DispatchRuntimeEvent | None) -> bool:
    if row.status == "PENDING" or _dispatch_terminal(row.status):
        return False
    return _runtime_failure(event)


def _executor_failover(row: Dispatch, event: DispatchRuntimeEvent | None) -> bool:
    if row.status != "PENDING" or event is None:
        return False
    return event.event_type == "dispatch.executor_failover"


def _executor_offline(row: Dispatch) -> bool:
    if row.executor_id is None:
        return True
    return not is_online(row.executor_id)


def _can_continue(row: Dispatch, same: bool, offline: bool, now_ms: int) -> bool:
    if not same or not _dispatch_can_continue(row, now_ms):
        return False
    if row.status == "PAUSED" or _dispatch_terminal(row.status) or offline:
        return True
    return False


def _can_pause(row: Dispatch, same: bool, offline: bool, runtime_failed: bool) -> bool:
    if not same or runtime_failed or offline:
        return False
    if _pauseable(row.status) or row.status == "PAUSING" or row.status == "PAUSE_FAILED":
        return True
    return False


def _dispatch_can_continue(row: Dispatch, now_ms: int) -> bool:
    if row.status == "SUCCEEDED" or row.status == "PAUSED":
        return row.status == "PAUSED"
    if row.status == "PAUSING" or row.status == "PAUSE_FAILED":
        return True
    if row.status == "FAILED" or row.status == "TIMEOUT" or row.status == "CANCELED":
        return True
    if row.status not in _IN_FLIGHT or row.gmt_modified is None:
        return False
    return shanghai_millis(row.gmt_modified) < now_ms - _STUCK_MS


def _dispatch_substeps(row: Dispatch) -> list[SubStepView] | None:
    status = row.status
    if status == "PENDING":
        return [
            _sub("启动交付", "done"),
            _sub("等待调度执行", "active"),
            _sub("客户端接单", "pending"),
            _sub("执行中", "pending"),
        ]
    if status == "PACKAGING":
        return [
            _sub("启动交付", "done"),
            _sub("准备执行上下文", "active"),
            _sub("客户端接单", "pending"),
            _sub("执行中", "pending"),
        ]
    if status == "DISPATCHED":
        return [
            _sub("启动交付", "done"),
            _sub("准备执行上下文", "done"),
            _sub("等待客户端接单", "active"),
            _sub("执行中", "pending"),
        ]
    if status == "ACKED":
        return [
            _sub("启动交付", "done"),
            _sub("准备执行上下文", "done"),
            _sub("客户端已接单", "active"),
            _sub("执行中", "pending"),
        ]
    if status == "RUNNING":
        return [
            _sub("启动交付", "done"),
            _sub("准备执行上下文", "done"),
            _sub("客户端已接单", "done"),
            _sub("正在执行", "active"),
        ]
    if status == "PAUSING":
        return [
            _sub("启动交付", "done"),
            _sub("客户端已接单", "done"),
            _sub("等待当前动作安全结束", "active"),
            _sub("保存恢复检查点", "pending"),
        ]
    if status == "PAUSED":
        return [
            _sub("启动交付", "done"),
            _sub("客户端已接单", "done"),
            _sub("当前动作已安全结束", "done"),
            _sub("已暂停，恢复检查点已保存", "done"),
        ]
    if status == "SUCCEEDED":
        return [
            _sub("启动交付", "done"),
            _sub("准备执行上下文", "done"),
            _sub("客户端已接单", "done"),
            _sub("执行完成", "done"),
        ]
    if status == "CANCELED":
        return [_sub("本次执行已取消", "cancelled")]
    if _is_failed(status):
        if row.executor_id is None:
            return [_sub("启动交付", "done"), _sub("未派发到客户端", "failed")]
        return [
            _sub("启动交付", "done"),
            _sub("准备执行上下文", "done"),
            _sub("已分配执行器", "done"),
            _sub("执行失败", "failed"),
        ]
    return None


def _sub(name: str, status: str) -> SubStepView:
    return SubStepView(name=name, status=status)


def _compat_steps(
    workitem: Workitem, agents: list[AgentDeliveryProgressView]
) -> list[DeliveryStepView]:
    if len(agents) == 0:
        return []
    for agent in agents:
        if _has_blocking_step(agent):
            return agent.steps
    for agent in agents:
        if agent.status == "active":
            return agent.steps
    for agent in agents:
        if workitem.assignee_ref == agent.agent_id:
            return agent.steps
    return agents[len(agents) - 1].steps


def _has_blocking_step(agent: AgentDeliveryProgressView) -> bool:
    for step in agent.steps:
        if step.status == "active" or step.status == "paused" or step.status == "failed":
            return True
    return False


async def _workflow_plan(
    session: AsyncSession,
    names: dict[int, str | None],
    events: list[DispatchRuntimeEvent],
) -> WorkflowPlanView | None:
    latest = _latest_plan_event(events)
    if latest is None or latest.detail_json is None:
        return None
    if isinstance(latest.detail_json, str) and java_is_blank(latest.detail_json):
        return None
    try:
        detail = _detail_object(latest.detail_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return await _plan_view(session, names, latest, detail)


def _latest_plan_event(
    events: list[DispatchRuntimeEvent],
) -> DispatchRuntimeEvent | None:
    chosen: DispatchRuntimeEvent | None = None
    for event in events:
        if event.event_type != "workflow.plan_applied":
            continue
        if chosen is None or _plan_event_after(event, chosen):
            chosen = event
    return chosen


def _plan_event_after(left: DispatchRuntimeEvent, right: DispatchRuntimeEvent) -> bool:
    if left.id is not None and right.id is not None:
        return left.id > right.id
    if left.gmt_create is not None and right.gmt_create is not None:
        return left.gmt_create > right.gmt_create
    return False


def _detail_object(raw: object) -> dict[str, Any]:
    parsed: object = raw
    if isinstance(raw, str):
        parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError("workflow plan detail")
    return parsed


async def _plan_view(
    session: AsyncSession,
    names: dict[int, str | None],
    event: DispatchRuntimeEvent,
    detail: dict[str, Any],
) -> WorkflowPlanView | None:
    revision = _json_int(detail.get("revision"))
    target = detail.get("targetStepId")
    raw_steps = detail.get("steps")
    if revision is None or revision < 1:
        return None
    if not isinstance(target, str) or java_is_blank(target):
        return None
    if not isinstance(raw_steps, list) or len(raw_steps) == 0:
        return None
    steps: list[WorkflowPlanStepView] = []
    for raw in raw_steps:
        parsed = _plan_step(raw)
        if parsed is None:
            return None
        steps.append(parsed)
    guidance_ids: list[int | None] = []
    raw_ids = detail.get("sourceGuidanceIds")
    if isinstance(raw_ids, list):
        for item in raw_ids:
            guidance_ids.append(_json_int(item))
    reason = detail.get("reason")
    reason_text = None
    if isinstance(reason, str):
        reason_text = reason
    return WorkflowPlanView(
        revision=revision,
        agent_id=event.agent_id,
        agent_name=await _agent_name(session, names, event.agent_id),
        target_step_id=target,
        reason=reason_text,
        source_guidance_ids=guidance_ids,
        steps=steps,
    )


def _plan_step(raw: object) -> WorkflowPlanStepView | None:
    if not isinstance(raw, dict):
        return None
    step_key = raw.get("stepKey")
    name = raw.get("name")
    plan_status = raw.get("planStatus")
    key_text = step_key if isinstance(step_key, str) else None
    name_text = name if isinstance(name, str) else None
    key_blank = key_text is None or java_is_blank(key_text)
    name_blank = name_text is None or java_is_blank(name_text)
    if key_blank and name_blank:
        return None
    if not isinstance(plan_status, str) or plan_status not in _PLAN_STATUSES:
        return None
    return WorkflowPlanStepView(
        step_key=key_text,
        name=name_text,
        plan_status=plan_status,
        source_attempt=_json_int(raw.get("sourceAttempt")),
    )


def _apply_plan(plan: WorkflowPlanView | None, agents: list[AgentDeliveryProgressView]) -> bool:
    if plan is None:
        return True
    matches: list[tuple[DeliveryStepView, WorkflowPlanStepView]] = []
    for agent in agents:
        if plan.agent_id is not None and plan.agent_id != agent.agent_id:
            continue
        for planned in plan.steps:
            step = _match_planned_step(agent.steps, planned)
            if step is None:
                return False
            matches.append((step, planned))
        break
    if len(matches) != len(plan.steps):
        return False
    for step, planned in matches:
        step.plan_status = planned.plan_status
        step.source_attempt = planned.source_attempt
    return True


def _match_planned_step(
    steps: list[DeliveryStepView], planned: WorkflowPlanStepView
) -> DeliveryStepView | None:
    for step in steps:
        key_match = planned.step_key is not None and planned.step_key == step.step_key
        name_match = planned.name is not None and planned.name == step.name
        if key_match or name_match:
            return step
    return None


def _json_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def _process_graph(
    session: AsyncSession,
    names: dict[int, str | None],
    workitem: Workitem,
    dispatches: list[Dispatch],
    guidance_rows: list[WorkitemCommentDelivery],
) -> ProcessGraphView:
    formal = [item for item in dispatches if not _is_interaction(item) and item.id is not None]
    formal.sort(key=lambda item: item.id)
    by_id: dict[int, Dispatch] = {}
    for item in formal:
        if item.id not in by_id:
            by_id[item.id] = item
    guidance_by_dispatch: dict[int, WorkitemCommentDelivery] = {}
    for guidance in guidance_rows:
        if guidance.dispatch_id is not None and guidance.dispatch_id not in guidance_by_dispatch:
            guidance_by_dispatch[guidance.dispatch_id] = guidance
    nodes: list[ProcessGraphNodeView] = []
    edges: list[ProcessGraphEdgeView] = []
    nodes_by_dispatch: dict[int, ProcessGraphNodeView] = {}
    known_steps: dict[int, SdlcStep | None] = {}
    for item in formal:
        node = await _graph_node(session, names, known_steps, item)
        nodes.append(node)
        nodes_by_dispatch[item.id] = node
    for target in formal:
        _link_target(edges, by_id, nodes_by_dispatch, guidance_by_dispatch, target)
    await _append_human(session, workitem, formal, nodes, edges)
    return ProcessGraphView(nodes=nodes, edges=edges)


def _link_target(
    edges: list[ProcessGraphEdgeView],
    by_id: dict[int, Dispatch],
    nodes_by_dispatch: dict[int, ProcessGraphNodeView],
    guidance_by_dispatch: dict[int, WorkitemCommentDelivery],
    target: Dispatch,
) -> None:
    if target.resume_mode == "COMMENT_REWORK":
        side_id = _parse_prefixed_id(target.idempotency_key, "interaction-rework:")
        guidance = None
        if side_id is not None:
            guidance = guidance_by_dispatch.get(side_id)
        comment_id = None
        if guidance is not None:
            comment_id = guidance.comment_id
        nodes_by_dispatch[target.id].trigger_comment_id = comment_id
        label = "用户返工"
        if comment_id is not None:
            label = "用户返工（评论 #" + str(comment_id) + "）"
        source_id = _parse_prefixed_id(target.result_summary, "waitForDispatchId=")
        _add_edge(edges, by_id, source_id, target, "COMMENT_REWORK", comment_id, label)
        return
    handoff = _parse_prefixed_id(target.idempotency_key, "handoff:")
    if handoff is not None:
        source = by_id.get(handoff)
        label = "交接"
        if source is not None and _is_failed(source.status):
            label = "失败后交接"
        _add_edge(edges, by_id, handoff, target, "HANDOFF", None, label)
        return
    if target.resume_from_dispatch_id is not None:
        _add_edge(
            edges,
            by_id,
            target.resume_from_dispatch_id,
            target,
            "CONTINUE",
            None,
            "恢复执行",
        )


async def _graph_node(
    session: AsyncSession,
    names: dict[int, str | None],
    known_steps: dict[int, SdlcStep | None],
    row: Dispatch,
) -> ProcessGraphNodeView:
    step_name = None
    if row.sdlc_step_id is not None:
        if row.sdlc_step_id not in known_steps:
            known_steps[row.sdlc_step_id] = await _step_by_id(session, row.sdlc_step_id)
        step = known_steps[row.sdlc_step_id]
        if step is not None:
            step_name = step.name
    return ProcessGraphNodeView(
        key="dispatch:" + str(row.id),
        dispatch_id=row.id,
        agent_id=row.agent_id,
        agent_name=await _agent_name(session, names, row.agent_id),
        step_id=row.sdlc_step_id,
        step_name=step_name,
        status=row.status,
        started_at=row.gmt_create,
        duration_ms=_duration_ms(row),
        error=row.error,
    )


def _add_edge(
    edges: list[ProcessGraphEdgeView],
    by_id: dict[int, Dispatch],
    source_id: int | None,
    target: Dispatch,
    edge_type: str,
    comment_id: int | None,
    label: str,
) -> None:
    if source_id is None or target.id is None or source_id not in by_id:
        return
    edges.append(
        ProcessGraphEdgeView(
            source_key="dispatch:" + str(source_id),
            target_key="dispatch:" + str(target.id),
            type=edge_type,
            source_dispatch_id=source_id,
            target_dispatch_id=target.id,
            comment_id=comment_id,
            label=label,
        )
    )


async def _append_human(
    session: AsyncSession,
    workitem: Workitem,
    formal: list[Dispatch],
    nodes: list[ProcessGraphNodeView],
    edges: list[ProcessGraphEdgeView],
) -> None:
    if workitem.assignee_type != "HUMAN" or workitem.assignee_ref is None:
        return
    if len(formal) == 0:
        return
    latest = formal[len(formal) - 1]
    if latest.status != "SUCCEEDED" or latest.id is None:
        return
    user = await session.scalar(
        select(User).where(User.id == workitem.assignee_ref, User.is_deleted == 0).limit(1)
    )
    nodes.append(
        ProcessGraphNodeView(
            key="human:" + str(workitem.assignee_ref),
            agent_name=_human_name(user),
            status="HUMAN",
        )
    )
    edges.append(
        ProcessGraphEdgeView(
            source_key="dispatch:" + str(latest.id),
            target_key="human:" + str(workitem.assignee_ref),
            type="HUMAN_HANDOFF",
            source_dispatch_id=latest.id,
            label="交接真人",
        )
    )


def _human_name(user: User | None) -> str:
    nickname = None
    username = None
    if user is not None:
        nickname = user.nickname
        username = user.username
    if nickname is not None and not java_is_blank(nickname):
        return nickname
    if username is not None and not java_is_blank(username):
        return username
    return "真人"


def _parse_prefixed_id(value: str | None, prefix: str) -> int | None:
    if value is None or not value.startswith(prefix):
        return None
    try:
        return int(value[len(prefix) :])
    except ValueError:
        return None


async def _enrich_usage(
    session: AsyncSession,
    names: dict[int, str | None],
    tenant_id: int,
    dispatches: list[Dispatch],
    agents: list[AgentDeliveryProgressView],
) -> WorkitemUsageView | None:
    if len(dispatches) == 0:
        return None
    dispatch_ids = [row.id for row in dispatches if row.id is not None]
    if len(dispatch_ids) == 0:
        return None
    rows = await _usage_rows(session, tenant_id, dispatch_ids)
    if rows is None or len(rows) == 0:
        return None
    by_agent: dict[int, list[DispatchAiUsage]] = {}
    for row in rows:
        if row.agent_id is None:
            continue
        _append_group(by_agent, row.agent_id, row)
    for agent in agents:
        agent_rows = by_agent.get(agent.agent_id)
        if agent_rows is None or len(agent_rows) == 0:
            continue
        agent.usage = _aggregate_usage(agent_rows)
        _apply_step_usage(agent, agent_rows)
    return await _summarize_usage(session, names, rows, dispatches)


def _apply_step_usage(agent: AgentDeliveryProgressView, rows: list[DispatchAiUsage]) -> None:
    by_step: dict[str, list[DispatchAiUsage]] = {}
    for row in rows:
        if row.step_id is None or row.step_id == "":
            continue
        _append_group(by_step, row.step_id, row)
    if len(by_step) == 0:
        return
    for step in agent.steps:
        step_rows = by_step.get(_usage_step_key(step.step_id))
        if step_rows is None or len(step_rows) == 0:
            continue
        step.usage = _aggregate_usage(step_rows)


def _usage_step_key(step_id: int | None) -> str:
    if step_id is None:
        return "null"
    return str(step_id)


def _aggregate_usage(rows: list[DispatchAiUsage]) -> StepUsageView:
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    reasoning = 0
    credits = Decimal(0)
    model = None
    for row in rows:
        input_tokens = input_tokens + _token(row.input_tokens)
        output_tokens = output_tokens + _token(row.output_tokens)
        cache_read = cache_read + _token(row.cache_read_tokens)
        reasoning = reasoning + _token(row.reasoning_tokens)
        if row.credits is not None:
            credits = credits + _decimal(row.credits)
        if model is None and row.model is not None:
            model = row.model
    shown: Decimal | None = credits
    if credits == 0:
        shown = None
    return StepUsageView(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        reasoning_tokens=reasoning,
        credits=shown,
    )


async def _summarize_usage(
    session: AsyncSession,
    names: dict[int, str | None],
    rows: list[DispatchAiUsage],
    dispatches: list[Dispatch],
) -> WorkitemUsageView | None:
    credits_by_dispatch: dict[int, Decimal] = {}
    agent_by_dispatch: dict[int, int] = {}
    for usage in rows:
        if usage.dispatch_id is None or usage.credits is None:
            continue
        amount = credits_by_dispatch.get(usage.dispatch_id)
        if amount is None:
            credits_by_dispatch[usage.dispatch_id] = _decimal(usage.credits)
        else:
            credits_by_dispatch[usage.dispatch_id] = amount + _decimal(usage.credits)
        if usage.agent_id is not None and usage.dispatch_id not in agent_by_dispatch:
            agent_by_dispatch[usage.dispatch_id] = usage.agent_id
    if len(credits_by_dispatch) == 0:
        return None
    next_run: dict[object, int] = {}
    total = Decimal(0)
    runs: list[WorkitemUsageRunView] = []
    for dispatch in _ordered_for_usage(dispatches):
        credits = credits_by_dispatch.pop(dispatch.id, None)
        run_index = _next_run(next_run, dispatch.agent_id)
        if credits is None:
            continue
        total = total + credits
        await _add_run(session, names, runs, dispatch.agent_id, run_index, credits)
    for dispatch_id in sorted(credits_by_dispatch):
        credits = credits_by_dispatch[dispatch_id]
        agent_id = agent_by_dispatch.get(dispatch_id)
        run_index = _next_run(next_run, agent_id)
        total = total + credits
        await _add_run(session, names, runs, agent_id, run_index, credits)
    if total <= 0 or len(runs) == 0:
        return None
    return WorkitemUsageView(credits=total, runs=runs)


async def _add_run(
    session: AsyncSession,
    names: dict[int, str | None],
    runs: list[WorkitemUsageRunView],
    agent_id: int | None,
    run_index: int,
    credits: Decimal,
) -> None:
    if credits <= 0:
        return
    agent_name = await _agent_name(session, names, agent_id)
    runs.append(
        WorkitemUsageRunView(
            agent_id=agent_id,
            agent_name=agent_name,
            run_index=run_index,
            label=_display_name(agent_id, agent_name) + " run-" + str(run_index),
            credits=credits,
        )
    )


def _display_name(agent_id: int | None, agent_name: str | None) -> str:
    if agent_name is not None and not java_is_blank(agent_name):
        return java_trim(agent_name)
    if agent_id is None:
        return "unknown"
    return "agent-" + str(agent_id)


def _next_run(counter: dict[object, int], agent_id: int | None) -> int:
    key: object = _NO_AGENT
    if agent_id is not None:
        key = agent_id
    current = counter.get(key, 0) + 1
    counter[key] = current
    return current


def _ordered_for_usage(dispatches: list[Dispatch]) -> list[Dispatch]:
    rows = [row for row in dispatches if row.id is not None]
    rows.sort(key=lambda row: (_usage_time_key(row.gmt_create), row.id))
    return rows


def _usage_time_key(value: datetime | None) -> tuple[int, datetime]:
    if value is None:
        return (1, datetime.max)
    return (0, value)


async def _usage_rows(
    session: AsyncSession, tenant_id: int, dispatch_ids: list[int]
) -> list[DispatchAiUsage] | None:
    try:
        result = await session.scalars(
            select(DispatchAiUsage).where(
                DispatchAiUsage.tenant_id == tenant_id,
                DispatchAiUsage.dispatch_id.in_(dispatch_ids),
            )
        )
    except Exception:
        return None
    return list(result.all())


def _token(value: int | None) -> int:
    if value is None:
        return 0
    return value


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


async def _progress_agent_ids(
    session: AsyncSession,
    tenant_id: int,
    workitem: Workitem,
    dispatches: list[Dispatch],
) -> list[int]:
    ids: list[int] = []
    for row in dispatches:
        if row.agent_id is not None and row.agent_id not in ids:
            ids.append(row.agent_id)
    if workitem.assignee_type == "AGENT" and workitem.assignee_ref is not None:
        if workitem.assignee_ref not in ids:
            ids.append(workitem.assignee_ref)
    members = await _resolve_squad_members(session, tenant_id, list(ids))
    for member in members:
        if member.agent_id is not None and member.agent_id not in ids:
            ids.append(member.agent_id)
    return ids


async def _progress_sdlc(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    fallback: int | None,
) -> int | None:
    agent = await _find_agent(session, agent_id)
    resolved = None
    if agent is not None and agent.tenant_id == tenant_id:
        resolved = await _version_sdlc(session, agent, tenant_id)
    if resolved is None:
        return fallback
    return resolved


async def _steps_of(
    session: AsyncSession, cache: dict[int, list[SdlcStep]], sdlc_id: int | None
) -> list[SdlcStep]:
    if sdlc_id is None:
        return []
    cached = cache.get(sdlc_id)
    if cached is not None:
        return cached
    result = await session.scalars(
        select(SdlcStep).where(SdlcStep.sdlc_id == sdlc_id, SdlcStep.is_deleted == 0)
    )
    steps = list(result.all())
    steps.sort(key=lambda step: 0 if step.step_order is None else step.step_order)
    cache[sdlc_id] = steps
    return steps


async def _step_by_id(session: AsyncSession, step_id: int) -> SdlcStep | None:
    return await session.scalar(
        select(SdlcStep).where(SdlcStep.id == step_id, SdlcStep.is_deleted == 0).limit(1)
    )


async def _agent_name(
    session: AsyncSession, names: dict[int, str | None], agent_id: int | None
) -> str | None:
    if agent_id is None:
        return None
    if agent_id in names:
        return names[agent_id]
    agent: Agent | None = await _find_agent(session, agent_id)
    name = None
    if agent is not None:
        name = agent.name
    names[agent_id] = name
    return name


async def _workitem_dispatches(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> list[Dispatch]:
    result = await session.scalars(
        select(Dispatch).where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.workitem_id == workitem_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.is_deleted == 0,
        )
    )
    return list(result.all())


async def _runtime_events(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatches: list[Dispatch],
) -> list[DispatchRuntimeEvent]:
    result = await session.scalars(
        select(DispatchRuntimeEvent).where(
            DispatchRuntimeEvent.tenant_id == tenant_id,
            DispatchRuntimeEvent.workitem_id == workitem_id,
        )
    )
    allowed = {row.id for row in dispatches if row.id is not None}
    chosen = [event for event in result.all() if event.dispatch_id in allowed]
    chosen.sort(key=lambda event: 0 if event.id is None else event.id)
    return chosen


async def _guidance_rows(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> list[WorkitemCommentDelivery]:
    result = await session.scalars(
        select(WorkitemCommentDelivery).where(
            WorkitemCommentDelivery.tenant_id == tenant_id,
            WorkitemCommentDelivery.source_type == "WORKITEM",
            WorkitemCommentDelivery.workitem_id == workitem_id,
        )
    )
    rows = list(result.all())
    rows.sort(key=lambda row: 0 if row.id is None else row.id)
    return rows


def _dispatch_order(row: Dispatch) -> tuple[datetime, int]:
    created = row.gmt_create
    if created is None:
        created = datetime.min
    identity = 0
    if row.id is not None:
        identity = row.id
    return (created, identity)


def _append_group(groups: dict[Any, list[Any]], key: Any, row: Any) -> None:
    bucket = groups.get(key)
    if bucket is None:
        bucket = []
        groups[key] = bucket
    bucket.append(row)


def _latest(rows: list[Dispatch] | None) -> Dispatch | None:
    if rows is None or len(rows) == 0:
        return None
    return rows[len(rows) - 1]


def _completed_workflow(dispatches: list[Dispatch]) -> bool:
    if len(dispatches) == 0:
        return False
    latest = dispatches[len(dispatches) - 1]
    if latest.status != "SUCCEEDED":
        return False
    for row in dispatches:
        if not _workitem_terminal(row.status):
            return False
    distinct: set[int] = set()
    for row in dispatches:
        if row.sdlc_step_id is not None:
            distinct.add(row.sdlc_step_id)
    return len(distinct) <= 1


def _events_for_agent(
    agent_id: int,
    dispatches: list[Dispatch],
    events: list[DispatchRuntimeEvent],
) -> list[DispatchRuntimeEvent]:
    if len(events) == 0:
        return []
    dispatch_ids = {row.id for row in dispatches if row.id is not None}
    chosen: list[DispatchRuntimeEvent] = []
    for event in events:
        if event.agent_id != agent_id:
            continue
        if event.dispatch_id is None or event.dispatch_id in dispatch_ids:
            chosen.append(event)
    return chosen


def _events_for_dispatch(
    agent_id: int, dispatch: Dispatch, events: list[DispatchRuntimeEvent]
) -> list[DispatchRuntimeEvent]:
    if dispatch.id is None or len(events) == 0:
        return []
    return [
        event
        for event in events
        if event.agent_id == agent_id and event.dispatch_id == dispatch.id
    ]


def _latest_activity(events: list[DispatchRuntimeEvent]) -> str | None:
    chosen: DispatchRuntimeEvent | None = None
    for event in events:
        if event.event_type != "agent.progress":
            continue
        if event.message is None or java_is_blank(event.message):
            continue
        if looks_like_mojibake(event.message):
            continue
        if chosen is None or _activity_after(event, chosen):
            chosen = event
    if chosen is None:
        return None
    return chosen.message


def _activity_after(left: DispatchRuntimeEvent, right: DispatchRuntimeEvent) -> bool:
    if left.id is not None and right.id is not None:
        return left.id > right.id
    if left.event_time is not None and right.event_time is not None:
        return left.event_time > right.event_time
    return False


def _latest_event_by_dispatch(
    events: list[DispatchRuntimeEvent],
) -> dict[int, DispatchRuntimeEvent]:
    latest: dict[int, DispatchRuntimeEvent] = {}
    for event in events:
        if event.dispatch_id is None:
            continue
        current = latest.get(event.dispatch_id)
        if current is None or _runtime_event_after(event, current):
            latest[event.dispatch_id] = event
    return latest


def _runtime_event_after(left: DispatchRuntimeEvent, right: DispatchRuntimeEvent) -> bool:
    if left.id is not None and right.id is not None:
        return left.id > right.id
    if left.gmt_create is not None and right.gmt_create is not None:
        return left.gmt_create > right.gmt_create
    if left.event_time is not None and right.event_time is not None:
        return left.event_time > right.event_time
    return False


def _runtime_failure(event: DispatchRuntimeEvent | None) -> bool:
    if event is None:
        return False
    return event.error is not None or event.event_type in {
        "step.failed",
        "dispatch.failed",
        "runtime.failed",
        "task.failed",
    }


def _runtime_failure_message(event: DispatchRuntimeEvent | None) -> str | None:
    if event is None:
        return None
    if event.error is not None and not java_is_blank(event.error):
        return event.error
    if event.message is not None and not java_is_blank(event.message):
        return event.message
    return event.event_type


def _duration_ms(row: Dispatch) -> int | None:
    if row.gmt_create is None or row.gmt_modified is None:
        return None
    return shanghai_millis(row.gmt_modified) - shanghai_millis(row.gmt_create)


def _total_duration(dispatches: list[Dispatch]) -> int | None:
    total = 0
    any_duration = False
    for row in dispatches:
        duration = _duration_ms(row)
        if duration is None:
            continue
        total = total + duration
        any_duration = True
    if any_duration:
        return total
    return None


def _sum_agent_durations(agents: list[AgentDeliveryProgressView]) -> int | None:
    total = 0
    any_duration = False
    for agent in agents:
        if agent.duration_ms is None:
            continue
        total = total + agent.duration_ms
        any_duration = True
    if any_duration:
        return total
    return None


def _is_interaction(row: Dispatch) -> bool:
    return row.resume_mode in _INTERACTION


def _dispatch_terminal(status: str | None) -> bool:
    return status in _DISPATCH_TERMINAL


def _pauseable(status: str | None) -> bool:
    return status in _PAUSEABLE


def _is_failed(status: str | None) -> bool:
    return status in {"FAILED", "TIMEOUT", "CANCELED", "PAUSE_FAILED"}


def _workitem_terminal(status: str | None) -> bool:
    return status == "SUCCEEDED" or _is_failed(status)
