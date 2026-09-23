"""保存可恢复检查点，并按血缘挑出恢复描述和仓库基线。"""

import gzip
import io
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TypeGuard

from sqlalchemy import Select, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from autowonder.dispatch.legacy_checkpoint import (
    COMPAT_SUFFIX,
    CheckpointRecord,
    LegacyCheckpointNormalizer,
    sha256_hex,
)
from autowonder.dispatch.models import Dispatch, DispatchRecoveryCheckpoint, DispatchRuntimeEvent
from autowonder.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)

DOWNLOAD_TTL_SECONDS = 600
RETAIN_CHECKPOINTS = 2
MAX_RESUME_LINEAGE_DEPTH = 16
MAX_CHECKPOINT_SCAN_BYTES = 256 * 1024 * 1024
MAX_CHECKPOINT_METADATA_BYTES = 1024 * 1024
CHECKPOINT_SCHEMA = "autowonder.runtimeCheckpoint.v1"
REVISION_SCHEMA = "autowonder.checkpointSourceRevision.v1"
REPO_STATE_SUFFIX = ".repo-state.json"
SESSION_PINNED_EVENT = "agent.session_pinned"
_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_FORBIDDEN_BRANCH = set("~^:?*[\\")
_CANDIDATE_MODES = frozenset(
    {
        "RECOVERY",
        "CONTINUOUS",
        "DEGRADED_CONTINUOUS",
        "COMMENT_INTERACTION",
        "SIDE_INTERACTION",
        "CANONICAL_INTERACTION",
        "COMMENT_REWORK",
    }
)
_SOURCELESS_MODES = frozenset({"SIDE_INTERACTION", "CANONICAL_INTERACTION", "COMMENT_REWORK"})


class DuplicateCheckpoint(Exception):
    """同一调度和序号已经有一条检查点。"""


@dataclass
class StoreDispatch:
    """写入检查点时需要的调度字段。"""

    id: int
    tenant_id: int
    workitem_id: int
    agent_id: int
    executor_id: int | None = None


@dataclass
class ResumeDispatch:
    """计算恢复描述时需要的调度字段。"""

    id: int | None
    tenant_id: int
    resume_mode: str | None
    resume_from_dispatch_id: int | None


@dataclass
class ResumeCandidate:
    """一个可下载的检查点候选。"""

    download_url: str
    sha256: str
    checkpoint_seq: int


@dataclass
class ResumeDescriptor:
    """下发给运行时的恢复说明。规范交互在线路上写成旁路交互。"""

    mode: str | None
    session_behavior: str | None
    source_dispatch_id: int | None
    provider: str | None
    provider_session_id: str | None
    checkpoint_download_url: str | None
    checkpoint_sha256: str | None
    checkpoint_seq: int | None
    checkpoint_candidates: list[ResumeCandidate] = field(default_factory=list)


@dataclass
class RevisionArtifact:
    """打包时可以检出的仓库基线。"""

    name: str
    oss_ref: str


@dataclass
class _ProviderSession:
    provider: str | None
    session_id: str


@dataclass
class _ResumeSource:
    dispatch_id: int
    checkpoints: list[CheckpointRecord]
    provider_session: _ProviderSession | None


class CheckpointRepo:
    """检查点查询。测试用内存实现，请求里用同步会话实现。"""

    def find_by_seq(
        self, tenant_id: int, dispatch_id: int, checkpoint_seq: int
    ) -> CheckpointRecord | None:
        """按序号读取一条检查点。"""
        raise NotImplementedError

    def insert(self, row: CheckpointRecord) -> None:
        """插入检查点。序号冲突时抛出 ``DuplicateCheckpoint``。"""
        raise NotImplementedError

    def list_latest(self, tenant_id: int, dispatch_id: int, limit: int) -> list[CheckpointRecord]:
        """按序号从新到旧取有限条。"""
        raise NotImplementedError

    def find_latest(self, tenant_id: int, dispatch_id: int) -> CheckpointRecord | None:
        """最新一条检查点。"""
        raise NotImplementedError

    def list_obsolete(
        self, tenant_id: int, dispatch_id: int, retain: int
    ) -> list[CheckpointRecord]:
        """跳过需要保留的最新检查点，返回更旧的行。"""
        raise NotImplementedError

    def delete_by_id(self, tenant_id: int, dispatch_id: int, row_id: int) -> None:
        """删除一条检查点行。"""
        raise NotImplementedError

    def find_dispatch(self, dispatch_id: int) -> ResumeDispatch | None:
        """读取恢复血缘用的调度。"""
        raise NotImplementedError

    def find_pinned_detail(self, tenant_id: int, dispatch_id: int) -> object | None:
        """最近一条会话钉住事件的详情。"""
        raise NotImplementedError


class CheckpointEngine:
    """检查点归档、裁剪和恢复投影。"""

    def __init__(
        self,
        storage: ObjectStorage,
        bucket: str,
        normalizer: LegacyCheckpointNormalizer | None = None,
    ) -> None:
        self.storage = storage
        self.bucket = bucket
        self.normalizer = normalizer

    def store(
        self,
        dispatch: StoreDispatch,
        checkpoint_seq: int,
        provider: str | None,
        provider_session_id: str | None,
        runtime_id: str | None,
        active_step_id: str | None,
        archive: bytes,
        repo: CheckpointRepo,
    ) -> CheckpointRecord:
        """写入归档。同一序号已存在时沿用那一条，并尽量只留最新两份。"""
        existing = repo.find_by_seq(dispatch.tenant_id, dispatch.id, checkpoint_seq)
        if existing is not None:
            self._ensure_sidecar(existing, archive)
            self._prune(dispatch, repo)
            return existing
        digest = sha256_hex(archive)
        key = (
            "t/"
            + str(dispatch.tenant_id)
            + "/workitem/"
            + str(dispatch.workitem_id)
            + "/recovery/dispatch/"
            + str(dispatch.id)
            + "/checkpoint-"
            + str(checkpoint_seq)
            + ".tar.gz"
        )
        stored = self.storage.put(self.bucket, key, archive)
        checkpoint = CheckpointRecord(
            tenant_id=dispatch.tenant_id,
            workitem_id=dispatch.workitem_id,
            dispatch_id=dispatch.id,
            agent_id=dispatch.agent_id,
            checkpoint_seq=checkpoint_seq,
            provider=clip(provider, 32),
            provider_session_id=clip(provider_session_id, 256),
            runtime_id=clip(runtime_id, 128),
            executor_id=dispatch.executor_id,
            active_step_id=clip(active_step_id, 128),
            oss_ref=stored.oss_ref,
            sha256=digest,
            size_bytes=stored.size,
        )
        self._ensure_sidecar(checkpoint, archive)
        try:
            repo.insert(checkpoint)
        except DuplicateCheckpoint:
            winner = repo.find_by_seq(dispatch.tenant_id, dispatch.id, checkpoint_seq)
            if winner is not None:
                self._prune(dispatch, repo)
                return winner
            raise
        self._prune(dispatch, repo)
        return checkpoint

    def latest(
        self, tenant_id: int, dispatch_id: int, repo: CheckpointRepo
    ) -> CheckpointRecord | None:
        """该调度最新的一条检查点。"""
        return repo.find_latest(tenant_id, dispatch_id)

    def matches_durable_receipt(
        self,
        tenant_id: int,
        dispatch_id: int,
        checkpoint_seq: int,
        checkpoint_sha256: str | None,
        repo: CheckpointRepo,
    ) -> bool:
        """序号和摘要同时对上已落库的检查点才算收据成立。"""
        if checkpoint_seq <= 0 or checkpoint_sha256 is None or _blank(checkpoint_sha256):
            return False
        stored = repo.find_by_seq(tenant_id, dispatch_id, checkpoint_seq)
        if stored is None or stored.checkpoint_seq != checkpoint_seq or stored.sha256 is None:
            return False
        normalized = _java_trim(checkpoint_sha256)
        if normalized[:7].lower() == "sha256:":
            normalized = normalized[7:]
        return stored.sha256.lower() == normalized.lower()

    def has_resumable_session(
        self, tenant_id: int, dispatch_id: int, repo: CheckpointRepo
    ) -> bool:
        """血缘上还能找到提供者会话。"""
        source = self._resolve(tenant_id, dispatch_id, True, repo)
        return source.provider_session is not None

    def descriptor(
        self, dispatch: ResumeDispatch, repo: CheckpointRepo
    ) -> ResumeDescriptor | None:
        """按恢复模式组装线路上的检查点和会话。没有来源且不是交互时返回空。"""
        if dispatch.resume_from_dispatch_id is None:
            if dispatch.resume_mode in _SOURCELESS_MODES:
                return _wire(dispatch, None, None, None, None, None, None, [])
            return None
        source = self._resolve(dispatch.tenant_id, dispatch.resume_from_dispatch_id, False, repo)
        checkpoint = None
        if len(source.checkpoints) > 0:
            checkpoint = source.checkpoints[0]
        provider_session = source.provider_session
        if dispatch.resume_mode == "DEGRADED_CONTINUOUS":
            provider_session = None
        if checkpoint is None:
            provider = None
            session_id = None
            if provider_session is not None:
                provider = provider_session.provider
                session_id = provider_session.session_id
            return _wire(dispatch, source.dispatch_id, provider, session_id, None, None, None, [])
        candidates: list[ResumeCandidate] = []
        if dispatch.resume_mode in _CANDIDATE_MODES:
            for item in source.checkpoints:
                projected = item
                if self.normalizer is not None:
                    projected = self.normalizer.normalize(item)
                digest = projected.sha256
                if digest is None:
                    digest = ""
                url = ""
                if projected.oss_ref is not None:
                    url = self.storage.presign_get(projected.oss_ref, DOWNLOAD_TTL_SECONDS)
                candidates.append(
                    ResumeCandidate(url, "sha256:" + digest, projected.checkpoint_seq)
                )
        download_url = None
        checksum = "sha256:" + (checkpoint.sha256 or "")
        if len(candidates) > 0:
            download_url = candidates[0].download_url
            checksum = candidates[0].sha256
        provider = checkpoint.provider
        session_id = checkpoint.provider_session_id
        if provider_session is not None:
            provider = provider_session.provider
            session_id = provider_session.session_id
        if dispatch.resume_mode == "DEGRADED_CONTINUOUS":
            session_id = None
        logger.info(
            "dispatch resume checkpoint urls dispatchId=%s sourceDispatchId=%s "
            "checkpointSeq=%s candidateCount=%s",
            dispatch.id,
            source.dispatch_id,
            checkpoint.checkpoint_seq,
            len(candidates),
        )
        return _wire(
            dispatch,
            source.dispatch_id,
            provider,
            session_id,
            download_url,
            checksum,
            checkpoint.checkpoint_seq,
            candidates,
        )

    def find_repo_revision(
        self, tenant_id: int, dispatch_id: int, repo: CheckpointRepo
    ) -> RevisionArtifact | None:
        """从最新仍可读的检查点抽出可检出的提交，不把包内 HEAD 当成基线。"""
        checkpoints = repo.list_latest(tenant_id, dispatch_id, RETAIN_CHECKPOINTS)
        if len(checkpoints) == 0:
            latest = repo.find_latest(tenant_id, dispatch_id)
            if latest is None:
                checkpoints = []
            else:
                checkpoints = [latest]
        for checkpoint in checkpoints:
            if checkpoint.oss_ref is None:
                continue
            try:
                artifact = self._revision_from_checkpoint(checkpoint)
            except Exception:
                logger.warning(
                    "checkpoint repo revision ignored dispatchId=%s checkpointSeq=%s",
                    dispatch_id,
                    checkpoint.checkpoint_seq,
                )
                continue
            if artifact is not None:
                return artifact
        return None

    def _revision_from_checkpoint(self, checkpoint: CheckpointRecord) -> RevisionArtifact | None:
        if checkpoint.oss_ref is None:
            return None
        sidecar_ref = checkpoint.oss_ref + REPO_STATE_SUFFIX
        sidecar = None
        if self.storage.exists(sidecar_ref):
            sidecar = self.storage.get(sidecar_ref)
        normalized = normalize_revision_document(sidecar, "repositories")
        if normalized is None:
            archive = self.storage.get(checkpoint.oss_ref)
            digest = None
            if archive is not None:
                digest = sha256_hex(archive)
            if archive is None or checkpoint.sha256 is None or digest is None:
                return None
            if checkpoint.sha256.lower() != digest.lower():
                return None
            normalized = normalize_checkpoint_revision(archive)
            if normalized is None:
                return None
            sidecar_ref = self._put_sidecar(checkpoint.oss_ref, normalized)
        elif sidecar != normalized:
            sidecar_ref = self._put_sidecar(checkpoint.oss_ref, normalized)
        name = (
            "checkpoint/"
            + str(checkpoint.checkpoint_seq)
            + "/deliverables/runtime-source-revision.json"
        )
        return RevisionArtifact(name, sidecar_ref)

    def _ensure_sidecar(self, checkpoint: CheckpointRecord, archive: bytes) -> None:
        try:
            normalized = normalize_checkpoint_revision(archive)
            if normalized is not None and checkpoint.oss_ref is not None:
                self._put_sidecar(checkpoint.oss_ref, normalized)
        except Exception:
            logger.warning(
                "checkpoint repo metadata extraction failed dispatchId=%s checkpointSeq=%s",
                checkpoint.dispatch_id,
                checkpoint.checkpoint_seq,
            )

    def _put_sidecar(self, checkpoint_ref: str, normalized: bytes) -> str:
        separator = checkpoint_ref.find("/")
        if separator <= 0 or separator == len(checkpoint_ref) - 1:
            raise ValueError("invalid checkpoint ossRef")
        stored = self.storage.put(
            checkpoint_ref[:separator],
            checkpoint_ref[separator + 1 :] + REPO_STATE_SUFFIX,
            normalized,
        )
        return stored.oss_ref

    def _prune(self, dispatch: StoreDispatch, repo: CheckpointRepo) -> None:
        try:
            obsolete = repo.list_obsolete(dispatch.tenant_id, dispatch.id, RETAIN_CHECKPOINTS)
            for row in obsolete:
                if row.oss_ref is not None:
                    self.storage.delete(row.oss_ref)
                    self.storage.delete(row.oss_ref + REPO_STATE_SUFFIX)
                    self.storage.delete(row.oss_ref + COMPAT_SUFFIX)
                if row.id is not None:
                    repo.delete_by_id(dispatch.tenant_id, dispatch.id, row.id)
        except Exception:
            logger.warning(
                "checkpoint retention cleanup failed tenantId=%s dispatchId=%s",
                dispatch.tenant_id,
                dispatch.id,
            )

    def _resolve(
        self,
        tenant_id: int,
        initial_dispatch_id: int,
        require_provider_session: bool,
        repo: CheckpointRepo,
    ) -> _ResumeSource:
        current = initial_dispatch_id
        visited: list[int] = []
        fallback: _ResumeSource | None = None
        while len(visited) < MAX_RESUME_LINEAGE_DEPTH and current not in visited:
            visited.append(current)
            recorded = repo.list_latest(tenant_id, current, RETAIN_CHECKPOINTS)
            if len(recorded) == 0:
                latest = self.latest(tenant_id, current, repo)
                if latest is None:
                    recorded = []
                else:
                    recorded = [latest]
            recorded_checkpoint = None
            if len(recorded) > 0:
                recorded_checkpoint = recorded[0]
            available = [item for item in recorded if self._available(item)]
            provider_session = self._provider_session(
                tenant_id, current, recorded_checkpoint, repo
            )
            candidate = _ResumeSource(current, available, provider_session)
            if fallback is None:
                fallback = candidate
            if provider_session is not None or (
                not require_provider_session and len(available) > 0
            ):
                return candidate
            current_dispatch = repo.find_dispatch(current)
            if current_dispatch is None or current_dispatch.tenant_id != tenant_id:
                break
            if current_dispatch.resume_from_dispatch_id is None:
                break
            current = current_dispatch.resume_from_dispatch_id
        if fallback is not None:
            return fallback
        return _ResumeSource(initial_dispatch_id, [], None)

    def _available(self, checkpoint: CheckpointRecord) -> bool:
        try:
            if checkpoint.oss_ref is None:
                return False
            return self.storage.exists(checkpoint.oss_ref)
        except Exception:
            logger.warning(
                "checkpoint availability check failed dispatchId=%s checkpointSeq=%s",
                checkpoint.dispatch_id,
                checkpoint.checkpoint_seq,
            )
            return True

    def _provider_session(
        self,
        tenant_id: int,
        dispatch_id: int,
        checkpoint: CheckpointRecord | None,
        repo: CheckpointRepo,
    ) -> _ProviderSession | None:
        if checkpoint is not None and checkpoint.provider_session_id is not None:
            if not _blank(checkpoint.provider_session_id):
                return _ProviderSession(checkpoint.provider, checkpoint.provider_session_id)
        detail = repo.find_pinned_detail(tenant_id, dispatch_id)
        parsed = _detail_object(detail)
        if parsed is None:
            return None
        session_id = parsed.get("sessionId")
        if not isinstance(session_id, str) or _blank(session_id):
            return None
        provider = parsed.get("provider")
        provider_text = provider if isinstance(provider, str) else None
        clipped = clip(session_id, 256)
        if clipped is None:
            return None
        return _ProviderSession(clip(provider_text, 32), clipped)


def normalize_checkpoint_revision(archive: bytes | None) -> bytes | None:
    """从 gzip tar 里取出 ``checkpoint.json`` 并收成仓库基线。"""
    return normalize_revision_document(extract_checkpoint_json(archive), "repos")


def normalize_revision_document(raw: bytes | None, repositories_field: str) -> bytes | None:
    """只保留可检出的提交。包内 HEAD 仅在没有基线提交时才使用。"""
    if raw is None or len(raw) == 0 or len(raw) > MAX_CHECKPOINT_METADATA_BYTES:
        return None
    source = json.loads(raw.decode("utf-8"))
    if not isinstance(source, dict):
        raise ValueError("checkpoint revision is not an object")
    if repositories_field == "repos" and source.get("schemaVersion") != CHECKPOINT_SCHEMA:
        return None
    repositories = source.get(repositories_field)
    if not isinstance(repositories, list) or len(repositories) == 0 or len(repositories) > 100:
        return None
    normalized_repositories: list[dict[str, str]] = []
    for repo in repositories:
        if not isinstance(repo, dict):
            continue
        name = clip(_text(repo.get("name")), 255)
        checkout = _text(repo.get("baseCommit"))
        if not _valid_commit(checkout):
            checkout = _text(repo.get("headCommit"))
        if name is None or _blank(name) or not _valid_commit(checkout):
            continue
        item = {"name": name, "headCommit": checkout}
        branch = _text(repo.get("branch"))
        if _valid_branch(branch):
            item["branch"] = branch
        normalized_repositories.append(item)
    if len(normalized_repositories) == 0:
        return None
    document = {
        "schemaVersion": REVISION_SCHEMA,
        "repositories": normalized_repositories,
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def extract_checkpoint_json(archive: bytes | None) -> bytes | None:
    """按 ustar 头找到 ``checkpoint.json``。损坏的包返回空。"""
    if archive is None or len(archive) == 0:
        return None
    try:
        return _extract_checkpoint_json(archive)
    except Exception:
        return None


def checkpoint_by_seq_statement(
    tenant_id: int, dispatch_id: int, checkpoint_seq: int
) -> Select[tuple[DispatchRecoveryCheckpoint]]:
    """按租户、调度和序号定位一条检查点。"""
    return (
        select(DispatchRecoveryCheckpoint)
        .where(
            DispatchRecoveryCheckpoint.tenant_id == tenant_id,
            DispatchRecoveryCheckpoint.dispatch_id == dispatch_id,
            DispatchRecoveryCheckpoint.checkpoint_seq == checkpoint_seq,
        )
        .limit(1)
    )


def latest_checkpoints_statement(
    tenant_id: int, dispatch_id: int, limit: int
) -> Select[tuple[DispatchRecoveryCheckpoint]]:
    """序号从新到旧。"""
    return (
        select(DispatchRecoveryCheckpoint)
        .where(
            DispatchRecoveryCheckpoint.tenant_id == tenant_id,
            DispatchRecoveryCheckpoint.dispatch_id == dispatch_id,
        )
        .order_by(
            DispatchRecoveryCheckpoint.checkpoint_seq.desc(),
            DispatchRecoveryCheckpoint.id.desc(),
        )
        .limit(limit)
    )


def obsolete_checkpoints_statement(
    tenant_id: int, dispatch_id: int, retain: int
) -> Select[tuple[DispatchRecoveryCheckpoint]]:
    """跳过保留的最新行，其余都是可删除的旧检查点。"""
    return (
        select(DispatchRecoveryCheckpoint)
        .where(
            DispatchRecoveryCheckpoint.tenant_id == tenant_id,
            DispatchRecoveryCheckpoint.dispatch_id == dispatch_id,
        )
        .order_by(
            DispatchRecoveryCheckpoint.checkpoint_seq.desc(),
            DispatchRecoveryCheckpoint.id.desc(),
        )
        .offset(retain)
        .limit(2**64 - 1)
    )


def pinned_event_statement(
    tenant_id: int, dispatch_id: int
) -> Select[tuple[DispatchRuntimeEvent]]:
    """最近一条会话钉住事件。"""
    return (
        select(DispatchRuntimeEvent)
        .where(
            DispatchRuntimeEvent.tenant_id == tenant_id,
            DispatchRuntimeEvent.dispatch_id == dispatch_id,
            DispatchRuntimeEvent.event_type == SESSION_PINNED_EVENT,
        )
        .order_by(DispatchRuntimeEvent.id.desc())
        .limit(1)
    )


def dispatch_by_id_statement(dispatch_id: int) -> Select[tuple[Dispatch]]:
    """未删除的调度。"""
    return select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)


class SqlCheckpointRepo(CheckpointRepo):
    """用当前事务里的同步会话读写检查点。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_seq(
        self, tenant_id: int, dispatch_id: int, checkpoint_seq: int
    ) -> CheckpointRecord | None:
        row = self.session.scalars(
            checkpoint_by_seq_statement(tenant_id, dispatch_id, checkpoint_seq)
        ).first()
        if row is None:
            return None
        return record_from_row(row)

    def insert(self, row: CheckpointRecord) -> None:
        model = DispatchRecoveryCheckpoint(
            tenant_id=row.tenant_id,
            workitem_id=row.workitem_id,
            dispatch_id=row.dispatch_id,
            agent_id=row.agent_id,
            checkpoint_seq=row.checkpoint_seq,
            provider=row.provider,
            provider_session_id=row.provider_session_id,
            runtime_id=row.runtime_id,
            executor_id=row.executor_id,
            active_step_id=row.active_step_id,
            oss_ref=row.oss_ref or "",
            sha256=row.sha256 or "",
            size_bytes=row.size_bytes or 0,
        )
        self.session.add(model)
        try:
            self.session.flush()
        except IntegrityError as error:
            self.session.rollback()
            if _duplicate_key(error):
                raise DuplicateCheckpoint from error
            raise
        row.id = model.id

    def list_latest(self, tenant_id: int, dispatch_id: int, limit: int) -> list[CheckpointRecord]:
        rows = self.session.scalars(latest_checkpoints_statement(tenant_id, dispatch_id, limit))
        return [record_from_row(row) for row in rows]

    def find_latest(self, tenant_id: int, dispatch_id: int) -> CheckpointRecord | None:
        found = self.list_latest(tenant_id, dispatch_id, 1)
        if len(found) == 0:
            return None
        return found[0]

    def list_obsolete(
        self, tenant_id: int, dispatch_id: int, retain: int
    ) -> list[CheckpointRecord]:
        rows = self.session.scalars(obsolete_checkpoints_statement(tenant_id, dispatch_id, retain))
        return [record_from_row(row) for row in rows]

    def delete_by_id(self, tenant_id: int, dispatch_id: int, row_id: int) -> None:
        self.session.execute(
            delete(DispatchRecoveryCheckpoint).where(
                DispatchRecoveryCheckpoint.tenant_id == tenant_id,
                DispatchRecoveryCheckpoint.dispatch_id == dispatch_id,
                DispatchRecoveryCheckpoint.id == row_id,
            )
        )
        self.session.flush()

    def find_dispatch(self, dispatch_id: int) -> ResumeDispatch | None:
        row = self.session.scalars(dispatch_by_id_statement(dispatch_id)).first()
        if row is None:
            return None
        return ResumeDispatch(
            row.id,
            row.tenant_id,
            row.resume_mode,
            row.resume_from_dispatch_id,
        )

    def find_pinned_detail(self, tenant_id: int, dispatch_id: int) -> object | None:
        row = self.session.scalars(pinned_event_statement(tenant_id, dispatch_id)).first()
        if row is None:
            return None
        return row.detail_json


def record_from_row(row: DispatchRecoveryCheckpoint) -> CheckpointRecord:
    """把表行收成投影用的记录。"""
    return CheckpointRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        workitem_id=row.workitem_id,
        dispatch_id=row.dispatch_id,
        agent_id=row.agent_id,
        checkpoint_seq=row.checkpoint_seq,
        provider=row.provider,
        provider_session_id=row.provider_session_id,
        runtime_id=row.runtime_id,
        executor_id=row.executor_id,
        active_step_id=row.active_step_id,
        oss_ref=row.oss_ref,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
    )


def store_dispatch_from_row(row: Dispatch) -> StoreDispatch:
    """从调度行取出写入检查点需要的字段。"""
    return StoreDispatch(
        id=row.id,
        tenant_id=row.tenant_id,
        workitem_id=row.workitem_id,
        agent_id=row.agent_id,
        executor_id=row.executor_id,
    )


def clip(value: str | None, limit: int) -> str | None:
    """按 Java ``String.trim`` 去首尾，再按 UTF-16 码元截断。"""
    if value is None:
        return None
    trimmed = _java_trim(value)
    encoded = trimmed.encode("utf-16-le")
    if len(encoded) // 2 <= limit:
        return trimmed
    return encoded[: limit * 2].decode("utf-16-le", errors="surrogatepass")


def _wire(
    dispatch: ResumeDispatch,
    source_dispatch_id: int | None,
    provider: str | None,
    provider_session_id: str | None,
    download_url: str | None,
    checksum: str | None,
    checkpoint_seq: int | None,
    candidates: list[ResumeCandidate],
) -> ResumeDescriptor:
    canonical = dispatch.resume_mode == "CANONICAL_INTERACTION"
    mode = dispatch.resume_mode
    behavior = None
    if canonical:
        mode = "SIDE_INTERACTION"
        behavior = "CANONICAL"
    elif dispatch.resume_mode == "SIDE_INTERACTION":
        behavior = "FORK"
    return ResumeDescriptor(
        mode,
        behavior,
        source_dispatch_id,
        provider,
        provider_session_id,
        download_url,
        checksum,
        checkpoint_seq,
        candidates,
    )


def _detail_object(detail: object | None) -> dict[str, object] | None:
    if detail is None:
        return None
    if isinstance(detail, dict):
        return detail
    if not isinstance(detail, str) or _blank(detail):
        return None
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _valid_commit(commit: str | None) -> TypeGuard[str]:
    if commit is None:
        return False
    return _COMMIT.fullmatch(commit) is not None


def _valid_branch(branch: str | None) -> TypeGuard[str]:
    if branch is None or _blank(branch) or _utf16_len(branch) > 255:
        return False
    if branch.startswith("-") or branch.startswith("/") or branch.endswith("/"):
        return False
    if branch.endswith(".") or branch.endswith(".lock") or branch == "@":
        return False
    if ".." in branch or "@{" in branch or "//" in branch:
        return False
    for char in branch:
        code = ord(char)
        if _iso_control(code) or _java_whitespace(code) or char in _FORBIDDEN_BRANCH:
            return False
    for part in branch.split("/"):
        if part == "" or part.startswith(".") or part.endswith(".lock"):
            return False
    return True


def _extract_checkpoint_json(archive: bytes) -> bytes | None:
    with gzip.GzipFile(fileobj=io.BytesIO(archive), mode="rb") as compressed:
        scanned = 0
        while scanned <= MAX_CHECKPOINT_SCAN_BYTES:
            header = _read_exact(compressed, 512)
            if len(header) == 0 or _all_zero(header):
                return None
            if len(header) != 512:
                return None
            size = _tar_size(header)
            if size < 0 or size > MAX_CHECKPOINT_SCAN_BYTES:
                return None
            name = _tar_name(header)
            scanned += 512 + size
            if scanned > MAX_CHECKPOINT_SCAN_BYTES:
                return None
            if name == "checkpoint.json":
                if size > MAX_CHECKPOINT_METADATA_BYTES:
                    return None
                content = _read_exact(compressed, size)
                if len(content) == size:
                    return content
                return None
            _read_exact(compressed, size)
            padding = (512 - size % 512) % 512
            _read_exact(compressed, padding)
            scanned += padding
    return None


def _tar_name(header: bytes) -> str:
    length = 0
    while length < 100 and header[length] != 0:
        length += 1
    return header[:length].decode("utf-8")


def _tar_size(header: bytes) -> int:
    raw = header[124:136].decode("ascii", errors="replace").replace("\0", "")
    raw = _java_trim(raw)
    if raw == "":
        return 0
    try:
        return int(raw, 8)
    except ValueError:
        return -1


def _all_zero(block: bytes) -> bool:
    for value in block:
        if value != 0:
            return False
    return True


def _read_exact(stream: gzip.GzipFile, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        block = stream.read(remaining)
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None or not origin.args:
        return False
    return origin.args[0] == 1062


def _blank(text: str) -> bool:
    for char in text:
        if not _java_whitespace(ord(char)):
            return False
    return True


def _java_trim(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and ord(text[start]) <= 0x20:
        start += 1
    while end > start and ord(text[end - 1]) <= 0x20:
        end -= 1
    return text[start:end]


def _java_whitespace(code_point: int) -> bool:
    if code_point in {0x00A0, 0x2007, 0x202F}:
        return False
    if code_point in {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F}:
        return True
    category = unicodedata.category(chr(code_point))
    return category == "Zs" or category == "Zl" or category == "Zp"


def _iso_control(code_point: int) -> bool:
    if code_point <= 0x1F or 0x7F <= code_point <= 0x9F:
        return True
    return False


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
