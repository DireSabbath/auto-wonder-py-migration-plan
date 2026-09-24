"""平台数字人能力是否可调用。状态是只读快照，不预留执行器。"""

from typing import TypeGuard

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.agents.models import Agent, AgentVersion
from autowonder.agents.platform_status import platform_agent_statement
from autowonder.core.errors import IllegalArgumentError
from autowonder.core.schema import ApiModel
from autowonder.dispatch.selector import has_available_executor

NOT_CONFIGURED = "NOT_CONFIGURED"
AGENT_OFFLINE = "AGENT_OFFLINE"
VERSION_UNAVAILABLE = "VERSION_UNAVAILABLE"
RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
AVAILABLE = "AVAILABLE"


class CapabilityStatus(ApiModel):
    """能力快照。``available`` 仅在状态为 AVAILABLE 时为真。"""

    agent_id: int | None
    status: str
    available: bool


def version_statement(version_id: int) -> Select[tuple[AgentVersion]]:
    """按主键读取未删除版本。租户条件由查询时的隔离规则补上。"""
    return (
        select(AgentVersion)
        .where(AgentVersion.id == version_id, AgentVersion.is_deleted == 0)
        .limit(1)
    )


def require_positive_workspace(workspace_id: int | None) -> int:
    """工作空间必须是正整数。"""
    if workspace_id is None:
        raise IllegalArgumentError("workspaceId must be positive")
    if workspace_id <= 0:
        raise IllegalArgumentError("workspaceId must be positive")
    return workspace_id


def version_has_identity(version: AgentVersion) -> bool:
    """角色名、机器码、背景、职责或身份快照任一有内容即可。"""
    if _has_text(version.role_name):
        return True
    if _has_text(version.role_code):
        return True
    if _has_text(version.business_background):
        return True
    if _has_text(version.responsibilities):
        return True
    return _identity_present(version.identity_json)


async def get_capability_status(
    session: AsyncSession,
    workspace_id: int | None,
) -> CapabilityStatus:
    """按配置、在线版本和可调度执行器给出能力状态。"""
    checked = require_positive_workspace(workspace_id)
    agent = await session.scalar(platform_agent_statement(checked))
    if not _configured_platform_agent(agent, checked):
        return _status(None, NOT_CONFIGURED)
    if agent.status != "ONLINE":
        return _status(agent.id, AGENT_OFFLINE)
    version = None
    if agent.online_version_id is not None:
        version = await session.scalar(version_statement(agent.online_version_id))
    if not _usable_version(version, agent.id, checked):
        return _status(agent.id, VERSION_UNAVAILABLE)
    if await has_available_executor(agent.id):
        return _status(agent.id, AVAILABLE)
    return _status(agent.id, RUNTIME_UNAVAILABLE)


def _configured_platform_agent(agent: Agent | None, workspace_id: int) -> TypeGuard[Agent]:
    if agent is None:
        return False
    if agent.tenant_id != workspace_id:
        return False
    if agent.kind != "PLATFORM":
        return False
    if agent.is_deleted == 1:
        return False
    return True


def _usable_version(
    version: AgentVersion | None,
    agent_id: int,
    workspace_id: int,
) -> TypeGuard[AgentVersion]:
    if version is None:
        return False
    if version.agent_id != agent_id:
        return False
    if version.tenant_id != workspace_id:
        return False
    return version_has_identity(version)


def _status(agent_id: int | None, status: str) -> CapabilityStatus:
    return CapabilityStatus(
        agent_id=agent_id,
        status=status,
        available=status == AVAILABLE,
    )


def _has_text(value: str | None) -> bool:
    if value is None:
        return False
    return value != ""


def _identity_present(value: object) -> bool:
    """Java 把身份列当字符串；非空 JSON 文本算有身份，空串不算。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value != ""
    return True
