"""``/api/sdlcs``。查看要求只读，改流程要求读写。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.sdlcs.schemas import (
    CreateSdlcRequest,
    CreateStepRequest,
    ReorderStepsRequest,
    UpdateSdlcRequest,
    UpdateStepRequest,
)
from autowonder.sdlcs.service import (
    add_step,
    create_sdlc,
    delete_sdlc,
    delete_step,
    disable_sdlc,
    enable_sdlc,
    get_sdlc,
    list_sdlcs,
    reorder_steps,
    update_sdlc,
    update_step,
)

router = APIRouter(
    prefix="/api/sdlcs",
    tags=["sdlcs"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看SDLC流程"))],
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
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建SDLC流程"))],
)
async def create(
    body: CreateSdlcRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建 SDLC 流程。"""
    return ok(await create_sdlc(session, body, _workspace_id(), _user_id()))


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """流程详情。"""
    return ok(await get_sdlc(session, id))


@router.get("")
async def list_page(
    session: AsyncSession = Depends(get_session),
    work_type: Annotated[str | None, Query(alias="workType")] = None,
    status: str | None = None,
    squad_ids: Annotated[list[int] | None, Query(alias="squadIds")] = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """流程列表。"""
    return ok(await list_sdlcs(session, _workspace_id(), work_type, status, squad_ids, page, size))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新SDLC流程"))],
)
async def update(
    id: int,
    body: UpdateSdlcRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新流程。"""
    return ok(await update_sdlc(session, id, body, _workspace_id(), _user_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除SDLC流程"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除流程。"""
    await delete_sdlc(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.post(
    "/{id}/steps",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "添加SDLC步骤"))],
)
async def create_step(
    id: int,
    body: CreateStepRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """添加步骤。"""
    return ok(await add_step(session, id, body, _workspace_id(), _user_id()))


@router.put(
    "/{id}/steps/reorder",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "重排SDLC步骤"))],
)
async def reorder(
    id: int,
    body: ReorderStepsRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """重排步骤。"""
    await reorder_steps(session, id, body, _workspace_id(), _user_id())
    return ok(None)


@router.put(
    "/{id}/steps/{stepId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新SDLC步骤"))],
)
async def update_one_step(
    id: int,
    stepId: int,
    body: UpdateStepRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新步骤。"""
    return ok(await update_step(session, id, stepId, body, _workspace_id(), _user_id()))


@router.delete(
    "/{id}/steps/{stepId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除SDLC步骤"))],
)
async def remove_step(
    id: int,
    stepId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除步骤。"""
    await delete_step(session, id, stepId, _workspace_id(), _user_id())
    return ok(None)


@router.post(
    "/{id}/enable",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "启用SDLC流程"))],
)
async def enable(
    id: int,
    session: AsyncSession = Depends(get_session),
    status_template_id: Annotated[int | None, Query(alias="statusTemplateId")] = None,
) -> dict[str, Any]:
    """启用流程。"""
    return ok(await enable_sdlc(session, id, _workspace_id(), _user_id(), status_template_id))


@router.post(
    "/{id}/disable",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "停用SDLC流程"))],
)
async def disable(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """停用流程。"""
    await disable_sdlc(session, id, _workspace_id(), _user_id())
    return ok(None)
