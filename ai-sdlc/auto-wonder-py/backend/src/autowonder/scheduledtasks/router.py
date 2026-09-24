"""定时任务能力快照，以及任务定义的读写接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current, current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.scheduledtasks.capability import capability_snapshot, require_scheduled_capability
from autowonder.scheduledtasks.schemas import CreateScheduledTaskRequest, UpdateScheduledTaskRequest
from autowonder.scheduledtasks.service import (
    archive_task,
    create_task,
    delete_task,
    enable_task,
    get_task,
    list_runs,
    list_tasks,
    pause_task,
    preview_times,
    summarize_tasks,
    task_health,
    update_task,
)

router = APIRouter(
    tags=["scheduled-task-capability"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看定时任务能力"))],
)

task_router = APIRouter(
    prefix="/api/scheduled-tasks",
    tags=["scheduled-tasks"],
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看定时任务")),
        Depends(require_scheduled_capability),
    ],
)


@router.get("/api/capabilities/scheduled-task")
async def scheduled_task_capability() -> dict[str, Any]:
    """当前部署下定时任务是否可用。"""
    return ok(capability_snapshot())


def require_owner(owner_id: int | None) -> None:
    """创建者或工作空间管理员才能改任务。其他人是 10401。"""
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    if user_id != owner_id and current().access_level != WorkspaceAccessLevel.ADMIN.name:
        raise BizError(ErrorCode.UNAUTHORIZED)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


@task_router.get("")
async def list_scheduled_tasks(
    status: Annotated[str | None, Query()] = None,
    creator_id: Annotated[int | None, Query(alias="creatorId")] = None,
    squad_id: Annotated[int | None, Query(alias="squadId")] = None,
    keyword: Annotated[str | None, Query()] = None,
    size: Annotated[int, Query()] = 20,
    offset: Annotated[int, Query()] = 0,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """分页列出当前工作空间的定时任务。"""
    return ok(
        await list_tasks(
            session,
            _workspace_id(),
            status,
            creator_id,
            squad_id,
            keyword,
            size,
            offset,
        )
    )


@task_router.get("/preview")
async def preview_schedule(
    cron_expression: Annotated[str, Query(alias="cronExpression")],
    timezone: Annotated[str, Query()] = "Asia/Shanghai",
    count: Annotated[int, Query()] = 5,
) -> dict[str, Any]:
    """按服务端 cron 预览接下来的触发时间。"""
    _workspace_id()
    return ok(preview_times(cron_expression, timezone, count))


@task_router.get("/summary")
async def scheduled_task_summary(
    status: Annotated[str | None, Query()] = None,
    squad_id: Annotated[int | None, Query(alias="squadId")] = None,
    keyword: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """汇总运行中、今天、近 30 天和需要关注的任务。"""
    return ok(await summarize_tasks(session, _workspace_id(), status, squad_id, keyword))


@task_router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建定时任务"))],
)
async def create_scheduled_task(
    body: CreateScheduledTaskRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建定时任务。"""
    return ok(await create_task(session, body, _workspace_id(), _user_id()))


@task_router.get("/{id}")
async def get_scheduled_task(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """读取一个定时任务。"""
    return ok(await get_task(session, id, _workspace_id()))


@task_router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新定时任务"))],
)
async def update_scheduled_task(
    id: int,
    body: UpdateScheduledTaskRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新定时任务。只有创建者或管理员可以改。"""
    current_task = await get_task(session, id, _workspace_id())
    require_owner(current_task.creator_id)
    return ok(await update_task(session, id, body, _workspace_id(), _user_id()))


@task_router.post(
    "/{id}/enable",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "启用定时任务"))],
)
async def enable_scheduled_task(
    id: int,
    version: Annotated[int, Query()],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """启用暂停中的定时任务。"""
    current_task = await get_task(session, id, _workspace_id())
    require_owner(current_task.creator_id)
    return ok(await enable_task(session, id, version, _workspace_id(), _user_id()))


@task_router.post(
    "/{id}/pause",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "暂停定时任务"))],
)
async def pause_scheduled_task(
    id: int,
    version: Annotated[int, Query()],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """暂停运行中的定时任务。"""
    current_task = await get_task(session, id, _workspace_id())
    require_owner(current_task.creator_id)
    return ok(await pause_task(session, id, version, _workspace_id(), _user_id()))


@task_router.post(
    "/{id}/archive",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "归档定时任务"))],
)
async def archive_scheduled_task(
    id: int,
    version: Annotated[int, Query()],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """归档暂停或已耗尽的定时任务。"""
    current_task = await get_task(session, id, _workspace_id())
    require_owner(current_task.creator_id)
    return ok(await archive_task(session, id, version, _workspace_id(), _user_id()))


@task_router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除定时任务"))],
)
async def delete_scheduled_task(
    id: int,
    version: Annotated[int, Query()],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """软删除定时任务。先核对创建者，再执行删除。"""
    current_task = await get_task(session, id, _workspace_id())
    require_owner(current_task.creator_id)
    await delete_task(session, id, version, _workspace_id(), _user_id())
    return ok(None)


@task_router.get("/{id}/runs")
async def list_scheduled_task_runs(
    id: int,
    size: Annotated[int, Query()] = 20,
    offset: Annotated[int, Query()] = 0,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列出一个任务的运行实例。"""
    workspace_id = _workspace_id()
    await get_task(session, id, workspace_id)
    return ok(await list_runs(session, workspace_id, id, size, offset))


@task_router.get("/{id}/health")
async def scheduled_task_health(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """近 30 天的完成和成功次数。"""
    workspace_id = _workspace_id()
    await get_task(session, id, workspace_id)
    return ok(await task_health(session, workspace_id, id))
