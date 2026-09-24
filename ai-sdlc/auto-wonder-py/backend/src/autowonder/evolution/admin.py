"""演进管理概览：最近的提案和证据。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.evolution.models import EvolutionEvidence, EvolutionProposal
from autowonder.evolution.store import list_recent_proposals, list_recent_tenant_evidence


async def overview(
    session: AsyncSession,
    tenant_id: int,
    limit: int | None,
) -> tuple[list[EvolutionProposal], list[EvolutionEvidence]]:
    """条数缺省 20，并且限制在 1 到 20。"""
    bounded = 20
    if limit is not None:
        bounded = limit
    if bounded < 1:
        bounded = 1
    if bounded > 20:
        bounded = 20
    proposals = await list_recent_proposals(session, tenant_id, bounded)
    evidence = await list_recent_tenant_evidence(session, tenant_id, bounded)
    return proposals, evidence
