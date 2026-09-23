"""``/api/environment-variables``。查看要求只读，改库要求管理员。"""

from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.environments.schemas import (
    CreateEnvironmentVariableRequest,
    UpdateEnvironmentVariableRequest,
)
from autowonder.environments.service import (
    create_variable,
    delete_variable,
    list_variables,
    reveal_variable,
    update_variable,
)

router = APIRouter(
    prefix="/api/environment-variables",
    tags=["environment-variables"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看环境变量"))],
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
async def list_page(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出脱敏后的环境变量。"""
    return ok(await list_variables(session, _workspace_id()))


@router.get("/{id}/value")
async def reveal(
    id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """查看明文。响应禁止缓存。"""
    response.headers["Cache-Control"] = "no-store"
    return ok(await reveal_variable(session, _workspace_id(), _user_id(), id))


@router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "创建环境变量"))],
)
async def create(
    body: CreateEnvironmentVariableRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建环境变量。"""
    return ok(await create_variable(session, _workspace_id(), _user_id(), body))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "更新环境变量"))],
)
async def update(
    id: int,
    body: UpdateEnvironmentVariableRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新名称、说明或密文。"""
    return ok(await update_variable(session, _workspace_id(), _user_id(), id, body))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "删除环境变量"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除未被挂载的环境变量。"""
    await delete_variable(session, _workspace_id(), _user_id(), id)
    return ok(None)
