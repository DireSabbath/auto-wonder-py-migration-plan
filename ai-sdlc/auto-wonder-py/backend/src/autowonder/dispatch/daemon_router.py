"""Daemon 上传检查点，并续认领仍在执行的派发。这些路径靠执行器令牌校验。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from autowonder.artifacts.daemon_auth import UploadAuth, authenticate
from autowonder.audits.service import AuditRecord, record_required
from autowonder.config import get_settings
from autowonder.db.session import get_session
from autowonder.dispatch.checkpoint import (
    CheckpointEngine,
    CheckpointRecord,
    SqlCheckpointRepo,
    dispatch_by_id_statement,
    store_dispatch_from_row,
)
from autowonder.dispatch.package_url import refresh_package_url
from autowonder.dispatch.recovery_claim import claim_http_response, claim_recovery
from autowonder.storage.objects import get_object_storage

MAX_CHECKPOINT_BYTES = 50 * 1024 * 1024
_UNAVAILABLE = "checkpoint upload temporarily unavailable"

router = APIRouter(prefix="/api/daemon/dispatches", tags=["daemon-checkpoints"])


def checkpoint_http_result(
    auth: UploadAuth,
    dispatch_id: int,
    checkpoint_seq: int,
    payload: bytes,
    provider: str | None,
    provider_session_id: str | None,
    runtime_id: str | None,
    active_step_id: str | None,
    stored: CheckpointRecord | None,
    failed: bool,
) -> tuple[int, dict[str, Any] | None, AuditRecord | None]:
    """按 Java 控制器返回 401、400、503 或 200。存储失败不写审计。"""
    if not auth.success:
        return 401, None, None
    if checkpoint_seq <= 0 or len(payload) == 0 or len(payload) > MAX_CHECKPOINT_BYTES:
        return 400, {"error": "invalid checkpoint"}, None
    if failed or stored is None or stored.sha256 is None or stored.size_bytes is None:
        return 503, {"error": _UNAVAILABLE}, None
    audit = AuditRecord(
        tenant_id=auth.tenant_id,
        actor_id=auth.agent_id,
        actor_type="AGENT",
        module="DISPATCH",
        action="UPLOAD_CHECKPOINT",
        target_type="dispatch",
        target_id=dispatch_id,
        trigger_type="EVENT",
        trigger_source="DAEMON_CALLBACK",
        event_type="daemon.checkpoint",
    )
    audit.add("workitemId", auth.workitem_id)
    audit.add("checkpointSeq", checkpoint_seq)
    audit.add("provider", provider)
    audit.add("providerSessionId", provider_session_id)
    audit.add("runtimeId", runtime_id)
    audit.add("activeStepId", active_step_id)
    audit.add("sizeBytes", stored.size_bytes)
    body = {
        "checkpointSeq": stored.checkpoint_seq,
        "sha256": "sha256:" + stored.sha256,
        "sizeBytes": stored.size_bytes,
    }
    return 200, body, audit


@router.post("/{dispatchId}/checkpoint")
async def upload_checkpoint(
    dispatchId: int,
    token: Annotated[str, Form()],
    checkpointSeq: Annotated[int, Form()],
    checkpoint: UploadFile,
    provider: Annotated[str | None, Form()] = None,
    providerSessionId: Annotated[str | None, Form()] = None,
    runtimeId: Annotated[str | None, Form()] = None,
    activeStepId: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """接收运行时归档。令牌无效时正文为空。"""
    auth = await authenticate(session, dispatchId, token)
    payload = await checkpoint.read()
    stored: CheckpointRecord | None = None
    failed = False
    accepted = auth.success and checkpointSeq > 0 and len(payload) > 0
    if accepted and len(payload) <= MAX_CHECKPOINT_BYTES:
        try:
            stored = await _store(
                session,
                dispatchId,
                checkpointSeq,
                provider,
                providerSessionId,
                runtimeId,
                activeStepId,
                payload,
            )
        except Exception:
            await session.rollback()
            failed = True
    status, body, audit = checkpoint_http_result(
        auth,
        dispatchId,
        checkpointSeq,
        payload,
        provider,
        providerSessionId,
        runtimeId,
        activeStepId,
        stored,
        failed,
    )
    if status == 401:
        return Response(status_code=401)
    if audit is None or body is None:
        return JSONResponse(status_code=status, content=body)
    await record_required(session, audit)
    await session.commit()
    return JSONResponse(status_code=200, content=body)


async def _store(
    session: AsyncSession,
    dispatch_id: int,
    checkpoint_seq: int,
    provider: str | None,
    provider_session_id: str | None,
    runtime_id: str | None,
    active_step_id: str | None,
    payload: bytes,
) -> CheckpointRecord:
    bucket = _artifact_bucket()
    engine = CheckpointEngine(get_object_storage(), bucket)

    def write(sync_session: Session) -> CheckpointRecord:
        dispatch = sync_session.scalars(dispatch_by_id_statement(dispatch_id)).first()
        if dispatch is None:
            raise RuntimeError("dispatch missing")
        return engine.store(
            store_dispatch_from_row(dispatch),
            checkpoint_seq,
            provider,
            provider_session_id,
            runtime_id,
            active_step_id,
            payload,
            SqlCheckpointRepo(sync_session),
        )

    return await session.run_sync(write)


def _artifact_bucket() -> str:
    settings = get_settings()
    if settings.oss_artifact_bucket.strip() == "":
        return settings.oss_bucket
    return settings.oss_artifact_bucket


@router.post("/{dispatchId}/recovery-claim")
async def recovery_claim(
    dispatchId: int,
    token: Annotated[str, Query()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """续认领活动派发。成功时附上当前版本的环境变量，并禁止缓存。"""
    status, body = await claim_recovery(session, dispatchId, token)
    return claim_http_response(status, body)


@router.post("/{dispatchId}/package-url")
async def package_url(
    dispatchId: int,
    token: Annotated[str, Query()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """为仍在进行的派发重新签发任务包下载地址。"""
    status, body = await refresh_package_url(session, dispatchId, token)
    if body is None:
        return Response(status_code=status)
    return JSONResponse(status_code=status, content=body)
