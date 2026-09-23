"""``/api/skills``。查看要求只读，改技能和打标要求读写。"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.categories.service import batch_set_skill_category, set_skill_category
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.skills.schemas import (
    CreateSkillRequest,
    UpdateSkillRequest,
    category_id_from_json,
    skill_ids_from_json,
)
from autowonder.skills.service import (
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    update_skill,
)

router = APIRouter(
    prefix="/api/skills",
    tags=["skills"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看技能"))],
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


@router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建技能"))],
)
async def create(
    body: CreateSkillRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按安装规格创建技能。"""
    return ok(await create_skill(session, body, _workspace_id(), _user_id()))


@router.get("")
async def list_page(
    skill_type: Annotated[str | None, Query(alias="type")] = None,
    category_id: Annotated[int | None, Query(alias="categoryId")] = None,
    include_descendants: Annotated[bool, Query(alias="includeDescendants")] = True,
    uncategorized: Annotated[bool, Query(alias="uncategorized")] = False,
    page: Annotated[int, Query()] = 1,
    size: Annotated[int, Query()] = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """分页列出技能。"""
    return ok(
        await list_skills(
            session,
            _workspace_id(),
            skill_type,
            category_id,
            include_descendants,
            uncategorized,
            page,
            size,
        )
    )


@router.post(
    "/category/batch",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "批量设置能力分类"))],
)
async def batch_category(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """批量打标。categoryId 为 null 时取消打标。"""
    body = await _json_body(request)
    skill_ids = skill_ids_from_json(body)
    category_id = category_id_from_json(body)
    return ok(
        await batch_set_skill_category(
            session,
            skill_ids,
            category_id,
            _workspace_id(),
            _user_id(),
        )
    )


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """技能详情。"""
    return ok(await get_skill(session, id))


@router.put(
    "/{id}/category",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "设置能力分类"))],
)
async def set_category(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """设置或取消能力主分类。"""
    category_id = category_id_from_json(await _json_body(request))
    return ok(await set_skill_category(session, id, category_id, _workspace_id(), _user_id()))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新技能"))],
)
async def update(
    id: int,
    body: UpdateSkillRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按版本号更新技能。null 字段保留原值。"""
    return ok(await update_skill(session, id, body, _workspace_id(), _user_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除技能"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """软删除技能。仍被引用时拒绝。"""
    await delete_skill(session, id, _workspace_id(), _user_id())
    return ok(None)
