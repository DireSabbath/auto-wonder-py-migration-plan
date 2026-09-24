"""``/api/workitems``。查看要求只读，改工单要求读写。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.dispatch.continue_run import continue_workitem
from autowonder.dispatch.pause_request import request_workitem_pause
from autowonder.dispatch.recovery import cancel, close, reopen, state
from autowonder.guidance.service import attach_interaction_statuses, create_for_comment
from autowonder.workitems.comments import add_comment, list_comments, publish_mentions
from autowonder.workitems.participants import get_mention_candidates, get_participants
from autowonder.workitems.progress import get_delivery_progress
from autowonder.workitems.schemas import (
    AddCommentRequest,
    AssignRequest,
    CreateWorkitemRequest,
    RecoveryControlRequest,
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
from autowonder.workitems.timeline import timeline, unified_timeline
from autowonder.workitems.watchers import follow, list_watchers, unfollow

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
    return ok(await get_workitem(session, id, _workspace_id(), _user_id()))


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


@router.post(
    "/{id}/comments",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "添加工作项评论"))],
)
async def add_comment_item(
    id: int,
    body: AddCommentRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """添加评论，并把 @ 数字员工写成指引投递。"""
    comment, notices = await add_comment(
        session, id, body.content_md, body.target_human_ids, _workspace_id(), _user_id()
    )
    await create_for_comment(
        session,
        _workspace_id(),
        id,
        comment.id,
        body.content_md,
        body.target_agent_ids,
        _user_id(),
    )
    await session.commit()
    await publish_mentions(session, notices)
    return ok(comment)


@router.get("/{id}/comments")
async def list_comment_items(
    id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """列出评论。"""
    return ok(await list_comments(session, id))


@router.get("/{id}/timeline")
async def timeline_item(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """事件时间线。"""
    return ok(await timeline(session, id))


@router.get("/{id}/unified-timeline")
async def unified_timeline_item(
    id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """评论和系统事件混排，并附上指引状态。"""
    items = await unified_timeline(session, id)
    await attach_interaction_statuses(session, _workspace_id(), id, items)
    return ok(items)


@router.get("/{id}/delivery-progress")
async def delivery_progress_item(
    id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """交付进度。步骤耗时来自运行时事件，总耗时来自正式派发。"""
    return ok(await get_delivery_progress(session, id, _workspace_id()))


@router.get(
    "/{workitemId}/recovery",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看调度恢复"))],
)
async def recovery_state(
    workitemId: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """交付是否关闭，以及每条派发的恢复阶段。"""
    return ok(await state(session, _workspace_id(), workitemId))


@router.post(
    "/{workitemId}/recovery",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "恢复或关闭交付"))],
)
async def recovery_control(
    workitemId: int,
    body: RecoveryControlRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """关闭、重开或取消。未知 action 返回冲突。"""
    if body.action is None:
        raise BizError(ErrorCode.CONFLICT, "action required")
    tenant_id = _workspace_id()
    user_id = _user_id()
    if body.action == "close":
        data = await close(session, tenant_id, workitemId, user_id, body.force)
    elif body.action == "reopen":
        data = await reopen(session, tenant_id, workitemId, user_id)
    elif body.action == "cancel":
        data = await cancel(
            session, tenant_id, workitemId, body.dispatch_id, user_id, body.force
        )
    else:
        raise BizError(ErrorCode.CONFLICT, "不支持的恢复操作")
    return ok(data)


@router.post(
    "/{workitemId}/dispatches/{dispatchId}/pause",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "暂停调度"))],
)
async def pause_dispatch_item(
    workitemId: int,
    dispatchId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """请求暂停。执行器通道未接通时，状态落成 PAUSE_FAILED。"""
    dispatch = await request_workitem_pause(
        session, _workspace_id(), workitemId, dispatchId, _user_id()
    )
    return ok({"dispatchId": dispatch.id, "status": dispatch.status})


@router.post(
    "/{workitemId}/dispatches/{dispatchId}/continue",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "继续调度"))],
)
async def continue_dispatch_item(
    workitemId: int,
    dispatchId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """继续失败或暂停的派发。新行保持 PENDING，直到调度主环启动。"""
    created = await continue_workitem(
        session, _workspace_id(), workitemId, dispatchId, _user_id()
    )
    return ok(
        {"dispatchId": created.id, "attempt": created.attempt, "status": created.status}
    )


@router.get("/{id}/participants")
async def participant_items(
    id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """参与者。"""
    return ok(await get_participants(session, id, _workspace_id()))


@router.get("/{id}/mention-candidates")
async def mention_candidate_items(
    id: int,
    q: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 50,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """@ 候选人。"""
    return ok(await get_mention_candidates(session, id, _workspace_id(), q, limit))


@router.post("/{id}/watch")
async def watch_item(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """关注工单。这是个人订阅，沿用查看权限。"""
    return ok(await follow(session, id, _workspace_id(), _user_id()))


@router.delete("/{id}/watch")
async def unwatch_item(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """取消关注。没有关注记录时仍返回未关注。"""
    return ok(await unfollow(session, id, _workspace_id(), _user_id()))


@router.get("/{id}/watchers")
async def watcher_items(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """仍在工作空间内的关注人。"""
    return ok(await list_watchers(session, id, _workspace_id()))
