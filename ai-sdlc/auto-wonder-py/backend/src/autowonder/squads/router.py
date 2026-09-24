"""``/api/squads``。类级别要求只读，写操作再要求读写。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.squads.schemas import AddMembersRequest, CreateSquadRequest, UpdateSquadRequest
from autowonder.squads.service import (
    add_members,
    create_squad,
    delete_squad,
    get_squad,
    list_members,
    list_squads,
    remove_member,
    update_squad,
)

router = APIRouter(
    prefix="/api/squads",
    tags=["squads"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看小队"))],
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


@router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建小队"))],
)
async def create(
    body: CreateSquadRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建小队。"""
    return ok(await create_squad(session, body, _workspace_id(), _user_id()))


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """小队详情。"""
    return ok(await get_squad(session, id))


@router.get("")
async def list_page(
    session: AsyncSession = Depends(get_session),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """在用小队列表。"""
    return ok(await list_squads(session, page, size))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新小队"))],
)
async def update(
    id: int,
    body: UpdateSquadRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新小队。"""
    return ok(await update_squad(session, id, body, _workspace_id(), _user_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除小队"))],
)
async def delete(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除小队。"""
    await delete_squad(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.get("/{id}/members")
async def members(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """小队成员。"""
    return ok(await list_members(session, id, _workspace_id()))


@router.post(
    "/{id}/members",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "添加小队成员"))],
)
async def add(
    id: int,
    body: AddMembersRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """添加小队成员。"""
    await add_members(session, id, body, _workspace_id())
    return ok(None)


@router.delete(
    "/{id}/members/{agentId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "移除小队成员"))],
)
async def remove(
    id: int,
    agentId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """移除小队成员。"""
    await remove_member(session, id, agentId, _workspace_id())
    return ok(None)
