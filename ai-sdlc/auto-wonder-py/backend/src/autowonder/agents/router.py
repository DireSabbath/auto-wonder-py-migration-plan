"""``/api/agents``。查看要求只读，改员工要求读写。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.schemas import (
    CreateAgentRequest,
    MemoryRefRequest,
    RepoPermRequest,
    ReviewRequest,
    RollbackRequest,
    SkillRequest,
    UpdateAgentRequest,
    UpdateConfigRequest,
)
from autowonder.agents.service import (
    add_environment_variable_ref,
    add_memory_ref,
    add_repo_perm,
    add_skill,
    approve_agent,
    count_pending_reviews,
    create_agent,
    delete_agent,
    edit_config,
    get_agent,
    get_version,
    list_agents,
    list_memory_refs,
    list_versions,
    offline_agent,
    online_agent,
    reject_agent,
    remove_environment_variable_ref,
    remove_memory_ref,
    remove_repo_perm,
    remove_skill,
    rollback_agent,
    submit_agent,
    update_agent,
)
from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.squads.service import list_squads_by_agent

router = APIRouter(
    prefix="/api/agents",
    tags=["agents"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看智能体"))],
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
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建智能体"))],
)
async def create(
    body: CreateAgentRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建数字员工。"""
    return ok(await create_agent(session, body, _workspace_id(), _user_id()))


@router.get(
    "/reviews/count",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "查看待审核数量"))],
)
async def count_reviews(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """待审核数量。"""
    return ok(await count_pending_reviews(session, _workspace_id()))


@router.get("")
async def list_page(
    session: AsyncSession = Depends(get_session),
    status: str | None = None,
    kind: str | None = None,
    squad_ids: Annotated[list[int] | None, Query(alias="squadIds")] = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页列出数字员工。"""
    return ok(await list_agents(session, _workspace_id(), status, kind, squad_ids, page, size))


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """数字员工详情。"""
    return ok(await get_agent(session, id, _workspace_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除智能体"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除数字员工。"""
    await delete_agent(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.patch(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新智能体"))],
)
async def update(
    id: int,
    body: UpdateAgentRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新名称、头像和角色文案。"""
    return ok(await update_agent(session, id, body, _workspace_id(), _user_id()))


@router.put(
    "/{id}/config",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "编辑智能体配置"))],
)
async def config(
    id: int,
    body: UpdateConfigRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """编辑草稿配置。"""
    return ok(await edit_config(session, id, body, _workspace_id(), _user_id()))


@router.post(
    "/{id}/submit",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "提交智能体审核"))],
)
async def submit(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """提交审核。"""
    return ok(await submit_agent(session, id, _workspace_id(), _user_id()))


@router.post(
    "/{id}/approve",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "通过智能体审核"))],
)
async def approve(
    id: int,
    body: ReviewRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """通过审核。"""
    return ok(await approve_agent(session, id, _workspace_id(), _user_id(), body.comment))


@router.post(
    "/{id}/reject",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "驳回智能体审核"))],
)
async def reject(
    id: int,
    body: ReviewRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """驳回审核。"""
    return ok(await reject_agent(session, id, _workspace_id(), _user_id(), body.comment))


@router.post(
    "/{id}/rollback",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "回滚智能体版本"))],
)
async def rollback(
    id: int,
    body: RollbackRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """回退到已通过版本。"""
    return ok(await rollback_agent(session, id, body.version_no, _workspace_id(), _user_id()))


@router.post(
    "/{id}/offline",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "下线智能体"))],
)
async def offline(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """下线。"""
    return ok(await offline_agent(session, id, _workspace_id(), _user_id()))


@router.post(
    "/{id}/online",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "上线智能体"))],
)
async def online(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """重新上线。"""
    return ok(await online_agent(session, id, _workspace_id(), _user_id()))


@router.get("/{id}/versions")
async def versions(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """版本摘要。"""
    return ok(await list_versions(session, id, _workspace_id()))


@router.get("/{id}/versions/{versionNo}")
async def version_detail(
    id: int,
    versionNo: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """某个版本的完整配置。"""
    return ok(await get_version(session, id, versionNo, _workspace_id()))


@router.post(
    "/{id}/repos",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "添加智能体仓库权限"))],
)
async def add_repo(
    id: int,
    body: RepoPermRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """添加仓库权限。"""
    await add_repo_perm(session, id, body, _workspace_id(), _user_id())
    return ok(None)


@router.delete(
    "/{id}/repos/{repoId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "移除智能体仓库权限"))],
)
async def remove_repo(
    id: int,
    repoId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """移除仓库权限。"""
    await remove_repo_perm(session, id, repoId, _workspace_id(), _user_id())
    return ok(None)


@router.post(
    "/{id}/skills",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "添加智能体技能"))],
)
async def add_agent_skill(
    id: int,
    body: SkillRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """添加技能。"""
    await add_skill(session, id, body, _workspace_id(), _user_id())
    return ok(None)


@router.delete(
    "/{id}/skills/{skillId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "移除智能体技能"))],
)
async def remove_agent_skill(
    id: int,
    skillId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """移除技能。"""
    await remove_skill(session, id, skillId, _workspace_id(), _user_id())
    return ok(None)


@router.post(
    "/{id}/memories",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "添加智能体记忆"))],
)
async def add_memory(
    id: int,
    body: MemoryRefRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """添加记忆。"""
    await add_memory_ref(session, id, body, _workspace_id(), _user_id())
    return ok(None)


@router.delete(
    "/{id}/memories/{memoryId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "移除智能体记忆"))],
)
async def remove_memory(
    id: int,
    memoryId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """移除记忆。"""
    await remove_memory_ref(session, id, memoryId, _workspace_id(), _user_id())
    return ok(None)


@router.get("/{id}/memories")
async def memories(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """当前展示版本上的记忆。"""
    return ok(await list_memory_refs(session, id))


@router.post(
    "/{id}/environment-variables/{environmentVariableId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "挂载智能体环境变量"))],
)
async def add_environment_variable(
    id: int,
    environmentVariableId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """挂载环境变量。"""
    await add_environment_variable_ref(
        session,
        id,
        environmentVariableId,
        _workspace_id(),
        _user_id(),
    )
    return ok(None)


@router.delete(
    "/{id}/environment-variables/{environmentVariableId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "解绑智能体环境变量"))],
)
async def remove_environment_variable(
    id: int,
    environmentVariableId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """解绑环境变量。"""
    await remove_environment_variable_ref(
        session,
        id,
        environmentVariableId,
        _workspace_id(),
        _user_id(),
    )
    return ok(None)


@router.get("/{id}/squads")
async def squads(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """数字员工所在小队。"""
    return ok(await list_squads_by_agent(session, id))
