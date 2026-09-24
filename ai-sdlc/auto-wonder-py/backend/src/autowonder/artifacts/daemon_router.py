"""执行器上报产物。这条路径在鉴权白名单里，靠执行器令牌校验。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.evolution import resolve_runtime_evolution_mode
from autowonder.aiusage.dispatch_usage import ingest_usage_artifact
from autowonder.artifacts.daemon_auth import authenticate, load_mutation_fence
from autowonder.artifacts.daemon_upload import (
    DaemonFile,
    RelayTarget,
    ReportedArtifact,
    UploadHooks,
    upload_daemon_artifacts,
)
from autowonder.artifacts.service import record_reported_artifact
from autowonder.audits.service import AuditRecord, record_required
from autowonder.config import get_settings
from autowonder.db.session import get_session
from autowonder.debuglogs.relay import lookup_relay_target, record_relay_upload
from autowonder.evolution.delta import ingest_evolution_delta
from autowonder.memories.sedimentation import ingest as ingest_memory
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.scheduledtasks.notify import publish_artifact, scheduled_task_id
from autowonder.storage.objects import get_object_storage

router = APIRouter(prefix="/api/daemon", tags=["daemon-artifacts"])


def artifact_bucket() -> str:
    """产物桶空白时回落到默认桶。"""
    settings = get_settings()
    if settings.oss_artifact_bucket.strip() == "":
        return settings.oss_bucket
    return settings.oss_artifact_bucket


def session_hooks(session: AsyncSession) -> UploadHooks:
    """把上报动作接到当前请求的会话和进程内存储上。"""

    async def require_scheduled() -> None:
        require_scheduled_capability()

    async def resolve_mode(tenant_id: int, agent_id: int) -> str:
        return await resolve_runtime_evolution_mode(session, tenant_id, agent_id)

    async def record_artifact(reported: ReportedArtifact) -> int:
        return await record_reported_artifact(
            session,
            reported.tenant_id,
            reported.source_type,
            reported.source_id,
            reported.dispatch_id,
            reported.name,
            reported.artifact_type,
            reported.oss_ref,
            reported.size,
        )

    async def ingest_usage(
        tenant_id: int,
        workitem_id: int,
        dispatch_id: int,
        artifact_id: int,
        path: str,
        oss_ref: str,
        payload: bytes,
    ) -> None:
        await ingest_usage_artifact(
            session,
            tenant_id,
            workitem_id,
            dispatch_id,
            artifact_id,
            path,
            oss_ref,
            payload,
        )

    async def record_audit(record: AuditRecord) -> None:
        await record_required(session, record)

    async def remember(tenant_id: int, agent_id: int, dispatch_id: int, payload: bytes) -> None:
        await ingest_memory(session, tenant_id, agent_id, dispatch_id, payload)

    async def evolve(
        tenant_id: int,
        agent_id: int,
        dispatch_id: int,
        payload: bytes,
        mode: str,
    ) -> None:
        await ingest_evolution_delta(session, tenant_id, agent_id, dispatch_id, payload, mode)

    async def notify(tenant_id: int, run_id: int) -> None:
        await publish_artifact(session, tenant_id, run_id)

    async def relay_target(tenant_id: int, dispatch_id: int) -> RelayTarget | None:
        found = await lookup_relay_target(session, tenant_id, dispatch_id)
        if found is None:
            return None
        return RelayTarget(found[0], found[1])

    async def record_relay(
        tenant_id: int,
        dispatch_id: int,
        object_key: str,
        run_no: int,
        size_bytes: int,
        metadata: dict[str, Any] | None,
    ) -> None:
        await record_relay_upload(
            session,
            tenant_id,
            dispatch_id,
            object_key,
            run_no,
            size_bytes,
            metadata,
        )

    async def task_id(tenant_id: int, run_id: int) -> int | None:
        return await scheduled_task_id(session, tenant_id, run_id)

    return UploadHooks(
        require_scheduled=require_scheduled,
        resolve_mode=resolve_mode,
        record_artifact=record_artifact,
        ingest_usage=ingest_usage,
        record_audit=record_audit,
        ingest_memory=remember,
        ingest_evolution=evolve,
        notify_scheduled=notify,
        relay_target=relay_target,
        record_relay=record_relay,
        scheduled_task_id=task_id,
    )


@router.post("/dispatches/{dispatchId}/artifacts")
async def upload_artifacts(
    dispatchId: int,
    token: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    idempotencyKey: Annotated[str | None, Form()] = None,
    filesMetadata: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """接收执行器产物。令牌无效或写入被栅栏挡住时正文为空。

    ``idempotencyKey`` 与 Java 一样只绑定请求，不参与对象键。
    """
    _ = idempotencyKey
    parts: list[DaemonFile] = []
    for item in files:
        payload = await item.read()
        parts.append(DaemonFile(item.filename, len(payload), payload))
    auth = await authenticate(session, dispatchId, token)
    fenced = False
    if auth.success:
        fenced = await load_mutation_fence(session, dispatchId)
    outcome = await upload_daemon_artifacts(
        dispatchId,
        auth,
        fenced,
        filesMetadata,
        parts,
        artifact_bucket(),
        get_object_storage(),
        session_hooks(session),
    )
    if outcome.body is None:
        return Response(status_code=outcome.status)
    if outcome.status == 200 or outcome.status == 503:
        await session.commit()
    return JSONResponse(status_code=outcome.status, content=outcome.body)
