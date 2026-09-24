"""调度查询结果。列表不含产物，详情才带。运行轨迹字段与 Java VO 对齐。"""

from datetime import datetime
from typing import Any

from pydantic import Field

from autowonder.artifacts.schemas import ArtifactView
from autowonder.core.schema import ApiModel


class DispatchView(ApiModel):
    """一条调度记录及展示用名称。"""

    id: int | None = None
    source_type: str | None = None
    workitem_id: int | None = None
    sdlc_step_id: int | None = None
    agent_id: int | None = None
    agent_version_id: int | None = None
    executor_id: int | None = None
    status: str | None = None
    attempt: int | None = None
    result_summary: str | None = None
    error: str | None = None
    package_oss_ref: str | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None
    workitem_title: str | None = None
    agent_name: str | None = None
    agent_version_no: int | None = None
    executor_name: str | None = None
    artifacts: list[ArtifactView] | None = None


class DispatchPage(ApiModel):
    """调度分页。页码字段是 page 和 pageSize。"""

    list: list[DispatchView]
    total: int
    page: int
    page_size: int


class TokenUsage(ApiModel):
    """一次调用或汇总的 token。缺省不可用，数值为 0。"""

    available: bool = False
    availability: str | None = None
    source: str | None = None
    credits: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0


class RuntimeEvent(ApiModel):
    """投影后的一条运行事件。时间是 UTC 文本，没有时间则保持空。"""

    event_id: str | None = None
    seq: int | None = None
    event_type: str | None = None
    event_time: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class RuntimeBoundary(ApiModel):
    """会话生命周期边界。"""

    event_id: str | None = None
    kind: str | None = None
    type: str | None = None
    event_time: str | None = None
    time: str | None = None
    label: str | None = None
    detail: dict[str, Any] | None = None


class RuntimeSpan(ApiModel):
    """一个回合里的工具、模型或技能区间。"""

    span_id: str | None = None
    parent_span_id: str | None = None
    kind: str | None = None
    name: str | None = None
    status: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    model: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    input: Any = None
    output: str | None = None
    content: str | None = None
    error_category: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    event_ids: list[str] = Field(default_factory=list)


class RuntimeContextFile(ApiModel):
    """回合附带的上下文文件。"""

    role: str | None = None
    name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    content_ref: str | None = None
    previewable: bool = False


class RuntimeObservation(ApiModel):
    """完成态轨迹里的观测节点。大纲会清掉输入、输出和错误。"""

    observation_id: str | None = None
    parent_observation_id: str | None = None
    type: str | None = None
    name: str | None = None
    status: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    model: str | None = None
    input: Any = None
    output: Any = None
    error: Any = None
    orphan: bool = False
    usage: TokenUsage = Field(default_factory=TokenUsage)
    children: list["RuntimeObservation"] = Field(default_factory=list)


class RuntimeTurn(ApiModel):
    """一个会话回合。``usage`` 与 ``tokenUsage`` 都保留，投影只累计后者。"""

    trace_id: str | None = None
    turn_id: str | None = None
    step_id: str | None = None
    step_name: str | None = None
    status: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    prompt: str | None = None
    system_prompt: str | None = None
    output: str | None = None
    provider_coverage: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    context_files: list[RuntimeContextFile] = Field(default_factory=list)
    observations: list[RuntimeObservation] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    spans: list[RuntimeSpan] = Field(default_factory=list)


class RuntimeSession(ApiModel):
    """一条 provider 会话。同一 sessionId 的恢复仍算这一条。"""

    session_id: str | None = None
    parent_session_id: str | None = None
    provider: str | None = None
    status: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    event_ids: list[str] = Field(default_factory=list)
    boundaries: list[RuntimeBoundary] = Field(default_factory=list)
    turns: list[RuntimeTurn] = Field(default_factory=list)


class RuntimeTrace(ApiModel):
    """调度运行轨迹。``changed`` 默认真；序号未前进时为假且不带事件。"""

    schema_version: str | None = None
    source: str | None = None
    dispatch_id: int | None = None
    runtime_id: str | None = None
    provider: str | None = None
    changed: bool = True
    last_seq: int | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    events: list[RuntimeEvent] = Field(default_factory=list)
    sessions: list[RuntimeSession] = Field(default_factory=list)


class RuntimeActivity(ApiModel):
    """时间线上的一条可读活动。不含原始 detail 和工具输入。"""

    event_id: str | None = None
    seq: int | None = None
    event_time: str | None = None
    event_type: str | None = None
    level: str | None = None
    content: str | None = None


class RuntimeActivityTimeline(ApiModel):
    """活动时间线。只有调度 id 和活动列表。"""

    dispatch_id: int | None = None
    activities: list[RuntimeActivity] = Field(default_factory=list)


class LiveAction(ApiModel):
    """浏览器可见的一条实时动作。摘要已经脱敏并限长。"""

    event_id: str | None = None
    seq: int | None = None
    event_time: str | None = None
    event_type: str | None = None
    action_type: str | None = None
    summary: str | None = None
    status: str | None = None
    step_id: int | None = None
    step_key: str | None = None
    step_name: str | None = None
    agent_id: int | None = None
    dispatch_id: int | None = None
    attempt: int | None = None


class LiveActivity(ApiModel):
    """一次调度的实时活动。动作按新到旧排列，原始日志不出现。"""

    schema_version: str = "1"
    dispatch_id: int | None = None
    agent_id: int | None = None
    workitem_id: int | None = None
    source_type: str | None = None
    attempt: int | None = None
    dispatch_status: str | None = None
    changed: bool = True
    last_seq: int | None = None
    last_updated_at: str | None = None
    current_action: LiveAction | None = None
    actions: list[LiveAction] = Field(default_factory=list)
    total_actions: int = 0
    truncated: bool = False
    awaiting_runtime: bool = False
