"""``/api/workitems``。查看要求只读，改工单要求读写。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.workitems.schemas import (
    AssignRequest,
    CreateWorkitemRequest,
    ScheduledStartRequest,
    TransitionRequest,
    UpdateContentRequest,
    UpdateTagsRequest,
    parse_java_date,
)
from autowonder.workitems.service import (
    assign,
    create,
    delete_workitem,
    get_workitem,
    list_workitems,
    transition,
    update_content,
    update_scheduled_start,
    update_tags,
)

router = APIRouter(
    prefix="/api/workitems",
    tags=["workitems"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看工作项"))],
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
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建工作项"))],
)
async def create_item(
    body: CreateWorkitemRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建工单。"""
    return ok(
        await create(
            session,
            body,
            _workspace_id(),
            _user_id(),
            parse_java_date(body.scheduled_start_at),
        )
    )


@router.get("")
async def list_items(
    work_type: Annotated[str | None, Query(alias="workType")] = None,
    status_node_id: Annotated[int | None, Query(alias="statusNodeId")] = None,
    status_category: Annotated[str | None, Query(alias="statusCategory")] = None,
    assignee_type: Annotated[str | None, Query(alias="assigneeType")] = None,
    assignee_ref: Annotated[int | None, Query(alias="assigneeRef")] = None,
    pending_decision_only: Annotated[bool, Query(alias="pendingDecisionOnly")] = False,
    mine_scope: Annotated[str | None, Query(alias="mineScope")] = None,
    keyword: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    scheduled_start: Annotated[str | None, Query(alias="scheduledStart")] = None,
    page: Annotated[int, Query()] = 1,
    size: Annotated[int, Query()] = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """分页列出工单。"""
    return ok(
        await list_workitems(
            session,
            _workspace_id(),
            _user_id(),
            work_type,
            status_node_id,
            status_category,
            assignee_type,
            assignee_ref,
            pending_decision_only,
            mine_scope,
            keyword,
            tag,
            scheduled_start,
            page,
            size,
        )
    )


@router.get("/{id}")
async def get_item(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """工单详情。"""
    return ok(await get_workitem(session, id))


@router.post(
    "/{id}/transition",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "流转工作项"))],
)
async def transition_item(
    id: int,
    body: TransitionRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """沿状态边流转。"""
    if body.to_node_id is None:
        raise BizError(ErrorCode.ILLEGAL_TRANSITION)
    return ok(
        await transition(
            session,
            id,
            body.to_node_id,
            _workspace_id(),
            _user_id(),
            body.from_node_id,
            body.expected_version,
        )
    )


@router.put(
    "/{id}/assignee",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "指派工作项"))],
)
async def assign_item(
    id: int,
    body: AssignRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """指派负责人。"""
    return ok(
        await assign(
            session,
            id,
            body.assignee_type,
            body.assignee_ref,
            body.sdlc_id,
            body.squad_id,
            parse_java_date(body.scheduled_start_at),
            _workspace_id(),
            _user_id(),
        )
    )


@router.put(
    "/{id}/scheduled-start",
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "调整工作项计划执行时间"))
    ],
)
async def scheduled_start_item(
    id: int,
    body: ScheduledStartRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """调整计划执行时间。"""
    execute_now = False
    if body.execute_now is True:
        execute_now = True
    return ok(
        await update_scheduled_start(
            session,
            id,
            parse_java_date(body.scheduled_start_at),
            execute_now,
            _workspace_id(),
            _user_id(),
        )
    )


@router.put(
    "/{id}/tags",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新工作项标签"))],
)
async def tags_item(
    id: int,
    body: UpdateTagsRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新标签。"""
    return ok(await update_tags(session, id, body.tags, _workspace_id(), _user_id()))


@router.put(
    "/{id}/content",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新工作项内容"))],
)
async def content_item(
    id: int,
    body: UpdateContentRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新标题和正文。"""
    return ok(
        await update_content(
            session,
            id,
            body.title,
            body.content_md,
            _workspace_id(),
            _user_id(),
        )
    )


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除工作项"))],
)
async def delete_item(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除工单。"""
    await delete_workitem(session, id, _workspace_id(), _user_id())
    return ok(None)
