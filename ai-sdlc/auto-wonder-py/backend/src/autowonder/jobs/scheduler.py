"""把 18 个定时任务挂到 AsyncIOScheduler。``create_app`` 不启动它们。"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

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
    """按目录里的间隔和 cron 注册全部任务并启动。同一任务不重叠执行。"""
    scheduler = AsyncIOScheduler()
    for job in SCHEDULED_JOBS:
        scheduler.add_job(
            RUNNERS[job.name],
            _trigger(job),
            id=job.name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )
    scheduler.start()
    logger.info("已启动 %s 个定时任务", len(SCHEDULED_JOBS))
    return scheduler


def _trigger(job: ScheduledJob) -> IntervalTrigger | CronTrigger:
    if job.trigger == "cron":
        second, minute, hour, day, month, day_of_week = (job.cron or "").split()
        return CronTrigger(
            second=second,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=job.timezone,
        )
    start_date = None
    if job.initial_delay_seconds is not None:
        start_date = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(
            seconds=job.initial_delay_seconds
        )
    return IntervalTrigger(seconds=job.every_seconds, start_date=start_date)


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
