"""``/api/dispatches``。查看要求只读。运行轨迹使用单独的访问动作。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.dispatch.live import load_live_activity
from autowonder.dispatch.query import get_dispatch, list_dispatches
from autowonder.dispatch.trace import choose_published_trace, load_activities, load_projected_trace
from autowonder.dispatch.trace_artifact import (
    load_context,
    load_observation,
    load_outline_if_present,
    load_turn,
)

router = APIRouter(
    prefix="/api/dispatches",
    tags=["dispatches"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看调度"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("")
async def list_page(
    page: Annotated[int, Query()] = 1,
    page_size: Annotated[int, Query(alias="page_size")] = 50,
    status: Annotated[str | None, Query()] = None,
    agent_id: Annotated[int | None, Query(alias="agent_id")] = None,
    workitem_id: Annotated[int | None, Query(alias="workitem_id")] = None,
    time_range: Annotated[str, Query(alias="time_range")] = "30d",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按状态、数字员工、工单和时间窗分页查询调度。"""
    return ok(
        await list_dispatches(
            session,
            _workspace_id(),
            status,
            agent_id,
            workitem_id,
            time_range,
            page,
            page_size,
        )
    )


@router.get("/{id}")
async def get_one(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取一条调度，并带上用户可见产物。"""
    return ok(await get_dispatch(session, _workspace_id(), id))


trace_router = APIRouter(
    prefix="/api/dispatches",
    tags=["dispatch-runtime-trace"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看调度运行轨迹"))],
)


@trace_router.get("/{id}/runtime-trace")
async def get_runtime_trace(
    id: int,
    afterSeq: Annotated[int | None, Query(alias="afterSeq")] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """有完成态大纲就返回大纲，否则按序号投影持久化事件。"""
    workspace_id = _workspace_id()
    outline = await load_outline_if_present(session, workspace_id, id)
    if outline is None:
        projected = await load_projected_trace(session, workspace_id, id, afterSeq)
        return ok(choose_published_trace(outline, projected))
    return ok(outline)


@trace_router.get("/{id}/runtime-trace/events")
async def get_runtime_events(
    id: int,
    afterSeq: Annotated[int | None, Query(alias="afterSeq")] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """持久化事件日志，不看完成态大纲。"""
    return ok(await load_projected_trace(session, _workspace_id(), id, afterSeq))


@trace_router.get("/{id}/runtime-trace/activities")
async def get_runtime_activities(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按到达顺序返回可读活动。"""
    return ok(await load_activities(session, _workspace_id(), id))


@trace_router.get("/{id}/runtime-trace/turns/{traceId}")
async def get_runtime_turn(
    id: int,
    traceId: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """读取完整回合，保留提示词。"""
    return ok(await load_turn(session, _workspace_id(), id, traceId))


@trace_router.get("/{id}/runtime-trace/observations/{observationId}")
async def get_runtime_observation(
    id: int,
    observationId: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """读取一条观测，保留输入和输出。"""
    return ok(await load_observation(session, _workspace_id(), id, observationId))


@trace_router.get("/{id}/runtime-trace/context")
async def get_runtime_context(
    id: int,
    ref: Annotated[str, Query()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """按引用返回上下文原文。"""
    content = await load_context(session, _workspace_id(), id, ref)
    return Response(
        content=content.payload,
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@trace_router.get("/{id}/live-activity")
async def get_live_activity(
    id: int,
    afterSeq: Annotated[int | None, Query(alias="afterSeq")] = None,
    limit: Annotated[int | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按允许名单返回一条调度的实时活动。"""
    return ok(await load_live_activity(session, _workspace_id(), id, afterSeq, limit))
