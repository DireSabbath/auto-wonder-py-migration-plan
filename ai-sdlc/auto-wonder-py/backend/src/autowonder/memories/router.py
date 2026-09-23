"""``/api/memories``。查看要求只读，创建、修改、删除和审核要求读写。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.memories.schemas import (
    CreateMemoryRequest,
    ImportFromArtifactRequest,
    ReviewRequest,
    UpdateMemoryRequest,
)
from autowonder.memories.service import (
    count_groups,
    count_list,
    count_pending_reviews,
    create_memory,
    delete_memory,
    get_memory,
    import_from_artifact,
    list_grouped,
    list_memories,
    review_memory,
    update_memory,
)

router = APIRouter(
    prefix="/api/memories",
    tags=["memories"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看记忆"))],
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
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建记忆"))],
)
async def create(
    body: CreateMemoryRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建待审核记忆。"""
    return ok(await create_memory(session, body, _workspace_id(), _user_id()))


@router.get("")
async def list_page(
    session: AsyncSession = Depends(get_session),
    scope: str | None = None,
    owner_ref: Annotated[int | None, Query(alias="ownerRef")] = None,
    memory_type: Annotated[str | None, Query(alias="type")] = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页列出记忆。未指定状态时排除已拒绝。"""
    return ok(
        await list_memories(
            session,
            _workspace_id(),
            scope,
            owner_ref,
            memory_type,
            status,
            None,
            None,
            page,
            size,
        )
    )


@router.get("/count")
async def count(
    session: AsyncSession = Depends(get_session),
    scope: str | None = None,
    owner_ref: Annotated[int | None, Query(alias="ownerRef")] = None,
    memory_type: Annotated[str | None, Query(alias="type")] = None,
    status: str | None = None,
) -> dict[str, Any]:
    """按列表条件计数。"""
    return ok(await count_list(session, _workspace_id(), scope, owner_ref, memory_type, status))


@router.get("/grouped")
async def grouped(
    session: AsyncSession = Depends(get_session),
    scope: str | None = None,
    owner_ref: Annotated[int | None, Query(alias="ownerRef")] = None,
    memory_type: Annotated[str | None, Query(alias="type")] = None,
    status: str | None = None,
    page: int = 1,
    size: int = 10,
) -> dict[str, Any]:
    """按范围和所有者分组。"""
    return ok(
        await list_grouped(
            session,
            _workspace_id(),
            scope,
            owner_ref,
            memory_type,
            status,
            page,
            size,
        )
    )


@router.get("/grouped/count")
async def grouped_count(
    session: AsyncSession = Depends(get_session),
    scope: str | None = None,
    owner_ref: Annotated[int | None, Query(alias="ownerRef")] = None,
    memory_type: Annotated[str | None, Query(alias="type")] = None,
    status: str | None = None,
) -> dict[str, Any]:
    """分组数量。"""
    return ok(await count_groups(session, _workspace_id(), scope, owner_ref, memory_type, status))


@router.get("/reviews")
async def reviews(
    session: AsyncSession = Depends(get_session),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """待审核记忆。"""
    return ok(
        await list_memories(
            session,
            _workspace_id(),
            None,
            None,
            None,
            "PENDING",
            None,
            None,
            page,
            size,
        )
    )


@router.get(
    "/reviews/count",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "查看待审核记忆数量"))],
)
async def reviews_count(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """待审核记忆数量。"""
    return ok(await count_pending_reviews(session, _workspace_id()))


@router.post(
    "/from-artifact",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "从产物导入记忆"))],
)
async def from_artifact(
    body: ImportFromArtifactRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """从产物导入待审核记忆。"""
    return ok(await import_from_artifact(session, body, _workspace_id(), _user_id()))


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取一条记忆。"""
    return ok(await get_memory(session, id))


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新记忆"))],
)
async def update(
    id: int,
    body: UpdateMemoryRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新记忆。"""
    return ok(await update_memory(session, id, body, _workspace_id(), _user_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除记忆"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """软删除记忆。"""
    await delete_memory(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.post(
    "/{id}/review",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "审核记忆"))],
)
async def review(
    id: int,
    body: ReviewRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """采纳或拒绝记忆。"""
    await review_memory(session, id, body, _workspace_id(), _user_id())
    return ok(None)
