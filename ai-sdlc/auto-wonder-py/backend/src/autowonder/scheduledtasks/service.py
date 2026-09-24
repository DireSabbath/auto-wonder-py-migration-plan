"""定时任务定义：创建、修改、启停、归档、删除，以及工作空间删除时的暂停。"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import and_, case, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.audits.service import AuditRecord, record_required
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.page import PageResult
from autowonder.db.rows import rowcount
from autowonder.evolution.jsontext import java_trim
from autowonder.scheduledtasks.models import ScheduledTask, ScheduledTaskRun
from autowonder.scheduledtasks.schedule import ScheduledTaskSchedule
from autowonder.scheduledtasks.schemas import (
    CreateScheduledTaskRequest,
    ScheduledTaskHealthView,
    ScheduledTaskRunView,
    ScheduledTaskSummaryView,
    ScheduledTaskView,
    UpdateScheduledTaskRequest,
)
from autowonder.scheduledtasks.validator import validate_definition, validate_references

logger = logging.getLogger(__name__)

DELETION_REASON = "工作空间已删除"
DEFAULT_START_DEADLINE_SECONDS = 21_600
DEFAULT_AFFINITY_TIMEOUT_SECONDS = 1_800
MAX_PAGE_SIZE = 100
LIST_STATUSES = frozenset({"ACTIVE", "PAUSED", "EXHAUSTED", "ARCHIVED"})
TERMINAL_RUN_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED"})
HEALTH_COMPLETED = ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED")
_ASCII_WS = re.compile(r"[ \t\n\x0b\f\r]+")
_SCHEDULE = ScheduledTaskSchedule()
_SHANGHAI_DAY_START = literal_column(
    "DATE(UTC_TIMESTAMP() + INTERVAL 8 HOUR) - INTERVAL 8 HOUR"
)
_SHANGHAI_DAY_END = literal_column("DATE(UTC_TIMESTAMP() + INTERVAL 8 HOUR) + INTERVAL 16 HOUR")
_SINCE_30_DAYS = literal_column("UTC_TIMESTAMP() - INTERVAL 30 DAY")


def utc_now() -> datetime:
    """当前 UTC 瞬间，测试可以替换。"""
    return datetime.now(UTC)


async def pause_active_by_workspace(
    session: AsyncSession,
    workspace_id: int,
    operator_id: int,
) -> int:
    """把该工作空间仍在运行的定时任务改为 PAUSED，并返回变更行数。"""
    result = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == workspace_id,
            ScheduledTask.status == "ACTIVE",
            ScheduledTask.is_deleted == 0,
        )
        .values(
            status="PAUSED",
            modifier_id=operator_id,
            version=ScheduledTask.version + 1,
        )
    )
    paused = rowcount(result)
    if paused > 0:
        logger.info(
            "Paused %s scheduled task(s) of workspace %s: %s",
            paused,
            workspace_id,
            DELETION_REASON,
        )
    return paused


async def create_task(
    session: AsyncSession,
    request: CreateScheduledTaskRequest,
    workspace_id: int,
    user_id: int,
) -> ScheduledTaskView:
    """创建任务并写下一次触发。一次性任务即使暂停也保留 runAt。"""
    _require_actor(workspace_id, user_id)
    task = _from_create(request, workspace_id, user_id)
    task.next_fire_at = _next_fire(task, utc_now())
    validate_definition(task, _SCHEDULE)
    await validate_references(session, task, workspace_id)
    session.add(task)
    await session.flush()
    await _audit(session, task, user_id, "CREATE", None)
    await session.commit()
    return to_view(task)


async def update_task(
    session: AsyncSession,
    task_id: int,
    request: UpdateScheduledTaskRequest,
    workspace_id: int,
    user_id: int,
) -> ScheduledTaskView:
    """按版本替换定义。归档任务不可改，创建人保持不变。"""
    _require_actor(workspace_id, user_id)
    _require_task_id(task_id)
    if request.version is None or request.version < 0:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED,
            "version 必须提供且不能为负数",
        )
    task = await _require_task(session, workspace_id, task_id)
    _require_version(task.version, request.version)
    if task.status == "ARCHIVED":
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "归档任务不可修改")
    _apply_update(task, request, user_id)
    task.next_fire_at = _next_fire(task, utc_now())
    validate_definition(task, _SCHEDULE)
    await validate_references(session, task, workspace_id)
    changed = await _update_definition(session, task, request.version)
    if changed != 1:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    task.version = request.version + 1
    session.expunge(task)
    await _audit(session, task, user_id, "UPDATE", task.status)
    await session.commit()
    return to_view(task)


async def get_task(session: AsyncSession, task_id: int, workspace_id: int) -> ScheduledTaskView:
    """按工作空间读取未删除的任务。不存在是 30001。"""
    return to_view(await _require_task(session, workspace_id, task_id))


async def list_tasks(
    session: AsyncSession,
    workspace_id: int,
    status: str | None,
    creator_id: int | None,
    squad_id: int | None,
    keyword: str | None,
    limit: int,
    offset: int,
) -> PageResult:
    """分页列出任务。size 收到 1 到 100，offset 小于 0 时从 0 开始。"""
    if (
        workspace_id <= 0
        or (status is not None and status not in LIST_STATUSES)
        or (creator_id is not None and creator_id <= 0)
    ):
        raise BizError(ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED, "查询条件不合法")
    bounded_limit = min(max(limit, 1), MAX_PAGE_SIZE)
    bounded_offset = max(offset, 0)
    rows = (
        await session.scalars(
            list_statement(
                workspace_id, status, creator_id, squad_id, keyword, bounded_limit, bounded_offset
            )
        )
    ).all()
    views = [
        to_view(task)
        for task in rows
        if task.workspace_id == workspace_id and task.is_deleted != 1
    ]
    total = await session.scalar(
        count_statement(workspace_id, status, creator_id, squad_id, keyword)
    )
    return PageResult(
        list_=views,
        total=int(total),
        page_num=bounded_offset // bounded_limit + 1,
        page_size=bounded_limit,
    )


def preview_times(
    cron_expression: str | None, timezone_name: str | None, count: int
) -> list[datetime]:
    """用当前时间预览接下来的触发瞬间。"""
    return _SCHEDULE.preview(
        normalize_cron(cron_expression),
        normalize_timezone(timezone_name),
        utc_now(),
        count,
    )


async def summarize_tasks(
    session: AsyncSession,
    workspace_id: int,
    status: str | None,
    squad_id: int | None,
    keyword: str | None,
) -> ScheduledTaskSummaryView:
    """按上海自然日和近 30 天汇总运行。"""
    result = await session.execute(summary_statement(workspace_id, status, squad_id, keyword))
    mapping = result.one()._mapping
    return ScheduledTaskSummaryView(
        running=int(mapping["running"]),
        today=int(mapping["today"]),
        success30d=int(mapping["success30d"]),
        completed30d=int(mapping["completed30d"]),
        attention=int(mapping["attention"]),
    )


async def enable_task(
    session: AsyncSession,
    task_id: int,
    version: int | None,
    workspace_id: int,
    user_id: int,
) -> ScheduledTaskView:
    """只从暂停恢复，并按当前时间重算下一次触发。版本增加 2。"""
    _require_actor(workspace_id, user_id)
    _require_task_id(task_id)
    task = await _require_task(session, workspace_id, task_id)
    _require_version(task.version, version)
    if task.status != "PAUSED":
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "只有暂停任务可以启用")
    validate_definition(task, _SCHEDULE)
    await validate_references(session, task, workspace_id)
    task.next_fire_at = _next_fire(task, utc_now())
    task.modifier_id = user_id
    task.version = version
    changed = await _update_definition(session, task, cast(int, version))
    if changed != 1:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    status_version = cast(int, version) + 1
    changed = await _update_status(
        session, workspace_id, task_id, "PAUSED", "ACTIVE", status_version, user_id
    )
    if changed != 1:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    task.status = "ACTIVE"
    task.version = status_version + 1
    session.expunge(task)
    await _audit(session, task, user_id, "ENABLE", "PAUSED")
    await session.commit()
    return to_view(task)


async def pause_task(
    session: AsyncSession,
    task_id: int,
    version: int | None,
    workspace_id: int,
    user_id: int,
) -> ScheduledTaskView:
    """只暂停运行中的任务，不改下一次触发。"""
    _require_actor(workspace_id, user_id)
    _require_task_id(task_id)
    return await _transition(
        session, task_id, version, workspace_id, user_id, "ACTIVE", "PAUSED", "PAUSE"
    )


async def archive_task(
    session: AsyncSession,
    task_id: int,
    version: int | None,
    workspace_id: int,
    user_id: int,
) -> ScheduledTaskView:
    """暂停或已耗尽的任务可以归档。"""
    _require_actor(workspace_id, user_id)
    _require_task_id(task_id)
    task = await _require_task(session, workspace_id, task_id)
    _require_version(task.version, version)
    if task.status != "PAUSED" and task.status != "EXHAUSTED":
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "只有暂停或已耗尽任务可以归档")
    return await _transition(
        session, task_id, version, workspace_id, user_id, task.status, "ARCHIVED", "ARCHIVE"
    )


async def delete_task(
    session: AsyncSession,
    task_id: int,
    version: int | None,
    workspace_id: int,
    user_id: int,
) -> None:
    """软删除。仍有未结束的运行时拒绝，避免恢复扫描继续派发。"""
    _require_actor(workspace_id, user_id)
    _require_task_id(task_id)
    task = await _require_task(session, workspace_id, task_id)
    _require_version(task.version, version)
    active = await _active_runs(session, workspace_id, task_id)
    if len(active) > 0:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "存在未结束的运行实例，请先取消后再删除",
        )
    previous_status = task.status
    changed = await _soft_delete(session, workspace_id, task_id, cast(int, version), user_id)
    if changed != 1:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    task.status = "ARCHIVED"
    task.next_fire_at = None
    task.is_deleted = 1
    task.modifier_id = user_id
    task.version = cast(int, version) + 1
    session.expunge(task)
    await _audit(session, task, user_id, "DELETE", previous_status)
    await session.commit()


async def list_runs(
    session: AsyncSession,
    workspace_id: int,
    task_id: int,
    limit: int,
    offset: int,
) -> list[ScheduledTaskRunView]:
    """某个任务的运行，按 id 倒序。"""
    bounded_limit = min(max(limit, 1), MAX_PAGE_SIZE)
    bounded_offset = max(offset, 0)
    rows = (
        await session.scalars(
            select(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.workspace_id == workspace_id,
                ScheduledTaskRun.scheduled_task_id == task_id,
            )
            .order_by(ScheduledTaskRun.id.desc())
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
    ).all()
    return [_run_view(row) for row in rows]


async def task_health(
    session: AsyncSession,
    workspace_id: int,
    task_id: int,
) -> ScheduledTaskHealthView:
    """近 30 天按完成时间统计。完成数包含跳过，成功数只算成功。"""
    since = naive_utc(utc_now() - timedelta(days=30))
    completed = await session.scalar(
        _health_count(workspace_id, task_id, HEALTH_COMPLETED, since)
    )
    succeeded = await session.scalar(
        _health_count(workspace_id, task_id, ("SUCCEEDED",), since)
    )
    return ScheduledTaskHealthView(completed30d=int(completed), success30d=int(succeeded))


def list_statement(
    workspace_id: int,
    status: str | None,
    creator_id: int | None,
    squad_id: int | None,
    keyword: str | None,
    limit: int,
    offset: int,
) -> Select[tuple[ScheduledTask]]:
    """列表 SQL：工作空间、未删除，可选状态、创建人、小队和名称。"""
    statement = select(ScheduledTask).where(
        *_filters(workspace_id, status, creator_id, squad_id, keyword)
    )
    return statement.order_by(ScheduledTask.id.desc()).limit(limit).offset(offset)


def count_statement(
    workspace_id: int,
    status: str | None,
    creator_id: int | None,
    squad_id: int | None,
    keyword: str | None,
) -> Select[tuple[int]]:
    """与列表相同的过滤条件下计数。"""
    return (
        select(func.count())
        .select_from(ScheduledTask)
        .where(*_filters(workspace_id, status, creator_id, squad_id, keyword))
    )


def summary_statement(
    workspace_id: int,
    status: str | None,
    squad_id: int | None,
    keyword: str | None,
) -> Select[tuple[object, object, object, object, object]]:
    """运行汇总。今天是上海日历日，按 UTC 墙钟比较 ``scheduled_at``。"""
    run = ScheduledTaskRun
    task = ScheduledTask
    running = func.count(func.distinct(case((run.status == "RUNNING", task.id))))
    today = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        run.scheduled_at >= _SHANGHAI_DAY_START,
                        run.scheduled_at < _SHANGHAI_DAY_END,
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        0,
    )
    success = func.coalesce(
        func.sum(
            case((and_(run.status == "SUCCEEDED", run.scheduled_at >= _SINCE_30_DAYS), 1), else_=0)
        ),
        0,
    )
    completed = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        run.status.in_(("SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT")),
                        run.scheduled_at >= _SINCE_30_DAYS,
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        0,
    )
    attention = func.count(func.distinct(case((run.status.in_(("FAILED", "PAUSED")), task.id))))
    statement = (
        select(
            running.label("running"),
            today.label("today"),
            success.label("success30d"),
            completed.label("completed30d"),
            attention.label("attention"),
        )
        .select_from(task)
        .outerjoin(
            run,
            and_(run.scheduled_task_id == task.id, run.workspace_id == task.workspace_id),
        )
        .where(task.workspace_id == workspace_id, task.is_deleted == 0)
    )
    if status is not None and status != "":
        statement = statement.where(task.status == status)
    if squad_id is not None:
        statement = statement.where(task.squad_id == squad_id)
    if keyword is not None and keyword != "":
        statement = statement.where(task.name.like(f"%{keyword}%"))
    return statement


def to_view(task: ScheduledTask) -> ScheduledTaskView:
    """定义视图。触发字段带 UTC，创建和修改时间保持上海本地钟。"""
    return ScheduledTaskView(
        id=task.id,
        name=task.name,
        instruction_md=task.instruction_md,
        squad_id=task.squad_id,
        initial_agent_id=task.initial_agent_id,
        schedule_type=task.schedule_type,
        run_at=aware_utc(task.run_at),
        cron_expression=task.cron_expression,
        timezone=task.timezone,
        session_mode=task.session_mode,
        overlap_policy=task.overlap_policy,
        misfire_policy=task.misfire_policy,
        start_deadline_seconds=task.start_deadline_seconds,
        affinity_timeout_seconds=task.affinity_timeout_seconds,
        status=task.status,
        next_fire_at=aware_utc(task.next_fire_at),
        last_fire_at=aware_utc(task.last_fire_at),
        gmt_create=task.gmt_create,
        gmt_modified=task.gmt_modified,
        creator_id=task.creator_id,
        modifier_id=task.modifier_id,
        version=task.version,
    )


def naive_utc(value: datetime | None) -> datetime | None:
    """UTC 瞬间收成不带时区的墙钟，按库里的 DATETIME 约定保存。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def aware_utc(value: datetime | None) -> datetime | None:
    """库里的 UTC 墙钟在响应里标成 UTC，避免被当成上海时间。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_cron(expression: str | None) -> str | None:
    """去掉首尾 Java trim 空白，并把中间空白收成一个空格。"""
    if expression is None:
        return None
    return _ASCII_WS.sub(" ", java_trim(expression))


def normalize_timezone(timezone_name: str | None) -> str | None:
    """时区只做 Java trim。"""
    if timezone_name is None:
        return None
    return java_trim(timezone_name)


def _from_create(
    request: CreateScheduledTaskRequest,
    workspace_id: int,
    user_id: int,
) -> ScheduledTask:
    task = ScheduledTask(
        workspace_id=workspace_id,
        name=cast(str, request.name),
        instruction_md=cast(str, request.instruction_md),
        squad_id=cast(int, request.squad_id),
        initial_agent_id=cast(int, request.initial_agent_id),
        schedule_type=cast(str, request.schedule_type),
        run_at=naive_utc(request.run_at),
        cron_expression=normalize_cron(request.cron_expression),
        timezone=cast(str, normalize_timezone(request.timezone)),
        session_mode=_default_text(request.session_mode, "ISOLATED"),
        overlap_policy=_default_text(request.overlap_policy, "SKIP"),
        misfire_policy=_default_text(request.misfire_policy, "FIRE_LATEST"),
        start_deadline_seconds=_default_int(
            request.start_deadline_seconds, DEFAULT_START_DEADLINE_SECONDS
        ),
        affinity_timeout_seconds=_default_int(
            request.affinity_timeout_seconds, DEFAULT_AFFINITY_TIMEOUT_SECONDS
        ),
        status=_initial_status(request.initial_status),
        creator_id=user_id,
        modifier_id=user_id,
        is_deleted=0,
        version=0,
    )
    return task


def _apply_update(task: ScheduledTask, request: UpdateScheduledTaskRequest, user_id: int) -> None:
    task.name = cast(str, request.name)
    task.instruction_md = cast(str, request.instruction_md)
    task.squad_id = cast(int, request.squad_id)
    task.initial_agent_id = cast(int, request.initial_agent_id)
    task.schedule_type = cast(str, request.schedule_type)
    task.run_at = naive_utc(request.run_at)
    task.cron_expression = normalize_cron(request.cron_expression)
    task.timezone = cast(str, normalize_timezone(request.timezone))
    task.session_mode = cast(str, request.session_mode)
    task.overlap_policy = cast(str, request.overlap_policy)
    task.misfire_policy = cast(str, request.misfire_policy)
    task.start_deadline_seconds = cast(int, request.start_deadline_seconds)
    task.affinity_timeout_seconds = cast(int, request.affinity_timeout_seconds)
    task.modifier_id = user_id
    task.version = cast(int, request.version)


def _initial_status(requested: str | None) -> str:
    status = "ACTIVE" if requested is None else requested
    if status != "ACTIVE" and status != "PAUSED":
        raise BizError(
            ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED,
            "initialStatus 仅支持 ACTIVE/PAUSED",
        )
    return status


def _next_fire(task: ScheduledTask, after: datetime) -> datetime | None:
    if task.schedule_type == "ONCE":
        return task.run_at
    if task.schedule_type == "CRON":
        return naive_utc(_SCHEDULE.next(task.cron_expression, task.timezone, after))
    return None


async def _require_task(session: AsyncSession, workspace_id: int, task_id: int) -> ScheduledTask:
    if workspace_id <= 0 or task_id <= 0:
        raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
    task = await session.scalar(
        select(ScheduledTask).where(
            ScheduledTask.workspace_id == workspace_id,
            ScheduledTask.id == task_id,
            ScheduledTask.is_deleted == 0,
        )
    )
    if task is None or task.workspace_id != workspace_id or task.is_deleted == 1:
        raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
    return task


def _require_version(actual: int | None, expected: int | None) -> None:
    if expected is None or expected < 0 or expected != actual:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)


async def _transition(
    session: AsyncSession,
    task_id: int,
    version: int | None,
    workspace_id: int,
    user_id: int,
    source: str,
    target: str,
    action: str,
) -> ScheduledTaskView:
    task = await _require_task(session, workspace_id, task_id)
    _require_version(task.version, version)
    if task.status != source:
        raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "任务来源状态不允许该操作")
    changed = await _update_status(
        session, workspace_id, task_id, source, target, cast(int, version), user_id
    )
    if changed != 1:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    task.status = target
    task.modifier_id = user_id
    task.version = cast(int, version) + 1
    session.expunge(task)
    await _audit(session, task, user_id, action, source)
    await session.commit()
    return to_view(task)


async def _update_definition(session: AsyncSession, task: ScheduledTask, expected: int) -> int:
    result = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == task.workspace_id,
            ScheduledTask.id == task.id,
            ScheduledTask.version == expected,
            ScheduledTask.is_deleted == 0,
        )
        .values(
            name=task.name,
            instruction_md=task.instruction_md,
            squad_id=task.squad_id,
            initial_agent_id=task.initial_agent_id,
            schedule_type=task.schedule_type,
            run_at=task.run_at,
            cron_expression=task.cron_expression,
            timezone=task.timezone,
            session_mode=task.session_mode,
            overlap_policy=task.overlap_policy,
            misfire_policy=task.misfire_policy,
            start_deadline_seconds=task.start_deadline_seconds,
            affinity_timeout_seconds=task.affinity_timeout_seconds,
            next_fire_at=task.next_fire_at,
            modifier_id=task.modifier_id,
            version=ScheduledTask.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return rowcount(result)


async def _update_status(
    session: AsyncSession,
    workspace_id: int,
    task_id: int,
    source: str,
    target: str,
    expected: int,
    user_id: int,
) -> int:
    result = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == workspace_id,
            ScheduledTask.id == task_id,
            ScheduledTask.status == source,
            ScheduledTask.version == expected,
            ScheduledTask.is_deleted == 0,
        )
        .values(
            status=target,
            modifier_id=user_id,
            version=ScheduledTask.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return rowcount(result)


async def _soft_delete(
    session: AsyncSession,
    workspace_id: int,
    task_id: int,
    expected: int,
    user_id: int,
) -> int:
    result = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == workspace_id,
            ScheduledTask.id == task_id,
            ScheduledTask.version == expected,
            ScheduledTask.is_deleted == 0,
        )
        .values(
            is_deleted=1,
            status="ARCHIVED",
            next_fire_at=None,
            modifier_id=user_id,
            version=ScheduledTask.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return rowcount(result)


async def _active_runs(
    session: AsyncSession,
    workspace_id: int,
    task_id: int,
) -> list[ScheduledTaskRun]:
    rows = (
        await session.scalars(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.workspace_id == workspace_id,
                ScheduledTaskRun.scheduled_task_id == task_id,
                ScheduledTaskRun.status.notin_(tuple(TERMINAL_RUN_STATUSES)),
            )
        )
    ).all()
    return [row for row in rows if row.status not in TERMINAL_RUN_STATUSES]


async def _audit(
    session: AsyncSession,
    task: ScheduledTask,
    user_id: int,
    action: str,
    previous_status: str | None,
) -> None:
    record = AuditRecord(
        tenant_id=task.workspace_id,
        actor_id=user_id,
        actor_type="HUMAN",
        module="SCHEDULED_TASK",
        action=action,
        target_type="SCHEDULED_TASK",
        target_id=task.id,
        trigger_type="EVENT",
        trigger_source="WEB",
        event_type="SCHEDULED_TASK_DEFINITION",
    )
    record.add("scheduleType", task.schedule_type)
    record.add("status", task.status)
    record.add("previousStatus", previous_status)
    record.add("squadId", task.squad_id)
    record.add("initialAgentId", task.initial_agent_id)
    record.add("version", task.version)
    await record_required(session, record)


def _filters(
    workspace_id: int,
    status: str | None,
    creator_id: int | None,
    squad_id: int | None,
    keyword: str | None,
) -> list[object]:
    clauses: list[object] = [
        ScheduledTask.workspace_id == workspace_id,
        ScheduledTask.is_deleted == 0,
    ]
    if status is not None and status != "":
        clauses.append(ScheduledTask.status == status)
    if creator_id is not None:
        clauses.append(ScheduledTask.creator_id == creator_id)
    if squad_id is not None:
        clauses.append(ScheduledTask.squad_id == squad_id)
    if keyword is not None and keyword != "":
        clauses.append(ScheduledTask.name.like(f"%{keyword}%"))
    return clauses


def _health_count(
    workspace_id: int,
    task_id: int,
    statuses: tuple[str, ...],
    since: datetime | None,
) -> Select[tuple[int]]:
    return (
        select(func.count())
        .select_from(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == workspace_id,
            ScheduledTaskRun.scheduled_task_id == task_id,
            ScheduledTaskRun.finished_at >= since,
            ScheduledTaskRun.status.in_(statuses),
        )
    )


def _run_view(run: ScheduledTaskRun) -> ScheduledTaskRunView:
    return ScheduledTaskRunView(
        id=run.id,
        scheduled_task_id=run.scheduled_task_id,
        trigger_type=run.trigger_type,
        scheduled_at=aware_utc(run.scheduled_at),
        started_at=aware_utc(run.started_at),
        finished_at=aware_utc(run.finished_at),
        status=run.status,
        skip_reason=run.skip_reason,
        current_agent_id=run.current_agent_id,
        sdlc_id=run.sdlc_id,
        current_step_id=run.current_step_id,
        degraded_resume=run.degraded_resume == 1,
        degraded_reason=run.degraded_reason,
        result_summary=run.result_summary,
        error=run.error,
        version=run.version,
        gmt_create=run.gmt_create,
        gmt_modified=run.gmt_modified,
    )


def _require_actor(workspace_id: int, user_id: int) -> None:
    if workspace_id <= 0 or user_id <= 0:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED,
            "workspaceId 和 userId 必须为正数",
        )


def _require_task_id(task_id: int) -> None:
    if task_id <= 0:
        raise BizError(ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED, "任务 ID 必须为正数")


def _default_text(value: str | None, default: str) -> str:
    if value is None:
        return default
    return value


def _default_int(value: int | None, default: int) -> int:
    if value is None:
        return default
    return value
