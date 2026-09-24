"""执行器上报产物。对象键按内容哈希，同名后写不会覆盖先前字节。"""

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from autowonder.agents.evolution import accepts_runtime_delta
from autowonder.artifacts.classification import classify_artifact
from autowonder.artifacts.daemon_auth import UploadAuth
from autowonder.audits.service import AuditRecord
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)

MAX_FILES_PER_UPLOAD = 200
MAX_SINGLE_FILE_BYTES = 50 * 1024 * 1024
_DRIVE = re.compile(r"[A-Za-z]:/.*")
_UNAVAILABLE = "artifact upload temporarily unavailable"


@dataclass(frozen=True)
class DaemonFile:
    """一次 multipart 文件。大小在读正文之前就按声明值判断。"""

    filename: str | None
    size: int
    payload: bytes


@dataclass(frozen=True)
class RelayTarget:
    """调试日志中转要写入的规范对象键和轮次。"""

    object_key: str
    run_no: int


@dataclass
class ReportedArtifact:
    """交给产物登记的一行。类型在登记时再按路径补全。"""

    tenant_id: int
    source_type: str
    source_id: int
    workitem_id: int
    dispatch_id: int
    name: str
    artifact_type: str
    oss_ref: str
    size: int


@dataclass
class UploadHooks:
    """上报过程里的邻域动作。调试日志和定时任务通知可以不装配。"""

    require_scheduled: Callable[[], Awaitable[None]]
    resolve_mode: Callable[[int, int], Awaitable[str]]
    record_artifact: Callable[[ReportedArtifact], Awaitable[int]]
    ingest_usage: Callable[[int, int, int, int, str, str, bytes], Awaitable[None]]
    record_audit: Callable[[AuditRecord], Awaitable[None]]
    ingest_memory: Callable[[int, int, int, bytes], Awaitable[None]]
    ingest_evolution: Callable[[int, int, int, bytes, str], Awaitable[None]]
    notify_scheduled: Callable[[int, int], Awaitable[None]] | None = None
    relay_target: Callable[[int, int], Awaitable[RelayTarget | None]] | None = None
    record_relay: Callable[
        [int, int, str, int, int, dict[str, Any] | None],
        Awaitable[None],
    ] | None = None
    scheduled_task_id: Callable[[int, int], Awaitable[int | None]] | None = None


@dataclass
class UploadResult:
    """HTTP 状态和正文。401 的正文为空。"""

    status: int
    body: dict[str, Any] | None


def classify(path: str | None) -> str:
    """按逻辑路径给出产物类型。"""
    return classify_artifact(path)


def logical_path(path: str | None) -> str | None:
    """去掉执行器加上的 artifacts/output 或 output 前缀。"""
    if path is None:
        return None
    if path.startswith("artifacts/output/"):
        return path[len("artifacts/output/") :]
    if path.startswith("output/"):
        return path[len("output/") :]
    return path


def is_telemetry(path: str | None) -> bool:
    """观测目录登记为 TELEMETRY，并且不写产物审计。"""
    if path is None:
        return False
    if path.startswith("observability/"):
        return True
    return "/observability/" in path


def is_debug_log(path: str | None) -> bool:
    """只认 logical 路径上的 debug/ 前缀。"""
    if path is None:
        return False
    return path.startswith("debug/")


def sanitize_path(raw: str | None) -> str | None:
    """绝对路径、盘符和目录穿越都不接收。反斜杠先折成斜杠。"""
    if raw is None or java_is_blank(raw):
        return None
    if "\0" in raw:
        return None
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/"):
        return None
    if _DRIVE.fullmatch(normalized):
        return None
    if normalized.startswith(".."):
        return None
    if "/../" in normalized or normalized.endswith("/.."):
        return None
    return normalized


def resolve_file_path(metadata: list[Any] | None, index: int, fallback: str | None) -> str:
    """元数据里的 path 优先。没有时用原始文件名，再没有就用序号。"""
    if metadata is not None and index < len(metadata):
        entry = metadata[index]
        if isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str) and not java_is_blank(path):
                return path
    if fallback is not None:
        return fallback
    return "file_" + str(index)


def metadata_entry(metadata: list[Any] | None, index: int) -> dict[str, Any] | None:
    """中转登记只取同一下标上的对象。"""
    if metadata is None or index >= len(metadata):
        return None
    entry = metadata[index]
    if isinstance(entry, dict):
        return entry
    return None


def rejected_file(
    index: int,
    path: str | None,
    size: int,
    code: str,
    max_bytes: int | None,
) -> dict[str, Any]:
    """拒绝回执。没有路径时写成空字符串，超限才带 maxBytes。"""
    if path is None:
        shown = ""
    else:
        shown = path
    receipt: dict[str, Any] = {
        "index": index,
        "path": shown,
        "status": "REJECTED",
        "code": code,
        "sizeBytes": size,
    }
    if max_bytes is not None:
        receipt["maxBytes"] = max_bytes
    return receipt


def _owner_prefix(auth: UploadAuth, dispatch_id: int) -> str:
    if auth.source_type == "SCHEDULED_TASK_RUN":
        owner_segment = "scheduled-task-run"
    else:
        owner_segment = "workitem"
    return (
        "t/"
        + str(auth.tenant_id)
        + "/"
        + owner_segment
        + "/"
        + str(auth.workitem_id)
        + "/dispatch/"
        + str(dispatch_id)
        + "/"
    )


def _parse_metadata(dispatch_id: int, files_metadata: str | None) -> list[Any] | None:
    if files_metadata is None or java_is_blank(files_metadata):
        return None
    try:
        parsed = json.loads(files_metadata)
    except json.JSONDecodeError:
        logger.warning("invalid filesMetadata JSON for dispatchId=%s", dispatch_id)
        return None
    if isinstance(parsed, list):
        return parsed
    if parsed is not None:
        logger.warning("invalid filesMetadata JSON for dispatchId=%s", dispatch_id)
    return None


async def upload_daemon_artifacts(
    dispatch_id: int,
    auth: UploadAuth,
    fenced: bool,
    files_metadata: str | None,
    files: list[DaemonFile],
    bucket: str,
    storage: ObjectStorage,
    hooks: UploadHooks,
) -> UploadResult:
    """按 Java 控制器逐个文件回执。存储失败时整批返回 503。

    请求里的幂等键不参与选键。同一路径的不同字节各自落到 sha256 前缀下。
    """
    logger.info("artifact upload request dispatchId=%s fileCount=%s", dispatch_id, len(files))
    if not auth.success or fenced:
        logger.info("artifact upload auth failed dispatchId=%s", dispatch_id)
        return UploadResult(401, None)
    if auth.source_type == "SCHEDULED_TASK_RUN":
        await hooks.require_scheduled()
    mode = await hooks.resolve_mode(auth.tenant_id, auth.agent_id)
    metadata = _parse_metadata(dispatch_id, files_metadata)
    if len(files) > MAX_FILES_PER_UPLOAD:
        return UploadResult(400, {"error": "too many files"})

    prefix = _owner_prefix(auth, dispatch_id)
    receipts: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        requested = resolve_file_path(metadata, index, item.filename)
        path = sanitize_path(requested)
        if path is None:
            logger.warning("rejected unsafe file path in dispatch=%s index=%s", dispatch_id, index)
            receipts.append(
                rejected_file(index, requested, item.size, "INVALID_PATH", None),
            )
            continue
        if item.size > MAX_SINGLE_FILE_BYTES:
            logger.warning(
                "rejected oversized file dispatch=%s path=%s size=%s",
                dispatch_id,
                path,
                item.size,
            )
            receipts.append(
                rejected_file(index, path, item.size, "FILE_TOO_LARGE", MAX_SINGLE_FILE_BYTES),
            )
            continue
        logical = logical_path(path)
        debug_file = is_debug_log(logical)
        digest = hashlib.sha256(item.payload).hexdigest()
        storage_key = prefix + "objects/sha256/" + digest + "/" + path
        relay: RelayTarget | None = None
        if debug_file:
            relay = await _lookup_relay(hooks, auth.tenant_id, dispatch_id, path)
            if relay is None:
                logger.warning("rejected debug log relay dispatch=%s path=%s", dispatch_id, path)
                receipts.append(
                    rejected_file(index, path, item.size, "DEBUG_LOG_DISABLED", None),
                )
                continue
            storage_key = relay.object_key
        try:
            stored = storage.put(bucket, storage_key, item.payload)
        except Exception:
            logger.exception("artifact upload unavailable dispatchId=%s", dispatch_id)
            return UploadResult(503, {"error": _UNAVAILABLE})
        if is_telemetry(logical):
            artifact_type = "TELEMETRY"
        else:
            artifact_type = classify(logical)
        logger.info(
            "artifact uploaded dispatchId=%s path=%s type=%s size=%s",
            dispatch_id,
            path,
            artifact_type,
            item.size,
        )
        artifact_id = await hooks.record_artifact(
            ReportedArtifact(
                tenant_id=auth.tenant_id,
                source_type=auth.source_type,
                source_id=auth.workitem_id,
                workitem_id=auth.workitem_id,
                dispatch_id=dispatch_id,
                name=path,
                artifact_type=artifact_type,
                oss_ref=stored.oss_ref,
                size=stored.size,
            )
        )
        if auth.source_type == "SCHEDULED_TASK_RUN" and hooks.notify_scheduled is not None:
            await hooks.notify_scheduled(auth.tenant_id, auth.workitem_id)
        await hooks.ingest_usage(
            auth.tenant_id,
            auth.workitem_id,
            dispatch_id,
            artifact_id,
            path,
            stored.oss_ref,
            item.payload,
        )
        if not is_telemetry(logical) and not debug_file:
            await _audit(hooks, auth, dispatch_id, path, classify(logical), stored.size)
        receipts.append(
            {
                "index": index,
                "path": path,
                "status": "ACCEPTED",
                "sizeBytes": item.size,
                "ossRef": stored.oss_ref,
                "remoteRef": stored.oss_ref,
            }
        )
        if debug_file and relay is not None and hooks.record_relay is not None:
            try:
                await hooks.record_relay(
                    auth.tenant_id,
                    dispatch_id,
                    relay.object_key,
                    relay.run_no,
                    stored.size,
                    metadata_entry(metadata, index),
                )
            except Exception:
                logger.warning(
                    "debug log relay record failed dispatchId=%s key=%s",
                    dispatch_id,
                    storage_key,
                    exc_info=True,
                )
        if accepts_runtime_delta(mode) and logical == "learning_delta/memory_delta.json":
            logger.info(
                "artifact memory_delta ingested dispatchId=%s agentId=%s",
                dispatch_id,
                auth.agent_id,
            )
            await hooks.ingest_memory(auth.tenant_id, auth.agent_id, dispatch_id, item.payload)
        if accepts_runtime_delta(mode) and logical == "learning_delta/evolution_delta.json":
            try:
                await hooks.ingest_evolution(
                    auth.tenant_id,
                    auth.agent_id,
                    dispatch_id,
                    item.payload,
                    mode,
                )
                logger.info(
                    "artifact evolution_delta ingested dispatchId=%s agentId=%s",
                    dispatch_id,
                    auth.agent_id,
                )
            except Exception:
                logger.warning(
                    "artifact evolution_delta ingestion skipped dispatchId=%s agentId=%s",
                    dispatch_id,
                    auth.agent_id,
                    exc_info=True,
                )
    return UploadResult(200, {"remoteRef": prefix, "files": receipts})


async def _lookup_relay(
    hooks: UploadHooks,
    tenant_id: int,
    dispatch_id: int,
    path: str,
) -> RelayTarget | None:
    if hooks.relay_target is None:
        return None
    try:
        return await hooks.relay_target(tenant_id, dispatch_id)
    except Exception:
        logger.warning(
            "debug log relay lookup failed dispatchId=%s path=%s",
            dispatch_id,
            path,
            exc_info=True,
        )
        return None


async def _audit(
    hooks: UploadHooks,
    auth: UploadAuth,
    dispatch_id: int,
    path: str,
    artifact_type: str,
    size: int,
) -> None:
    record = AuditRecord(
        tenant_id=auth.tenant_id,
        actor_id=auth.agent_id,
        actor_type="AGENT",
        module="ARTIFACT",
        action="UPLOAD_ARTIFACT",
        target_type="dispatch",
        target_id=dispatch_id,
        trigger_type="EVENT",
        trigger_source="DAEMON_CALLBACK",
        event_type="daemon.artifact",
    )
    record.add("workitemId", auth.workitem_id)
    record.add("sourceType", auth.source_type)
    record.add("path", path)
    record.add("type", artifact_type)
    record.add("size", size)
    if auth.source_type == "SCHEDULED_TASK_RUN":
        record.add("runId", auth.workitem_id)
        if hooks.scheduled_task_id is not None:
            task_id = await hooks.scheduled_task_id(auth.tenant_id, auth.workitem_id)
            record.add("taskId", task_id)
    await hooks.record_audit(record)
