"""实时仪表盘汇总。计算规则与 ``DashboardService`` 一致。"""

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dashboards.schemas import (
    ByLifecycle,
    ByType,
    CompletedWorkitemView,
    HealthView,
    InventoryView,
    KpiView,
    RealtimeDashboardView,
    RecentTaskView,
    RunningTaskView,
    SquadLineView,
    WorkstationView,
)
from autowonder.dashboards.sql import (
    AGENT_EXISTS,
    AVG_TODAY_COMPLETED_TASK_DURATION,
    AVG_TODAY_SUCCESS_DURATION,
    COUNT_ACTIVE_SQUADS,
    COUNT_IN_PROGRESS_WORKITEMS,
    COUNT_ONLINE_AGENTS,
    COUNT_QUEUED_DISPATCHES,
    COUNT_RUNNING_DISPATCHES,
    COUNT_TODAY_COMPLETED_TASKS,
    COUNT_TODAY_FAILED_OR_TIMEOUT,
    COUNT_TODAY_RETRIES,
    COUNT_TODAY_SUCCEEDED,
    COUNT_WEEK_COMPLETED_TASKS,
    COUNT_WORKITEMS_BY_LIFECYCLE,
    COUNT_WORKITEMS_BY_TYPE,
    FEED_LIMIT,
    LIST_AGENT_RUNNING,
    LIST_RECENT_FEED,
    LIST_RUNNING_FEED,
    LIST_RUNNING_WORKITEMS,
    LIST_TODAY_COMPLETED_WORKITEMS,
    LIST_WEEK_COMPLETED_WORKITEMS,
    ONLINE_WORKSTATIONS,
    SQUAD_IN_PROGRESS_WORKITEMS,
    SQUAD_LINE_AGGREGATES,
)


def java_round(value: float) -> int:
    """``Math.round(double)``：加 0.5 后向负无穷取整。"""
    return math.floor(value + 0.5)


def round1(value: float) -> float:
    """保留一位小数，舍入方式与 Java ``Math.round`` 一致。"""
    return java_round(value * 10) / 10.0


def round2(value: float) -> float:
    """保留两位小数，舍入方式与 Java ``Math.round`` 一致。"""
    return java_round(value * 100) / 100.0


def int_val(value: Any) -> int:
    """SQL 数字为空时按 0，与 ``DashboardService.intVal`` 一致。"""
    if value is None:
        return 0
    return int(value)


def optional_int(value: Any) -> int | None:
    """可空整数聚合。``None`` 留给调用方按 Java 回落为 0。"""
    if value is None:
        return None
    return int(value)


def text_val(value: Any) -> str | None:
    """SQL 文本。``None`` 保持空，其余按 ``toString``。"""
    if value is None:
        return None
    return str(value)


def long_val(value: Any) -> int | None:
    """SQL 长整型。``None`` 保持空。"""
    if value is None:
        return None
    return int(value)


def format_generated_at(moment: datetime) -> str:
    """生成时间，格式 ``yyyy-MM-dd HH:mm:ss``。"""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def ensure_agent_present(count: int) -> None:
    """当前工作空间没有该数字员工时拒绝展开。"""
    if count == 0:
        raise BizError(ErrorCode.AGENT_NOT_FOUND)


def build_kpi(
    running_dispatches: int,
    today_completed_tasks: int,
    week_completed_tasks: int,
    avg_task_duration_minutes: int | None,
    in_progress_workitems: int,
    queued_dispatches: int,
    active_squads: int,
    online_agents: int,
) -> KpiView:
    """组装 KPI。在线人数为 0 时负载为 0，平均时长为空时为 0。"""
    if online_agents > 0:
        avg_load = round2(running_dispatches / online_agents)
    else:
        avg_load = 0.0
    if avg_task_duration_minutes is None:
        duration = 0
    else:
        duration = avg_task_duration_minutes
    return KpiView(
        running_dispatches=running_dispatches,
        today_completed_tasks=today_completed_tasks,
        week_completed_tasks=week_completed_tasks,
        avg_task_duration_minutes=duration,
        in_progress_workitems=in_progress_workitems,
        queued_dispatches=queued_dispatches,
        active_squads=active_squads,
        online_agents=online_agents,
        avg_load=avg_load,
    )


def build_inventory(
    lifecycle_rows: Sequence[Mapping[str, Any]],
    type_rows: Sequence[Mapping[str, Any]],
) -> InventoryView:
    """把生命周期和工单类型计数填进固定分类。未知分类忽略。"""
    lifecycle = {"init": 0, "in_progress": 0, "done": 0, "canceled": 0}
    for row in lifecycle_rows:
        category = text_val(row.get("category"))
        count = int_val(row.get("cnt"))
        if category == "INIT":
            lifecycle["init"] = count
        elif category == "IN_PROGRESS":
            lifecycle["in_progress"] = count
        elif category == "DONE":
            lifecycle["done"] = count
        elif category == "CANCELED":
            lifecycle["canceled"] = count
    types = {"req": 0, "task": 0, "bug": 0}
    for row in type_rows:
        work_type = text_val(row.get("workType"))
        count = int_val(row.get("cnt"))
        if work_type == "REQ":
            types["req"] = count
        elif work_type == "TASK":
            types["task"] = count
        elif work_type == "BUG":
            types["bug"] = count
    return InventoryView(
        by_lifecycle=ByLifecycle.model_validate(lifecycle),
        by_type=ByType.model_validate(types),
    )


def build_squads(
    aggregates: Sequence[Mapping[str, Any]],
    in_progress_rows: Sequence[Mapping[str, Any]],
) -> list[SquadLineView]:
    """小队负载。成员数为 0 时负载为 0，没有进行中工单的小队记 0。"""
    in_progress: dict[int | None, int] = {}
    for row in in_progress_rows:
        in_progress[long_val(row.get("squadId"))] = int_val(row.get("cnt"))
    result: list[SquadLineView] = []
    for row in aggregates:
        squad_id = long_val(row.get("squadId"))
        members = int_val(row.get("members"))
        running_tasks = int_val(row.get("runningTasks"))
        if members > 0:
            load = round2(running_tasks / members)
        else:
            load = 0.0
        if squad_id in in_progress:
            progress = in_progress[squad_id]
        else:
            progress = 0
        result.append(
            SquadLineView(
                squad_id=squad_id,
                name=text_val(row.get("name")),
                members=members,
                online=int_val(row.get("online")),
                busy=int_val(row.get("busy")),
                running_tasks=running_tasks,
                in_progress_workitems=progress,
                load=load,
            )
        )
    return result


def build_workstations(rows: Sequence[Mapping[str, Any]]) -> list[WorkstationView]:
    """在线工位。运行中任务数大于 0 时标记忙碌。"""
    result: list[WorkstationView] = []
    for row in rows:
        running_tasks = int_val(row.get("runningTasks"))
        if running_tasks > 0:
            busy = True
        else:
            busy = False
        result.append(
            WorkstationView(
                agent_id=long_val(row.get("agentId")),
                name=text_val(row.get("name")),
                avatar_url=text_val(row.get("avatarUrl")),
                running_tasks=running_tasks,
                busy=busy,
            )
        )
    return result


def build_health(
    succeeded: int,
    failed_or_timeout: int,
    retries: int,
    avg_duration_minutes: int | None,
) -> HealthView:
    """成功率 = 成功 / (成功 + 失败或超时)。终态为 0 时成功率为 0。"""
    terminal = succeeded + failed_or_timeout
    if terminal > 0:
        success_rate = round1(succeeded / terminal * 100.0)
    else:
        success_rate = 0.0
    if avg_duration_minutes is None:
        duration = 0
    else:
        duration = avg_duration_minutes
    return HealthView(
        success_rate=success_rate,
        failed_or_timeout=failed_or_timeout,
        retries=retries,
        avg_duration_minutes=duration,
    )


def running_tasks(rows: Sequence[Mapping[str, Any]]) -> list[RunningTaskView]:
    """进行中调度行。"""
    return [
        RunningTaskView(
            dispatch_id=long_val(row.get("dispatchId")),
            agent_id=long_val(row.get("agentId")),
            agent_name=text_val(row.get("agentName")),
            workitem_id=long_val(row.get("workitemId")),
            workitem_title=text_val(row.get("workitemTitle")),
            step_name=text_val(row.get("stepName")),
            running_minutes=int_val(row.get("runningMinutes")),
        )
        for row in rows
    ]


def recent_tasks(rows: Sequence[Mapping[str, Any]]) -> list[RecentTaskView]:
    """最近结束的调度行。"""
    return [
        RecentTaskView(
            dispatch_id=long_val(row.get("dispatchId")),
            agent_name=text_val(row.get("agentName")),
            workitem_title=text_val(row.get("workitemTitle")),
            status=text_val(row.get("status")),
            duration_minutes=int_val(row.get("durationMinutes")),
            finished_at=text_val(row.get("finishedAt")),
        )
        for row in rows
    ]


def completed_workitems(rows: Sequence[Mapping[str, Any]]) -> list[CompletedWorkitemView]:
    """端到端成功工单行。"""
    return [
        CompletedWorkitemView(
            workitem_id=long_val(row.get("workitemId")),
            title=text_val(row.get("title")),
        )
        for row in rows
    ]


async def _scalar(
    session: AsyncSession,
    sql: str,
    tenant_id: int,
    **extra: int,
) -> Any:
    params: dict[str, int] = {"tenant_id": tenant_id}
    params.update(extra)
    result = await session.execute(text(sql), params)
    return result.scalar_one()


async def _rows(
    session: AsyncSession,
    sql: str,
    tenant_id: int,
    **extra: int,
) -> list[Mapping[str, Any]]:
    params: dict[str, int] = {"tenant_id": tenant_id}
    params.update(extra)
    result = await session.execute(text(sql), params)
    return cast(list[Mapping[str, Any]], list(result.mappings().all()))


async def get_realtime(session: AsyncSession, tenant_id: int) -> RealtimeDashboardView:
    """汇总当前工作空间的实时仪表盘。"""
    avg_task = optional_int(await _scalar(session, AVG_TODAY_COMPLETED_TASK_DURATION, tenant_id))
    avg_success = optional_int(await _scalar(session, AVG_TODAY_SUCCESS_DURATION, tenant_id))
    return RealtimeDashboardView(
        kpi=build_kpi(
            running_dispatches=int_val(
                await _scalar(session, COUNT_RUNNING_DISPATCHES, tenant_id)
            ),
            today_completed_tasks=int_val(
                await _scalar(session, COUNT_TODAY_COMPLETED_TASKS, tenant_id)
            ),
            week_completed_tasks=int_val(
                await _scalar(session, COUNT_WEEK_COMPLETED_TASKS, tenant_id)
            ),
            avg_task_duration_minutes=avg_task,
            in_progress_workitems=int_val(
                await _scalar(session, COUNT_IN_PROGRESS_WORKITEMS, tenant_id)
            ),
            queued_dispatches=int_val(await _scalar(session, COUNT_QUEUED_DISPATCHES, tenant_id)),
            active_squads=int_val(await _scalar(session, COUNT_ACTIVE_SQUADS, tenant_id)),
            online_agents=int_val(await _scalar(session, COUNT_ONLINE_AGENTS, tenant_id)),
        ),
        inventory=build_inventory(
            await _rows(session, COUNT_WORKITEMS_BY_LIFECYCLE, tenant_id),
            await _rows(session, COUNT_WORKITEMS_BY_TYPE, tenant_id),
        ),
        squads=build_squads(
            await _rows(session, SQUAD_LINE_AGGREGATES, tenant_id),
            await _rows(session, SQUAD_IN_PROGRESS_WORKITEMS, tenant_id),
        ),
        workstations=build_workstations(await _rows(session, ONLINE_WORKSTATIONS, tenant_id)),
        health=build_health(
            succeeded=int_val(await _scalar(session, COUNT_TODAY_SUCCEEDED, tenant_id)),
            failed_or_timeout=int_val(
                await _scalar(session, COUNT_TODAY_FAILED_OR_TIMEOUT, tenant_id)
            ),
            retries=int_val(await _scalar(session, COUNT_TODAY_RETRIES, tenant_id)),
            avg_duration_minutes=avg_success,
        ),
        running_feed=running_tasks(
            await _rows(session, LIST_RUNNING_FEED, tenant_id, limit=FEED_LIMIT)
        ),
        recent_feed=recent_tasks(
            await _rows(session, LIST_RECENT_FEED, tenant_id, limit=FEED_LIMIT)
        ),
        generated_at=format_generated_at(now_local()),
    )


async def get_agent_running(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
) -> list[RunningTaskView]:
    """展开一台数字员工当前运行中的工单调度。"""
    present = int_val(await _scalar(session, AGENT_EXISTS, tenant_id, agent_id=agent_id))
    ensure_agent_present(present)
    return running_tasks(await _rows(session, LIST_AGENT_RUNNING, tenant_id, agent_id=agent_id))


async def get_today_completed(
    session: AsyncSession,
    tenant_id: int,
) -> list[CompletedWorkitemView]:
    """今日端到端成功工单。"""
    return completed_workitems(await _rows(session, LIST_TODAY_COMPLETED_WORKITEMS, tenant_id))


async def get_week_completed(
    session: AsyncSession,
    tenant_id: int,
) -> list[CompletedWorkitemView]:
    """本周端到端成功工单。周一为一周起点。"""
    return completed_workitems(await _rows(session, LIST_WEEK_COMPLETED_WORKITEMS, tenant_id))


async def get_running_workitems(
    session: AsyncSession,
    tenant_id: int,
) -> list[RunningTaskView]:
    """全部运行中的工单调度，不截断。"""
    return running_tasks(await _rows(session, LIST_RUNNING_WORKITEMS, tenant_id))
