"""证据和提案的读写。语句与 MyBatis 映射一致，调用方负责提交。"""

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.evolution.models import EvolutionEvidence, EvolutionProposal

LOOKBACK_LIMIT = 100


async def find_evidence_by_key(
    session: AsyncSession,
    tenant_id: int,
    idempotency_key: str,
) -> EvolutionEvidence | None:
    """按租户和幂等键取已有证据。"""
    statement = (
        select(EvolutionEvidence)
        .where(
            EvolutionEvidence.tenant_id == tenant_id,
            EvolutionEvidence.idempotency_key == idempotency_key,
        )
        .limit(1)
    )
    result = await session.execute(statement)
    return result.scalars().first()


async def find_latest_evidence(
    session: AsyncSession,
    tenant_id: int,
    asset_type: str,
    asset_id: int,
    posterior_type: str,
    context_key: str,
) -> EvolutionEvidence | None:
    """同一资产、后验和上下文上最新的一条证据。"""
    statement = (
        select(EvolutionEvidence)
        .where(
            EvolutionEvidence.tenant_id == tenant_id,
            EvolutionEvidence.asset_type == asset_type,
            EvolutionEvidence.asset_id == asset_id,
            EvolutionEvidence.posterior_type == posterior_type,
            EvolutionEvidence.context_key == context_key,
        )
        .order_by(EvolutionEvidence.id.desc())
        .limit(1)
    )
    result = await session.execute(statement)
    return result.scalars().first()


async def list_recent_evidence(
    session: AsyncSession,
    tenant_id: int,
    asset_type: str,
    asset_id: int,
    posterior_type: str,
) -> list[EvolutionEvidence]:
    """该资产某一维度最近的证据，新的在前。"""
    statement = (
        select(EvolutionEvidence)
        .where(
            EvolutionEvidence.tenant_id == tenant_id,
            EvolutionEvidence.asset_type == asset_type,
            EvolutionEvidence.asset_id == asset_id,
            EvolutionEvidence.posterior_type == posterior_type,
        )
        .order_by(EvolutionEvidence.id.desc())
        .limit(LOOKBACK_LIMIT)
    )
    result = await session.execute(statement)
    return list(result.scalars().all())


async def insert_evidence(session: AsyncSession, row: EvolutionEvidence) -> EvolutionEvidence:
    """插入证据并取回生成的主键。"""
    session.add(row)
    await session.flush()
    return row


async def find_proposal(session: AsyncSession, proposal_id: int) -> EvolutionProposal | None:
    """按主键读取未删除的提案。租户由调用方再核对。"""
    statement = (
        select(EvolutionProposal)
        .where(EvolutionProposal.id == proposal_id, EvolutionProposal.is_deleted == 0)
        .limit(1)
    )
    result = await session.execute(statement)
    return result.scalars().first()


async def insert_proposal(session: AsyncSession, row: EvolutionProposal) -> EvolutionProposal:
    """插入提案并取回生成的主键。"""
    session.add(row)
    await session.flush()
    return row


async def mark_proposal(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    status: str,
    lifecycle_json: object,
    version: int,
    user_id: int,
) -> int:
    """按版本推进提案状态。返回更新行数。"""
    statement = (
        update(EvolutionProposal)
        .where(
            EvolutionProposal.id == proposal_id,
            EvolutionProposal.tenant_id == tenant_id,
            EvolutionProposal.version == version,
            EvolutionProposal.is_deleted == 0,
        )
        .values(
            status=status,
            lifecycle_json=lifecycle_json,
            version=version + 1,
            modifier_id=user_id,
        )
    )
    result = await session.execute(statement)
    cursor: CursorResult[object] = result  # type: ignore[assignment]
    return cursor.rowcount
