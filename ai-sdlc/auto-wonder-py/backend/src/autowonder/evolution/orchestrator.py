"""证据入账之后，策略决定停在探索，还是起草提案并打开试验。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.bayes import decide
from autowonder.evolution.commands import OrchestrateCommand, OrchestrateResult, PolicyRequest
from autowonder.evolution.drafting import draft
from autowonder.evolution.evidence import record_event
from autowonder.evolution.jsontext import blank
from autowonder.evolution.routing import route
from autowonder.evolution.trial import start_trial


async def orchestrate(
    session: AsyncSession,
    command: OrchestrateCommand,
    tenant_id: int,
    user_id: int,
) -> OrchestrateResult:
    """记录证据并询问策略。需要演进时才创建提案和试验。"""
    event = command.evidence_event
    if event is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    if blank(command.candidate_asset_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    evidence = await record_event(session, event, tenant_id, user_id)
    policy = await decide(
        session,
        tenant_id,
        PolicyRequest(
            asset_type=event.asset_type,
            asset_id=event.asset_id,
            posterior_type=event.posterior_type,
            context_key=event.context_key,
        ),
    )
    result = OrchestrateResult(evidence_id=evidence.id, action=policy.action, policy=policy)
    if not policy.should_evolve:
        return result
    run = draft(command, event, policy)
    routed = await route(session, run, tenant_id, user_id)
    pattern = command.context_key
    if blank(pattern):
        pattern = event.context_key
    trial = await start_trial(session, routed.proposal_id, pattern, tenant_id, user_id)
    result.proposal_id = routed.proposal_id
    result.trial = trial
    result.proposal_status = trial.proposal_status
    result.action = "TRIAL_STARTED"
    return result
