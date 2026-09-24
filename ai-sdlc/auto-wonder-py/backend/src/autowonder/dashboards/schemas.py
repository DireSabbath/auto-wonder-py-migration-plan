"""实时仪表盘响应。字段名与 Java VO 的 Jackson 序列化一致。"""

from autowonder.core.schema import ApiModel


class KpiView(ApiModel):
    """工坊总览指标。"""

    running_dispatches: int
    today_completed_tasks: int
    week_completed_tasks: int
    avg_task_duration_minutes: int
    in_progress_workitems: int
    queued_dispatches: int
    active_squads: int
    online_agents: int
    avg_load: float


class ByLifecycle(ApiModel):
    """工单按生命周期计数。未出现的分类为 0。"""

    init: int
    in_progress: int
    done: int
    canceled: int


class ByType(ApiModel):
    """工单按类型计数。未出现的类型为 0。"""

    req: int
    task: int
    bug: int


class InventoryView(ApiModel):
    """工单库存。"""

    by_lifecycle: ByLifecycle
    by_type: ByType


class SquadLineView(ApiModel):
    """一条小队负载。"""

    squad_id: int | None
    name: str | None
    members: int
    online: int
    busy: int
    running_tasks: int
    in_progress_workitems: int
    load: float


class WorkstationView(ApiModel):
    """一台在线数字员工工位。"""

    agent_id: int | None
    name: str | None
    avatar_url: str | None
    running_tasks: int
    busy: bool


class HealthView(ApiModel):
    """今日调度健康度。"""

    success_rate: float
    failed_or_timeout: int
    retries: int
    avg_duration_minutes: int


class RunningTaskView(ApiModel):
    """一条进行中的工单调度。"""

    dispatch_id: int | None
    agent_id: int | None
    agent_name: str | None
    workitem_id: int | None
    workitem_title: str | None
    step_name: str | None
    running_minutes: int


class RecentTaskView(ApiModel):
    """一条已结束的工单调度。"""

    dispatch_id: int | None
    agent_name: str | None
    workitem_title: str | None
    status: str | None
    duration_minutes: int
    finished_at: str | None


class CompletedWorkitemView(ApiModel):
    """一条端到端成功工单。"""

    workitem_id: int | None
    title: str | None


class RealtimeDashboardView(ApiModel):
    """实时仪表盘。"""

    kpi: KpiView
    inventory: InventoryView
    squads: list[SquadLineView]
    workstations: list[WorkstationView]
    health: HealthView
    running_feed: list[RunningTaskView]
    recent_feed: list[RecentTaskView]
    generated_at: str
