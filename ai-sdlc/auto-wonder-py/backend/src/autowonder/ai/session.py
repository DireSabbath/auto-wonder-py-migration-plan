"""AI 会话的创建、追问、确认和取消。执行阶段再调用 CLI。"""

import logging
import os
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.ai.adapters import SCENES, persist_confirmed, validate_result
from autowonder.ai.cli_executor import CliExecutor, CliResult
from autowonder.ai.models import AiMessage, AiSession
from autowonder.aiusage.service import check_quota
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.redis import redis_client
from autowonder.core.schema import ApiModel
from autowonder.db.rows import rowcount
from autowonder.repos.models import Repo

logger = logging.getLogger(__name__)

_QUEUE = "ai:queue:global"
_TERMINAL_CANCEL = frozenset({"COMPLETED", "CANCELED", "FAILED", "RUNNING"})


class CreateSessionRequest(ApiModel):
    """创建会话。仓库扫描不把 input 写成首条用户消息。"""

    scene: str | None = None
    biz_ref_type: str | None = None
    biz_ref_id: int | None = None
    input: str | None = None


class AppendMessageRequest(ApiModel):
    """待确认会话上的下一条用户消息。"""

    content: str | None = None


class ConfirmResultRequest(ApiModel):
    """用户确认的结构化结果。"""

    result_json: str | None = None


class AiMessageView(ApiModel):
    """会话消息。"""

    id: int | None = None
    session_id: int | None = None
    seq: int | None = None
    role: str | None = None
    content: str | None = None
    meta_json: object | None = None
    gmt_create: datetime | None = None


class AiSessionView(ApiModel):
    """会话详情，带全部消息。"""

    id: int | None = None
    scene: str | None = None
    biz_ref_type: str | None = None
    biz_ref_id: int | None = None
    status: str | None = None
    result_json: object | None = None
    error: str | None = None
    gmt_create: datetime | None = None
    messages: list[AiMessageView] = []


def should_persist_initial_user_message(request: CreateSessionRequest) -> bool:
    """仓库扫描的首条输入由扫描器自己生成，不落用户消息。"""
    return (
        request.scene != "REPO_SCAN"
        and request.input is not None
        and request.input.strip() != ""
    )


def cli_executor() -> CliExecutor:
    """按部署环境构造 CLI。缺省二进制是 claude，超时 300 秒。"""
    return CliExecutor(
        os.environ.get("AUTOWONDER_AI_CLI_BINARY", "claude"),
        int(os.environ.get("AUTOWONDER_AI_CLI_TIMEOUT_SEC", "300")),
        os.environ.get("AUTOWONDER_AI_CLI_LAUNCH_MODE", "direct"),
        os.environ.get("AUTOWONDER_AI_CLI_SHELL", "/bin/bash"),
        os.environ.get("AUTOWONDER_AI_ANTHROPIC_API_KEY", ""),
        os.environ.get("AUTOWONDER_AI_ANTHROPIC_AUTH_TOKEN", ""),
        os.environ.get("AUTOWONDER_AI_ANTHROPIC_BASE_URL", ""),
        os.environ.get("AUTOWONDER_AI_ANTHROPIC_MODEL", ""),
    )


async def create_session(
    session: AsyncSession,
    request: CreateSessionRequest,
    tenant_id: int,
    user_id: int,
) -> int:
    """创建排队中的会话，提交成功后再推进全局队列。"""
    if request.scene not in SCENES:
        raise BizError(ErrorCode.AI_SCENE_NOT_SUPPORTED)
    await check_quota(session, tenant_id)
    await _mark_repo_scan(session, request, tenant_id, user_id)
    row = AiSession(
        tenant_id=tenant_id,
        scene=request.scene or "",
        biz_ref_type=request.biz_ref_type,
        biz_ref_id=request.biz_ref_id,
        status="QUEUED",
        creator_id=user_id,
    )
    session.add(row)
    await session.flush()
    if should_persist_initial_user_message(request):
        session.add(
            AiMessage(
                tenant_id=tenant_id,
                session_id=row.id,
                seq=1,
                role="USER",
                content=request.input,
            )
        )
        await session.flush()
    await session.commit()
    await redis_client().lpush(_QUEUE, str(row.id))
    logger.info(
        "AI session queued sessionId=%s scene=%s bizRefType=%s bizRefId=%s tenantId=%s userId=%s",
        row.id,
        request.scene,
        request.biz_ref_type,
        request.biz_ref_id,
        tenant_id,
        user_id,
    )
    return row.id


async def get_session_view(
    session: AsyncSession,
    session_id: int,
    tenant_id: int,
) -> AiSessionView:
    """读取本工作空间的会话和消息。"""
    row = await _require(session, session_id, tenant_id)
    messages = await session.scalars(
        select(AiMessage)
        .where(AiMessage.session_id == session_id, AiMessage.tenant_id == tenant_id)
        .order_by(AiMessage.seq.asc())
    )
    return AiSessionView(
        id=row.id,
        scene=row.scene,
        biz_ref_type=row.biz_ref_type,
        biz_ref_id=row.biz_ref_id,
        status=row.status,
        result_json=row.result_json,
        error=row.error,
        gmt_create=row.gmt_create,
        messages=[
            AiMessageView(
                id=item.id,
                session_id=item.session_id,
                seq=item.seq,
                role=item.role,
                content=item.content,
                meta_json=item.meta_json,
                gmt_create=item.gmt_create,
            )
            for item in messages.all()
        ],
    )


async def append_message(
    session: AsyncSession,
    session_id: int,
    request: AppendMessageRequest,
    tenant_id: int,
) -> None:
    """只接受待确认会话。写入后重新排队。"""
    row = await _require(session, session_id, tenant_id)
    if row.status != "WAIT_USER":
        raise BizError(ErrorCode.AI_SESSION_NOT_WAIT_USER)
    seq = await session.scalar(
        select(func.max(AiMessage.seq)).where(AiMessage.session_id == session_id)
    )
    session.add(
        AiMessage(
            tenant_id=tenant_id,
            session_id=session_id,
            seq=1 if seq is None else seq + 1,
            role="USER",
            content=request.content,
        )
    )
    changed = await session.execute(
        update(AiSession)
        .where(
            AiSession.id == row.id,
            AiSession.tenant_id == tenant_id,
            AiSession.status == "WAIT_USER",
            AiSession.version == row.version,
        )
        .values(status="QUEUED", version=AiSession.version + 1)
    )
    if rowcount(changed) != 1:
        raise BizError(ErrorCode.AI_SESSION_NOT_WAIT_USER)
    await session.commit()
    await redis_client().lpush(_QUEUE, str(session_id))
    logger.info("ai session appendMessage sessionId=%s re-queued", session_id)


async def confirm_session(
    session: AsyncSession,
    session_id: int,
    request: ConfirmResultRequest,
    tenant_id: int,
) -> None:
    """校验并落库确认结果，然后把会话标成完成。"""
    row = await _require(session, session_id, tenant_id)
    if row.status != "WAIT_USER":
        raise BizError(ErrorCode.AI_SESSION_NOT_WAIT_USER)
    if row.scene not in SCENES:
        raise BizError(ErrorCode.AI_SCENE_NOT_SUPPORTED)
    validation = validate_result(row.scene, request.result_json)
    if validation is not None:
        raise BizError(ErrorCode.AI_CONFIRM_VALIDATION_FAILED, validation)
    await persist_confirmed(session, row, request.result_json)
    await session.execute(
        update(AiSession)
        .where(
            AiSession.id == row.id,
            AiSession.tenant_id == tenant_id,
            AiSession.status == "WAIT_USER",
            AiSession.version == row.version,
        )
        .values(status="COMPLETED", version=AiSession.version + 1)
    )
    await session.commit()
    logger.info("ai session confirmed sessionId=%s", session_id)


async def cancel_session(session: AsyncSession, session_id: int, tenant_id: int) -> None:
    """终态和运行中取消是空操作。其余状态改成已取消。"""
    row = await _require(session, session_id, tenant_id)
    if row.status in _TERMINAL_CANCEL:
        return
    await session.execute(
        update(AiSession)
        .where(
            AiSession.id == row.id,
            AiSession.tenant_id == tenant_id,
            AiSession.status == row.status,
            AiSession.version == row.version,
        )
        .values(status="CANCELED", version=AiSession.version + 1)
    )
    await session.commit()
    logger.info("ai session canceled sessionId=%s", session_id)


async def drive_session(
    session: AsyncSession,
    session_id: int,
    tenant_id: int,
    work_dir: str,
    prompt: str,
    executor: CliExecutor | None = None,
) -> CliResult:
    """排队中的会话调用 CLI。这是工作循环需要执行时的入口。"""
    row = await _require(session, session_id, tenant_id)
    if row.status != "QUEUED":
        return CliResult(exit_code=-1, error="session is not queued")
    runner = cli_executor() if executor is None else executor
    result = await runner.execute(prompt, row.cli_session_ref, work_dir, None, None, None)
    status = "FAILED" if result.exit_code not in {0, None} else "WAIT_USER"
    if result.exit_code == 0:
        status = "WAIT_USER"
    else:
        status = "FAILED"
    await session.execute(
        update(AiSession)
        .where(AiSession.id == row.id, AiSession.tenant_id == tenant_id)
        .values(
            status=status,
            error=result.error,
            cli_session_ref=result.cli_session_id or row.cli_session_ref,
            result_json=result.extracted_json,
        )
    )
    await session.commit()
    return result


async def _require(session: AsyncSession, session_id: int, tenant_id: int) -> AiSession:
    row = await session.get(AiSession, session_id)
    if row is None or row.tenant_id != tenant_id or row.is_deleted == 1:
        raise BizError(ErrorCode.AI_SESSION_NOT_FOUND)
    return row


async def _mark_repo_scan(
    session: AsyncSession,
    request: CreateSessionRequest,
    tenant_id: int,
    user_id: int,
) -> None:
    if (
        request.scene != "REPO_SCAN"
        or request.biz_ref_type != "REPO"
        or request.biz_ref_id is None
    ):
        return
    repo = await session.get(Repo, request.biz_ref_id)
    if repo is None or repo.tenant_id != tenant_id:
        raise BizError(ErrorCode.REPO_NOT_FOUND)
    await session.execute(
        update(Repo)
        .where(
            Repo.id == repo.id,
            Repo.tenant_id == tenant_id,
            Repo.version == repo.version,
        )
        .values(scan_status="SCANNING", version=Repo.version + 1, modifier_id=user_id)
    )
