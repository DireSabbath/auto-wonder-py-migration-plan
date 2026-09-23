"""``/api/settings``。管理系统设置要求工作空间管理员。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.settings.schemas import UpdateSettingsRequest
from autowonder.settings.service import list_by_group, update_group

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "管理系统设置"))],
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


@router.get("/{group}")
async def list_group(group: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出一个分组的设置。"""
    return ok(await list_by_group(session, group, _workspace_id()))


@router.put("/{group}")
async def save_group(
    group: str,
    body: UpdateSettingsRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按分组写入设置。"""
    await update_group(session, group, body, _workspace_id(), _user_id())
    return ok(None)
