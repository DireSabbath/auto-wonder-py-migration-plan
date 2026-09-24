"""定时任务定义与小队、数字员工引用校验。失败码 30004。"""

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.scheduledtasks.models import ScheduledTask
from autowonder.scheduledtasks.schedule import ScheduledTaskSchedule
from autowonder.squads.models import Squad, SquadMember

MAX_NAME_CHARS = 256
MAX_INSTRUCTION_BYTES = 16_777_215
SESSION_MODES = frozenset({"ISOLATED", "CONTINUOUS"})
OVERLAP_POLICIES = frozenset({"SKIP", "QUEUE", "ALLOW"})
MISFIRE_POLICIES = frozenset({"FIRE_LATEST", "FIRE_ALL", "SKIP_ALL"})


def validate_modes(session_mode: str | None, overlap_policy: str | None) -> None:
    """会话模式、重叠策略，以及连续会话不能并行。"""
    if session_mode not in SESSION_MODES:
        _fail("sessionMode 不合法")
    if overlap_policy not in OVERLAP_POLICIES:
        _fail("overlapPolicy 不合法")
    if session_mode == "CONTINUOUS" and overlap_policy == "ALLOW":
        _fail("CONTINUOUS 模式不能并行执行")


def validate_definition(task: ScheduledTask, schedule: ScheduledTaskSchedule) -> None:
    """名称、指令、日程、模式和超时。不查库。"""
    name = task.name
    instruction = task.instruction_md
    if name is None or java_is_blank(name) or _utf16_len(name) > MAX_NAME_CHARS:
        _fail("任务名称不能为空且不能超过 256 个字符")
    if instruction is None or java_is_blank(instruction) or len(instruction.encode("utf-8")) > (
        MAX_INSTRUCTION_BYTES
    ):
        _fail("任务指令不能为空或过长")
    if not _positive(task.squad_id) or not _positive(task.initial_agent_id):
        _fail("小队和初始 Agent ID 必须为正数")
    _validate_schedule(task, schedule)
    validate_modes(task.session_mode, task.overlap_policy)
    if task.misfire_policy not in MISFIRE_POLICIES:
        _fail("misfirePolicy 不合法")
    if not _positive(task.start_deadline_seconds):
        _fail("startDeadlineSeconds 必须为正数")
    affinity = cast(int | None, task.affinity_timeout_seconds)
    if affinity is None:
        _fail("affinityTimeoutSeconds 不能为空")
    if task.session_mode == "CONTINUOUS" and affinity <= 0:
        _fail("CONTINUOUS 模式的 affinityTimeoutSeconds 必须为正数")


async def validate_references(
    session: AsyncSession,
    task: ScheduledTask,
    workspace_id: int,
) -> None:
    """小队、在线数字员工和成员关系必须属于当前工作空间。"""
    squad = await session.scalar(select(Squad).where(Squad.id == task.squad_id))
    if squad is None or squad.tenant_id != workspace_id or squad.is_deleted == 1:
        _fail("小队不存在或不属于当前工作空间")
    agent = await session.scalar(select(Agent).where(Agent.id == task.initial_agent_id))
    if (
        agent is None
        or agent.tenant_id != workspace_id
        or agent.is_deleted == 1
        or agent.online_version_id is None
    ):
        _fail("初始 Agent 不存在、未上线或不属于当前工作空间")
    member = await session.scalar(
        select(SquadMember).where(
            SquadMember.squad_id == task.squad_id,
            SquadMember.agent_id == task.initial_agent_id,
        )
    )
    if (
        member is None
        or member.tenant_id != workspace_id
        or member.squad_id != task.squad_id
        or member.agent_id != task.initial_agent_id
    ):
        _fail("初始 Agent 不是该小队成员")


def _validate_schedule(task: ScheduledTask, schedule: ScheduledTaskSchedule) -> None:
    if task.timezone is None or java_is_blank(task.timezone):
        _fail("timezone 不能为空")
    cron = cast(str | None, task.cron_expression)
    cron_blank = cron is None or java_is_blank(cron)
    if task.schedule_type == "ONCE":
        if task.run_at is None or not cron_blank:
            _fail("ONCE 必须且只能设置 runAt")
        schedule.validate("0 0 0 * * *", task.timezone)
        return
    if task.schedule_type == "CRON":
        if cron_blank or task.run_at is not None:
            _fail("CRON 必须且只能设置 cronExpression")
        schedule.validate(cron, task.timezone)
        return
    _fail("scheduleType 仅支持 ONCE/CRON")


def _positive(value: int | None) -> bool:
    return value is not None and value > 0


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _fail(message: str) -> None:
    raise BizError(ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED, message)
