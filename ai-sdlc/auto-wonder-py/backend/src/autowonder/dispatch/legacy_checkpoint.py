"""把旧检查点里只写了目录前缀的产物引用，投影成可校验的对象地址。"""

import gzip
import hashlib
import io
import json
import logging
import re
import tarfile
from collections.abc import Callable
from dataclasses import dataclass

from autowonder.artifacts.models import Artifact
from autowonder.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
COMPAT_SUFFIX = ".compat-exact-ref-v1.tar.gz"
_ACCEPTED = "artifacts/accepted/publish-manifest.json"
_ATTEMPT = re.compile(r"artifacts/attempts/[^/]+/attempt-[0-9]+/publish-manifest\.json")
_PENDING = "state/accepted-publication-pending.json"
_BUCKET = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")
_DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}")


class CheckpointUnusable(Exception):
    """旧包无法证明引用指向哪一个产物。"""


@dataclass
class CheckpointRecord:
    """一行可恢复检查点。投影函数不要求它已经绑定会话。"""

    id: int | None = None
    tenant_id: int = 0
    workitem_id: int = 0
    dispatch_id: int = 0
    agent_id: int = 0
    checkpoint_seq: int = 0
    provider: str | None = None
    provider_session_id: str | None = None
    runtime_id: str | None = None
    executor_id: int | None = None
    active_step_id: str | None = None
    oss_ref: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None


def compatibility_ref(original: str) -> str:
    """兼容包挂在原检查点引用后面。"""
    return original + COMPAT_SUFFIX


class LegacyCheckpointNormalizer:
    """读取时改写旧清单。失败时仍返回原来的检查点。"""

    def __init__(
        self,
        storage: ObjectStorage,
        list_artifacts: Callable[[int, int], list[Artifact]],
    ) -> None:
        self.storage = storage
        self.list_artifacts = list_artifacts

    def normalize(self, source: CheckpointRecord) -> CheckpointRecord:
        """能证明产物就换上兼容包，否则保持原引用。"""
        try:
            return self._project(source)
        except Exception:
            logger.warning(
                "legacy checkpoint compatibility unavailable dispatchId=%s checkpointSeq=%s",
                source.dispatch_id,
                source.checkpoint_seq,
            )
            return source

    def _project(self, source: CheckpointRecord) -> CheckpointRecord:
        if source.size_bytes is None or source.size_bytes <= 0:
            raise CheckpointUnusable()
        if source.size_bytes > MAX_ARCHIVE_BYTES or source.oss_ref is None:
            raise CheckpointUnusable()
        original = self.storage.get(source.oss_ref)
        if original is None or len(original) != source.size_bytes:
            raise CheckpointUnusable()
        if source.sha256 is None or sha256_hex(original).lower() != source.sha256.lower():
            raise CheckpointUnusable()
        members = _scan_manifests(original)
        metadata = _json_object(members.get("checkpoint.json"))
        if metadata.get("schemaVersion") != "autowonder.runtimeCheckpoint.v2":
            return source
        if str(source.dispatch_id) != metadata.get("dispatchId"):
            raise CheckpointUnusable()
        if source.checkpoint_seq != metadata.get("checkpointSeq"):
            raise CheckpointUnusable()
        if _PENDING in members:
            return source
        prefix = (
            "t/"
            + str(source.tenant_id)
            + "/workitem/"
            + str(source.workitem_id)
            + "/dispatch/"
            + str(source.dispatch_id)
            + "/"
        )
        rows: list[Artifact] | None = None
        verified: dict[str, str] = {}
        changed = False
        for name, payload in members.items():
            if not _manifest(name):
                continue
            document = _json_object(payload)
            if document.get("schemaVersion") != "autowonder.publishManifest.v1":
                raise CheckpointUnusable()
            entries = document.get("entries")
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise CheckpointUnusable()
            edited = False
            for entry in entries:
                if not isinstance(entry, dict):
                    raise CheckpointUnusable()
                remote_ref = entry.get("remoteRef")
                if entry.get("disposition") != "reference_only" or remote_ref != prefix:
                    continue
                if rows is None:
                    rows = self.list_artifacts(source.tenant_id, source.dispatch_id)
                path = entry.get("path")
                digest = entry.get("sha256")
                size = entry.get("sizeBytes")
                if not isinstance(path, str) or not _safe_path(path):
                    raise CheckpointUnusable()
                if not isinstance(size, int) or isinstance(size, bool):
                    raise CheckpointUnusable()
                if size < 0 or size > MAX_ARTIFACT_BYTES:
                    raise CheckpointUnusable()
                if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
                    raise CheckpointUnusable()
                cache_key = path + ":" + digest + ":" + str(size)
                ref = verified.get(cache_key)
                if ref is None:
                    ref = self._resolve(source, rows, prefix, path, digest, size)
                    verified[cache_key] = ref
                entry["remoteRef"] = ref
                edited = True
            if edited:
                rendered = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
                members[name] = rendered.encode("utf-8")
                changed = True
        if not changed:
            return source
        compatible = _pack(original, members)
        ref = compatibility_ref(source.oss_ref)
        existing = self.storage.get(ref) if self.storage.exists(ref) else None
        if existing is not None:
            if existing != compatible:
                raise CheckpointUnusable()
        else:
            slash = ref.find("/")
            if slash <= 0:
                raise CheckpointUnusable()
            self.storage.put(ref[:slash], ref[slash + 1 :], compatible)
        projected = CheckpointRecord(
            id=source.id,
            tenant_id=source.tenant_id,
            workitem_id=source.workitem_id,
            dispatch_id=source.dispatch_id,
            agent_id=source.agent_id,
            checkpoint_seq=source.checkpoint_seq,
            provider=source.provider,
            provider_session_id=source.provider_session_id,
            runtime_id=source.runtime_id,
            executor_id=source.executor_id,
            active_step_id=source.active_step_id,
            oss_ref=ref,
            sha256=sha256_hex(compatible),
            size_bytes=len(compatible),
        )
        return projected

    def _resolve(
        self,
        source: CheckpointRecord,
        rows: list[Artifact],
        prefix: str,
        path: str,
        digest: str,
        size: int,
    ) -> str:
        name = "artifacts/output/" + path
        matches = [row for row in rows if row.name == name]
        if len(matches) != 1:
            raise CheckpointUnusable()
        row = matches[0]
        if row.tenant_id != source.tenant_id or row.workitem_id != source.workitem_id:
            raise CheckpointUnusable()
        if row.dispatch_id != source.dispatch_id or row.source_type != "WORKITEM":
            raise CheckpointUnusable()
        if row.size != size:
            raise CheckpointUnusable()
        ref = row.oss_ref
        slash = -1 if ref is None else ref.find("/")
        if slash <= 0:
            raise CheckpointUnusable()
        bucket = ref[:slash]
        key = ref[slash + 1 :]
        if _BUCKET.fullmatch(bucket) is None or key != prefix + name:
            raise CheckpointUnusable()
        payload = self.storage.get(ref)
        if payload is None or len(payload) != size:
            raise CheckpointUnusable()
        if sha256_hex(payload).lower() != digest[7:].lower():
            raise CheckpointUnusable()
        return ref


def sha256_hex(payload: bytes) -> str:
    """小写十六进制 SHA-256。"""
    return hashlib.sha256(payload).hexdigest()


def _manifest(name: str) -> bool:
    if name == _ACCEPTED:
        return True
    return _ATTEMPT.fullmatch(name) is not None


def _json_object(payload: bytes | None) -> dict[str, object]:
    if payload is None or len(payload) > MAX_JSON_BYTES:
        raise CheckpointUnusable()
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise CheckpointUnusable()
    return parsed


def _safe_path(path: str) -> bool:
    if path == "" or path.startswith("/") or "\\" in path or ":" in path:
        return False
    for part in path.split("/"):
        if part == "" or part == "." or part == "..":
            return False
    return True


def _scan_manifests(archive: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    names: set[str] = set()
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(archive), mode="rb") as compressed:
        with tarfile.open(fileobj=compressed, mode="r|") as tar:
            for entry in tar:
                name = entry.name
                path = name
                if entry.isdir() and name.endswith("/"):
                    path = name[:-1]
                if not _safe_path(path) or entry.issym() or entry.islnk():
                    raise CheckpointUnusable()
                if entry.sparse is not None:
                    raise CheckpointUnusable()
                if not entry.isfile() and not entry.isdir():
                    raise CheckpointUnusable()
                if name in names or len(names) >= 10000:
                    raise CheckpointUnusable()
                names.add(name)
                total += entry.size
                if entry.size < 0 or total > MAX_ARCHIVE_BYTES:
                    raise CheckpointUnusable()
                kept = _manifest(name) or name == "checkpoint.json" or name == _PENDING
                if not kept:
                    if entry.isfile():
                        skipped = tar.extractfile(entry)
                        if skipped is not None:
                            skipped.read()
                    continue
                if entry.size > MAX_JSON_BYTES:
                    raise CheckpointUnusable()
                extracted = tar.extractfile(entry)
                if extracted is None:
                    raise CheckpointUnusable()
                data = extracted.read()
                if len(data) != entry.size:
                    raise CheckpointUnusable()
                members[name] = data
    return members


def _pack(original: bytes, replacements: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    compressed_out = gzip.GzipFile(fileobj=out, mode="wb", mtime=0)
    source = gzip.GzipFile(fileobj=io.BytesIO(original), mode="rb")
    with tarfile.open(fileobj=source, mode="r|") as incoming:
        with tarfile.open(fileobj=compressed_out, mode="w|", format=tarfile.PAX_FORMAT) as output:
            for entry in incoming:
                replacement = replacements.get(entry.name)
                if replacement is None:
                    if entry.isdir():
                        output.addfile(entry)
                        continue
                    extracted = incoming.extractfile(entry)
                    payload = b"" if extracted is None else extracted.read()
                    output.addfile(entry, io.BytesIO(payload))
                    continue
                entry.size = len(replacement)
                output.addfile(entry, io.BytesIO(replacement))
    compressed_out.close()
    return out.getvalue()
