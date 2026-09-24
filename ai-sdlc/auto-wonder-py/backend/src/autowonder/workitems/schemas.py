"""工单请求和响应。JSON 字段使用 camelCase。"""

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from autowonder.core.clock import SHANGHAI
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel


class WorkitemOriginView(ApiModel):
    """服务端回链。请求体不能写入来源。"""

    type: str
    id: int
    scheduled_task_id: int | None = None
    scheduled_task_name: str | None = None


class WorkitemView(ApiModel):
    """工单卡片。时间为上海本地钟，序列化成毫秒。"""

    id: int
    work_type: str
    title: str
    execution_status: str | None = None
    content_md: str | None = None
    template_id: int | None = None
    status_node_id: int | None = None
    status_name: str | None = None
    sdlc_id: int | None = None
    sdlc_name: str | None = None
    assignee_type: str | None = None
    assignee_ref: int | None = None
    assignee_name: str | None = None
    assignee_display_name: str | None = None
    creator_id: int | None = None
    creator_name: str | None = None
    creator_display_name: str | None = None
    priority: int
    version: int
    gmt_create: datetime
    gmt_modified: datetime
    health: str | None = None
    health_reason: str | None = None
    pending_decision: bool = False
    source_type: str | None = None
    source_provider: str | None = None
    source_url: str | None = None
    deletable: bool | None = None
    deletable_reason: str | None = None
    origin: WorkitemOriginView | None = None
    external_collaboration: None = None
    source_creator: None = None
    scheduled_start_at: datetime | None = None
    scheduled_start_triggered_at: datetime | None = None
    scheduled_phase: str | None = None
    tags: list[str]
    watched: bool | None = None


class CreateWorkitemRequest(ApiModel):
    """创建工单。来源只能由服务端调用写入。"""

    work_type: str | None = None
    title: str | None = None
    content_md: str | None = None
    priority: int | None = None
    assignee_type: str | None = None
    assignee_ref: int | None = None
    sdlc_id: int | None = None
    squad_id: int | None = None
    scheduled_start_at: datetime | int | float | str | None = None


class AssignRequest(ApiModel):
    """指派。计划时间为空表示立即交付。"""

    assignee_type: str | None = None
    assignee_ref: int | None = None
    sdlc_id: int | None = None
    squad_id: int | None = None
    scheduled_start_at: datetime | int | float | str | None = None


class ScheduledStartRequest(ApiModel):
    """改期、取消或立即执行。``executeNow`` 优先于新的计划时间。"""

    scheduled_start_at: datetime | int | float | str | None = None
    execute_now: bool | None = None


class TransitionRequest(ApiModel):
    """流转。目标节点为空时控制器直接拒绝。"""

    to_node_id: int | None = None
    from_node_id: int | None = None
    expected_version: int | None = None


class UpdateTagsRequest(ApiModel):
    """空列表清空标签。"""

    tags: list[str | None] | None = None


class CommentView(ApiModel):
    """评论。时间为上海本地钟，序列化成毫秒。"""

    id: int
    workitem_id: int
    author_type: str
    author_ref: int
    content_md: str | None = None
    gmt_create: datetime


class AddCommentRequest(ApiModel):
    """添加评论。数字员工和真人提及都可以省略。"""

    content_md: str | None = None
    target_agent_ids: list[int | None] | None = None
    target_human_ids: list[int | None] | None = None


class ParticipantView(ApiModel):
    """参与者。``isAgent`` 与 Jackson 的布尔字段名一致。"""

    user_id: int | None = None
    target_type: str | None = None
    name: str | None = None
    display_id: str | None = None
    role: str | None = None
    role_name: str | None = None
    agent: bool = Field(default=False, serialization_alias="isAgent")
    online: bool = False
    status: str | None = None
    executor_status: str | None = None


class CommentInteractionView(ApiModel):
    """评论上挂着的指引投递。"""

    guidance_id: int | None = None
    dispatch_id: int | None = None
    execution_status: str | None = None
    target_agent_id: int | None = None
    target_agent_name: str | None = None
    status: str | None = None
    error: str | None = None
    reply_comment_id: int | None = None
    reply_content: str | None = None
    replied_at: datetime | None = None


class TimelineItemView(ApiModel):
    """统一时间线的一条评论或系统事件。"""

    id: int | None = None
    type: str | None = None
    author_id: int | None = None
    author_name: str | None = None
    author_type: str | None = None
    agent: bool = Field(default=False, serialization_alias="isAgent")
    content: str | None = None
    gmt_create: datetime | None = None
    source_provider: str | None = None
    source_external_workitem_id: str | None = None
    source_external_url: str | None = None
    interactions: list[CommentInteractionView] | None = None


class EventView(ApiModel):
    """工单事件。``detailJson`` 保持字符串。"""

    id: int
    event_type: str
    from_val: str | None = None
    to_val: str | None = None
    actor_type: str | None = None
    actor_ref: int | None = None
    actor_name: str | None = None
    actor_display_name: str | None = None
    from_val_display: str | None = None
    to_val_display: str | None = None
    detail_json: str | None = None
    gmt_create: datetime


class WatchStateView(ApiModel):
    """当前用户是否关注，以及仍在空间内的关注人数。"""

    workitem_id: int
    watched: bool
    watcher_count: int


class UpdateContentRequest(ApiModel):
    """省略的标题或正文保持原值。"""

    title: str | None = None
    content_md: str | None = None


class SubStepView(ApiModel):
    """交付步骤里的一个子动作。"""

    name: str
    status: str


class StepUsageView(ApiModel):
    """一步或一个数字员工累加后的 token 与 credits。"""

    model: str | None = None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    credits: Decimal | None = None


class DispatchAttemptView(ApiModel):
    """同一步骤上的一次派发。``startedAt`` 是毫秒时间戳。"""

    dispatch_id: int | None = None
    executor_name: str | None = None
    status: str | None = None
    resume_mode: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    duration_ms: int | None = None
    can_continue: bool
    can_pause: bool


class DeliveryStepView(ApiModel):
    """SDLC 步骤在交付进度里的状态。"""

    step_id: int | None = None
    step_key: str | None = None
    name: str | None = None
    status: str | None = None
    plan_status: str | None = None
    source_attempt: int | None = None
    executor_name: str | None = None
    error: str | None = None
    sub_steps: list[SubStepView] | None = None
    duration_ms: int | None = None
    attempts: list[DispatchAttemptView]
    usage: StepUsageView | None = None


class AgentDeliveryProgressView(ApiModel):
    """一个数字员工自己的 SDLC 进度。"""

    agent_id: int
    agent_name: str | None = None
    status: str
    duration_ms: int | None = None
    current_activity: str | None = None
    steps: list[DeliveryStepView]
    usage: StepUsageView | None = None


class WorkflowPlanStepView(ApiModel):
    """运行时宣布的一步计划。"""

    step_key: str | None = None
    name: str | None = None
    plan_status: str
    source_attempt: int | None = None


class WorkflowPlanView(ApiModel):
    """最新一份可套用的工作流计划。"""

    revision: int
    agent_id: int | None = None
    agent_name: str | None = None
    target_step_id: str
    reason: str | None = None
    source_guidance_ids: list[int | None]
    steps: list[WorkflowPlanStepView]


class ProcessGraphNodeView(ApiModel):
    """流程图节点。派发节点的 ``startedAt`` 是毫秒时间戳。"""

    key: str
    dispatch_id: int | None = None
    agent_id: int | None = None
    agent_name: str | None = None
    step_id: int | None = None
    step_name: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    duration_ms: int | None = None
    error: str | None = None
    trigger_comment_id: int | None = None


class ProcessGraphEdgeView(ApiModel):
    """流程图上的交接、返工或恢复。"""

    source_key: str
    target_key: str
    type: str
    source_dispatch_id: int | None = None
    target_dispatch_id: int | None = None
    comment_id: int | None = None
    label: str


class ProcessGraphView(ApiModel):
    """正式派发组成的交付过程图。"""

    nodes: list[ProcessGraphNodeView]
    edges: list[ProcessGraphEdgeView]


class WorkitemUsageRunView(ApiModel):
    """一个数字员工在本工单里的一轮 credits。"""

    agent_id: int | None = None
    agent_name: str | None = None
    run_index: int
    label: str
    credits: Decimal


class WorkitemUsageView(ApiModel):
    """工单 credits 总计，以及按执行轮次拆开的明细。"""

    credits: Decimal
    runs: list[WorkitemUsageRunView]


class RecoveryControlRequest(ApiModel):
    """关闭、重开或取消交付。缺省 ``dispatchId`` 为 0，``force`` 为 false。"""

    action: str | None = None
    dispatch_id: int = 0
    force: bool = False


class DeliveryProgressView(ApiModel):
    """工单交付进度。没有正数 credits 时 ``totalUsage`` 为空。"""

    steps: list[DeliveryStepView]
    agents: list[AgentDeliveryProgressView]
    workflow_plan: WorkflowPlanView | None = None
    process_graph: ProcessGraphView
    total_duration_ms: int | None = None
    total_usage: WorkitemUsageView | None = None


def parse_java_date(value: object) -> datetime | None:
    """毫秒时间戳、带时区的 ISO 和上海本地 ISO 都收成 naive 本地时间。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_local(value)
    if isinstance(value, bool):
        raise BizError(ErrorCode.PARAM_INVALID)
    if isinstance(value, int) or isinstance(value, float):
        return datetime.fromtimestamp(value / 1000, SHANGHAI).replace(tzinfo=None)
    if isinstance(value, str):
        return _parse_iso(value)
    raise BizError(ErrorCode.PARAM_INVALID)


def _as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(SHANGHAI).replace(tzinfo=None)


def _parse_iso(value: str) -> datetime:
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    return _as_local(parsed)
