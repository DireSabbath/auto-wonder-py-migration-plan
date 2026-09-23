"""平台数字人 Chief of Staff 的执行器配置与在线状态。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.agents.models import Agent
from autowonder.core.schema import ApiModel
from autowonder.executors.models import Executor
from autowonder.executors.registry import is_online

STATE_OK = "OK"
STATE_OFFLINE = "OFFLINE"
STATE_NOT_CONFIGURED = "NOT_CONFIGURED"


class PlatformAgentStatus(ApiModel):
    """供管理端提示的状态快照。没有平台数字人时 agentId 为空。"""

    state: str
    agent_id: int | None
    executor_count: int
    online_executor_count: int


def platform_agent_statement(tenant_id: int) -> Select[tuple[Agent]]:
    """当前工作空间最早的一条未删除平台数字人。"""
    return (
        select(Agent)
        .where(
            Agent.tenant_id == tenant_id,
            Agent.kind == "PLATFORM",
            Agent.is_deleted == 0,
        )
        .order_by(Agent.id.asc())
        .limit(1)
    )


def executors_for_agent_statement(tenant_id: int, agent_id: int) -> Select[tuple[Executor]]:
    """该数字员工未删除的执行器，按 id 倒序。"""
    return (
        select(Executor)
        .where(
            Executor.tenant_id == tenant_id,
            Executor.agent_id == agent_id,
            Executor.is_deleted == 0,
        )
        .order_by(Executor.id.desc())
    )


def platform_agent_status(agent_id: int | None, executor_ids: list[int]) -> PlatformAgentStatus:
    """没有数字人或没有任何执行器时为未配置；有执行器但都不在线时为离线。"""
    if agent_id is None:
        return PlatformAgentStatus(
            state=STATE_NOT_CONFIGURED,
            agent_id=None,
            executor_count=0,
            online_executor_count=0,
        )
    total = len(executor_ids)
    online = 0
    for executor_id in executor_ids:
        if is_online(executor_id):
            online = online + 1
    if total == 0:
        state = STATE_NOT_CONFIGURED
    elif online == 0:
        state = STATE_OFFLINE
    else:
        state = STATE_OK
    return PlatformAgentStatus(
        state=state,
        agent_id=agent_id,
        executor_count=total,
        online_executor_count=online,
    )


async def get_platform_agent_status(
    session: AsyncSession,
    tenant_id: int,
) -> PlatformAgentStatus:
    """读取平台数字人及其执行器，再按在线会话汇总。"""
    agent = await session.scalar(platform_agent_statement(tenant_id))
    if agent is None:
        return platform_agent_status(None, [])
    rows = await session.scalars(executors_for_agent_statement(tenant_id, agent.id))
    return platform_agent_status(agent.id, [row.id for row in rows])
