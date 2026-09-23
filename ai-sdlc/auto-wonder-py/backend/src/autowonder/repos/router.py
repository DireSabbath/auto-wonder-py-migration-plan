"""``/api/repos``。查看要求只读，改仓库要求读写。"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.repos.connection import probe_connection
from autowonder.repos.schemas import (
    ConnectionTestRequest,
    CreateRelationRequest,
    CreateRepoRequest,
    UpdateConclusionRequest,
    repo_update_from_json,
)
from autowonder.repos.service import (
    create_relation,
    create_repo,
    delete_relation,
    delete_repo,
    get_conclusion,
    get_repo,
    list_relations,
    list_relations_by_repo,
    list_repos,
    start_scan,
    update_conclusion,
    update_repo,
)

router = APIRouter(
    prefix="/api/repos",
    tags=["repos"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看代码仓库"))],
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
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建代码仓库"))],
)
async def create(
    body: CreateRepoRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建仓库。"""
    return ok(await create_repo(session, body, _workspace_id(), _user_id()))


@router.post(
    "/test-connection",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "测试代码仓库连接"))],
)
async def test(body: ConnectionTestRequest) -> dict[str, Any]:
    """测试 git 读取权限。"""
    return ok(probe_connection(body))


@router.get("/relations")
async def relations(
    session: AsyncSession = Depends(get_session),
    repoId: int | None = None,
) -> dict[str, Any]:
    """仓库关系。带 repoId 时只返回该仓库相关的边。"""
    if repoId is not None:
        return ok(await list_relations_by_repo(session, _workspace_id(), repoId))
    return ok(await list_relations(session, _workspace_id()))


@router.post(
    "/relations",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建代码仓库关联"))],
)
async def add_relation(
    body: CreateRelationRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建仓库关系。"""
    return ok(await create_relation(session, body, _workspace_id(), _user_id()))


@router.delete(
    "/relations/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除代码仓库关联"))],
)
async def remove_relation(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除仓库关系。"""
    await delete_relation(session, id, _workspace_id())
    return ok(None)


@router.get("")
async def list_page(
    session: AsyncSession = Depends(get_session),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页列出仓库。"""
    return ok(await list_repos(session, _workspace_id(), page, size))


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """仓库详情。"""
    return ok(await get_repo(session, id))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新代码仓库"))],
)
async def update(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按 JSON 字段是否出现更新仓库。"""
    raw = await request.body()
    body: object = None
    if raw:
        body = json.loads(raw)
    return ok(
        await update_repo(session, id, repo_update_from_json(body), _workspace_id(), _user_id())
    )


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除代码仓库"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除仓库。"""
    await delete_repo(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.post(
    "/{id}/scan",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "扫描代码仓库"))],
)
async def scan(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """把仓库标成扫描中。"""
    await start_scan(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.get("/{id}/conclusion")
async def conclusion(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """仓库结论。没有结论时 data 为 null。"""
    return ok(await get_conclusion(session, id))


@router.put(
    "/{id}/conclusion",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新代码仓库结论"))],
)
async def save_conclusion(
    id: int,
    body: UpdateConclusionRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """写入或替换仓库结论。"""
    return ok(await update_conclusion(session, id, body, _workspace_id(), _user_id()))
