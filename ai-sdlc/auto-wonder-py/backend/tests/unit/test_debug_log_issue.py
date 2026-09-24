"""调试日志直传签发、结果收尾、对账和集群锁。"""

import base64
import logging
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList

from autowonder.agents.models import AgentVersion
from autowonder.artifacts.daemon_auth import DetailedUploadAuth, authenticate_detailed
from autowonder.db.session import get_session
from autowonder.debuglogs.issue import (
    PENDING_TIMEOUT_ERROR,
    UPLOAD_URL_TTL_SECONDS,
    IssueResult,
    issue_upload,
    reconcile_pending_once,
    record_task_result_report,
)
from autowonder.debuglogs.models import DebugLog
from autowonder.dispatch.models import Dispatch
from autowonder.executors.models import Executor
from autowonder.jobs.debug_log_reconciliation import (
    LOCK_KEY,
    LOCK_TTL_MILLIS,
    reconcile_debug_logs,
)
from autowonder.main import create_app
from autowonder.scheduledtasks.models import ScheduledTaskRun

_HEX = "a" * 64


class _Settings:
    def __init__(self, bucket: str) -> None:
        self.oss_artifact_bucket = bucket


class _Cursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Nested:
    async def __aenter__(self) -> "_Nested":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _Dup(Exception):
    def __init__(self) -> None:
        super().__init__(1062, "Duplicate entry")


class _Storage:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str, int]] = []
        self.objects: set[str] = set()
        self.fail: set[str] = set()

    def presign_put(self, bucket: str, key: str, ttl_seconds: int) -> str:
        self.puts.append((bucket, key, ttl_seconds))
        return "https://oss/put?sig=1"

    def exists(self, oss_ref: str) -> bool:
        if oss_ref in self.fail:
            raise RuntimeError("oss timeout")
        return oss_ref in self.objects


class MemorySession:
    """按语句里的等值条件回放登记行、调度和版本。"""

    def __init__(self) -> None:
        self.logs: dict[int, DebugLog] = {}
        self.by_id: dict[int, DebugLog] = {}
        self.dispatches: list[Dispatch] = []
        self.versions: dict[int, AgentVersion] = {}
        self.runs: dict[int, ScheduledTaskRun] = {}
        self.executors: dict[int, Executor] = {}
        self.hidden: DebugLog | None = None
        self.fail_insert = False
        self.issue_matches = True
        self.boom_ids: set[int] = set()
        self.commits = 0
        self.rollbacks = 0
        self.cutoffs: list[datetime] = []
        self._pending: list[DebugLog] = []
        self._next_id = 50

    def add(self, row: DebugLog) -> None:
        self._pending.append(row)

    async def flush(self) -> None:
        row = self._pending.pop()
        if self.fail_insert:
            self.fail_insert = False
            if self.hidden is not None:
                self.logs[self.hidden.dispatch_id] = self.hidden
                self.by_id[self.hidden.id] = self.hidden
            raise IntegrityError("INSERT INTO debug_log", {}, _Dup())
        row.id = self._next_id
        self._next_id += 1
        self.logs[row.dispatch_id] = row
        self.by_id[row.id] = row

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def scalar(self, statement: object) -> object:
        rows = self._rows(statement)
        if len(rows) == 0:
            return None
        return rows[0]

    async def scalars(self, statement: object) -> list[object]:
        return self._rows(statement)

    async def execute(self, statement: object) -> _Cursor:
        values = {column.key: bind.value for column, bind in statement._values.items()}
        comps = _comparisons(statement.whereclause)
        log_id = _eq(comps, "id")
        if log_id in self.boom_ids:
            raise RuntimeError("mysql gone")
        row = self.by_id.get(log_id)
        if row is None:
            return _Cursor(0)
        blocked = _ne(comps, "status")
        if blocked is not None and row.status == blocked:
            return _Cursor(0)
        required = _eq(comps, "status")
        if required is not None and row.status != required:
            return _Cursor(0)
        if values.get("status") == "PENDING" and not self.issue_matches:
            return _Cursor(0)
        for key, value in values.items():
            setattr(row, key, value)
        return _Cursor(1)

    def _rows(self, statement: object) -> list[object]:
        entity = statement.column_descriptions[0]["entity"]
        comps = _comparisons(statement.whereclause)
        if entity is DebugLog:
            dispatch_id = _eq(comps, "dispatch_id")
            if dispatch_id is not None:
                row = self.logs.get(dispatch_id)
                if row is None:
                    return []
                return [row]
            cutoff = _lt(comps, "gmt_modified")
            if isinstance(cutoff, datetime):
                self.cutoffs.append(cutoff)
            rows = [row for row in self.by_id.values() if row.status == "PENDING"]
            if isinstance(cutoff, datetime):
                rows = [row for row in rows if row.gmt_modified < cutoff]
            rows.sort(key=lambda row: row.gmt_modified)
            return rows
        if entity is Dispatch:
            dispatch_id = _eq(comps, "id")
            if dispatch_id is not None:
                return [
                    row
                    for row in self.dispatches
                    if row.id == dispatch_id and row.is_deleted == 0
                ]
            source_type = _eq(comps, "source_type")
            tenant_id = _eq(comps, "tenant_id")
            workitem_id = _eq(comps, "workitem_id")
            return [
                row
                for row in self.dispatches
                if row.tenant_id == tenant_id
                and row.source_type == source_type
                and row.workitem_id == workitem_id
                and row.is_deleted == 0
            ]
        if entity is AgentVersion:
            version_id = _eq(comps, "id")
            version = self.versions.get(version_id)
            if version is None or version.is_deleted != 0:
                return []
            return [version]
        if entity is ScheduledTaskRun:
            run = self.runs.get(_eq(comps, "id"))
            if run is None or run.workspace_id != _eq(comps, "workspace_id"):
                return []
            return [run]
        if entity is Executor:
            executor = self.executors.get(_eq(comps, "id"))
            if executor is None or executor.is_deleted != 0:
                return []
            return [executor]
        return []


def _comparisons(clause: object) -> list[tuple[str, str, object]]:
    found: list[tuple[str, str, object]] = []
    _walk(clause, found)
    return found


def _walk(node: object, found: list[tuple[str, str, object]]) -> None:
    if isinstance(node, BooleanClauseList):
        for child in node.clauses:
            _walk(child, found)
        return
    left = getattr(node, "left", None)
    key = getattr(left, "key", None)
    operator = getattr(getattr(node, "operator", None), "__name__", "")
    right = getattr(node, "right", None)
    if isinstance(key, str) and isinstance(right, BindParameter):
        found.append((key, operator, right.value))


def _eq(comps: list[tuple[str, str, object]], key: str) -> Any:
    for name, operator, value in comps:
        if name == key and operator == "eq":
            return value
    return None


def _ne(comps: list[tuple[str, str, object]], key: str) -> Any:
    for name, operator, value in comps:
        if name == key and operator == "ne":
            return value
    return None


def _lt(comps: list[tuple[str, str, object]], key: str) -> Any:
    for name, operator, value in comps:
        if name == key and operator == "lt":
            return value
    return None


def _dispatch(**overrides: object) -> Dispatch:
    values: dict[str, object] = {
        "id": 900,
        "tenant_id": 100,
        "source_type": "WORKITEM",
        "workitem_id": 200,
        "agent_id": 400,
        "agent_version_id": 410,
        "executor_id": 5,
        "status": "SUCCEEDED",
        "attempt": 0,
        "idempotency_key": "workitem-200",
        "debug_log_enabled": 1,
        "gmt_create": datetime(2026, 1, 1, 0, 0, 1),
        "gmt_modified": datetime(2026, 1, 1, 0, 0, 1),
        "is_deleted": 0,
    }
    values.update(overrides)
    return Dispatch(**values)


def _version(role_code: str | None, tenant_id: int = 100) -> AgentVersion:
    return AgentVersion(
        id=410,
        tenant_id=tenant_id,
        agent_id=400,
        version_no=1,
        role_code=role_code,
        is_deleted=0,
    )


def _log(**overrides: object) -> DebugLog:
    values: dict[str, object] = {
        "id": 12,
        "tenant_id": 100,
        "source_type": "WORKITEM",
        "source_id": 200,
        "dispatch_id": 900,
        "agent_id": 400,
        "run_no": 1,
        "dispatch_status": "SUCCEEDED",
        "object_key": "debug/200/DevAgent-run-1.log.gz",
        "truncated": 0,
        "status": "PENDING",
        "gmt_create": datetime(2026, 1, 1, 0, 0, 1),
        "gmt_modified": datetime(2026, 1, 1, 0, 0, 1),
    }
    values.update(overrides)
    return DebugLog(**values)


def _store(session: MemorySession, row: DebugLog) -> None:
    session.logs[row.dispatch_id] = row
    session.by_id[row.id] = row


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    session: MemorySession,
    bucket: str = "test-artifact-bucket",
) -> _Storage:
    storage = _Storage()
    monkeypatch.setattr(
        "autowonder.debuglogs.issue.get_settings",
        lambda: _Settings(bucket),
    )
    monkeypatch.setattr(
        "autowonder.debuglogs.issue.get_object_storage",
        lambda: storage,
    )
    return storage


def _ready(monkeypatch: pytest.MonkeyPatch) -> tuple[MemorySession, _Storage]:
    session = MemorySession()
    self_row = _dispatch()
    earlier = _dispatch(id=800, gmt_create=datetime(2025, 12, 1, 0, 0, 1))
    other_agent = _dispatch(id=850, agent_id=500, gmt_create=datetime(2025, 12, 2, 0, 0, 1))
    session.dispatches.extend([earlier, other_agent, self_row])
    session.versions[410] = _version("DevAgent")
    storage = _bind(monkeypatch, session)
    return session, storage


def _warns(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


def _infos(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]


async def test_issue_presigns_the_canonical_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一数字员工按创建顺序数轮次，并写入 PENDING 行。"""
    session, storage = _ready(monkeypatch)
    result = await issue_upload(session, session.dispatches[2], 123, _HEX, False, "SUCCEEDED")
    assert result.object_key == "debug/200/DevAgent-run-2.log.gz"
    assert result.upload_url == "https://oss/put?sig=1"
    assert result.already_uploaded is False
    assert result.expires_at is not None
    assert storage.puts == [
        ("test-artifact-bucket", "debug/200/DevAgent-run-2.log.gz", UPLOAD_URL_TTL_SECONDS)
    ]
    row = session.logs[900]
    assert row.run_no == 2
    assert row.status == "PENDING"
    assert row.size_bytes == 123
    assert row.sha256 == _HEX
    assert row.truncated == 0
    assert row.dispatch_status == "SUCCEEDED"
    assert row.upload_channel is None
    assert session.commits == 1


async def test_issue_returns_existing_key_without_presign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经 UPLOADED 的行不再签名，也不改登记。"""
    session, storage = _ready(monkeypatch)
    _store(session, _log(status="UPLOADED"))
    result = await issue_upload(session, session.dispatches[2], 123, None, False, "SUCCEEDED")
    assert result.object_key == "debug/200/DevAgent-run-1.log.gz"
    assert result.upload_url is None
    assert result.expires_at is None
    assert result.already_uploaded is True
    assert storage.puts == []
    assert session.commits == 0


async def test_issue_refreshes_a_pending_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """已有 PENDING 行改写快照，不另插一行。"""
    session, storage = _ready(monkeypatch)
    session.dispatches = [_dispatch()]
    _store(session, _log())
    result = await issue_upload(session, session.dispatches[0], 456, None, True, "FAILED")
    assert result.object_key == "debug/200/DevAgent-run-1.log.gz"
    assert result.already_uploaded is False
    row = session.by_id[12]
    assert row.size_bytes == 456
    assert row.sha256 is None
    assert row.truncated == 1
    assert row.dispatch_status == "FAILED"
    assert row.status == "PENDING"
    assert row.error_message is None
    assert storage.puts != []
    assert 900 not in session.logs or session.logs[900].id == 12


async def test_insert_race_updates_the_winner(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """插入撞上唯一键时改更新胜者行。"""
    caplog.set_level(logging.WARNING)
    session, _storage = _ready(monkeypatch)
    session.dispatches = [_dispatch()]
    session.fail_insert = True
    session.hidden = _log(id=13, status="PENDING")
    result = await issue_upload(session, session.dispatches[0], 1, None, False, "SUCCEEDED")
    assert result.already_uploaded is False
    assert session.by_id[13].object_key == "debug/200/DevAgent-run-1.log.gz"
    assert any(
        "reason=DEBUG_LOG_INSERT_RACE" in message and "winnerId=13" in message
        for message in _warns(caplog)
    )


async def test_insert_race_without_a_winner_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读不到胜者时把重复键抛给签发端点。"""
    session, storage = _ready(monkeypatch)
    session.dispatches = [_dispatch()]
    session.fail_insert = True
    with pytest.raises(IntegrityError):
        await issue_upload(session, session.dispatches[0], 1, None, False, "SUCCEEDED")
    assert storage.puts == []


async def test_scheduled_key_embeds_task_and_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """定时任务对象键带上任务 id 和运行 id。"""
    session, _storage = _ready(monkeypatch)
    dispatch = _dispatch(source_type="SCHEDULED_TASK_RUN", workitem_id=77)
    session.dispatches = [dispatch]
    session.runs[77] = ScheduledTaskRun(id=77, workspace_id=100, scheduled_task_id=12)
    result = await issue_upload(session, dispatch, 1, None, False, "CANCELED")
    assert result.object_key == "debug/scheduled-12-run-77/DevAgent-run-1.log.gz"


async def test_missing_task_id_degrades_the_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """任务 id 为空时键退化为 scheduled-0，并留下警告。"""
    caplog.set_level(logging.WARNING)
    session, _storage = _ready(monkeypatch)
    dispatch = _dispatch(source_type="SCHEDULED_TASK_RUN", workitem_id=77)
    session.dispatches = [dispatch]
    run = ScheduledTaskRun(id=77, workspace_id=100, scheduled_task_id=1)
    run.scheduled_task_id = None
    session.runs[77] = run
    result = await issue_upload(session, dispatch, 1, None, False, "SUCCEEDED")
    assert result.object_key == "debug/scheduled-0-run-77/DevAgent-run-1.log.gz"
    assert any("reason=SCHEDULED_TASK_ID_MISSING" in message for message in _warns(caplog))


async def test_missing_role_falls_back_to_agent_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """版本不存在时对象名使用 agent-{id}。"""
    caplog.set_level(logging.WARNING)
    session, _storage = _ready(monkeypatch)
    session.dispatches = [_dispatch()]
    session.versions.clear()
    result = await issue_upload(session, session.dispatches[0], None, None, False, "SUCCEEDED")
    assert result.object_key == "debug/200/agent-400-run-1.log.gz"
    assert any("reason=AGENT_VERSION_NOT_FOUND" in message for message in _warns(caplog))


async def test_refresh_with_no_matching_row_still_presigns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """并发窗口里行已变成 UPLOADED 时，本次更新落空，地址仍然签发。"""
    caplog.set_level(logging.WARNING)
    session, storage = _ready(monkeypatch)
    session.dispatches = [_dispatch()]
    session.issue_matches = False
    _store(session, _log())
    result = await issue_upload(session, session.dispatches[0], 1, None, False, "SUCCEEDED")
    assert result.already_uploaded is False
    assert storage.puts != []
    assert any("reason=DEBUG_LOG_ISSUE_UPDATE_NO_ROW" in message for message in _warns(caplog))


async def test_report_updates_an_existing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """已有行按白名单写入 DIRECT，并带上终态。"""
    session, _storage = _ready(monkeypatch)
    _store(session, _log())
    await record_task_result_report(session, 100, 5, 900, _report("UPLOADED"))
    row = session.by_id[12]
    assert row.status == "UPLOADED"
    assert row.upload_channel == "DIRECT"
    assert row.size_bytes == 123
    assert row.sha256 == _HEX
    assert row.truncated == 0
    assert row.dispatch_status == "SUCCEEDED"


async def test_report_ignores_foreign_executor_and_disabled_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """租户、执行器或开关对不上时不写登记行。"""
    session, _storage = _ready(monkeypatch)
    await record_task_result_report(session, 999, 5, 900, _report("UPLOADED"))
    session.dispatches[2].debug_log_enabled = 0
    await record_task_result_report(session, 100, 5, 900, _report("UPLOADED"))
    assert session.logs == {}


async def test_report_skips_a_non_terminal_dispatch_without_a_row(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """调度还没终态且没有旧行时不补插。"""
    caplog.set_level(logging.WARNING)
    session, _storage = _ready(monkeypatch)
    session.dispatches[2].status = "RUNNING"
    await record_task_result_report(session, 100, 5, 900, _report("UPLOADED"))
    assert session.logs == {}
    assert any(
        "reason=DEBUG_LOG_REPORT_DISPATCH_NOT_TERMINAL" in message for message in _warns(caplog)
    )


async def test_report_keeps_provisional_status_on_an_existing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有行在调度未终态时不改 dispatch_status。"""
    session, _storage = _ready(monkeypatch)
    session.dispatches = [_dispatch(status="PAUSED")]
    _store(session, _log(dispatch_status="RUNNING"))
    await record_task_result_report(session, 100, 5, 900, _report("UPLOADED"))
    row = session.by_id[12]
    assert row.status == "UPLOADED"
    assert row.dispatch_status == "RUNNING"


async def test_duplicate_uploaded_report_logs_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """已经 UPLOADED 的行不再回退，只记一条 info。"""
    caplog.set_level(logging.INFO)
    session, _storage = _ready(monkeypatch)
    _store(session, _log(status="UPLOADED", upload_channel="DIRECT"))
    await record_task_result_report(session, 100, 5, 900, _report("FAILED"))
    assert session.by_id[12].status == "UPLOADED"
    infos = _infos(caplog)
    assert any("reason=DEBUG_LOG_RESULT_UPDATE_NO_ROW" in message for message in infos)
    assert all("reason=DEBUG_LOG_INSERT_RACE" not in message for message in infos)


async def test_report_inserts_when_the_row_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态调度没有旧行时补插一条失败记录。"""
    session, _storage = _ready(monkeypatch)
    session.dispatches = [_dispatch()]
    await record_task_result_report(session, 100, 5, 900, _report("FAILED"))
    row = session.logs[900]
    assert row.status == "FAILED"
    assert row.upload_channel == "DIRECT"
    assert row.dispatch_status == "SUCCEEDED"
    assert row.object_key == "debug/200/DevAgent-run-1.log.gz"
    assert row.size_bytes == 123


async def test_report_race_falls_back_and_unreadable_winner_returns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """结果补插撞键时更新胜者；读不到胜者只记警告。"""
    caplog.set_level(logging.WARNING)
    session, _storage = _ready(monkeypatch)
    session.dispatches = [_dispatch()]
    session.fail_insert = True
    session.hidden = _log(id=13, status="PENDING")
    await record_task_result_report(session, 100, 5, 900, _report("UPLOADED"))
    assert session.by_id[13].status == "UPLOADED"
    assert any(
        "winnerId=13" in message and "reason=DEBUG_LOG_INSERT_RACE" in message
        for message in _warns(caplog)
    )

    caplog.clear()
    session.fail_insert = True
    session.hidden = None
    session.logs.clear()
    session.by_id.clear()
    await record_task_result_report(session, 100, 5, 900, _report("UPLOADED"))
    assert any("reason=DEBUG_LOG_INSERT_RACE_UNREADABLE" in message for message in _warns(caplog))


async def test_illegal_channel_is_stored_as_null(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """超长 channel 不入库，日志里截到 32 个字符。"""
    caplog.set_level(logging.WARNING)
    session, _storage = _ready(monkeypatch)
    _store(session, _log())
    payload = _report("UPLOADED")
    payload["channel"] = "x" * 40
    await record_task_result_report(session, 100, 5, 900, payload)
    assert session.by_id[12].upload_channel is None
    assert session.by_id[12].status == "UPLOADED"
    assert any("channel=" + "x" * 32 in message for message in _warns(caplog))
    assert any("reason=DEBUG_LOG_REPORT_BAD_CHANNEL" in message for message in _warns(caplog))


async def test_reconcile_marks_uploaded_failed_and_skips_a_row(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """对象还在就标 UPLOADED，缺失标 FAILED，单行异常不挡住后面的行。"""
    caplog.set_level(logging.INFO)
    session, storage = _ready(monkeypatch)
    old = datetime(2020, 1, 1, 0, 0, 0)
    uploaded = _log(id=31, object_key="debug/300/DevAgent-run-1.log.gz", gmt_modified=old)
    failed = _log(
        id=32,
        dispatch_id=902,
        object_key="debug/300/DevAgent-run-2.log.gz",
        gmt_modified=old,
    )
    boom = _log(id=34, object_key="debug/300/DevAgent-run-4.log.gz", gmt_modified=old)
    _store(session, uploaded)
    _store(session, failed)
    _store(session, boom)
    storage.objects.add("test-artifact-bucket/debug/300/DevAgent-run-1.log.gz")
    storage.fail.add("test-artifact-bucket/debug/300/DevAgent-run-4.log.gz")
    assert await reconcile_pending_once(session) == 2
    assert uploaded.status == "UPLOADED"
    assert uploaded.error_message is None
    assert failed.status == "FAILED"
    assert failed.error_message == PENDING_TIMEOUT_ERROR
    assert boom.status == "PENDING"
    infos = _infos(caplog)
    assert any(
        "scanned=3 uploaded=1 failed=1 skipped=1 reason=DEBUG_LOG_RECONCILE_SWEEP" in message
        for message in infos
    )
    assert any("reason=DEBUG_LOG_RECONCILE_MARK_FAILED" in message for message in infos)
    assert any("reason=DEBUG_LOG_RECONCILE_ROW_FAILED" in message for message in _warns(caplog))


async def test_blank_bucket_skips_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """产物桶为空或空白时不扫，避免把过期行批量标失败。"""
    caplog.set_level(logging.ERROR)
    session, storage = _ready(monkeypatch)
    _store(session, _log(gmt_modified=datetime(2020, 1, 1)))
    monkeypatch.setattr("autowonder.debuglogs.issue.get_settings", lambda: _Settings("   "))
    assert await reconcile_pending_once(session) == 0
    assert storage.objects == set()
    assert session.by_id[12].status == "PENDING"
    assert any(
        "reason=DEBUG_LOG_RECONCILE_BUCKET_UNCONFIGURED" in record.getMessage()
        for record in caplog.records
    )


async def test_idle_sweep_logs_a_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """没有过期行时也留下一轮汇总。"""
    caplog.set_level(logging.INFO)
    session, storage = _ready(monkeypatch)
    assert await reconcile_pending_once(session) == 0
    assert storage.puts == []
    assert any(
        message == "debug log reconciliation swept scanned=0 uploaded=0 failed=0 skipped=0 "
        "reason=DEBUG_LOG_RECONCILE_SWEEP"
        for message in _infos(caplog)
    )
    assert session.cutoffs != []


def test_upload_route_follows_the_status_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """404、403、409、422 先于请求体，签发成功和失败返回原始 JSON。"""
    state: dict[str, Any] = {"status": "OK", "dispatch": _dispatch(), "calls": 0}

    async def _auth(session: object, dispatch_id: int, token: str) -> DetailedUploadAuth:
        return DetailedUploadAuth(state["status"], state["dispatch"])

    async def _issue(
        session: object,
        dispatch: Dispatch,
        size_bytes: int | None,
        sha256: str | None,
        truncated: bool,
        dispatch_status: str,
    ) -> IssueResult:
        state["calls"] += 1
        state["issued"] = (size_bytes, sha256, truncated, dispatch_status)
        if state.get("boom"):
            raise RuntimeError("db down")
        return IssueResult(
            "debug/200/DevAgent-run-1.log.gz",
            "https://oss/put",
            datetime(2026, 9, 4, 12, 34, 56, tzinfo=None),
            False,
        )

    monkeypatch.setattr("autowonder.debuglogs.upload_router.authenticate_detailed", _auth)
    monkeypatch.setattr("autowonder.debuglogs.upload_router.issue_upload", _issue)
    client = _client()
    state["status"] = "DISPATCH_NOT_FOUND"
    missing = client.post(_path(), params={"token": "tok"}, json=_body())
    assert missing.status_code == 404
    assert missing.json() == {"error": "dispatch_not_found"}
    assert state["calls"] == 0

    state["status"] = "TOKEN_INVALID"
    denied = client.post(_path(), params={"token": "tok"}, json=_body())
    assert denied.status_code == 403
    assert denied.json()["error"] == "token_invalid"

    state["status"] = "OK"
    state["dispatch"] = _dispatch(status="RUNNING")
    running = client.post(_path(), params={"token": "tok"}, json=_body("SUCCEEDED", sha256="xyz"))
    assert running.status_code == 409
    assert running.json()["error"] == "dispatch_not_terminal"

    state["dispatch"] = _dispatch(debug_log_enabled=0)
    disabled = client.post(_path(), params={"token": "tok"}, json=_body("RUNNING"))
    assert disabled.status_code == 422
    assert disabled.json()["error"] == "debug_log_disabled"

    state["dispatch"] = _dispatch()
    bad_status = client.post(_path(), params={"token": "tok"}, json=_body("RUNNING"))
    assert bad_status.status_code == 400
    assert bad_status.json()["error"] == "invalid_dispatch_status"
    empty = client.post(_path(), params={"token": "tok"})
    assert empty.status_code == 400
    short = client.post(_path(), params={"token": "tok"}, json=_body(sha256="a" * 63))
    assert short.status_code == 400
    assert short.json()["error"] == "invalid_sha256"
    negative = client.post(_path(), params={"token": "tok"}, json=_body(sizeBytes=-1))
    assert negative.status_code == 400
    assert state["calls"] == 0

    zero = client.post(_path(), params={"token": "tok"}, json=_body(sizeBytes=0, sha256="A" * 64))
    assert zero.status_code == 200
    assert state["issued"] == (0, "A" * 64, False, "SUCCEEDED")
    body = zero.json()
    assert body["objectKey"] == "debug/200/DevAgent-run-1.log.gz"
    assert body["uploadUrl"] == "https://oss/put"
    assert body["alreadyUploaded"] is False

    state["boom"] = True
    failed = client.post(_path(), params={"token": "tok"}, json=_body())
    assert failed.status_code == 503
    assert failed.json() == {"error": "issue_failed"}


def test_already_uploaded_body_keeps_null_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """已经传过时地址和到期时间是 JSON null。"""

    async def _auth(session: object, dispatch_id: int, token: str) -> DetailedUploadAuth:
        return DetailedUploadAuth("OK", _dispatch())

    async def _issue(*args: object, **kwargs: object) -> IssueResult:
        return IssueResult("debug/200/DevAgent-run-1.log.gz", None, None, True)

    monkeypatch.setattr("autowonder.debuglogs.upload_router.authenticate_detailed", _auth)
    monkeypatch.setattr("autowonder.debuglogs.upload_router.issue_upload", _issue)
    response = _client().post(_path(), params={"token": "tok"}, json=_body())
    assert response.status_code == 200
    assert response.json()["uploadUrl"] is None
    assert response.json()["expiresAt"] is None
    assert response.json()["alreadyUploaded"] is True


def test_debug_log_upload_route_is_registered() -> None:
    """直传签发路径出现在 OpenAPI 里。"""
    paths = create_app().openapi()["paths"]
    assert "post" in paths["/api/daemon/dispatches/{dispatchId}/debug-log-upload"]


async def test_detailed_auth_separates_missing_dispatch_from_bad_token() -> None:
    """调度不存在是 404 语义，令牌不对是 403 语义，令牌正确才算通过。"""
    session = MemorySession()
    missing = await authenticate_detailed(session, 900, "tok")
    assert missing.status == "DISPATCH_NOT_FOUND"
    session.dispatches.append(_dispatch())
    invalid = await authenticate_detailed(session, 900, "tok")
    assert invalid.status == "TOKEN_INVALID"
    assert invalid.dispatch is not None
    session.executors[5] = Executor(
        id=5,
        tenant_id=100,
        agent_id=400,
        name="exec",
        token_ref="b64:" + base64.b64encode(b"tok").decode("ascii"),
        is_deleted=0,
    )
    ok = await authenticate_detailed(session, 900, "tok")
    assert ok.status == "OK"
    assert ok.dispatch is not None
    assert ok.dispatch.id == 900


async def test_reconciliation_lock_runs_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿到锁才扫，释放时使用同一个 owner。没拿到锁不释放。"""
    seen: dict[str, object] = {}

    async def _acquire(key: str, owner: str, ttl: int) -> bool:
        seen["acquire"] = (key, owner, ttl)
        return bool(seen.get("locked", True))

    async def _release(key: str, owner: str) -> bool:
        seen["release"] = (key, owner)
        return True

    async def _sweep(session: object) -> int:
        seen["sweep"] = True
        if seen.get("boom"):
            raise RuntimeError("db down")
        return 0

    class _Ctx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    monkeypatch.setattr("autowonder.jobs.debug_log_reconciliation.try_acquire_lock", _acquire)
    monkeypatch.setattr("autowonder.jobs.debug_log_reconciliation.release_lock", _release)
    monkeypatch.setattr(
        "autowonder.jobs.debug_log_reconciliation.reconcile_pending_once",
        _sweep,
    )
    monkeypatch.setattr("autowonder.jobs.debug_log_reconciliation.SessionLocal", lambda: _Ctx())
    await reconcile_debug_logs()
    acquire = seen["acquire"]
    assert acquire[0] == LOCK_KEY
    assert acquire[2] == LOCK_TTL_MILLIS
    assert seen["release"] == (LOCK_KEY, acquire[1])
    assert seen["sweep"] is True

    seen.clear()
    seen["locked"] = False
    await reconcile_debug_logs()
    assert "sweep" not in seen
    assert "release" not in seen

    seen.clear()
    seen["boom"] = True
    await reconcile_debug_logs()
    assert seen["release"][0] == LOCK_KEY


def _report(status: str) -> dict[str, object]:
    return {
        "status": status,
        "channel": "DIRECT",
        "sizeBytes": 123,
        "sha256": _HEX,
        "truncated": False,
    }


def _body(
    status: str = "SUCCEEDED",
    sha256: str | None = None,
    sizeBytes: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "sizeBytes": 123,
        "sha256": _HEX,
        "truncated": False,
        "dispatchStatus": status,
    }
    if sha256 is not None:
        payload["sha256"] = sha256
    if sizeBytes is not None:
        payload["sizeBytes"] = sizeBytes
    return payload


def _path() -> str:
    return "/api/daemon/dispatches/900/debug-log-upload"


def _client() -> TestClient:
    app = create_app()

    async def _empty() -> object:
        yield object()

    app.dependency_overrides[get_session] = _empty
    return TestClient(app)
