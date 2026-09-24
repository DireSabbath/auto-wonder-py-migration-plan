"""``/api/squad-templates``。查看要求只读，应用要求读写。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.templates.service import apply_template, get_template, list_templates

router = APIRouter(
    prefix="/api/squad-templates",
    tags=["squad-templates"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看小队模板"))],
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
async def list_active(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """当前工作空间可见的启用模板。"""
    return ok(await list_templates(session, _workspace_id()))


@router.get("/{id}")
async def detail(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """模板详情。"""
    return ok(await get_template(session, id))


@router.post(
    "/{id}/apply",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "应用小队模板"))],
)
async def apply(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按模板创建小队和数字员工。"""
    return ok(await apply_template(session, id, _workspace_id(), _user_id()))
