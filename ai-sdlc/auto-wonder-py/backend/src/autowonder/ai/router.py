"""AI 会话 HTTP 接口。"""

import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.ai.session import (
    AppendMessageRequest,
    ConfirmResultRequest,
    CreateSessionRequest,
    append_message,
    cancel_session,
    confirm_session,
    create_session,
    get_session_view,
)
from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/ai/sessions",
    tags=["ai-sessions"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看AI会话"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


@router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建AI会话"))],
)
async def create_ai_session(
    body: CreateSessionRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建会话并返回会话 id。"""
    user_id = _user_id()
    logger.info(
        "ai session create scene=%s bizRefType=%s bizRefId=%s userId=%s",
        body.scene,
        body.biz_ref_type,
        body.biz_ref_id,
        user_id,
    )
    session_id = await create_session(session, body, _workspace_id(), user_id)
    return ok(session_id)


@router.get("/{id}")
async def get_ai_session(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取会话。"""
    tenant_id = _workspace_id()
    logger.info("ai session get id=%s tenantId=%s", id, tenant_id)
    return ok(await get_session_view(session, id, tenant_id))


@router.post(
    "/{id}/messages",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "追加AI会话消息"))],
)
async def append_ai_message(
    id: int,
    body: AppendMessageRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """向待确认会话追加用户消息。"""
    tenant_id = _workspace_id()
    logger.info("ai session appendMessage id=%s tenantId=%s", id, tenant_id)
    await append_message(session, id, body, tenant_id)
    return ok(None)


@router.post(
    "/{id}/confirm",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "确认AI会话"))],
)
async def confirm_ai_session(
    id: int,
    body: ConfirmResultRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """确认结构化结果。"""
    tenant_id = _workspace_id()
    logger.info("ai session confirm id=%s tenantId=%s", id, tenant_id)
    await confirm_session(session, id, body, tenant_id)
    return ok(None)


@router.post(
    "/{id}/cancel",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "取消AI会话"))],
)
async def cancel_ai_session(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """取消尚未运行完的会话。"""
    tenant_id = _workspace_id()
    logger.info("ai session cancel id=%s tenantId=%s", id, tenant_id)
    await cancel_session(session, id, tenant_id)
    return ok(None)
