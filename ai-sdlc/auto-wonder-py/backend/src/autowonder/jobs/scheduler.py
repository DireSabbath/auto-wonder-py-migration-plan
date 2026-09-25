"""把 18 个定时任务挂到 AsyncIOScheduler。``create_app`` 不启动它们。

间隔任务按 Spring ``fixedDelay`` 排期：第一次在 ``initialDelay``（缺省 0）后执行，
之后从上一次结束时刻再等 ``every_seconds``。APScheduler 的 ``IntervalTrigger``
在提交时就按下一次开始时刻排期，长任务会把间隔吃进执行时间里，所以这里不用它。
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import cast

from apscheduler.events import (  # type: ignore[import-untyped]
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    JobExecutionEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.schedulers.base import STATE_RUNNING  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]

from autowonder.core.clock import SHANGHAI
from autowonder.jobs.catalog import SCHEDULED_JOBS, ScheduledJob
from autowonder.jobs.scheduled import scheduled_task_compensation, scheduled_task_scan
from autowonder.jobs.sweeps import (
    account_deactivation_expiry,
    agent_conversation_recovery,
    ai_compensation,
    aone_inbound_poll,
    aone_outbox_dispatch,
    conversation_elicitation_expiry,
    conversation_turn_event_cleanup,
    debug_log_reconciliation,
    dingtalk_stream_reconcile,
    dispatch_compensation,
    executor_update_scan,
    external_operation_recovery,
    feishu_inbox_drain,
    human_agent_participation_snapshot,
    provider_model_catalog_refresh,
    workspace_cleanup,
)

logger = logging.getLogger(__name__)

Runner = Callable[[], Awaitable[None]]

RUNNERS: dict[str, Runner] = {
    "scheduled_task_scan": scheduled_task_scan,
    "scheduled_task_compensation": scheduled_task_compensation,
    "executor_update_scan": executor_update_scan,
    "provider_model_catalog_refresh": provider_model_catalog_refresh,
    "account_deactivation_expiry": account_deactivation_expiry,
    "conversation_turn_event_cleanup": conversation_turn_event_cleanup,
    "agent_conversation_recovery": agent_conversation_recovery,
    "conversation_elicitation_expiry": conversation_elicitation_expiry,
    "debug_log_reconciliation": debug_log_reconciliation,
    "ai_compensation": ai_compensation,
    "workspace_cleanup": workspace_cleanup,
    "dispatch_compensation": dispatch_compensation,
    "aone_outbox_dispatch": aone_outbox_dispatch,
    "aone_inbound_poll": aone_inbound_poll,
    "feishu_inbox_drain": feishu_inbox_drain,
    "dingtalk_stream_reconcile": dingtalk_stream_reconcile,
    "external_operation_recovery": external_operation_recovery,
    "human_agent_participation_snapshot": human_agent_participation_snapshot,
}


def start_scheduler() -> AsyncIOScheduler:
    """注册全部任务并启动。间隔任务结束后再排下一次，cron 仍按表达式触发。"""
    scheduler = AsyncIOScheduler()
    now = datetime.now(SHANGHAI)
    for job in SCHEDULED_JOBS:
        if job.trigger == "cron":
            scheduler.add_job(
                RUNNERS[job.name],
                _cron_trigger(job),
                id=job.name,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=None,
            )
        else:
            arm_interval(scheduler, job.name, RUNNERS[job.name], first_run_at(job, now))
    scheduler.add_listener(
        fixed_delay_listener(scheduler, SCHEDULED_JOBS, RUNNERS),
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )
    scheduler.start()
    logger.info("已启动 %s 个定时任务", len(SCHEDULED_JOBS))
    return scheduler


def first_run_at(job: ScheduledJob, now: datetime) -> datetime:
    """第一次执行时刻。目录没写初始延迟时，与 Java 缺省的 initialDelay=0 一样立即执行。"""
    delay = job.initial_delay_seconds
    if delay is None:
        delay = 0.0
    return now + timedelta(seconds=delay)


def arm_interval(
    scheduler: AsyncIOScheduler,
    job_id: str,
    runner: Runner,
    run_at: datetime,
) -> None:
    """登记一次执行。同一 id 已在队列里时替换它，避免结束后的再登记撞上旧记录。"""
    scheduler.add_job(
        runner,
        DateTrigger(run_date=run_at),
        id=job_id,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )


def fixed_delay_listener(
    scheduler: AsyncIOScheduler,
    jobs: Iterable[ScheduledJob],
    runners: Mapping[str, Runner],
) -> Callable[[JobExecutionEvent], None]:
    """任务成功或抛错后，从上一次结束时刻再等固定间隔。cron 任务不经过这里重排。"""
    intervals = {job.name: job for job in jobs if job.trigger == "interval"}

    def listener(event: JobExecutionEvent) -> None:
        if scheduler.state != STATE_RUNNING:
            return
        spec = intervals.get(event.job_id)
        if spec is None:
            return
        arm_interval(
            scheduler,
            spec.name,
            runners[spec.name],
            datetime.now(SHANGHAI) + _fixed_delay(spec),
        )

    return listener


def _fixed_delay(job: ScheduledJob) -> timedelta:
    return timedelta(seconds=cast(float, job.every_seconds))


def _cron_trigger(job: ScheduledJob) -> CronTrigger:
    second, minute, hour, day, month, day_of_week = cast(str, job.cron).split()
    return CronTrigger(
        second=second,
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=job.timezone,
    )


@asynccontextmanager
async def scheduler_lifespan(_app: object) -> AsyncIterator[None]:
    """进程退出时停掉调度器。单个任务抛错由 APScheduler 记录，不中断其余任务。"""
    from autowonder.ai.worker import start_workers, stop_workers

    scheduler = start_scheduler()
    workers = start_workers()
    try:
        yield
    finally:
        await stop_workers(workers)
        scheduler.shutdown(wait=False)
