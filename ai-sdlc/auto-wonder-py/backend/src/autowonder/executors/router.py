"""``/api`` 下的执行器用户接口。查看要求只读，变更要求管理员。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.executors.catalog import read_catalog
from autowonder.executors.launch import build_for_executor, get_launch_config, update_launch_config
from autowonder.executors.restart import request_restart
from autowonder.executors.schemas import (
    CreateExecutorRequest,
    ExecutorLaunchCommandRequest,
    RestartRequest,
    UpdateExecutorLaunchConfigRequest,
)
from autowonder.executors.service import (
    create_executor,
    delete_executor,
    executor_token,
    list_all,
    list_by_agent,
)
from autowonder.executors.upgrade import update_all, update_one

router = APIRouter(
    prefix="/api",
    tags=["executors"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看执行器"))],
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
    "/agents/{agentId}/executors",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "创建执行器"))],
)
async def create_agent_executor(
    agentId: int,
    body: CreateExecutorRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建执行器并返回一次性令牌。"""
    return ok(await create_executor(session, agentId, body, _workspace_id(), _user_id()))


@router.get("/agents/{agentId}/executors")
async def list_agent_executors(
    agentId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列出一个数字员工的执行器。"""
    return ok(await list_by_agent(session, agentId, _workspace_id()))


@router.get("/executors")
async def list_executors(
    squadIds: Annotated[list[int] | None, Query(alias="squadIds")] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列出当前工作空间的执行器，可按小队过滤。"""
    return ok(await list_all(session, _workspace_id(), squadIds))


@router.get("/executor-model-catalogs/{provider}")
async def model_catalog(provider: str) -> dict[str, Any]:
    """读取一个 provider 已经刷好的模型目录。"""
    return ok(await read_catalog(provider))


@router.post(
    "/executors/update-all",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "批量升级执行器"))],
)
async def update_all_executors(
    squadIds: Annotated[list[int] | None, Query(alias="squadIds")] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """给当前能看到的执行器排升级，跳过的原因一并返回。"""
    return ok(await update_all(session, _workspace_id(), squadIds, _user_id()))


@router.get(
    "/executors/{id}/token",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "获取执行器令牌"))],
)
async def get_executor_token(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """回显执行器令牌。"""
    return ok(await executor_token(session, id, _workspace_id()))


@router.get(
    "/executors/{id}/launch-config",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "查看执行器启动配置"))],
)
async def read_launch_config(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """读取启动配置。目录中失效的模型会在这里被清掉。"""
    return ok(await get_launch_config(session, id, _workspace_id(), _user_id()))


@router.put(
    "/executors/{id}/launch-config",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "更新执行器启动配置"))],
)
async def put_launch_config(
    id: int,
    body: UpdateExecutorLaunchConfigRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按版本号保存启动配置。"""
    return ok(await update_launch_config(session, id, _workspace_id(), body, _user_id()))


@router.post(
    "/executors/{id}/launch-command",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "生成执行器启动命令"))],
)
async def launch_command(
    id: int,
    body: ExecutorLaunchCommandRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按已保存的配置生成启动命令。请求体只选择系统和调试输出。"""
    request = body if body is not None else ExecutorLaunchCommandRequest()
    debug = request.debug is True
    return ok(
        await build_for_executor(
            session,
            id,
            _workspace_id(),
            request.os,
            debug,
            request.shell,
        )
    )


@router.delete(
    "/executors/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "删除执行器"))],
)
async def remove_executor(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除执行器。"""
    await delete_executor(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.post(
    "/executors/{id}/restart",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "重启执行器"))],
)
async def restart_executor(
    id: int,
    body: RestartRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """请求远程重启。update 为真时让客户端先更新发布版。"""
    update = False
    if body is not None:
        update = body.update
    return ok(await request_restart(session, id, _workspace_id(), _user_id(), update))


@router.post(
    "/executors/{id}/update",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "升级执行器"))],
)
async def update_executor(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """为单台执行器创建升级任务。"""
    return ok(await update_one(session, id, _workspace_id(), _user_id()))
