"""``/api/status-templates``。查看要求只读，改模板要求读写。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.statemachines.schemas import (
    CreateNodeRequest,
    CreateTemplateRequest,
    CreateTransitionRequest,
    UpdateNodeRequest,
    UpdateTemplateRequest,
    UpdateTransitionRequest,
)
from autowonder.statemachines.service import (
    create_node,
    create_template,
    create_transition,
    delete_node,
    delete_template,
    delete_transition,
    get_template,
    list_nodes,
    list_templates,
    list_transitions,
    update_node,
    update_template,
    update_transition,
)

router = APIRouter(
    prefix="/api/status-templates",
    tags=["status-templates"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看状态模板"))],
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
async def list_page(
    workType: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按工单类型列出模板。"""
    return ok(await list_templates(session, _workspace_id(), workType))


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """模板详情。"""
    return ok(await get_template(session, id))


@router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建状态模板"))],
)
async def create(
    body: CreateTemplateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建状态模板。"""
    return ok(await create_template(session, body, _workspace_id(), _user_id()))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新状态模板"))],
)
async def update(
    id: int,
    body: UpdateTemplateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新状态模板。"""
    return ok(await update_template(session, id, body, _workspace_id(), _user_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除状态模板"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除状态模板。"""
    await delete_template(session, id, _workspace_id())
    return ok(None)


@router.get("/{id}/nodes")
async def nodes(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出节点。"""
    return ok(await list_nodes(session, id))


@router.post(
    "/{id}/nodes",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建状态节点"))],
)
async def add_node(
    id: int,
    body: CreateNodeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建状态节点。"""
    return ok(await create_node(session, id, body, _workspace_id()))


@router.put(
    "/{id}/nodes/{nodeId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新状态节点"))],
)
async def change_node(
    id: int,
    nodeId: int,
    body: UpdateNodeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新状态节点。路径里的模板 id 与 Java 一样不参与定位。"""
    return ok(await update_node(session, nodeId, body))


@router.delete(
    "/{id}/nodes/{nodeId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除状态节点"))],
)
async def remove_node(
    id: int,
    nodeId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除状态节点。"""
    await delete_node(session, nodeId)
    return ok(None)


@router.get("/{id}/transitions")
async def transitions(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出流转。"""
    return ok(await list_transitions(session, id))


@router.post(
    "/{id}/transitions",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建状态流转"))],
)
async def add_transition(
    id: int,
    body: CreateTransitionRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建状态流转。"""
    return ok(await create_transition(session, id, body, _workspace_id()))


@router.put(
    "/{id}/transitions/{tid}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新状态流转"))],
)
async def change_transition(
    id: int,
    tid: int,
    body: UpdateTransitionRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新状态流转。"""
    return ok(await update_transition(session, tid, body))


@router.delete(
    "/{id}/transitions/{tid}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除状态流转"))],
)
async def remove_transition(
    id: int,
    tid: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除状态流转。"""
    await delete_transition(session, tid)
    return ok(None)
