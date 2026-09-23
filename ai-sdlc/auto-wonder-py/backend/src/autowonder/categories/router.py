"""``/api/categories``。查看要求只读，改分类要求管理员。"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.categories.schemas import create_fields_from_json, update_fields_from_json
from autowonder.categories.service import (
    create_category,
    delete_category,
    get_category,
    list_categories,
    update_category,
)
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

router = APIRouter(
    prefix="/api/categories",
    tags=["categories"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看分类"))],
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


async def _json_body(request: Request) -> object:
    raw = await request.body()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error


@router.get("")
async def list_page(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出分类树。"""
    return ok(await list_categories(session, _workspace_id()))


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """分类详情。"""
    return ok(await get_category(session, id, _workspace_id()))


@router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "创建分类"))],
)
async def create(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """创建分类。"""
    fields = create_fields_from_json(await _json_body(request))
    return ok(await create_category(session, fields, _workspace_id(), _user_id()))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "更新分类"))],
)
async def update(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按 JSON 字段是否出现更新分类。"""
    fields = update_fields_from_json(await _json_body(request))
    return ok(await update_category(session, id, fields, _workspace_id(), _user_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "删除分类"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除空分类。"""
    await delete_category(session, id, _workspace_id(), _user_id())
    return ok(None)
