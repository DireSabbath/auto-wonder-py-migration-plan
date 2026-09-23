"""检查点归档、恢复描述、旧包兼容和 daemon 上传结果。"""

import base64
import gzip
import hashlib
import io
import json
import tarfile
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.artifacts.daemon_auth import authenticate_loaded, mutation_fenced
from autowonder.artifacts.models import Artifact
from autowonder.dispatch.checkpoint import (
    CheckpointEngine,
    CheckpointRecord,
    CheckpointRepo,
    ResumeDispatch,
    StoreDispatch,
    checkpoint_by_seq_statement,
    latest_checkpoints_statement,
    normalize_revision_document,
    obsolete_checkpoints_statement,
    pinned_event_statement,
)
from autowonder.dispatch.daemon_router import checkpoint_http_result
from autowonder.dispatch.legacy_checkpoint import (
    COMPAT_SUFFIX,
    LegacyCheckpointNormalizer,
    sha256_hex,
)
from autowonder.dispatch.models import Dispatch
from autowonder.executors.models import Executor
from autowonder.executors.tokens import validate
from autowonder.main import create_app
from autowonder.storage.objects import InMemoryObjectStorage, StoredObject

PREFIX = "t/10002/workitem/55411/dispatch/13378/"
ACCEPTED = "artifacts/accepted/publish-manifest.json"
ATTEMPT = "artifacts/attempts/400170-hash/attempt-1/publish-manifest.json"
PATH = "evidence/code-review-result.md"
CONTENT = b"verified review result"


class MemoryRepo(CheckpointRepo):
    """测试里的检查点目录。"""

    def __init__(self) -> None:
        self.by_seq: dict[tuple[int, int, int], CheckpointRecord] = {}
        self.rows: list[CheckpointRecord] = []
        self.obsolete: list[CheckpointRecord] = []
        self.deleted: list[tuple[int, int, int]] = []
        self.dispatches: dict[int, ResumeDispatch] = {}
        self.pinned: dict[tuple[int, int], object] = {}
        self.list_override: dict[tuple[int, int], list[CheckpointRecord]] = {}
        self.latest_calls: list[tuple[int, int]] = []

    def find_by_seq(
        self, tenant_id: int, dispatch_id: int, checkpoint_seq: int
    ) -> CheckpointRecord | None:
        return self.by_seq.get((tenant_id, dispatch_id, checkpoint_seq))

    def insert(self, row: CheckpointRecord) -> None:
        self.rows.append(row)
        self.by_seq[(row.tenant_id, row.dispatch_id, row.checkpoint_seq)] = row

    def list_latest(self, tenant_id: int, dispatch_id: int, limit: int) -> list[CheckpointRecord]:
        key = (tenant_id, dispatch_id)
        if key in self.list_override:
            return list(self.list_override[key][:limit])
        matched = [
            row
            for row in self.rows
            if row.tenant_id == tenant_id and row.dispatch_id == dispatch_id
        ]
        matched.sort(key=lambda row: (row.checkpoint_seq, row.id or 0), reverse=True)
        return matched[:limit]

    def find_latest(self, tenant_id: int, dispatch_id: int) -> CheckpointRecord | None:
        self.latest_calls.append((tenant_id, dispatch_id))
        found = [
            row
            for row in self.rows
            if row.tenant_id == tenant_id and row.dispatch_id == dispatch_id
        ]
        found.sort(key=lambda row: (row.checkpoint_seq, row.id or 0), reverse=True)
        if len(found) == 0:
            return None
        return found[0]

    def list_obsolete(
        self, tenant_id: int, dispatch_id: int, retain: int
    ) -> list[CheckpointRecord]:
        return list(self.obsolete)

    def delete_by_id(self, tenant_id: int, dispatch_id: int, row_id: int) -> None:
        self.deleted.append((tenant_id, dispatch_id, row_id))

    def find_dispatch(self, dispatch_id: int) -> ResumeDispatch | None:
        return self.dispatches.get(dispatch_id)

    def find_pinned_detail(self, tenant_id: int, dispatch_id: int) -> object | None:
        return self.pinned.get((tenant_id, dispatch_id))


class ScriptedStorage:
    """可预设存在性和下载地址的内存存储。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.exists_map: dict[str, bool] = {}
        self.presigns: dict[str, str] = {}
        self.gets: list[str] = []

    def put(self, bucket: str, key: str, data: bytes) -> StoredObject:
        ref = bucket + "/" + key
        self.objects[ref] = bytes(data)
        return StoredObject(ref, "md5", len(data))

    def get(self, oss_ref: str) -> bytes | None:
        self.gets.append(oss_ref)
        data = self.objects.get(oss_ref)
        if data is None:
            return None
        return bytes(data)

    def exists(self, oss_ref: str) -> bool:
        if oss_ref in self.exists_map:
            return self.exists_map[oss_ref]
        return oss_ref in self.objects

    def delete(self, oss_ref: str) -> None:
        self.deleted.append(oss_ref)
        self.objects.pop(oss_ref, None)

    def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
        if oss_ref in self.presigns:
            return self.presigns[oss_ref]
        return "mem://" + oss_ref + "?ttl=" + str(ttl_seconds)

    def presign_put(self, bucket: str, key: str, ttl_seconds: int) -> str:
        return "mem-put://" + bucket + "/" + key + "?ttl=" + str(ttl_seconds)


def test_store_prunes_checkpoints_older_than_the_latest_two() -> None:
    """新检查点落下后，更旧的归档、侧车和兼容包一起删掉。"""
    storage = ScriptedStorage()
    repo = MemoryRepo()
    obsolete = CheckpointRecord(id=1, oss_ref="artifact-bucket/old-checkpoint")
    repo.obsolete.append(obsolete)
    engine = CheckpointEngine(storage, "artifact-bucket")  # type: ignore[arg-type]
    stored = engine.store(
        _store_dispatch(), 3, "codex", "session", "runtime", "step", b"\x01", repo
    )
    assert stored.checkpoint_seq == 3
    assert "artifact-bucket/old-checkpoint" in storage.deleted
    assert "artifact-bucket/old-checkpoint" + ".repo-state.json" in storage.deleted
    assert "artifact-bucket/old-checkpoint" + COMPAT_SUFFIX in storage.deleted
    assert repo.deleted == [(100, 55, 1)]


def test_descriptor_modes_and_lineage() -> None:
    """交互模式、降级会话和血缘回退与 Java 描述一致。"""
    engine = CheckpointEngine(ScriptedStorage(), "")  # type: ignore[arg-type]
    side = engine.descriptor(_resume("SIDE_INTERACTION", None), MemoryRepo())
    assert side is not None
    assert side.mode == "SIDE_INTERACTION"
    assert side.session_behavior == "FORK"
    assert side.source_dispatch_id is None
    assert side.provider_session_id is None
    canonical = engine.descriptor(_resume("CANONICAL_INTERACTION", None), MemoryRepo())
    assert canonical is not None
    assert canonical.mode == "SIDE_INTERACTION"
    assert canonical.session_behavior == "CANONICAL"

    empty = MemoryRepo()
    recovery = engine.descriptor(_resume("RECOVERY", 55), empty)
    assert recovery is not None
    assert recovery.mode == "RECOVERY"
    assert recovery.source_dispatch_id == 55
    assert recovery.provider_session_id is None
    assert recovery.checkpoint_download_url is None
    assert (100, 55) in empty.latest_calls

    pinned = MemoryRepo()
    pinned.pinned[(100, 55)] = '{"sessionId":"session-55","provider":"codex"}'
    described = engine.descriptor(_resume("RETURNING_WORKER", 55), pinned)
    assert described is not None
    assert described.provider == "codex"
    assert described.provider_session_id == "session-55"
    assert described.checkpoint_download_url is None
    assert engine.has_resumable_session(100, 55, pinned) is True


def test_degraded_continuous_hides_native_session_and_lists_candidates() -> None:
    """降级连续运行可以下载检查点，但不下发提供者会话。"""
    storage = ScriptedStorage()
    storage.exists_map["bucket/checkpoint"] = True
    storage.presigns["bucket/checkpoint"] = "https://oss/checkpoint"
    repo = MemoryRepo()
    checkpoint = _checkpoint(1, "bucket/checkpoint", "abc")
    checkpoint.provider = "codex"
    checkpoint.provider_session_id = "native-session"
    repo.list_override[(100, 55)] = [checkpoint]
    engine = CheckpointEngine(storage, "")  # type: ignore[arg-type]
    described = engine.descriptor(_resume("DEGRADED_CONTINUOUS", 55), repo)
    assert described is not None
    assert described.provider_session_id is None
    assert described.checkpoint_download_url == "https://oss/checkpoint"

    both = ScriptedStorage()
    both.exists_map["bucket/latest"] = True
    both.exists_map["bucket/previous"] = True
    both.presigns["bucket/latest"] = "https://oss/latest"
    both.presigns["bucket/previous"] = "https://oss/previous"
    listed = MemoryRepo()
    latest = _checkpoint(2, "bucket/latest", "latest-sha")
    previous = _checkpoint(1, "bucket/previous", "previous-sha")
    listed.list_override[(100, 55)] = [latest, previous]
    described = CheckpointEngine(both, "").descriptor(_resume("RECOVERY", 55), listed)  # type: ignore[arg-type]
    assert described is not None
    assert described.checkpoint_download_url == "https://oss/latest"
    assert len(described.checkpoint_candidates) == 2
    assert described.checkpoint_candidates[0].checkpoint_seq == 2
    assert described.checkpoint_candidates[1].download_url == "https://oss/previous"


def test_recovery_walks_resume_lineage_past_a_missing_checkpoint() -> None:
    """直接来源没有可读检查点时，继续沿恢复来源往上找。"""
    storage = ScriptedStorage()
    storage.exists_map["old-bucket/missing"] = False
    storage.exists_map["new-bucket/ancestor"] = True
    storage.presigns["new-bucket/ancestor"] = "https://oss/ancestor"
    repo = MemoryRepo()
    repo.dispatches[55] = ResumeDispatch(55, 100, None, 44)
    repo.list_override[(100, 55)] = [_checkpoint(8, "old-bucket/missing", "missing-sha")]
    repo.list_override[(100, 44)] = [_checkpoint(7, "new-bucket/ancestor", "ancestor-sha")]
    described = CheckpointEngine(storage, "").descriptor(_resume("RECOVERY", 55), repo)  # type: ignore[arg-type]
    assert described is not None
    assert described.source_dispatch_id == 44
    assert described.checkpoint_download_url == "https://oss/ancestor"
    assert len(described.checkpoint_candidates) == 1
    assert described.checkpoint_candidates[0].checkpoint_seq == 7

    lineage = MemoryRepo()
    lineage.dispatches[55] = ResumeDispatch(55, 100, None, 44)
    lineage.pinned[(100, 44)] = '{"sessionId":"session-44","provider":"codex"}'
    engine = CheckpointEngine(ScriptedStorage(), "")  # type: ignore[arg-type]
    assert engine.has_resumable_session(100, 55, lineage) is True


def test_durable_receipt_matches_seq_and_hash_only() -> None:
    """收据只认指定序号上的摘要，不看最新一条。"""
    repo = MemoryRepo()
    repo.by_seq[(100, 55, 7)] = _checkpoint(7, "bucket/checkpoint", "abc")
    engine = CheckpointEngine(ScriptedStorage(), "")  # type: ignore[arg-type]
    assert engine.matches_durable_receipt(100, 55, 7, "sha256:abc", repo) is True
    assert engine.matches_durable_receipt(100, 55, 6, "sha256:abc", repo) is False
    assert engine.matches_durable_receipt(100, 55, 7, "sha256:wrong", repo) is False
    assert repo.latest_calls == []


def test_repo_revision_uses_base_commit_and_repairs_legacy_sidecar() -> None:
    """可检出的基线是 baseCommit。旧侧车里的包内 HEAD 会被改掉。"""
    storage = InMemoryObjectStorage()
    repo = MemoryRepo()
    engine = CheckpointEngine(storage, "artifact-bucket")
    archive = _checkpoint_archive(
        "{"
        '"schemaVersion":"autowonder.runtimeCheckpoint.v1",'
        '"repos":[{"name":"auto-wonder",'
        '"baseCommit":"1111111111111111111111111111111111111111",'
        '"headCommit":"2222222222222222222222222222222222222222",'
        '"branch":"aw/可靠恢复"}]}'
    )
    stored = engine.store(
        _store_dispatch(), 7, "codex", "session", "runtime", "step", archive, repo
    )
    repo.list_override[(100, 55)] = [stored]
    revision = engine.find_repo_revision(100, 55, repo)
    assert revision is not None
    assert revision.name.endswith("deliverables/runtime-source-revision.json")
    text = storage.get(revision.oss_ref)
    assert text is not None
    rendered = text.decode("utf-8")
    assert '"branch":"aw/可靠恢复"' in rendered
    assert "1111111111111111111111111111111111111111" in rendered
    assert '"baseCommit"' not in rendered
    assert "2222222222222222222222222222222222222222" not in rendered

    previous = stored
    unavailable = _checkpoint(8, "artifact-bucket/missing", "missing-sha")
    listed = MemoryRepo()
    listed.list_override[(100, 55)] = [unavailable, previous]
    fallback = CheckpointEngine(storage, "artifact-bucket")
    # 上一次 store 已经把侧车写进同一个 InMemory 存储。
    found = fallback.find_repo_revision(100, 55, listed)
    assert found is not None
    body = storage.get(found.oss_ref)
    assert body is not None
    assert '"branch":"aw/可靠恢复"' in body.decode("utf-8")


def test_missing_sidecar_reads_archive_without_getting_the_sidecar() -> None:
    """侧车不存在时直接读归档，不把不存在的对象再 get 一次。"""
    storage = ScriptedStorage()
    checkpoint_ref = "artifact-bucket/t/100/checkpoint-7.tar.gz"
    sidecar_ref = checkpoint_ref + ".repo-state.json"
    archive = _checkpoint_archive(
        "{"
        '"schemaVersion":"autowonder.runtimeCheckpoint.v1","repos":[{'
        '"name":"auto-wonder",'
        '"baseCommit":"1111111111111111111111111111111111111111",'
        '"headCommit":"2222222222222222222222222222222222222222"}]}'
    )
    storage.objects[checkpoint_ref] = archive
    storage.exists_map[sidecar_ref] = False
    stored = _checkpoint(7, checkpoint_ref, sha256_hex(archive))
    repo = MemoryRepo()
    repo.list_override[(100, 55)] = [stored]
    revision = CheckpointEngine(storage, "").find_repo_revision(100, 55, repo)  # type: ignore[arg-type]
    assert revision is not None
    assert revision.oss_ref == sidecar_ref
    assert sidecar_ref not in storage.gets
    assert checkpoint_ref in storage.gets


def test_legacy_sidecar_exposing_local_head_is_repaired() -> None:
    """旧侧车把包内 HEAD 写成了 headCommit 时，改回基线提交。"""
    storage = InMemoryObjectStorage()
    checkpoint_ref = "artifact-bucket/t/100/checkpoint-7.tar.gz"
    legacy = (
        '{"schemaVersion":"autowonder.checkpointSourceRevision.v1","repositories":[{'
        '"name":"auto-wonder",'
        '"baseCommit":"1111111111111111111111111111111111111111",'
        '"headCommit":"2222222222222222222222222222222222222222",'
        '"branch":"fix/local-only"}]}'
    )
    storage.put(
        "artifact-bucket",
        "t/100/checkpoint-7.tar.gz.repo-state.json",
        legacy.encode("utf-8"),
    )
    repo = MemoryRepo()
    repo.list_override[(100, 55)] = [_checkpoint(7, checkpoint_ref, "unused")]
    revision = CheckpointEngine(storage, "").find_repo_revision(100, 55, repo)
    assert revision is not None
    repaired = storage.get(revision.oss_ref)
    assert repaired is not None
    text = repaired.decode("utf-8")
    assert "1111111111111111111111111111111111111111" in text
    assert "2222222222222222222222222222222222222222" not in text
    assert normalize_revision_document(legacy.encode("utf-8"), "repositories") == repaired


def test_legacy_normalizer_rewrites_directory_receipts() -> None:
    """目录前缀改成已核对的对象地址，原包和第二次投影保持稳定。"""
    storage = InMemoryObjectStorage()
    source, artifact = _legacy_source(storage, PREFIX)
    normalizer = LegacyCheckpointNormalizer(storage, lambda tenant_id, dispatch_id: [artifact])
    original = storage.get(source.oss_ref or "")
    projected = normalizer.normalize(source)
    assert projected.oss_ref != source.oss_ref
    assert storage.get(source.oss_ref or "") == original
    compatible = storage.get(projected.oss_ref or "")
    assert compatible is not None
    assert sha256_hex(compatible) == projected.sha256
    before, _modes = _unpack(original or b"")
    after, _after_modes = _unpack(compatible)
    assert set(before) == set(after)
    for name in (ACCEPTED, ATTEMPT):
        entry = json.loads(after[name].decode("utf-8"))["entries"][0]
        assert entry["remoteRef"] == artifact.oss_ref
        assert entry["disposition"] == "reference_only"
    for name, payload in before.items():
        if name not in {ACCEPTED, ATTEMPT}:
            assert after[name] == payload
    again = normalizer.normalize(source)
    assert again.sha256 == projected.sha256
    assert again.oss_ref == projected.oss_ref


def test_legacy_normalizer_keeps_original_when_unverifiable() -> None:
    """引用已经精确、产物对不上或兼容包冲突时，不改原检查点。"""
    storage = InMemoryObjectStorage()
    exact, _artifact = _legacy_source(storage, "bucket/" + PREFIX + "artifacts/output/" + PATH)
    calls: list[int] = []

    def list_rows(tenant_id: int, dispatch_id: int) -> list[Artifact]:
        calls.append(1)
        return []

    assert LegacyCheckpointNormalizer(storage, list_rows).normalize(exact) is exact
    assert calls == []

    source, artifact = _legacy_source(storage, PREFIX)
    normalizer = LegacyCheckpointNormalizer(storage, lambda tenant_id, dispatch_id: [artifact])
    storage.delete(artifact.oss_ref)
    assert normalizer.normalize(source) is source


def test_upload_result_audits_success_and_hides_storage_failure() -> None:
    """成功上传带代理审计。存储失败是 503，并且不产生审计。"""
    auth = _auth()
    stored = CheckpointRecord(checkpoint_seq=2, sha256="abc", size_bytes=3)
    status, body, audit = checkpoint_http_result(
        auth, 500, 2, b"abc", "codex", "session-1", "rt-1", "400165", stored, False
    )
    assert status == 200
    assert body == {"checkpointSeq": 2, "sha256": "sha256:abc", "sizeBytes": 3}
    assert audit is not None
    assert audit.tenant_id == 100
    assert audit.actor_id == 300
    assert audit.actor_type == "AGENT"
    assert audit.module == "DISPATCH"
    assert audit.action == "UPLOAD_CHECKPOINT"
    assert audit.target_type == "dispatch"
    assert audit.target_id == 500
    assert audit.trigger_type == "EVENT"
    assert audit.trigger_source == "DAEMON_CALLBACK"
    assert audit.event_type == "daemon.checkpoint"
    failed_status, failed_body, failed_audit = checkpoint_http_result(
        auth, 500, 2, b"abc", "codex", "session-1", "rt-1", "400165", None, True
    )
    assert failed_status == 503
    assert failed_body == {"error": "checkpoint upload temporarily unavailable"}
    assert failed_audit is None
    denied, empty, no_audit = checkpoint_http_result(
        _auth(False), 500, 2, b"abc", None, None, None, None, None, False
    )
    assert denied == 401
    assert empty is None
    assert no_audit is None


def test_executor_token_and_upload_auth() -> None:
    """b64 与 sha256 引用都能验令牌。调度或执行器缺失则上传失败。"""
    plaintext = "exec_1_abc"
    reference = "b64:" + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    assert validate(reference, plaintext) is True
    assert validate(reference, "other") is False
    digest = hashlib.sha256(("autowonder-executor" + plaintext).encode("utf-8")).hexdigest()
    assert validate("sha256:" + digest, plaintext) is True
    dispatch = Dispatch(
        id=1,
        tenant_id=100,
        workitem_id=200,
        agent_id=300,
        executor_id=900,
        idempotency_key="k",
        resume_mode="SIDE_INTERACTION",
        status="RUNNING",
    )
    executor = Executor(
        id=900,
        tenant_id=100,
        agent_id=300,
        name="executor",
        token_ref=reference,
    )
    auth = authenticate_loaded(dispatch, executor, plaintext)
    assert auth.success is True
    assert auth.tenant_id == 100
    assert auth.workitem_id == 200
    assert auth.agent_id == 300
    assert auth.resume_mode == "SIDE_INTERACTION"
    assert auth.interaction() is True
    assert authenticate_loaded(None, executor, plaintext).success is False
    assert authenticate_loaded(dispatch, None, plaintext).success is False
    assert authenticate_loaded(dispatch, executor, "wrong").success is False
    dispatch.status = "CANCELED"
    assert mutation_fenced(dispatch, False, False) is True


def test_checkpoint_routes_and_sql() -> None:
    """检查点上传已注册。查询按序号倒序，并带租户和调度。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "post" in paths["/api/daemon/dispatches/{dispatchId}/checkpoint"]
    seq_sql = _sql(checkpoint_by_seq_statement(100, 55, 7))
    latest_sql = _sql(latest_checkpoints_statement(100, 55, 2))
    obsolete_sql = _sql(obsolete_checkpoints_statement(100, 55, 2))
    pinned_sql = _sql(pinned_event_statement(100, 55))
    assert "dispatch_recovery_checkpoint.tenant_id = 100" in seq_sql
    assert "dispatch_recovery_checkpoint.dispatch_id = 55" in seq_sql
    assert "dispatch_recovery_checkpoint.checkpoint_seq = 7" in seq_sql
    assert "checkpoint_seq DESC" in latest_sql
    assert "LIMIT 2, 18446744073709551615" in obsolete_sql
    assert "agent.session_pinned" in pinned_sql


def _store_dispatch() -> StoreDispatch:
    return StoreDispatch(id=55, tenant_id=100, workitem_id=200, agent_id=300, executor_id=9)


def _resume(mode: str, source_id: int | None) -> ResumeDispatch:
    return ResumeDispatch(
        id=70,
        tenant_id=100,
        resume_mode=mode,
        resume_from_dispatch_id=source_id,
    )


def _checkpoint(seq: int, oss_ref: str, digest: str) -> CheckpointRecord:
    return CheckpointRecord(checkpoint_seq=seq, oss_ref=oss_ref, sha256=digest)


def _auth(success: bool = True) -> Any:
    from autowonder.artifacts.daemon_auth import UploadAuth

    if not success:
        return UploadAuth(False, 0, 0, 0, None, "WORKITEM")
    return UploadAuth(True, 100, 200, 300, None, "WORKITEM")


def _checkpoint_archive(checkpoint_json: str) -> bytes:
    content = checkpoint_json.encode("utf-8")
    header = bytearray(512)
    name = b"checkpoint.json"
    header[: len(name)] = name
    size = f"{len(content):011o}".encode("ascii")
    header[124 : 124 + len(size)] = size
    header[156] = ord("0")
    tar = bytes(header) + content + bytes((512 - len(content) % 512) % 512 + 1024)
    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=gzip_buffer, mode="wb", mtime=0) as compressed:
        compressed.write(tar)
    return gzip_buffer.getvalue()


def _legacy_source(
    storage: InMemoryObjectStorage, remote_ref: str
) -> tuple[CheckpointRecord, Artifact]:
    entry = {
        "path": PATH,
        "sha256": "sha256:" + sha256_hex(CONTENT),
        "sizeBytes": len(CONTENT),
        "disposition": "reference_only",
        "remoteRef": remote_ref,
    }
    manifest = {"schemaVersion": "autowonder.publishManifest.v1", "entries": [entry]}
    rendered = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    files = {
        "checkpoint.json": (
            b'{"schemaVersion":"autowonder.runtimeCheckpoint.v2","dispatchId":"13378",'
            b'"checkpointSeq":123,"stateSha256":"producer-fingerprint"}'
        ),
        ACCEPTED: rendered,
        ATTEMPT: rendered,
        "publication-payload-map.json": (
            b'{"schemaVersion":"autowonder.publicationPayloadMap.v1","entries":null}'
        ),
        "state/artifact-receipts.json": (
            b'{"identity":{"tenantId":"10002","workitemId":"55411","dispatchId":"13378","attempt":1}}'
        ),
        "state/sdlc-progress.json": b'{"completedStepIds":["400169"]}',
    }
    archive = _pack(files)
    stored = storage.put("bucket", PREFIX + "checkpoint.tar.gz", archive)
    source = CheckpointRecord(
        id=51256,
        tenant_id=10002,
        workitem_id=55411,
        dispatch_id=13378,
        agent_id=40014,
        checkpoint_seq=123,
        sha256=sha256_hex(archive),
        size_bytes=len(archive),
        oss_ref=stored.oss_ref,
    )
    artifact_ref = storage.put("bucket", PREFIX + "artifacts/output/" + PATH, CONTENT)
    artifact = Artifact(
        tenant_id=10002,
        workitem_id=55411,
        dispatch_id=13378,
        name="artifacts/output/" + PATH,
        type="FILE",
        source_type="WORKITEM",
        oss_ref=artifact_ref.oss_ref,
        size=len(CONTENT),
    )
    return source, artifact


def _pack(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    compressed = gzip.GzipFile(fileobj=out, mode="wb", mtime=0)
    with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            if name.endswith("run.sh"):
                info.mode = 0o755
            archive.addfile(info, io.BytesIO(payload))
    compressed.close()
    return out.getvalue()


def _unpack(data: bytes) -> tuple[dict[str, bytes], dict[str, int]]:
    members: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as compressed:
        with tarfile.open(fileobj=compressed, mode="r|") as archive:
            for entry in archive:
                extracted = archive.extractfile(entry)
                payload = b"" if extracted is None else extracted.read()
                members[entry.name] = payload
                modes[entry.name] = entry.mode
    return members, modes


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)
