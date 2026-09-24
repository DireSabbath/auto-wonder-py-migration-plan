"""执行器控制帧。本机有连接就直发，否则广播给持有会话的节点。"""

import logging
from typing import cast

from sqlalchemy.orm import Session

from autowonder.config import get_settings
from autowonder.dispatch.checkpoint import (
    CheckpointEngine,
    ResumeDescriptor,
    ResumeDispatch,
    SqlCheckpointRepo,
)
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.selector import ProtocolCompatibilityError
from autowonder.environments.snapshot import resolve_snapshot
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.storage.factory import resolve_bucket
from autowonder.storage.objects import get_object_storage
from autowonder.taskpackages.context import TaskPackageResult
from autowonder.ws.frames import (
    AGENT_ENVIRONMENT_VARIABLES_V1,
    DEBUG_LOG_V1,
    TASK_PACKAGE_HOOKS_V1,
    TASK_PACKAGE_SIGNATURE_V1,
    TASK_PACKAGE_TOOL_HOOKS_V1,
    ResumeCheckpointCandidate,
    build_task_dispatch_frame,
    dump_frame,
    enabled_debug_log,
    task_pause_frame,
)
from autowonder.ws.mailbox import deliver_executor_frame
from autowonder.ws.presence import presence_manager

logger = logging.getLogger(__name__)

PAUSE_SEND_FAILURE = "暂停请求发送失败，请重试暂停"


async def deliver_pause(dispatch: Dispatch) -> None:
    """向已分配的执行器发送 ``TASK_PAUSE``。"""
    executor_id = dispatch.executor_id
    if executor_id is None:
        raise RuntimeError("pause requires an assigned executor")
    try:
        await deliver_executor_frame(executor_id, task_pause_frame(dispatch.id, executor_id))
    except Exception as error:
        raise RuntimeError("WebSocket pause send failed") from error


async def send_task(session: object, dispatch: Dispatch, package: TaskPackageResult) -> None:
    """组装 ``TASK_DISPATCH`` 并交给本机或广播。发送失败保持已占用的派发。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    from autowonder.mcp.dispatch_tokens import issue_dispatch_token

    bound = cast(AsyncSession, session)
    executor_id = cast(int, dispatch.executor_id)
    await _require_package_protocol(executor_id, package)
    frame = build_task_dispatch_frame(
        dispatch_id=dispatch.id,
        executor_id=executor_id,
        tenant_id=dispatch.tenant_id,
        workitem_id=dispatch.workitem_id,
        idempotency_key=dispatch.idempotency_key,
        agent_id=dispatch.agent_id,
        agent_version_id=cast(int, dispatch.agent_version_id),
        sdlc_step_id=dispatch.sdlc_step_id,
        attempt=dispatch.attempt,
        download_url=package.download_url,
        md5=package.md5,
        size=package.size,
        sha256=package.sha256,
        allow_commit=package.allow_commit,
        allow_push=package.allow_push,
        allow_network=package.allow_network,
        issuer=package.issuer,
        signature_ref=package.signature_ref,
        signature=package.signature,
        signature_algorithm=package.signature_algorithm,
        signature_public_key=package.signature_public_key,
        expires_at=package.expires_at,
    )
    await _apply_debug_log(frame, dispatch, executor_id)
    frame.dispatch_mcp_token = await issue_dispatch_token(bound, dispatch)
    if package.mcp_secret_refs:
        crypto = AesGcmSecretCrypto(get_settings().secret_master_key)
        frame.mcp_secrets = {ref: crypto.decrypt(ref) for ref in package.mcp_secret_refs}
    resume = await bound.run_sync(lambda sync_session: _resume(sync_session, dispatch))
    if resume is not None:
        frame.resume_mode = resume.mode
        frame.resume_session_behavior = resume.session_behavior
        frame.resume_from_dispatch_id = resume.source_dispatch_id
        frame.resume_provider = resume.provider
        frame.resume_session_id = resume.provider_session_id
        frame.resume_checkpoint_url = resume.checkpoint_download_url
        frame.resume_checkpoint_sha256 = resume.checkpoint_sha256
        frame.resume_checkpoint_seq = resume.checkpoint_seq
        frame.resume_checkpoint_candidates = [
            ResumeCheckpointCandidate(
                download_url=item.download_url,
                sha256=item.sha256,
                checkpoint_seq=item.checkpoint_seq,
            )
            for item in resume.checkpoint_candidates
        ]
    variables = dict(
        await resolve_snapshot(bound, dispatch.tenant_id, cast(int, dispatch.agent_version_id))
    )
    if variables and not await presence_manager.supports_protocol_feature(
        executor_id, AGENT_ENVIRONMENT_VARIABLES_V1
    ):
        raise ProtocolCompatibilityError(AGENT_ENVIRONMENT_VARIABLES_V1)
    frame.environment_variables = variables
    logger.info(
        "dispatch sending dispatchId=%s executorId=%s pkgSize=%s",
        dispatch.id,
        executor_id,
        package.size,
    )
    await deliver_executor_frame(executor_id, dump_frame(frame))


async def _require_package_protocol(executor_id: int, package: TaskPackageResult) -> None:
    if package.requires_tool_hook_protocol:
        tool_hooks = await presence_manager.supports_protocol_feature(
            executor_id, TASK_PACKAGE_TOOL_HOOKS_V1
        )
        if not tool_hooks:
            raise RuntimeError("Executor does not support blocking tool hooks")
    if package.signature is None:
        return
    if not await presence_manager.supports_protocol_feature(
        executor_id, TASK_PACKAGE_SIGNATURE_V1
    ):
        logger.warning(
            "executor %s does not declare TASK_PACKAGE_SIGNATURE_V1; "
            "dispatching without enforcement",
            executor_id,
        )
        return
    if package.requires_hook_protocol and not await presence_manager.supports_protocol_feature(
        executor_id, TASK_PACKAGE_HOOKS_V1
    ):
        logger.warning(
            "executor %s does not declare TASK_PACKAGE_HOOKS_V1; "
            "dispatching without hook enforcement",
            executor_id,
        )


async def _apply_debug_log(frame: object, dispatch: Dispatch, executor_id: int) -> None:
    from autowonder.ws.frames import TaskDispatchFrame

    target = cast(TaskDispatchFrame, frame)
    if dispatch.debug_log_enabled != 1:
        return
    try:
        if await presence_manager.supports_protocol_feature(executor_id, DEBUG_LOG_V1):
            target.debug_log = enabled_debug_log()
    except Exception:
        logger.warning(
            "debug log directive skipped dispatchId=%s executorId=%s "
            "reason=DEBUG_LOG_NEGOTIATE_ERROR",
            dispatch.id,
            executor_id,
            exc_info=True,
        )


def _resume(sync_session: Session, dispatch: Dispatch) -> ResumeDescriptor | None:
    settings = get_settings()
    bucket = resolve_bucket(settings.oss_task_pkg_bucket, settings.oss_bucket)
    engine = CheckpointEngine(get_object_storage(), bucket)
    described = engine.descriptor(
        ResumeDispatch(
            dispatch.id,
            dispatch.tenant_id,
            dispatch.resume_mode,
            dispatch.resume_from_dispatch_id,
        ),
        SqlCheckpointRepo(sync_session),
    )
    return described
