"""执行器表读写。租户和未删除条件写在查询里，与 ExecutorDao 一致。"""

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.executors.models import Executor
from autowonder.squads.models import Squad, SquadMember


async def require_executor(session: AsyncSession, executor_id: int, tenant_id: int) -> Executor:
    """按主键取未删除执行器。不属于当前工作空间时与不存在同一错误。"""
    executor = await session.scalar(
        select(Executor).where(Executor.id == executor_id, Executor.is_deleted == 0).limit(1)
    )
    if executor is None or executor.tenant_id != tenant_id:
        raise BizError(ErrorCode.EXECUTOR_NOT_FOUND)
    return executor


async def list_executors(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int | None,
    squad_ids: list[int] | None,
) -> list[Executor]:
    """列出工作空间内的执行器。传入小队时只保留这些小队里的数字员工。"""
    statement = select(Executor).where(Executor.tenant_id == tenant_id, Executor.is_deleted == 0)
    if agent_id is not None:
        statement = statement.where(Executor.agent_id == agent_id)
    if squad_ids:
        members = (
            select(SquadMember.agent_id)
            .join(
                Squad,
                and_(Squad.id == SquadMember.squad_id, Squad.is_deleted == 0),
            )
            .where(
                SquadMember.tenant_id == tenant_id,
                SquadMember.squad_id.in_(squad_ids),
            )
        )
        statement = statement.where(Executor.agent_id.in_(members))
    statement = statement.order_by(Executor.id.desc())
    return list(await session.scalars(statement))
