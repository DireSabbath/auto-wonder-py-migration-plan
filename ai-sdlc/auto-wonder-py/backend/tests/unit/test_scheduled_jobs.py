"""定时任务的错过策略、触发键和调度登记。这些检查不连接数据库。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from autowonder.core.clock import SHANGHAI
from autowonder.jobs.catalog import SCHEDULED_JOBS, ScheduledJob
from autowonder.jobs.cluster import under_lock
from autowonder.jobs.scheduled import due_occurrences, misfire_plan
from autowonder.jobs.scheduler import (
    RUNNERS,
    _cron_trigger,
    arm_interval,
    first_run_at,
    fixed_delay_listener,
)
from autowonder.jobs.sweeps import (
    _POLL_ATTEMPTS,
    poll_interval_seconds,
    recovery_action,
    should_poll,
)
from autowonder.scheduledtasks.trigger import java_instant

UTC = UTC


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_catalog_names_match_runners() -> None:
    """18 个目录项各有一个同名执行入口，顺序与触发契约一致。"""
    assert [job.name for job in SCHEDULED_JOBS] == list(RUNNERS)


def test_cron_and_fixed_delay_first_run() -> None:
    """上海凌晨 3 点用 cron。飞书第一次晚 15 秒，其余间隔任务立即执行。"""
    nightly = next(
        job for job in SCHEDULED_JOBS if job.name == "human_agent_participation_snapshot"
    )
    feishu = next(job for job in SCHEDULED_JOBS if job.name == "feishu_inbox_drain")
    inbound = next(job for job in SCHEDULED_JOBS if job.name == "aone_inbound_poll")
    cron = _cron_trigger(nightly)
    now = datetime.now(SHANGHAI)
    assert cron.fields[5].expressions[0].first == 3
    assert cron.fields[7].expressions[0].first == 0
    assert first_run_at(feishu, now) - now == timedelta(seconds=15)
    assert first_run_at(inbound, now) == now


async def test_fixed_delay_starts_after_the_previous_run_finishes() -> None:
    """间隔从结束时刻起算。执行中的时间不计入下一次等待。"""
    loop = asyncio.get_running_loop()
    starts: list[float] = []
    completions: list[float] = []
    done = asyncio.Event()

    async def probe() -> None:
        starts.append(loop.time())
        if len(starts) == 1:
            await asyncio.sleep(0.3)
            completions.append(loop.time())
            return
        done.set()

    spec = ScheduledJob("probe", "interval", every_seconds=0.4)
    scheduler = AsyncIOScheduler()
    scheduler.add_listener(
        fixed_delay_listener(scheduler, (spec,), {"probe": probe}),
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )
    arm_interval(scheduler, "probe", probe, datetime.now(SHANGHAI))
    scheduler.start()
    try:
        await asyncio.wait_for(done.wait(), timeout=3)
    finally:
        scheduler.shutdown(wait=False)
    gap = starts[1] - completions[0]
    assert gap >= 0.35
    assert gap < 1.5


async def test_fixed_delay_reschedules_after_an_error() -> None:
    """任务抛错后仍从上一次结束时刻再等固定间隔，与 Spring 的错误处理后重排一致。"""
    loop = asyncio.get_running_loop()
    starts: list[float] = []
    done = asyncio.Event()

    async def probe() -> None:
        starts.append(loop.time())
        if len(starts) == 1:
            raise RuntimeError("scan failed")
        done.set()

    spec = ScheduledJob("probe", "interval", every_seconds=0.25)
    scheduler = AsyncIOScheduler()
    scheduler.add_listener(
        fixed_delay_listener(scheduler, (spec,), {"probe": probe}),
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )
    arm_interval(scheduler, "probe", probe, datetime.now(SHANGHAI))
    scheduler.start()
    try:
        await asyncio.wait_for(done.wait(), timeout=3)
    finally:
        scheduler.shutdown(wait=False)
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.2


def test_hourly_due_occurrences_include_the_cursor() -> None:
    """整点 cron 从游标收到当前时刻，包含两端。"""
    found = due_occurrences(
        "CRON",
        "0 0 * * * *",
        "UTC",
        _instant("2026-08-10T00:00:00Z"),
        _instant("2026-08-10T02:00:00Z"),
        _instant("2026-08-10T00:00:00Z"),
        100,
    )
    assert found == [
        _instant("2026-08-10T00:00:00Z"),
        _instant("2026-08-10T01:00:00Z"),
        _instant("2026-08-10T02:00:00Z"),
    ]


def test_once_before_the_earliest_is_dropped() -> None:
    """一次性触发若早于任务创建时刻，这一轮不产生触发点。"""
    found = due_occurrences(
        "ONCE",
        None,
        "UTC",
        _instant("2026-08-10T00:00:00Z"),
        _instant("2026-08-10T02:00:00Z"),
        _instant("2026-08-10T01:00:00Z"),
        100,
    )
    assert found == []


def test_misfire_policies() -> None:
    """准时、全部跳过、全部补触发和只保留最后一次，对应四种计划。"""
    first = _instant("2026-08-10T00:00:00Z")
    second = _instant("2026-08-10T01:00:00Z")
    assert misfire_plan([first], [first], "FIRE_LATEST") == [(first, "SCHEDULED", None, False)]
    assert misfire_plan([first, second], [first, second], "SKIP_ALL") == [
        (first, "MISFIRE", "MISFIRE_POLICY", False),
        (second, "MISFIRE", "MISFIRE_POLICY", False),
    ]
    assert misfire_plan([first, second], [first, second], "FIRE_ALL") == [
        (first, "MISFIRE", None, True),
        (second, "MISFIRE", None, True),
    ]
    assert misfire_plan([first, second], [second], "FIRE_LATEST") == [
        (first, "MISFIRE", "START_DEADLINE", False),
        (second, "MISFIRE", None, False),
    ]


def test_recovery_action_gates() -> None:
    """没上报、仍在执行、次数用尽和可以重投。"""
    assert recovery_action(False, set(), 4, 0) == "skip_unknown"
    assert recovery_action(True, {4}, 4, 0) == "skip_active"
    assert recovery_action(True, set(), 4, 3) == "fail"
    assert recovery_action(True, set(), 4, 2) == "redeliver"


def test_java_instant_strips_trailing_zeros() -> None:
    """触发键里的时间与 Java Instant.toString() 一样去掉小数末尾的 0。"""
    assert java_instant(_instant("2026-08-10T18:00:00Z")) == "2026-08-10T18:00:00Z"
    assert java_instant(_instant("2026-08-10T18:00:00.120000Z")) == "2026-08-10T18:00:00.12Z"


def test_poll_interval_and_recent_attempt() -> None:
    """轮询间隔不低于 15 秒；本进程刚打过 Aone 就不再打。"""
    assert poll_interval_seconds(None) == 15
    assert poll_interval_seconds(10) == 15
    assert poll_interval_seconds(30) == 30
    now = _instant("2026-09-24T00:00:00Z")
    binding_id = 910024
    _POLL_ATTEMPTS[binding_id] = now
    assert should_poll(binding_id, 15, None, now) is False
    assert should_poll(binding_id + 1, 15, None, now) is True
    assert should_poll(binding_id + 1, 15, now, now) is False
    assert should_poll(binding_id + 1, None, now - timedelta(seconds=15), now) is True
    del _POLL_ATTEMPTS[binding_id]


async def test_under_lock_releases_when_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """任务失败时仍释放当前持有者的锁。"""
    released: list[str] = []

    async def acquire(lock_key: str, owner_token: str, ttl_millis: int) -> bool:
        del lock_key, owner_token, ttl_millis
        return True

    async def release(lock_key: str, owner_token: str) -> bool:
        released.append(owner_token)
        del lock_key
        return True

    monkeypatch.setattr("autowonder.jobs.cluster.try_acquire_lock", acquire)
    monkeypatch.setattr("autowonder.jobs.cluster.release_lock", release)

    async def boom() -> None:
        raise RuntimeError("scan failed")

    with pytest.raises(RuntimeError, match="scan failed"):
        await under_lock("scheduled-task:scanner:lock", 30_000, boom)
    assert len(released) == 1
