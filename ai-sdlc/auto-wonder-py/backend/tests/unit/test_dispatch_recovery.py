"""调度恢复、取消和继续围栏。这些检查不连接 MySQL。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError
from autowonder.dispatch.continue_run import continue_workitem
from autowonder.dispatch.models import Dispatch, DispatchRecovery
from autowonder.dispatch.recovery import (
    cancel,
    cancel_requested,
    close,
    closed,
    fenced,
    find_dispatch,
    force_cancel_scheduled_run,
    insert_dispatch,
    on_stopped,
    ready,
    reconcile,
    reconcile_executor,
    reopen,
    retry_packaging,
    transition,
)
from autowonder.executors.registry import DispatchSnapshot
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.workitems.models import Workitem
from tests.unit.test_workitems import MemorySession


class RecoverySession(MemorySession):
    """带保存点回滚，用来核对状态和评论投影是否一起提交。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_delivery = False

    async def execute(self, statement: object) -> object:
        if self.fail_delivery and statement.__class__.__name__ == "Update":
            entity = statement.entity_description["entity"]
            if entity is WorkitemCommentDelivery:
                raise RuntimeError("workitem_comment_delivery missing")
        return await MemorySession.execute(self, statement)

    @asynccontextmanager
    async def begin_nested(self):
        rows = list(self.rows)
        pending = list(self._pending)
        values = {id(row): _column_values(row) for row in rows}
        next_id = self._next_id
        try:
            yield
        except Exception:
            self.rows[:] = rows
            self._pending[:] = pending
            self._next_id = next_id
            for row in rows:
                for key, value in values[id(row)].items():
                    setattr(row, key, value)
            raise


class Transport:
    def __init__(self) -> None:
        self.calls: list[Dispatch] = []
        self.error: BaseException | None = None

    async def pause(self, dispatch: Dispatch) -> None:
        self.calls.append(dispatch)
        if self.error is not None:
            raise self.error


async def harness() -> tuple[RecoverySession, Transport]:
    session = RecoverySession()
    session.add(Workitem(id=10, tenant_id=1, work_type="TASK", title="恢复"))
    await session.flush()
    return session, Transport()


async def put_dispatch(
    session: RecoverySession,
    status: str,
    executor: int | None,
    dispatch_id: int = 100,
    source: str = "WORKITEM",
    workitem_id: int = 10,
    tenant_id: int = 1,
) -> None:
    session.add(
        Dispatch(
            id=dispatch_id,
            tenant_id=tenant_id,
            workitem_id=workitem_id,
            source_type=source,
            agent_id=20,
            status=status,
            executor_id=executor,
            attempt=1,
            idempotency_key="dispatch-" + str(dispatch_id),
            version=0,
            is_deleted=0,
            gmt_create=now_local(),
            gmt_modified=now_local(),
        )
    )
    await session.flush()


async def put_guidance(session: RecoverySession, status: str) -> None:
    session.add(
        WorkitemCommentDelivery(
            tenant_id=1,
            workitem_id=10,
            comment_id=30,
            target_agent_id=20,
            dispatch_id=100,
            status=status,
        )
    )
    await session.flush()


def stored(session: RecoverySession, dispatch_id: int = 100) -> Dispatch:
    for row in session.rows:
        if isinstance(row, Dispatch) and row.id == dispatch_id:
            return row
    raise AssertionError(dispatch_id)


def guidance_row(session: RecoverySession, guidance_id: int = 1) -> WorkitemCommentDelivery:
    for row in session.rows:
        if isinstance(row, WorkitemCommentDelivery) and row.id == guidance_id:
            return row
    raise AssertionError(guidance_id)


def recovery_row(session: RecoverySession, dispatch_id: int = 100) -> DispatchRecovery:
    for row in session.rows:
        if isinstance(row, DispatchRecovery) and row.dispatch_id == dispatch_id:
            return row
    raise AssertionError(dispatch_id)


def stop_pending(session: RecoverySession, dispatch_id: int = 100) -> bool:
    for row in session.rows:
        if (
            isinstance(row, DispatchRecovery)
            and row.dispatch_id == dispatch_id
            and row.stop_pending == 1
        ):
            return True
    return False


def _column_values(row: object) -> dict[str, object]:
    mapper = getattr(row, "__mapper__", None)
    if mapper is None:
        return {}
    values: dict[str, object] = {}
    for prop in mapper.column_attrs:
        column = prop.columns[0]
        if column.computed is not None:
            continue
        values[prop.key] = getattr(row, prop.key)
    return values


async def move(session: RecoverySession, status: str, error: str | None) -> int:
    current = await find_dispatch(session, 100)
    return await transition(session, current, status, None, None, None, None, error)


def online_50(executor_id: int) -> bool:
    return executor_id == 50


def snapshot_of(reported: bool, active: bool):
    def lookup(executor_id: int) -> DispatchSnapshot | None:
        if executor_id != 50 or not reported:
            return None
        owned: frozenset[int] = frozenset()
        if active:
            owned = frozenset({100})
        return DispatchSnapshot(
            capacity=10,
            authoritative_inventory=True,
            inventory_ready=True,
            inventory_error=None,
            running_dispatch_ids=owned,
            running_conversation_turn_ids=frozenset(),
            owned_dispatch_ids=owned,
        )

    return lookup


async def test_scheduled_force_cancel_persists_stop_and_retries_offline_executor() -> None:
    session, transport = await harness()
    await put_dispatch(session, "RUNNING", 50)
    stored(session).source_type = "SCHEDULED_TASK_RUN"
    transport.error = RuntimeError("offline")
    await force_cancel_scheduled_run(session, 1, 10, 100, 7, transport.pause)
    assert (await find_dispatch(session, 100)).status == "CANCELED"
    assert await cancel_requested(session, 1, 100) is True
    assert stop_pending(session) is True
    assert len(transport.calls) == 1
    recovery_row(session).last_sent_at = None
    await reconcile(session, transport.pause)
    assert len(transport.calls) == 2
    assert await on_stopped(session, 1, 51, 100) is False
    assert await on_stopped(session, 1, 50, 100) is True
    assert stop_pending(session) is False
    assert (await find_dispatch(session, 100)).status == "CANCELED"


async def test_scheduled_force_cancel_rejects_wrong_source_workspace_or_run() -> None:
    session, transport = await harness()
    await put_dispatch(session, "RUNNING", 50)
    with pytest.raises(BizError):
        await force_cancel_scheduled_run(session, 1, 10, 100, 7, transport.pause)
    stored(session).source_type = "SCHEDULED_TASK_RUN"
    with pytest.raises(BizError):
        await force_cancel_scheduled_run(session, 2, 10, 100, 7, transport.pause)
    with pytest.raises(BizError):
        await force_cancel_scheduled_run(session, 1, 11, 100, 7, transport.pause)
    assert transport.calls == []
    assert (await find_dispatch(session, 100)).status == "RUNNING"


async def test_timeout_keeps_stop_pending_until_runtime_confirms_release() -> None:
    session, _transport = await harness()
    await put_dispatch(session, "DISPATCHED", 50)
    await put_guidance(session, "QUEUED")
    assert await move(session, "TIMEOUT", "DISPATCH_ACK_TIMEOUT") == 1
    assert stop_pending(session) is True
    assert guidance_row(session).status == "FAILED"
    assert await on_stopped(session, 1, 50, 100) is True
    assert stop_pending(session) is False
    assert (await find_dispatch(session, 100)).status == "TIMEOUT"


async def test_server_failure_converges_queued_comment_but_preserves_completed_reply() -> None:
    session, transport = await harness()
    await put_dispatch(session, "PACKAGING", None)
    await put_guidance(session, "QUEUED")
    assert await move(session, "FAILED", "BAD_PACKAGE") == 1
    assert guidance_row(session).status == "FAILED"
    reply = guidance_row(session)
    reply.status = "APPLIED"
    reply.reply_comment_id = 44
    await cancel(session, 1, 10, 100, 7, False, transport.pause)
    assert guidance_row(session).status == "APPLIED"


async def test_terminal_and_projection_rollback_together() -> None:
    session, _transport = await harness()
    await put_dispatch(session, "PACKAGING", None)
    await put_guidance(session, "QUEUED")
    session.fail_delivery = True
    with pytest.raises(RuntimeError):
        await move(session, "FAILED", "error")
    assert (await find_dispatch(session, 100)).status == "PACKAGING"


async def test_cancel_before_delivery_fences_packaging_worker_without_sending_stop() -> None:
    session, transport = await harness()
    await put_dispatch(session, "PACKAGING", 50)
    await put_guidance(session, "QUEUED")
    stale = await find_dispatch(session, 100)
    await cancel(session, 1, 10, 100, 7, False, transport.pause)
    assert (await find_dispatch(session, 100)).status == "CANCELED"
    assert guidance_row(session).status == "CANCELED"
    assert await transition(session, stale, "DISPATCHED", None, 50, None, None, None) == 0
    assert transport.calls == []
    assert stop_pending(session) is False


async def test_forced_cancellation_keeps_stop_pending_until_authenticated_stop() -> None:
    session, transport = await harness()
    await put_dispatch(session, "RUNNING", 50)
    await put_guidance(session, "DELIVERED")
    stale = await find_dispatch(session, 100)
    await cancel(session, 1, 10, 100, 7, False, transport.pause)
    assert len(transport.calls) == 1
    assert (await find_dispatch(session, 100)).status == "PAUSING"
    await cancel(session, 1, 10, 100, 7, True, transport.pause)
    assert (await find_dispatch(session, 100)).status == "CANCELED"
    assert stop_pending(session) is True
    assert await fenced(session, await find_dispatch(session, 100)) is True
    assert await on_stopped(session, 1, 51, 100) is False
    assert (
        await transition(session, stale, "SUCCEEDED", None, None, None, "late result", None) == 0
    )
    assert await on_stopped(session, 1, 50, 100) is True
    assert await on_stopped(session, 1, 50, 100) is True
    assert stop_pending(session) is False
    assert (await find_dispatch(session, 100)).status == "CANCELED"


async def test_close_blocks_new_execution_and_reopen_does_not_run_anything() -> None:
    session, transport = await harness()
    await put_dispatch(session, "PENDING", None)
    await put_guidance(session, "QUEUED")
    await close(session, 1, 10, 7, False, transport.pause)
    assert await closed(session, 1, 10) is True
    assert guidance_row(session).status == "CANCELED"
    nxt = await find_dispatch(session, 100)
    nxt.id = 101
    nxt.status = "PENDING"
    with pytest.raises(BizError):
        await insert_dispatch(session, nxt)
    await reopen(session, 1, 10, 7)
    await insert_dispatch(session, nxt)
    assert await find_dispatch(session, 101) is not None
    assert transport.calls == []


async def test_retries_keep_old_guidance_identity() -> None:
    session, _transport = await harness()
    await put_dispatch(session, "FAILED", None)
    await put_guidance(session, "FAILED")
    nxt = await find_dispatch(session, 100)
    nxt.id = 101
    nxt.status = "PENDING"
    nxt.resume_mode = "CANONICAL_INTERACTION"
    nxt.idempotency_key = "continue:100"
    nxt.resume_from_dispatch_id = 100
    await insert_dispatch(session, nxt)
    assert guidance_row(session).status == "FAILED"
    rows = [row for row in session.rows if isinstance(row, WorkitemCommentDelivery)]
    rows.sort(key=lambda row: row.id)
    assert [row.dispatch_id for row in rows] == [100, 101]


async def test_automatic_packaging_retries_are_bounded_and_persist_backoff() -> None:
    session, _transport = await harness()
    await put_dispatch(session, "PACKAGING", None)
    for _count in range(3):
        stored(session).status = "PACKAGING"
        current = await find_dispatch(session, 100)
        assert await retry_packaging(session, current, "temporary network error") is True
        assert await ready(session, current) is False
    stored(session).status = "PACKAGING"
    current = await find_dispatch(session, 100)
    assert await retry_packaging(session, current, "temporary network error") is False
    assert recovery_row(session).retry_count == 3


async def test_reconcile_repairs_history_and_retries_durable_stop_intent() -> None:
    session, transport = await harness()
    await put_dispatch(session, "FAILED", None)
    await put_guidance(session, "QUEUED")
    await reconcile(session, transport.pause)
    assert guidance_row(session).status == "FAILED"
    row = stored(session)
    row.status = "RUNNING"
    row.executor_id = 50
    transport.error = RuntimeError("offline")
    await cancel(session, 1, 10, 100, 7, False, transport.pause)
    recovery_row(session).last_sent_at = now_local() - timedelta(milliseconds=60_000)
    transport.calls.clear()
    transport.error = None
    await reconcile(session, transport.pause)
    assert len(transport.calls) == 1
    assert stop_pending(session) is True


async def test_reconcile_self_heals_stop_the_live_runtime_disproves() -> None:
    session, transport = await harness()
    await put_dispatch(session, "DISPATCHED", 50)
    assert await move(session, "TIMEOUT", "DISPATCH_ACK_TIMEOUT") == 1
    assert stop_pending(session) is True
    row = recovery_row(session)
    row.requested_at = now_local() - timedelta(milliseconds=180_000)
    row.last_sent_at = now_local() - timedelta(milliseconds=60_000)
    await reconcile(session, transport.pause, online_50, snapshot_of(True, False))
    assert stop_pending(session) is False
    assert transport.calls == []


async def test_reconcile_keeps_retrying_stops_the_runtime_cannot_disprove() -> None:
    session, transport = await harness()
    await put_dispatch(session, "DISPATCHED", 50)
    assert await move(session, "TIMEOUT", "DISPATCH_ACK_TIMEOUT") == 1
    row = recovery_row(session)
    row.requested_at = now_local() - timedelta(milliseconds=180_000)
    row.last_sent_at = now_local() - timedelta(milliseconds=60_000)
    await reconcile(session, transport.pause, online_50, snapshot_of(False, False))
    assert stop_pending(session) is True
    assert len(transport.calls) == 1


async def test_periodic_stop_reconciliation_supports_mysql_datetime_mapping() -> None:
    await _mysql_datetime_stop_retry(False)


async def test_heartbeat_stop_reconciliation_supports_mysql_datetime_mapping() -> None:
    await _mysql_datetime_stop_retry(True)


async def _mysql_datetime_stop_retry(heartbeat: bool) -> None:
    session, transport = await harness()
    await put_dispatch(session, "RUNNING", 50)
    await cancel(session, 1, 10, 100, 7, False, transport.pause)
    row = recovery_row(session)
    row.requested_at = now_local() - timedelta(milliseconds=180_000)
    row.last_sent_at = now_local() - timedelta(milliseconds=60_000)
    transport.calls.clear()
    if heartbeat:
        await reconcile_executor(session, 50, transport.pause, online_50, snapshot_of(True, True))
    else:
        await reconcile(session, transport.pause, online_50, snapshot_of(True, True))
    assert len(transport.calls) == 1
    assert transport.calls[0].id == 100
    assert stop_pending(session) is True
    assert recovery_row(session).last_sent_at > now_local() - timedelta(milliseconds=30_000)


async def test_reconcile_does_not_heal_stops_younger_than_the_handshake_grace() -> None:
    session, transport = await harness()
    await put_dispatch(session, "DISPATCHED", 50)
    assert await move(session, "TIMEOUT", "DISPATCH_ACK_TIMEOUT") == 1
    row = recovery_row(session)
    row.requested_at = now_local() - timedelta(milliseconds=10_000)
    row.last_sent_at = now_local() - timedelta(milliseconds=60_000)
    await reconcile(session, transport.pause, online_50, snapshot_of(True, False))
    assert stop_pending(session) is True
    assert len(transport.calls) == 1


async def test_reconcile_heals_old_stops_when_dates_stay_naive() -> None:
    session, transport = await harness()
    await put_dispatch(session, "DISPATCHED", 50)
    assert await move(session, "TIMEOUT", "DISPATCH_ACK_TIMEOUT") == 1
    row = recovery_row(session)
    row.requested_at = now_local() - timedelta(milliseconds=180_000)
    row.last_sent_at = now_local() - timedelta(milliseconds=60_000)
    assert isinstance(row.requested_at, datetime)
    await reconcile_executor(session, 50, transport.pause, online_50, snapshot_of(True, False))
    assert stop_pending(session) is False
    assert transport.calls == []


async def test_reconcile_preserves_young_stop_grace_when_dates_stay_naive() -> None:
    session, transport = await harness()
    await put_dispatch(session, "DISPATCHED", 50)
    assert await move(session, "TIMEOUT", "DISPATCH_ACK_TIMEOUT") == 1
    row = recovery_row(session)
    row.requested_at = now_local() - timedelta(milliseconds=10_000)
    row.last_sent_at = now_local() - timedelta(milliseconds=60_000)
    await reconcile_executor(session, 50, transport.pause, online_50, snapshot_of(True, False))
    assert stop_pending(session) is True
    assert len(transport.calls) == 1


async def test_successful_reply_without_ack_is_not_invented() -> None:
    session, transport = await harness()
    await put_dispatch(session, "SUCCEEDED", None)
    await put_guidance(session, "DELIVERED")
    stored(session).gmt_modified = now_local() - timedelta(milliseconds=180_000)
    await reconcile(session, transport.pause)
    assert guidance_row(session).status == "FAILED"


async def test_continue_rejects_scheduled_run_before_executor_presence_check() -> None:
    session, _transport = await harness()
    await put_dispatch(
        session,
        "RUNNING",
        7,
        dispatch_id=55,
        source="SCHEDULED_TASK_RUN",
        workitem_id=200,
        tenant_id=100,
    )

    def online(executor_id: int) -> bool:
        raise AssertionError(executor_id)

    with pytest.raises(BizError):
        await continue_workitem(session, 100, 200, 55, 9, online)
