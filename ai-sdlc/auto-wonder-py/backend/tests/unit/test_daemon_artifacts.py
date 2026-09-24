"""执行器产物上报的回执、路径和邻域调用。"""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.agents.evolution import accepts_runtime_delta
from autowonder.aiusage.dispatch_usage import is_usage_artifact, usage_upsert_statement
from autowonder.artifacts.daemon_auth import UploadAuth
from autowonder.artifacts.daemon_upload import (
    DaemonFile,
    RelayTarget,
    ReportedArtifact,
    UploadHooks,
    UploadResult,
    classify,
    is_debug_log,
    logical_path,
    sanitize_path,
    upload_daemon_artifacts,
)
from autowonder.artifacts.service import reported_artifact_statement
from autowonder.audits.service import AuditRecord
from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.delta import ingest_evolution_delta
from autowonder.main import create_app
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.storage.objects import InMemoryObjectStorage, StoredObject


class Sink:
    """记下上报过程调用了哪些邻域。"""

    def __init__(self) -> None:
        self.records: list[ReportedArtifact] = []
        self.audits: list[AuditRecord] = []
        self.usage: list[tuple[Any, ...]] = []
        self.memory: list[tuple[Any, ...]] = []
        self.evolution: list[tuple[Any, ...]] = []
        self.modes: list[tuple[int, int]] = []
        self.capability = 0
        self.mode = "ASSISTED"
        self.fail_capability = False
        self.relays: list[tuple[Any, ...]] = []
        self.lookup_calls = 0
        self.relay_result: RelayTarget | None = None
        self.relay_lookup_error: Exception | None = None
        self.relay_record_error: Exception | None = None
        self.evolution_error: Exception | None = None
        self.notifies: list[tuple[int, int]] = []
        self.task_id: int | None = None
        self.next_id = 50


class ScriptedStorage:
    """按脚本返回引用，或在 put 时失败。"""

    def __init__(self, stored: StoredObject | None = None) -> None:
        self.stored = stored
        self.error: Exception | None = None
        self.puts: list[tuple[str, str, bytes]] = []

    def put(self, bucket: str, key: str, data: bytes) -> StoredObject:
        if self.error is not None:
            raise self.error
        payload = bytes(data)
        self.puts.append((bucket, key, payload))
        if self.stored is not None:
            return self.stored
        return StoredObject(bucket + "/" + key, "md5", len(payload))


def test_classify_and_sanitize_match_the_daemon_rules() -> None:
    """路径分类、调试前缀和穿越拒绝与 Java 控制器一致。"""
    assert classify("deliverables/report.md") == "DELIVERABLE"
    assert classify("patches/fix.patch") == "PATCH"
    assert classify("evidence/screenshot.png") == "EVIDENCE"
    assert classify("handoff/proposal.json") == "HANDOFF"
    assert classify("learning_delta/memory_delta.json") == "LEARNING"
    assert classify("other/something.txt") == "FILE"
    assert classify("debug/DevAgent-99.log.gz") == "DEBUG_LOG"
    assert is_debug_log("debug/DevAgent-99.log.gz") is True
    assert is_debug_log(logical_path("artifacts/output/debug/DevAgent-99.log.gz")) is True
    assert is_debug_log("deliverables/debug-notes.md") is False
    assert is_debug_log(None) is False
    assert sanitize_path("../secret") is None
    assert sanitize_path("/etc/passwd") is None
    assert sanitize_path("C:\\temp\\secret.txt") is None
    assert sanitize_path("foo/../../bar") is None
    assert sanitize_path("foo\0bar") is None
    assert sanitize_path("") is None
    assert sanitize_path(None) is None
    assert sanitize_path("deliverables/report.md") == "deliverables/report.md"
    assert (
        sanitize_path("artifacts/output/handoff/handoff-to-指派操作人.md")
        == "artifacts/output/handoff/handoff-to-指派操作人.md"
    )
    assert sanitize_path("file.txt") == "file.txt"
    assert accepts_runtime_delta("ASSISTED") is True
    assert accepts_runtime_delta("MANUAL") is False


def test_daemon_artifact_route_is_registered() -> None:
    """产物上报挂在 daemon 前缀下，不走会话登录。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "post" in paths["/api/daemon/dispatches/{dispatchId}/artifacts"]


def test_reported_artifact_upsert_reuses_the_primary_key() -> None:
    """同名再上报复用原主键，并覆盖类型和对象引用。"""
    statement = reported_artifact_statement(
        10,
        "WORKITEM",
        20,
        99,
        "deliverables/report.md",
        "DELIVERABLE",
        "oss://bucket/k",
        5,
    )
    compiled = str(statement.compile(dialect=mysql.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in compiled
    assert "LAST_INSERT_ID(id)" in compiled
    assert "oss_ref" in compiled
    usage = usage_upsert_statement(
        10,
        20,
        99,
        30,
        4,
        50,
        "step",
        "openai",
        "gpt",
        1,
        2,
        0,
        0,
        0,
        None,
        3,
        {"provider": "openai"},
    )
    usage_sql = str(usage.compile(dialect=mysql.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in usage_sql
    assert "LAST_INSERT_ID(id)" in usage_sql
    assert "coalesce" in usage_sql.lower()


def test_usage_artifact_names() -> None:
    """用量文件可以带目录前缀，反斜杠先折成斜杠。"""
    assert is_usage_artifact("observability/usage.json") is True
    assert is_usage_artifact("artifacts\\output\\observability\\usage.json") is True
    assert is_usage_artifact("deliverables/report.md") is False
    assert is_usage_artifact(None) is False


def test_scheduled_capability_follows_deployment_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """任一部署开关关闭时，定时任务上报在写存储之前拒绝。"""

    class Flags:
        scheduled_task_enabled = False
        scheduled_task_cluster_ready = True

    monkeypatch.setattr(
        "autowonder.scheduledtasks.capability.get_settings",
        lambda: Flags(),
    )
    with pytest.raises(BizError) as caught:
        require_scheduled_capability()
    assert caught.value.code == "30006"


async def test_returns_401_when_auth_fails() -> None:
    """令牌无效时不碰存储，正文为空。"""
    sink = Sink()
    storage = ScriptedStorage()
    result = await _upload(
        storage,
        _hooks(sink),
        [_file("f.txt", b"hi")],
        auth=UploadAuth(False, 0, 0, 0, None, "WORKITEM"),
    )
    assert result.status == 401
    assert result.body is None
    assert storage.puts == []
    assert sink.records == []
    assert sink.modes == []


async def test_returns_401_when_mutation_is_fenced() -> None:
    """调度已取消或工单已关闭时，同样返回空的 401。"""
    sink = Sink()
    storage = ScriptedStorage()
    result = await _upload(storage, _hooks(sink), [_file("f.txt", b"hi")], fenced=True)
    assert result.status == 401
    assert storage.puts == []
    assert sink.modes == []


async def test_scheduled_upload_fails_before_storage_when_unavailable() -> None:
    """定时任务能力未就绪时，不解析演进模式，也不写存储。"""
    sink = Sink()
    sink.fail_capability = True
    storage = ScriptedStorage()
    with pytest.raises(BizError) as caught:
        await _upload(
            storage,
            _hooks(sink),
            [_file("x", b"\x01")],
            auth=_auth("SCHEDULED_TASK_RUN"),
        )
    assert caught.value.code == "30006"
    assert storage.puts == []
    assert sink.records == []
    assert sink.modes == []


async def test_stores_files_and_records_artifacts() -> None:
    """工单产物按内容哈希落桶，并写审计和用量入口。"""
    sink = Sink()
    storage = ScriptedStorage(StoredObject("oss://bucket/k", "md5", 5))
    metadata = '[{"path":"deliverables/report.md","sha256":"abc","sizeBytes":5}]'
    result = await _upload(storage, _hooks(sink), [_file("report.md", b"hello")], metadata)
    assert result.status == 200
    assert result.body is not None
    assert "t/10/workitem/20/dispatch/99/" in result.body["remoteRef"]
    receipts = result.body["files"]
    assert receipts[0]["path"] == "deliverables/report.md"
    assert receipts[0]["status"] == "ACCEPTED"
    assert receipts[0]["ossRef"] == "oss://bucket/k"
    assert receipts[0]["remoteRef"] == "oss://bucket/k"
    bucket, key, payload = storage.puts[0]
    assert bucket == "test-artifact-bucket"
    _assert_key(key, "deliverables/report.md")
    assert payload == b"hello"
    recorded = sink.records[0]
    assert recorded.name == "deliverables/report.md"
    assert recorded.artifact_type == "DELIVERABLE"
    assert recorded.dispatch_id == 99
    assert recorded.workitem_id == 20
    assert recorded.source_type == "WORKITEM"
    audit = sink.audits[0]
    assert audit.tenant_id == 10
    assert audit.actor_id == 30
    assert audit.module == "ARTIFACT"
    assert audit.action == "UPLOAD_ARTIFACT"
    assert sink.usage[0][0:6] == (10, 20, 99, 51, "deliverables/report.md", "oss://bucket/k")
    assert sink.capability == 0


async def test_stores_observability_without_audit() -> None:
    """观测文件类型是 TELEMETRY，用量入口仍调用，审计不写。"""
    sink = Sink()
    events = b'{"eventId":"99:1"}\n'
    storage = ScriptedStorage(StoredObject("oss://bucket/events", "md5", 20))
    await _upload(
        storage,
        _hooks(sink),
        [_file("events.jsonl", events)],
        '[{"path":"observability/events.jsonl"}]',
    )
    _assert_key(storage.puts[0][1], "observability/events.jsonl")
    assert sink.records[0].artifact_type == "TELEMETRY"
    assert sink.usage[0][4] == "observability/events.jsonl"
    assert sink.usage[0][6] == events
    assert sink.audits == []


async def test_returns_503_when_storage_is_unavailable() -> None:
    """存储失败时不登记产物。"""
    sink = Sink()
    storage = ScriptedStorage()
    storage.error = RuntimeError("oss unavailable")
    result = await _upload(storage, _hooks(sink), [_file("result.md", b"data")])
    assert result.status == 503
    assert result.body == {"error": "artifact upload temporarily unavailable"}
    assert sink.records == []


async def test_invokes_memory_and_evolution_for_learning_delta() -> None:
    """ASSISTED 模式把两类增量交给对应入口。"""
    sink = Sink()
    storage = ScriptedStorage(StoredObject("oss://bucket/k", "md5", 100))
    memory = b'{"entries":[{"type":"memory","title":"t","content":"c"}]}'
    await _upload(
        storage,
        _hooks(sink),
        [_file("memory_delta.json", memory)],
        '[{"path":"learning_delta/memory_delta.json"}]',
    )
    assert sink.memory == [(10, 30, 99, memory)]
    evolution = b'{"candidates":[{"assetType":"SKILL","assetId":88}]}'
    await _upload(
        storage,
        _hooks(sink),
        [_file("evolution_delta.json", evolution)],
        '[{"path":"artifacts/output/learning_delta/evolution_delta.json"}]',
    )
    assert sink.evolution[-1] == (10, 30, 99, evolution, "ASSISTED")


async def test_invalid_evolution_delta_keeps_the_artifact_accepted() -> None:
    """演进解析失败不改变 ACCEPTED 回执。"""
    sink = Sink()
    sink.evolution_error = BizError(ErrorCode.PARAM_INVALID)
    storage = ScriptedStorage(StoredObject("oss://bucket/k", "md5", 16))
    content = b'{"proposals":[]}'
    result = await _upload(
        storage,
        _hooks(sink),
        [_file("evolution_delta.json", content)],
        '[{"path":"learning_delta/evolution_delta.json"}]',
    )
    assert result.status == 200
    assert result.body is not None
    assert result.body["files"][0]["status"] == "ACCEPTED"
    assert len(sink.records) == 1


async def test_manual_mode_stores_delta_without_ingesting() -> None:
    """手动模式只保存增量文件。"""
    sink = Sink()
    sink.mode = "MANUAL"
    storage = ScriptedStorage(StoredObject("oss://bucket/k", "md5", 100))
    memory = b'{"entries":[{"type":"memory","title":"t","content":"c"}]}'
    evolution = b'{"candidates":[{"assetType":"SKILL","assetId":88}]}'
    metadata = (
        '[{"path":"learning_delta/memory_delta.json"},'
        '{"path":"learning_delta/evolution_delta.json"}]'
    )
    result = await _upload(
        storage,
        _hooks(sink),
        [_file("memory_delta.json", memory), _file("evolution_delta.json", evolution)],
        metadata,
    )
    assert result.status == 200
    assert len(sink.records) == 2
    assert sink.memory == []
    assert sink.evolution == []


async def test_uses_filename_when_metadata_missing() -> None:
    """没有 filesMetadata 时，对象键使用原始文件名。"""
    sink = Sink()
    storage = ScriptedStorage(StoredObject("oss://bucket/k", "md5", 3))
    await _upload(storage, _hooks(sink), [_file("myfile.txt", b"abc")])
    _assert_key(storage.puts[0][1], "myfile.txt")


async def test_rejects_path_traversal() -> None:
    """穿越路径整文件拒绝，存储和登记都不发生。"""
    sink = Sink()
    storage = ScriptedStorage()
    result = await _upload(
        storage,
        _hooks(sink),
        [_file("passwd", b"evil")],
        '[{"path":"../../etc/passwd"}]',
    )
    assert storage.puts == []
    assert sink.records == []
    assert result.body is not None
    receipt = result.body["files"][0]
    assert receipt["status"] == "REJECTED"
    assert receipt["code"] == "INVALID_PATH"
    assert "maxBytes" not in receipt


async def test_later_canonical_upload_keeps_the_previous_object() -> None:
    """同名后写使用新的内容哈希，先前接受的引用仍能读到原来的字节。"""
    sink = Sink()
    storage = InMemoryObjectStorage()
    metadata = '[{"path":"artifacts/output/deliverables/report.md"}]'
    first = await _upload(storage, _hooks(sink), [_file("report.md", b"AAAA")], metadata)
    assert first.body is not None
    accepted = first.body["files"][0]["ossRef"]
    assert first.body["files"][0]["remoteRef"] == accepted
    assert storage.get(accepted) == b"AAAA"
    await _upload(storage, _hooks(sink), [_file("report.md", b"BBBB")], metadata)
    assert storage.get(accepted) == b"AAAA"
    retry = await _upload(storage, _hooks(sink), [_file("report.md", b"AAAA")], metadata)
    assert retry.body is not None
    assert retry.body["files"][0]["remoteRef"] == accepted


async def test_debug_relay_uses_the_canonical_key() -> None:
    """调试文件改写到规范键，跳过审计，类型仍是 DEBUG_LOG。"""
    sink = Sink()
    sink.relay_result = RelayTarget("debug/20/DevAgent-run-1.log.gz", 1)
    storage = ScriptedStorage(
        StoredObject("test-artifact-bucket/debug/20/DevAgent-run-1.log.gz", "md5", 7),
    )
    metadata = (
        '[{"path":"debug/DevAgent-99.log.gz","sha256":"' + ("a" * 64) + '","sizeBytes":7}]'
    )
    result = await _upload(
        storage,
        _hooks(sink, relay=True),
        [_file("DevAgent-99.log.gz", bytes(7))],
        metadata,
    )
    assert result.status == 200
    assert storage.puts[0][1] == "debug/20/DevAgent-run-1.log.gz"
    assert sink.relays[0][2] == "debug/20/DevAgent-run-1.log.gz"
    assert sink.relays[0][3] == 1
    assert sink.relays[0][4] == 7
    assert sink.relays[0][5]["sha256"] == "a" * 64
    assert sink.audits == []
    assert sink.records[0].artifact_type == "DEBUG_LOG"
    assert result.body is not None
    assert result.body["files"][0]["status"] == "ACCEPTED"


async def test_debug_relay_is_rejected_when_disabled() -> None:
    """调度没有打开调试日志时，该文件拒绝，存储不被调用。"""
    sink = Sink()
    sink.relay_result = None
    storage = ScriptedStorage()
    result = await _upload(
        storage,
        _hooks(sink, relay=True),
        [_file("DevAgent-99.log.gz", bytes(7))],
        '[{"path":"debug/DevAgent-99.log.gz"}]',
    )
    assert result.status == 200
    assert storage.puts == []
    assert result.body is not None
    assert result.body["files"][0]["status"] == "REJECTED"
    assert result.body["files"][0]["code"] == "DEBUG_LOG_DISABLED"


async def test_debug_relay_still_rejects_oversized_files() -> None:
    """50MB 上限先于中转判定。"""
    sink = Sink()
    storage = ScriptedStorage()
    result = await _upload(
        storage,
        _hooks(sink, relay=True),
        [_file("DevAgent-99.log.gz", b"", 51 * 1024 * 1024)],
        '[{"path":"debug/DevAgent-99.log.gz"}]',
    )
    assert result.status == 200
    assert storage.puts == []
    assert sink.lookup_calls == 0
    assert result.body is not None
    assert result.body["files"][0]["code"] == "FILE_TOO_LARGE"
    assert result.body["files"][0]["maxBytes"] == 50 * 1024 * 1024


async def test_debug_lookup_failure_rejects_only_that_file() -> None:
    """中转查询失败只拒绝调试文件，同批其他产物照常入库。"""
    sink = Sink()
    sink.relay_lookup_error = RuntimeError("db down")
    storage = ScriptedStorage(StoredObject("oss://bucket/k", "md5", 5))
    metadata = '[{"path":"debug/DevAgent-99.log.gz"},{"path":"deliverables/report.md"}]'
    result = await _upload(
        storage,
        _hooks(sink, relay=True),
        [_file("DevAgent-99.log.gz", bytes(7)), _file("report.md", b"hello")],
        metadata,
    )
    assert result.status == 200
    assert result.body is not None
    assert result.body["files"][0]["status"] == "REJECTED"
    assert result.body["files"][0]["code"] == "DEBUG_LOG_DISABLED"
    assert result.body["files"][1]["status"] == "ACCEPTED"
    _assert_key(storage.puts[0][1], "deliverables/report.md")
    assert sink.relays == []


async def test_debug_record_failure_keeps_receipt_accepted() -> None:
    """中转落库失败不回滚已经接受的回执。"""
    sink = Sink()
    sink.relay_result = RelayTarget("debug/20/DevAgent-run-1.log.gz", 1)
    sink.relay_record_error = RuntimeError("db down")
    storage = ScriptedStorage(
        StoredObject("test-artifact-bucket/debug/20/DevAgent-run-1.log.gz", "md5", 7),
    )
    result = await _upload(
        storage,
        _hooks(sink, relay=True),
        [_file("DevAgent-99.log.gz", bytes(7))],
        '[{"path":"debug/DevAgent-99.log.gz"}]',
    )
    assert result.status == 200
    assert result.body is not None
    assert result.body["files"][0]["status"] == "ACCEPTED"
    assert len(sink.records) == 1


async def test_debug_relay_is_rejected_when_the_service_is_absent() -> None:
    """没有调试日志服务时，debug/ 文件只拒绝。"""
    sink = Sink()
    storage = ScriptedStorage()
    result = await _upload(
        storage,
        _hooks(sink),
        [_file("DevAgent-99.log.gz", bytes(7))],
        '[{"path":"debug/DevAgent-99.log.gz"}]',
    )
    assert result.status == 200
    assert storage.puts == []
    assert result.body is not None
    assert result.body["files"][0]["code"] == "DEBUG_LOG_DISABLED"


async def test_scheduled_upload_records_the_run_owner() -> None:
    """定时任务产物的目录和归属都是运行，审计带上任务 id。"""
    sink = Sink()
    sink.task_id = 77
    storage = ScriptedStorage(StoredObject("oss://bucket/k", "md5", 1))
    result = await _upload(
        storage,
        _hooks(sink, notify=True, task=True),
        [_file("note.txt", b"z")],
        auth=_auth("SCHEDULED_TASK_RUN"),
    )
    assert result.status == 200
    assert result.body is not None
    assert "t/10/scheduled-task-run/20/dispatch/99/" in result.body["remoteRef"]
    assert sink.records[0].source_type == "SCHEDULED_TASK_RUN"
    assert sink.audits[0].detail["runId"] == 20
    assert sink.audits[0].detail["taskId"] == 77
    assert sink.notifies == [(10, 20)]
    assert sink.capability == 1


async def test_too_many_files_is_rejected_before_storage() -> None:
    """超过 200 个文件时整批 400，此时演进模式已经解析。"""
    sink = Sink()
    storage = ScriptedStorage()
    files = [_file("a.txt", b"a") for _ in range(201)]
    result = await _upload(storage, _hooks(sink), files)
    assert result.status == 400
    assert result.body == {"error": "too many files"}
    assert storage.puts == []
    assert sink.modes == [(10, 30)]


async def test_empty_evolution_delta_is_invalid() -> None:
    """空对象在进入编排前就是参数不合法。"""
    with pytest.raises(BizError) as caught:
        await ingest_evolution_delta(
            None,  # type: ignore[arg-type]
            1,
            2,
            3,
            b"{}",
            "ASSISTED",
        )
    assert caught.value.code == "10001"


def _auth(source: str) -> UploadAuth:
    return UploadAuth(True, 10, 20, 30, None, source)


def _file(name: str | None, payload: bytes, size: int | None = None) -> DaemonFile:
    if size is None:
        resolved = len(payload)
    else:
        resolved = size
    return DaemonFile(name, resolved, payload)


def _assert_key(key: str, suffix: str) -> None:
    pattern = "t/10/workitem/20/dispatch/99/objects/sha256/[a-f0-9]{64}/" + re.escape(suffix)
    assert re.fullmatch(pattern, key)


def _hooks(
    sink: Sink,
    relay: bool = False,
    notify: bool = False,
    task: bool = False,
) -> UploadHooks:
    async def require_scheduled() -> None:
        sink.capability += 1
        if sink.fail_capability:
            raise BizError(ErrorCode.SCHEDULED_TASK_SCHEMA_NOT_READY)

    async def resolve_mode(tenant_id: int, agent_id: int) -> str:
        sink.modes.append((tenant_id, agent_id))
        return sink.mode

    async def record_artifact(item: ReportedArtifact) -> int:
        sink.records.append(item)
        sink.next_id += 1
        return sink.next_id

    async def ingest_usage(
        tenant_id: int,
        workitem_id: int,
        dispatch_id: int,
        artifact_id: int,
        path: str,
        oss_ref: str,
        payload: bytes,
    ) -> None:
        sink.usage.append(
            (tenant_id, workitem_id, dispatch_id, artifact_id, path, oss_ref, payload),
        )

    async def record_audit(record: AuditRecord) -> None:
        sink.audits.append(record)

    async def ingest_memory(
        tenant_id: int,
        agent_id: int,
        dispatch_id: int,
        payload: bytes,
    ) -> None:
        sink.memory.append((tenant_id, agent_id, dispatch_id, payload))

    async def ingest_evolution(
        tenant_id: int,
        agent_id: int,
        dispatch_id: int,
        payload: bytes,
        mode: str,
    ) -> None:
        sink.evolution.append((tenant_id, agent_id, dispatch_id, payload, mode))
        if sink.evolution_error is not None:
            raise sink.evolution_error

    notify_scheduled = None
    if notify:

        async def notify_scheduled(tenant_id: int, run_id: int) -> None:
            sink.notifies.append((tenant_id, run_id))

    relay_target = None
    record_relay = None
    if relay:

        async def relay_target(tenant_id: int, dispatch_id: int) -> RelayTarget | None:
            sink.lookup_calls += 1
            if sink.relay_lookup_error is not None:
                raise sink.relay_lookup_error
            return sink.relay_result

        async def record_relay(
            tenant_id: int,
            dispatch_id: int,
            object_key: str,
            run_no: int,
            size_bytes: int,
            metadata: dict[str, Any] | None,
        ) -> None:
            if sink.relay_record_error is not None:
                raise sink.relay_record_error
            sink.relays.append((tenant_id, dispatch_id, object_key, run_no, size_bytes, metadata))

    scheduled_task_id = None
    if task:

        async def scheduled_task_id(tenant_id: int, run_id: int) -> int | None:
            return sink.task_id

    return UploadHooks(
        require_scheduled=require_scheduled,
        resolve_mode=resolve_mode,
        record_artifact=record_artifact,
        ingest_usage=ingest_usage,
        record_audit=record_audit,
        ingest_memory=ingest_memory,
        ingest_evolution=ingest_evolution,
        notify_scheduled=notify_scheduled,
        relay_target=relay_target,
        record_relay=record_relay,
        scheduled_task_id=scheduled_task_id,
    )


async def _upload(
    storage: Any,
    hooks: UploadHooks,
    files: list[DaemonFile],
    metadata: str | None = None,
    auth: UploadAuth | None = None,
    fenced: bool = False,
) -> UploadResult:
    if auth is None:
        resolved = _auth("WORKITEM")
    else:
        resolved = auth
    return await upload_daemon_artifacts(
        99,
        resolved,
        fenced,
        metadata,
        files,
        "test-artifact-bucket",
        storage,
        hooks,
    )
