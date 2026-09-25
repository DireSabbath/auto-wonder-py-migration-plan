"""暂停与成功结果竞争，以及定时运行在全部停下后取消。"""

from datetime import UTC, datetime

from autowonder.core.clock import now_local
from autowonder.dispatch.executor_reports import on_completed_while_pausing, on_paused
from autowonder.dispatch.models import Dispatch
from autowonder.scheduledtasks.models import ScheduledTaskRun
from tests.unit.test_workitems import MemorySession


class _Store(MemorySession):
    async def get(self, model: type[object], ident: int) -> object | None:
        for row in self.rows:
            if isinstance(row, model) and getattr(row, "id", None) == ident:
                return row
        return None


def _dispatch(status: str, source: str = "WORKITEM") -> Dispatch:
    return Dispatch(
        id=8,
        tenant_id=1,
        source_type=source,
        workitem_id=3,
        agent_id=20,
        executor_id=50,
        status=status,
        attempt=1,
        idempotency_key="dispatch-8",
        version=0,
        is_deleted=0,
        gmt_create=now_local(),
        gmt_modified=now_local(),
    )


def _run() -> ScheduledTaskRun:
    moment = datetime(2026, 9, 24, tzinfo=UTC).replace(tzinfo=None)
    return ScheduledTaskRun(
        id=3,
        workspace_id=1,
        scheduled_task_id=9,
        trigger_key="task:9:scheduled:2026-09-24T00:00:00Z",
        trigger_type="SCHEDULED",
        scheduled_at=moment,
        status="RUNNING",
        squad_id=4,
        initial_agent_id=20,
        session_mode="ISOLATED",
        execution_snapshot_json={},
        error="CANCEL_PENDING",
        owner_id=7,
        creator_id=7,
        version=1,
        gmt_create=now_local(),
        gmt_modified=now_local(),
    )


async def test_success_while_pausing_becomes_paused() -> None:
    """可对上的成功结果在暂停中或暂停失败时改成 PAUSED。"""
    session = _Store()
    session.add(_dispatch("PAUSING"))
    await session.flush()
    assert await on_completed_while_pausing(session, 1, 50, 8, True) == "PAUSED"
    assert session.rows[0].status == "PAUSED"
    assert await on_completed_while_pausing(session, 1, 50, 8, True) == "NOT_PAUSING"


async def test_pause_race_rejects_without_a_durable_checkpoint() -> None:
    """暂停竞争没有持久检查点时拒绝，不改状态。"""
    session = _Store()
    session.add(_dispatch("PAUSE_FAILED"))
    await session.flush()
    assert await on_completed_while_pausing(session, 1, 50, 8, False) == "REJECTED"
    assert session.rows[0].status == "PAUSE_FAILED"
    assert await on_completed_while_pausing(session, 1, 99, 8, True) == "REJECTED"


async def test_quiescent_scheduled_pause_finishes_cancel() -> None:
    """定时运行已写取消意图，最后一条派发暂停后运行变为 CANCELED。"""
    session = _Store()
    session.add(_dispatch("PAUSING", "SCHEDULED_TASK_RUN"))
    session.add(_run())
    await session.flush()
    assert await on_paused(session, 1, 50, 8, True) is True
    run = next(row for row in session.rows if isinstance(row, ScheduledTaskRun))
    assert run.status == "CANCELED"
    assert run.error == "CANCELED"
