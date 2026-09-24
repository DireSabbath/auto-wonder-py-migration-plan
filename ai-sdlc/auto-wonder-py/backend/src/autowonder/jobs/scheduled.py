"""定时任务扫描和运行补偿。锁键、批次和错过策略与 Java 调度器一致。"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.clock import SHANGHAI
from autowonder.db.rows import rowcount
from autowonder.db.session import SessionLocal
from autowonder.jobs.cluster import under_lock
from autowonder.scheduledtasks.models import ScheduledTask, ScheduledTaskRun
from autowonder.scheduledtasks.orchestrator import start_run
from autowonder.scheduledtasks.schedule import ScheduledTaskSchedule
from autowonder.scheduledtasks.service import naive_utc, utc_now
from autowonder.scheduledtasks.trigger import fire_misfire, fire_scheduled

logger = logging.getLogger(__name__)

SCAN_LOCK = "scheduled-task:scanner:lock"
COMPENSATION_LOCK = "scheduled-task-run:compensation:lock"
SCAN_BATCH = 100
LOCK_TTL_MILLIS = 30_000
COMPENSATION_LOCK_TTL_MILLIS = 60_000
COMPENSATION_BATCH = 200
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED"})
_SCHEDULE = ScheduledTaskSchedule()


def scanner_enabled() -> bool:
    """扫描器同时服从模块开关、集群就绪和扫描开关。"""
    settings = get_settings()
    return (
        settings.scheduled_task_enabled
        and settings.scheduled_task_cluster_ready
        and settings.scheduled_task_scanner_enabled
    )


def compensation_enabled() -> bool:
    """补偿只要求定时任务能力可用。"""
    settings = get_settings()
    return settings.scheduled_task_enabled and settings.scheduled_task_cluster_ready


def due_occurrences(
    schedule_type: str,
    cron_expression: str | None,
    timezone_name: str,
    first: datetime,
    now: datetime,
    earliest: datetime,
    cap: int,
) -> list[datetime]:
    """从已到期的游标收集不超过批次的触发点。结果是 UTC。"""
    found: list[datetime] = []
    if not first < earliest:
        found.append(first)
    if schedule_type == "ONCE":
        return found
    cursor = first
    while len(found) < cap:
        nxt = _SCHEDULE.next(cron_expression, timezone_name, cursor)
        if nxt > now:
            break
        if not nxt < earliest:
            found.append(nxt)
        cursor = nxt
    return found


def misfire_plan(
    occurrences: list[datetime],
    non_expired: list[datetime],
    policy: str,
) -> list[tuple[datetime, str, str | None, bool]]:
    """每个触发点对应触发类型、跳过原因，以及是否绕过重叠策略。

    返回 ``(instant, trigger_type, skip_reason, bypass_overlap)``。
    """
    if len(occurrences) == 1 and len(non_expired) == 1:
        return [(occurrences[0], "SCHEDULED", None, False)]
    plan: list[tuple[datetime, str, str | None, bool]] = []
    if policy == "SKIP_ALL":
        for occurrence in occurrences:
            reason = "MISFIRE_POLICY" if occurrence in non_expired else "START_DEADLINE"
            plan.append((occurrence, "MISFIRE", reason, False))
        return plan
    if policy == "FIRE_ALL":
        for occurrence in occurrences:
            if occurrence in non_expired:
                plan.append((occurrence, "MISFIRE", None, True))
            else:
                plan.append((occurrence, "MISFIRE", "START_DEADLINE", False))
        return plan
    selected = None if not non_expired else non_expired[-1]
    for occurrence in occurrences:
        if selected is not None and occurrence == selected:
            plan.append((occurrence, "MISFIRE", None, False))
        elif occurrence in non_expired:
            plan.append((occurrence, "MISFIRE", "MISFIRE_POLICY", False))
        else:
            plan.append((occurrence, "MISFIRE", "START_DEADLINE", False))
    return plan


async def scheduled_task_scan() -> None:
    """抢到扫描锁后认领到期任务并创建运行。"""
    if not scanner_enabled():
        return
    await under_lock(SCAN_LOCK, LOCK_TTL_MILLIS, _scan_due)


async def scheduled_task_compensation() -> None:
    """把卡住的 STARTING / QUEUED 运行重新交给启动器。"""
    if not compensation_enabled():
        return
    await under_lock(COMPENSATION_LOCK, COMPENSATION_LOCK_TTL_MILLIS, _compensate)


async def _scan_due() -> None:
    now = utc_now()
    async with SessionLocal() as session:
        due = list(
            await session.scalars(
                select(ScheduledTask)
                .where(
                    ScheduledTask.status == "ACTIVE",
                    ScheduledTask.is_deleted == 0,
                    ScheduledTask.next_fire_at.is_not(None),
                    ScheduledTask.next_fire_at <= _naive(now),
                )
                .order_by(ScheduledTask.next_fire_at.asc(), ScheduledTask.id.asc())
                .limit(SCAN_BATCH)
            )
        )
        for task in due:
            await _claim_and_fire(session, task, now)
        await session.commit()


async def _claim_and_fire(session: AsyncSession, task: ScheduledTask, now: datetime) -> None:
    if task.next_fire_at is None or task.version is None:
        return
    first = _as_utc(task.next_fire_at)
    earliest = _shanghai_utc(task.gmt_create)
    occurrences = due_occurrences(
        task.schedule_type,
        task.cron_expression,
        task.timezone,
        first,
        now,
        earliest,
        SCAN_BATCH,
    )
    if not occurrences:
        return
    last = occurrences[-1]
    nxt = None
    if task.schedule_type != "ONCE":
        nxt = _SCHEDULE.next(task.cron_expression, task.timezone, last)
    status = "EXHAUSTED" if nxt is None else "ACTIVE"
    claimed = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == task.workspace_id,
            ScheduledTask.id == task.id,
            ScheduledTask.status == "ACTIVE",
            ScheduledTask.version == task.version,
            ScheduledTask.is_deleted == 0,
            ScheduledTask.next_fire_at == task.next_fire_at,
        )
        .values(
            next_fire_at=naive_utc(nxt),
            last_fire_at=naive_utc(last),
            status=status,
            version=ScheduledTask.version + 1,
            modifier_id=task.creator_id,
        )
    )
    if rowcount(claimed) != 1:
        return
    deadline = 0 if task.start_deadline_seconds is None else task.start_deadline_seconds
    non_expired = [
        occurrence
        for occurrence in occurrences
        if not now > occurrence + timedelta(seconds=deadline)
    ]
    policy = "FIRE_LATEST" if task.misfire_policy is None else task.misfire_policy
    queued: list[int] = []
    for occurrence, trigger_type, skip_reason, bypass in misfire_plan(
        occurrences, non_expired, policy
    ):
        scheduled_at = naive_utc(occurrence)
        if scheduled_at is None:
            continue
        if trigger_type == "SCHEDULED":
            run = await fire_scheduled(session, task, scheduled_at)
        else:
            run = await fire_misfire(session, task, scheduled_at, skip_reason, bypass)
        if run.status == "QUEUED" and run.id is not None:
            queued.append(run.id)
    task.status = status
    if task.overlap_policy == "QUEUE":
        return
    await session.commit()
    for run_id in queued:
        await start_run(task.workspace_id, run_id, task.creator_id)


async def _compensate() -> None:
    stale_before = now_local_cutoff(30)
    async with SessionLocal() as session:
        starting = list(
            await session.scalars(
                select(ScheduledTaskRun)
                .where(
                    ScheduledTaskRun.status == "STARTING",
                    ScheduledTaskRun.gmt_modified < stale_before,
                )
                .order_by(ScheduledTaskRun.gmt_modified.asc(), ScheduledTaskRun.id.asc())
                .limit(COMPENSATION_BATCH)
            )
        )
        queued = list(
            await session.scalars(
                select(ScheduledTaskRun)
                .where(
                    ScheduledTaskRun.status.in_(("QUEUED", "WAITING_EXECUTOR")),
                    ScheduledTaskRun.gmt_modified < stale_before,
                )
                .order_by(ScheduledTaskRun.scheduled_at.asc(), ScheduledTaskRun.id.asc())
                .limit(COMPENSATION_BATCH)
            )
        )
        starting_ids = [(run.workspace_id, run.id) for run in starting]
        queued_ids = [
            (run.workspace_id, run.id) for run in queued if await _may_start(session, run)
        ]
    for workspace_id, run_id in starting_ids:
        await start_run(workspace_id, run_id, 0)
    for workspace_id, run_id in queued_ids:
        await start_run(workspace_id, run_id, 0)


async def _may_start(session: AsyncSession, run: ScheduledTaskRun) -> bool:
    if run.workspace_id is None or run.scheduled_task_id is None or run.id is None:
        return False
    others = list(
        await session.scalars(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.workspace_id == run.workspace_id,
                ScheduledTaskRun.scheduled_task_id == run.scheduled_task_id,
                ScheduledTaskRun.status.not_in(_TERMINAL),
            )
        )
    )
    for other in others:
        if other.id is not None and other.id < run.id and other.status not in _TERMINAL:
            return False
    return True


def now_local_cutoff(seconds: int) -> datetime:
    """上海墙钟往前推若干秒，用来和库里的 gmt_modified 比较。"""
    from autowonder.core.clock import now_local

    return now_local() - timedelta(seconds=seconds)


def _naive(value: datetime) -> datetime:
    converted = naive_utc(value)
    if converted is None:
        return value
    return converted


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _shanghai_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI).astimezone(UTC)
    return value.astimezone(UTC)
