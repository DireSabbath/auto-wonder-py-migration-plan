"""定时任务请求与响应。触发时间按 UTC 瞬间输出，创建时间仍是上海本地钟。"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BeforeValidator

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel


def parse_utc_instant(value: object) -> datetime | None:
    """毫秒时间戳或带时区的 ISO，收成 UTC。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BizError(ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED, "runAt 不合法")
    return datetime.fromtimestamp(float(value) / 1000, UTC)


UtcInstant = Annotated[datetime | None, BeforeValidator(parse_utc_instant)]


class CreateScheduledTaskRequest(ApiModel):
    """创建定时任务。缺省模式在服务里补上，不在这里填业务默认值。"""

    name: str | None = None
    instruction_md: str | None = None
    squad_id: int | None = None
    initial_agent_id: int | None = None
    schedule_type: str | None = None
    run_at: UtcInstant = None
    cron_expression: str | None = None
    timezone: str | None = None
    session_mode: str | None = None
    overlap_policy: str | None = None
    misfire_policy: str | None = None
    start_deadline_seconds: int | None = None
    affinity_timeout_seconds: int | None = None
    initial_status: str | None = None


class UpdateScheduledTaskRequest(ApiModel):
    """更新定时任务。版本必须由调用方带来，缺省模式不会被补上。"""

    version: int | None = None
    name: str | None = None
    instruction_md: str | None = None
    squad_id: int | None = None
    initial_agent_id: int | None = None
    schedule_type: str | None = None
    run_at: UtcInstant = None
    cron_expression: str | None = None
    timezone: str | None = None
    session_mode: str | None = None
    overlap_policy: str | None = None
    misfire_policy: str | None = None
    start_deadline_seconds: int | None = None
    affinity_timeout_seconds: int | None = None


class ScheduledTaskView(ApiModel):
    """任务定义。``runAt`` / ``nextFireAt`` / ``lastFireAt`` 是 UTC 瞬间。"""

    id: int | None = None
    name: str | None = None
    instruction_md: str | None = None
    squad_id: int | None = None
    initial_agent_id: int | None = None
    schedule_type: str | None = None
    run_at: datetime | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    session_mode: str | None = None
    overlap_policy: str | None = None
    misfire_policy: str | None = None
    start_deadline_seconds: int | None = None
    affinity_timeout_seconds: int | None = None
    status: str | None = None
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None
    creator_id: int | None = None
    modifier_id: int | None = None
    version: int | None = None


class ScheduledTaskSummaryView(ApiModel):
    """工作空间内运行汇总。空结果的各项计数是 0。"""

    running: int = 0
    today: int = 0
    success30d: int = 0
    completed30d: int = 0
    attention: int = 0


class ScheduledTaskHealthView(ApiModel):
    """单个任务近 30 天的完成与成功次数。"""

    completed30d: int = 0
    success30d: int = 0


class RunNowRequest(ApiModel):
    """立即运行。``requestId`` 和 ``version`` 都必须由调用方带来。"""

    request_id: str | None = None
    version: int | None = None


class ScheduledRunMentionCandidateView(ApiModel):
    """运行上的 @ 候选。不在冻结快照里的数字人仍返回，并带上不能 @ 的原因。"""

    user_id: int | None = None
    target_type: str | None = None
    name: str | None = None
    display_id: str | None = None
    agent: bool = False
    online: bool = False
    executor_status: str | None = None
    mentionable: bool = False
    mention_disabled_reason: str | None = None


class ScheduledTaskRunView(ApiModel):
    """运行实例列表项。计划时间是 UTC 瞬间。"""

    id: int | None = None
    scheduled_task_id: int | None = None
    trigger_type: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str | None = None
    skip_reason: str | None = None
    current_agent_id: int | None = None
    sdlc_id: int | None = None
    current_step_id: int | None = None
    degraded_resume: bool = False
    degraded_reason: str | None = None
    result_summary: str | None = None
    error: str | None = None
    version: int | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None


class ScheduledTaskRunDetailView(ScheduledTaskRunView):
    """运行详情。最后一条派发的执行器 id 单独带上。"""

    executor_id: int | None = None
