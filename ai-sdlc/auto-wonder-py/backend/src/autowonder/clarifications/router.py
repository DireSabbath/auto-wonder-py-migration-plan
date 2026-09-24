"""``/api/workitems/{workitemId}/clarification``。更新要求读写。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.clarifications.schemas import PutClarificationRequest
from autowonder.clarifications.service import get_clarification, put_clarification
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

router = APIRouter(
    prefix="/api/workitems/{workitemId}/clarification",
    tags=["clarifications"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看澄清信息"))],
)


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("")
async def get(workitemId: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取澄清材料。"""
    return ok(await get_clarification(session, workitemId))


@router.put(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新澄清信息"))],
)
async def put(
    workitemId: int,
    body: PutClarificationRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """替换澄清正文。"""
    return ok(
        await put_clarification(session, workitemId, body.content_md, _workspace_id(), _user_id())
    )
