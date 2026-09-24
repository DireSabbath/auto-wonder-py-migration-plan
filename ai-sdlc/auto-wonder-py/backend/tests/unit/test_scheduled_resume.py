"""暂停的定时运行按冻结版本开一条 CONTINUOUS 续跑，并在提交后 runPending。"""

from datetime import datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.scheduledtasks.orchestrator import resume_paused
from tests.unit.test_workitems import MemorySession

_SCHEMA = "autowonder.scheduledTaskExecutionSnapshot.v1"


class _ResumeSession(MemorySession):
    def expire(self, row: object) -> None:
        return None

    async def get(self, entity: type[object], identity: int) -> object | None:
        for row in self.rows:
            if isinstance(row, entity) and getattr(row, "id", None) == identity:
                return row
        return None


def _snapshot() -> dict[str, object]:
    return {
        "schemaVersion": _SCHEMA,
        "task": {"id": 12, "name": "nightly", "instructionMd": "do it"},
        "assignment": {"squadId": 3, "initialAgentId": 20},
        "policies": {"sessionMode": "CONTINUOUS"},
        "requirementDocuments": [],
        "agentContexts": [
            {"agentId": 20, "agentVersionId": 401},
            {"agentId": 30, "agentVersionId": 402},
        ],
    }


def _run() -> ScheduledTaskRun:
    moment = datetime(2026, 9, 24, 8, 0, 0)
    return ScheduledTaskRun(
        id=77,
        workspace_id=1,
        scheduled_task_id=12,
        trigger_key="nightly",
        trigger_type="SCHEDULED",
        scheduled_at=moment,
        status="QUEUED",
        squad_id=3,
        initial_agent_id=20,
        current_agent_id=30,
        sdlc_id=52,
        current_step_id=90,
        session_mode="CONTINUOUS",
        execution_snapshot_json=_snapshot(),
        owner_id=9,
        creator_id=9,
        version=0,
        gmt_create=moment,
        gmt_modified=moment,
    )


def _paused(dispatch_id: int, attempt: int = 1) -> Dispatch:
    moment = datetime(2026, 9, 24, 8, 0, 0)
    return Dispatch(
        id=dispatch_id,
        tenant_id=1,
        source_type="SCHEDULED_TASK_RUN",
        workitem_id=77,
        sdlc_step_id=91,
        agent_id=30,
        status="PAUSED",
        attempt=attempt,
        idempotency_key="paused-" + str(dispatch_id),
        version=2,
        is_deleted=0,
        gmt_create=moment,
        gmt_modified=moment,
    )


def _patch(monkeypatch: pytest.MonkeyPatch, ran: list[int]) -> None:
    async def run_pending(session: AsyncSession, dispatch_id: int) -> bool:
        pinned = await session.get(Dispatch, dispatch_id)
        assert pinned is not None
        assert pinned.agent_version_id == 402
        ran.append(dispatch_id)
        return True

    async def publish_status(
        session: AsyncSession, workspace_id: int, run_id: int
    ) -> None:
        return None

    monkeypatch.setattr(
        "autowonder.scheduledtasks.orchestrator.run_pending",
        run_pending,
    )
    monkeypatch.setattr(
        "autowonder.scheduledtasks.notify.publish_status",
        publish_status,
    )


async def test_paused_dispatch_resumes_with_the_frozen_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[int] = []
    _patch(monkeypatch, ran)
    session = _ResumeSession()
    run = _run()
    session.add(run)
    session.add(_paused(700))
    session.add(_paused(810, attempt=3))
    await session.flush()

    continued = await resume_paused(cast(AsyncSession, session), 1, 77, 9)

    assert continued is True
    assert run.status == "WAITING_EXECUTOR"
    assert run.current_agent_id == 30
    assert run.current_step_id == 91
    assert run.sdlc_id == 52
    created = [
        row
        for row in session.rows
        if isinstance(row, Dispatch) and row.status == "PENDING"
    ]
    assert len(created) == 1
    continuation = created[0]
    assert continuation.resume_from_dispatch_id == 810
    assert continuation.resume_mode == "CONTINUOUS"
    assert continuation.attempt == 4
    assert continuation.agent_version_id == 402
    assert continuation.idempotency_key == "scheduled-resume:77:810:native"
    assert ran == [continuation.id]


async def test_missing_paused_dispatch_does_not_start_a_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[int] = []
    _patch(monkeypatch, ran)
    session = _ResumeSession()
    run = _run()
    session.add(run)
    await session.flush()

    continued = await resume_paused(cast(AsyncSession, session), 1, 77, 9)

    assert continued is False
    assert run.status == "QUEUED"
    assert ran == []


async def test_invalid_snapshot_fails_before_a_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[int] = []
    _patch(monkeypatch, ran)
    session = _ResumeSession()
    run = _run()
    run.execution_snapshot_json = {"schemaVersion": "other"}
    session.add(run)
    session.add(_paused(700))
    await session.flush()

    with pytest.raises(BizError) as raised:
        await resume_paused(cast(AsyncSession, session), 1, 77, 9)

    assert raised.value.code == ErrorCode.SCHEDULED_TASK_INVALID_STATE.code
    assert run.status == "QUEUED"
    assert ran == []
