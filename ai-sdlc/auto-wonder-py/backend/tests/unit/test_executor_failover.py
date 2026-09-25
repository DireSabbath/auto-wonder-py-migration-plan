"""执行器故障把活跃派发退回 PENDING，并在结果提交后再跑 runPending。"""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.audits.models import AuditLog
from autowonder.core.clock import now_local
from autowonder.dispatch.models import Dispatch, DispatchRuntimeEvent
from autowonder.ws.inbound import _apply_failover, inbound_router
from autowonder.ws.session import ExecutorSession
from tests.unit.test_workitems import MemorySession


class _Redis:
    """记下冷却键和会话恢复计数。"""

    def __init__(self, count: int | None = None, broken: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.fixed = count
        self.broken = broken
        self.seen = 0

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.values[key] = value
        return True

    async def eval(self, script: str, numkeys: int, key: str, ttl: str) -> int:
        if self.broken:
            raise RuntimeError("redis down")
        self.seen += 1
        if self.fixed is not None:
            return self.fixed
        return self.seen


class _FailoverSession(MemorySession):
    def expire(self, row: object) -> None:
        return None

    async def get(self, entity: type[object], identity: int) -> object | None:
        for row in self.rows:
            if isinstance(row, entity) and getattr(row, "id", None) == identity:
                return row
        return None

    async def refresh(self, row: object) -> None:
        return None


class _Bound:
    def __init__(self, session: _FailoverSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FailoverSession:
        return self.session

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Executor:
    def __init__(self) -> None:
        self.executor_id = 9
        self.agent_id = 20
        self.tenant_id = 1
        self.sent: list[str] = []

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


def _use_redis(monkeypatch: pytest.MonkeyPatch, redis: _Redis) -> None:
    monkeypatch.setattr("autowonder.core.redis.redis_client", lambda: redis)


async def _put(
    session: _FailoverSession,
    status: str,
    executor_id: int | None,
    source: str = "WORKITEM",
) -> Dispatch:
    dispatch = Dispatch(
        id=500,
        tenant_id=1,
        workitem_id=200,
        sdlc_step_id=300,
        source_type=source,
        agent_id=20,
        status=status,
        executor_id=executor_id,
        attempt=1,
        idempotency_key="dispatch-500",
        version=4,
        is_deleted=0,
        package_oss_ref="oss://pkg",
        gmt_create=now_local(),
        gmt_modified=now_local(),
    )
    session.add(dispatch)
    await session.flush()
    return dispatch


def _events(session: _FailoverSession) -> list[DispatchRuntimeEvent]:
    return [row for row in session.rows if isinstance(row, DispatchRuntimeEvent)]


def _audits(session: _FailoverSession) -> list[AuditLog]:
    return [row for row in session.rows if isinstance(row, AuditLog)]


async def test_provider_failure_requeues_same_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    await _put(session, "RUNNING", 9)

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "agent_error.provider_quota_limit",
        "quota exhausted",
    )

    dispatch = cast(Dispatch, session.rows[0])
    assert accepted is True
    assert dispatch.status == "PENDING"
    assert dispatch.executor_id is None
    assert dispatch.package_oss_ref is None
    assert redis.values["exec:provider-cooldown:9"] == (
        "failover:agent_error.provider_quota_limit"
    )
    event = _events(session)[0]
    assert event.event_type == "dispatch.executor_failover"
    assert event.event_id == "dispatch:500:executor-failover:4"
    assert event.step_id == 300
    assert event.error is not None
    assert "agent_error.provider_quota_limit" in event.error
    assert "quota exhausted" in event.error
    detail = cast(dict[str, object], event.detail_json)
    assert detail["executorId"] == 9
    assert detail["failureScope"] == "EXECUTOR"
    assert detail["retrying"] is True
    audit = _audits(session)[0]
    assert audit.action == "FAILOVER_DISPATCH"
    recorded = cast(dict[str, object], audit.detail_json)
    assert recorded["failureCategory"] == "agent_error.provider_quota_limit"
    assert recorded["eventType"] == "dispatch.executor_failover"


async def test_foreign_executor_does_not_mark_or_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    dispatch = await _put(session, "RUNNING", 8)

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "agent_error.provider_quota_limit",
        "quota exhausted",
    )

    assert accepted is False
    assert dispatch.status == "RUNNING"
    assert dispatch.executor_id == 8
    assert redis.values == {}
    assert _events(session) == []


async def test_pause_refuses_requeue_after_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    dispatch = await _put(session, "PAUSING", 9)

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "agent_error.provider_network",
        "reset",
    )

    assert accepted is False
    assert dispatch.status == "PAUSING"
    assert redis.values["exec:provider-cooldown:9"] == "failover:agent_error.provider_network"
    assert _events(session) == []


async def test_first_runtime_recovery_requeues_without_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis(count=1)
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    dispatch = await _put(session, "RUNNING", 9)

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "runtime_recovery",
        "session not found",
    )

    assert accepted is True
    assert dispatch.status == "PENDING"
    assert redis.values == {}
    assert redis.seen == 1
    assert _events(session)[0].event_id == "dispatch:500:executor-failover:4"


async def test_second_runtime_recovery_fails_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis(count=2)
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    dispatch = await _put(session, "RUNNING", 9)

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "runtime_recovery",
        "session not found again",
    )

    assert accepted is True
    assert dispatch.status == "FAILED"
    assert dispatch.error is not None
    assert "SESSION_RECOVERY_EXHAUSTED" in dispatch.error
    assert "session not found again" in dispatch.error
    assert _events(session) == []


async def test_terminal_result_accepts_without_a_second_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    dispatch = await _put(session, "FAILED", 9)

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "agent_error.provider_quota_limit",
        "quota exhausted",
    )

    assert accepted is True
    assert dispatch.status == "FAILED"
    assert dispatch.executor_id == 9
    assert redis.values["exec:provider-cooldown:9"].startswith("failover:")
    assert _events(session) == []


async def test_unknown_category_stays_on_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    dispatch = await _put(session, "RUNNING", 9)

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "agent_error.unknown",
        "tests failed",
    )

    assert accepted is False
    assert dispatch.status == "RUNNING"
    assert redis.values == {}


async def test_scheduled_requeue_publishes_runtime_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    _use_redis(monkeypatch, redis)
    published: list[tuple[int, int]] = []

    async def publish_runtime(session: AsyncSession, workspace_id: int, run_id: int) -> None:
        published.append((workspace_id, run_id))

    monkeypatch.setattr(
        "autowonder.scheduledtasks.notify.publish_runtime",
        publish_runtime,
    )
    session = _FailoverSession()
    await _put(session, "RUNNING", 9, source="SCHEDULED_TASK_RUN")

    accepted = await _apply_failover(
        cast(AsyncSession, session),
        1,
        9,
        500,
        "agent_error.provider_quota_limit",
        "quota exhausted",
    )

    assert accepted is True
    assert published == [(1, 200)]


async def test_result_frame_runs_pending_instead_of_draining_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    _use_redis(monkeypatch, redis)
    session = _FailoverSession()
    await _put(session, "RUNNING", 9)
    ran: list[int] = []

    async def run_pending(bound: AsyncSession, dispatch_id: int) -> bool:
        ran.append(dispatch_id)
        return True

    async def drain_pending(bound: AsyncSession, agent_id: int) -> None:
        raise AssertionError(agent_id)

    monkeypatch.setattr("autowonder.ws.inbound.run_pending", run_pending)
    monkeypatch.setattr("autowonder.ws.inbound.drain_pending", drain_pending)
    monkeypatch.setattr(
        "autowonder.ws.inbound.SessionLocal",
        lambda: _Bound(session),
    )
    executor = _Executor()

    await inbound_router._result(
        cast(ExecutorSession, executor),
        {
            "dispatchId": 500,
            "success": False,
            "failureCategory": "agent_error.provider_quota_limit",
            "error": "quota exhausted",
        },
    )

    assert ran == [500]
    assert executor.sent[0].find('"accepted":true') >= 0
