"""假设试验：开试验时只定义对照臂，证据够了再采纳或拒绝。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.bayes import (
    ACTION_ASSET_ID,
    ACTION_ASSET_TYPE,
    BetaPosterior,
    action_posterior_type,
    compare,
    posterior,
)
from autowonder.evolution.commands import EvidenceCommand, TrialDecision, TrialEvidenceCommand
from autowonder.evolution.evidence import normalize_outcome, record_evidence
from autowonder.evolution.jsontext import as_dict, blank, dump_json, java_trim, text_field
from autowonder.evolution.models import EvolutionEvidence, EvolutionProposal
from autowonder.evolution.store import (
    find_evidence_by_key,
    find_latest_evidence,
    find_proposal,
    mark_proposal,
)

TARGET_MIN_LIFT = 0.02
RELIABILITY_MARGIN = 0.05
ADOPT_PROBABILITY = 0.90
REJECT_PROBABILITY = 0.80
MIN_ARM_SAMPLE = 5.0


async def start_trial(
    session: AsyncSession,
    proposal_id: int,
    task_pattern_key: str | None,
    tenant_id: int,
    user_id: int,
) -> TrialDecision:
    """把 PROPOSED 或 VALIDATED 提案标成 TRIAL，并写下对照臂。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    if proposal.status != "PROPOSED" and proposal.status != "VALIDATED":
        raise BizError(ErrorCode.CONFLICT)
    pattern = _require_pattern(task_pattern_key)
    target = _target_posterior_type(proposal)
    trial: dict[str, object] = {
        "taskPatternKey": pattern,
        "targetPosteriorType": target,
        "decision": "CONTINUE_TRIAL",
        "reasonCode": "trial_started",
        "baselineArm": {"assetType": "TRIAL_BASELINE", "assetId": proposal_id},
        "candidateArm": {"assetType": "TRIAL_CANDIDATE", "assetId": proposal_id},
        "decisionRule": {
            "minArmEffectiveSampleSize": MIN_ARM_SAMPLE,
            "targetMinLift": TARGET_MIN_LIFT,
            "adoptProbability": ADOPT_PROBABILITY,
            "rejectProbability": REJECT_PROBABILITY,
            "reliabilityNonInferiorityMargin": RELIABILITY_MARGIN,
        },
    }
    lifecycle = _with_trial(proposal.lifecycle_json, trial)
    proposal.lifecycle_json = lifecycle
    updated = await mark_proposal(
        session,
        proposal_id,
        tenant_id,
        "TRIAL",
        lifecycle,
        proposal.version,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.CONFLICT)
    return _decision(proposal_id, "CONTINUE_TRIAL", "TRIAL", "trial_started", pattern, target)


async def record_outcome(
    session: AsyncSession,
    proposal_id: int,
    command: TrialEvidenceCommand | None,
    tenant_id: int,
    user_id: int,
) -> TrialDecision:
    """兼容入口：把一条结果记到候选臂，再重新裁决。"""
    proposal = await _require_trial_proposal(session, proposal_id, tenant_id)
    trial = _require_trial(proposal)
    if command is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    source_ref = command.source_ref
    source_type = command.source_type
    if source_ref is None or blank(source_ref):
        raise BizError(ErrorCode.PARAM_INVALID)
    if source_type is None or blank(source_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    if blank(command.raw_outcome):
        raise BizError(ErrorCode.PARAM_INVALID)
    outcome = normalize_outcome(command.raw_outcome)
    observation = 0.0
    if outcome == "POSITIVE":
        observation = 1.0
    idempotency_key = command.idempotency_key
    if idempotency_key is None or blank(idempotency_key):
        idempotency_key = "proposal:" + str(proposal_id) + ":" + source_ref
    evidence = EvidenceCommand(
        asset_type="TRIAL_CANDIDATE",
        asset_id=proposal_id,
        posterior_type=text_field(trial, "targetPosteriorType"),
        context_key=text_field(trial, "taskPatternKey"),
        source_type=source_type,
        source_ref=source_ref,
        outcome=outcome,
        observation=observation,
        weight=command.weight,
        evidence_json=command.evidence_json,
        dependency_group="proposal:" + str(proposal_id),
        idempotency_key=idempotency_key,
    )
    await record_evidence(session, evidence, tenant_id, user_id)
    return await decide(session, proposal_id, tenant_id, user_id)


async def decide(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    user_id: int,
) -> TrialDecision:
    """样本不足就继续；候选打赢目标且可靠性不差才采纳。"""
    proposal = await _require_trial_proposal(session, proposal_id, tenant_id)
    trial = _require_trial(proposal)
    context = text_field(trial, "taskPatternKey")
    target_type = text_field(trial, "targetPosteriorType")
    if context is None or target_type is None:
        raise BizError(ErrorCode.CONFLICT)
    baseline_target = await _latest(
        session, tenant_id, "TRIAL_BASELINE", proposal_id, target_type, context
    )
    candidate_target = await _latest(
        session, tenant_id, "TRIAL_CANDIDATE", proposal_id, target_type, context
    )
    if target_type == "RELIABILITY":
        baseline_reliability = baseline_target
        candidate_reliability = candidate_target
    else:
        baseline_reliability = await _latest(
            session, tenant_id, "TRIAL_BASELINE", proposal_id, "RELIABILITY", context
        )
        candidate_reliability = await _latest(
            session, tenant_id, "TRIAL_CANDIDATE", proposal_id, "RELIABILITY", context
        )
    if not _enough(baseline_target, candidate_target) or not _enough(
        baseline_reliability, candidate_reliability
    ):
        return _decision(
            proposal_id,
            "CONTINUE_TRIAL",
            "TRIAL",
            "insufficient_arm_evidence",
            context,
            target_type,
            baseline_target,
            candidate_target,
        )
    target_comparison = compare(
        _beta(baseline_target),
        _beta(candidate_target),
        TARGET_MIN_LIFT,
    )
    reliability_guard = compare(
        _beta(baseline_reliability),
        _beta(candidate_reliability),
        -RELIABILITY_MARGIN,
    )
    reliability_harm = compare(
        _beta(baseline_reliability),
        _beta(candidate_reliability),
        RELIABILITY_MARGIN,
    )
    result: TrialDecision | None = None
    if (
        target_comparison.win_probability >= ADOPT_PROBABILITY
        and reliability_guard.win_probability >= ADOPT_PROBABILITY
    ):
        result = _decision(
            proposal_id,
            "ADOPT",
            "TRIAL_ADOPTED",
            "candidate_beats_live_baseline",
            context,
            target_type,
            baseline_target,
            candidate_target,
            reliability_guard.win_probability,
            target_comparison.win_probability,
            target_comparison.lose_probability,
            target_comparison.expected_lift,
        )
    elif reliability_harm.lose_probability >= REJECT_PROBABILITY:
        result = _decision(
            proposal_id,
            "REJECT",
            "REJECTED",
            "candidate_breaks_reliability_guardrail",
            context,
            target_type,
            baseline_target,
            candidate_target,
            reliability_guard.win_probability,
            target_comparison.win_probability,
            target_comparison.lose_probability,
            target_comparison.expected_lift,
        )
    elif target_comparison.lose_probability >= REJECT_PROBABILITY:
        result = _decision(
            proposal_id,
            "REJECT",
            "REJECTED",
            "candidate_underperforms_live_baseline",
            context,
            target_type,
            baseline_target,
            candidate_target,
            reliability_guard.win_probability,
            target_comparison.win_probability,
            target_comparison.lose_probability,
            target_comparison.expected_lift,
        )
    if result is None:
        return _decision(
            proposal_id,
            "CONTINUE_TRIAL",
            "TRIAL",
            "posterior_still_uncertain",
            context,
            target_type,
            baseline_target,
            candidate_target,
            reliability_guard.win_probability,
            target_comparison.win_probability,
            target_comparison.lose_probability,
            target_comparison.expected_lift,
        )
    await _persist_decision(session, proposal, trial, result, tenant_id, user_id)
    await _record_action_outcome(session, proposal, trial, result, tenant_id, user_id)
    return result


async def decide_if_active(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    user_id: int,
) -> TrialDecision | None:
    """试验已经结束时，晚到的遥测不再改裁决。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    if proposal.status != "TRIAL":
        return None
    return await decide(session, proposal_id, tenant_id, user_id)


def _decision(
    proposal_id: int,
    value: str,
    status: str,
    reason: str,
    context: str | None = None,
    target_type: str | None = None,
    baseline: EvolutionEvidence | None = None,
    candidate: EvolutionEvidence | None = None,
    reliability_guard: float | None = None,
    win_probability: float | None = None,
    lose_probability: float | None = None,
    expected_lift: float | None = None,
) -> TrialDecision:
    baseline_mean = None
    baseline_sample = None
    candidate_mean = None
    candidate_sample = None
    if baseline is not None:
        baseline_mean = baseline.posterior_mean
        baseline_sample = baseline.effective_sample_size
    if candidate is not None:
        candidate_mean = candidate.posterior_mean
        candidate_sample = candidate.effective_sample_size
    return TrialDecision(
        proposal_id=proposal_id,
        decision=value,
        proposal_status=status,
        reason_code=reason,
        task_pattern_key=context,
        target_posterior_type=target_type,
        baseline_posterior_mean=baseline_mean,
        baseline_effective_sample_size=baseline_sample,
        candidate_posterior_mean=candidate_mean,
        candidate_effective_sample_size=candidate_sample,
        reliability_guard_probability=reliability_guard,
        posterior_win_probability=win_probability,
        posterior_lose_probability=lose_probability,
        expected_lift=expected_lift,
    )


async def _persist_decision(
    session: AsyncSession,
    proposal: EvolutionProposal,
    trial: dict[str, object],
    decision: TrialDecision,
    tenant_id: int,
    user_id: int,
) -> None:
    trial["decision"] = decision.decision
    trial["reasonCode"] = decision.reason_code
    trial["targetPosteriorType"] = decision.target_posterior_type
    trial["baselinePosteriorMean"] = decision.baseline_posterior_mean
    trial["baselineEffectiveSampleSize"] = decision.baseline_effective_sample_size
    trial["candidatePosteriorMean"] = decision.candidate_posterior_mean
    trial["candidateEffectiveSampleSize"] = decision.candidate_effective_sample_size
    trial["posteriorWinProbability"] = decision.posterior_win_probability
    trial["posteriorLoseProbability"] = decision.posterior_lose_probability
    trial["expectedLift"] = decision.expected_lift
    trial["reliabilityGuardProbability"] = decision.reliability_guard_probability
    lifecycle = _with_trial(proposal.lifecycle_json, trial)
    proposal.lifecycle_json = lifecycle
    updated = await mark_proposal(
        session,
        proposal.id,
        tenant_id,
        decision.proposal_status,
        lifecycle,
        proposal.version,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.CONFLICT)


async def _record_action_outcome(
    session: AsyncSession,
    proposal: EvolutionProposal,
    trial: dict[str, object],
    decision: TrialDecision,
    tenant_id: int,
    user_id: int,
) -> None:
    action = _policy_action(proposal)
    if action is None or blank(action):
        return
    idempotency_key = "proposal:" + str(proposal.id) + ":action:" + action + ":decision"
    existing = await find_evidence_by_key(session, tenant_id, idempotency_key)
    if existing is not None:
        return
    outcome = "NEGATIVE"
    if decision.decision == "ADOPT":
        outcome = "POSITIVE"
    evidence = EvidenceCommand(
        asset_type=ACTION_ASSET_TYPE,
        asset_id=ACTION_ASSET_ID,
        posterior_type=action_posterior_type(action),
        context_key=text_field(trial, "taskPatternKey"),
        source_type="BAYESIAN_TRIAL_DECISION",
        source_ref="proposal:" + str(proposal.id) + ":trial",
        outcome=outcome,
        weight=1.0,
        evidence_json=dump_json(
            {
                "proposalId": proposal.id,
                "action": action,
                "trialDecision": decision.decision,
                "targetPosteriorType": decision.target_posterior_type,
            }
        ),
        dependency_group="proposal:" + str(proposal.id),
        idempotency_key=idempotency_key,
    )
    await record_evidence(session, evidence, tenant_id, user_id)


def _target_posterior_type(proposal: EvolutionProposal) -> str:
    policy = as_dict(proposal.policy_json)
    if (
        isinstance(proposal.policy_json, str)
        and policy is None
        and not blank(proposal.policy_json)
    ):
        return "RELIABILITY"
    target = None
    action = None
    if policy is not None:
        target = text_field(policy, "targetPosteriorType")
        action = text_field(policy, "action")
    if target is None or blank(target):
        return _default_target(action)
    return target


def _default_target(action: str | None) -> str:
    if action is not None and action.upper() == "COMPRESS":
        return "TOKEN_EFFICIENCY"
    return "RELIABILITY"


def _policy_action(proposal: EvolutionProposal) -> str | None:
    policy = as_dict(proposal.policy_json)
    if policy is None:
        return None
    return text_field(policy, "action")


async def _require_trial_proposal(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
) -> EvolutionProposal:
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    if proposal.status != "TRIAL":
        raise BizError(ErrorCode.CONFLICT)
    return proposal


async def _require_proposal(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
) -> EvolutionProposal:
    proposal = await find_proposal(session, proposal_id)
    if proposal is None or proposal.tenant_id != tenant_id:
        raise BizError(ErrorCode.NOT_FOUND)
    return proposal


def _require_trial(proposal: EvolutionProposal) -> dict[str, object]:
    lifecycle = proposal.lifecycle_json
    if not isinstance(lifecycle, dict):
        raise BizError(ErrorCode.CONFLICT)
    trial = lifecycle.get("trial")
    if not isinstance(trial, dict):
        raise BizError(ErrorCode.CONFLICT)
    if blank(text_field(trial, "taskPatternKey")) or blank(
        text_field(trial, "targetPosteriorType")
    ):
        raise BizError(ErrorCode.CONFLICT)
    return trial


def _require_pattern(value: str | None) -> str:
    if value is None or blank(value):
        raise BizError(ErrorCode.PARAM_INVALID)
    return java_trim(value)


def _with_trial(lifecycle_json: object, trial: dict[str, object]) -> dict[str, object]:
    lifecycle: dict[str, object] = {}
    if isinstance(lifecycle_json, dict):
        lifecycle = dict(lifecycle_json)
    lifecycle["trial"] = trial
    return lifecycle


async def _latest(
    session: AsyncSession,
    tenant_id: int,
    asset_type: str,
    proposal_id: int,
    posterior_type: str,
    context: str,
) -> EvolutionEvidence | None:
    return await find_latest_evidence(
        session,
        tenant_id,
        asset_type,
        proposal_id,
        posterior_type,
        context,
    )


def _enough(baseline: EvolutionEvidence | None, candidate: EvolutionEvidence | None) -> bool:
    return _sample(baseline) >= MIN_ARM_SAMPLE and _sample(candidate) >= MIN_ARM_SAMPLE


def _sample(evidence: EvolutionEvidence | None) -> float:
    if evidence is None:
        return 0.0
    return evidence.effective_sample_size


def _beta(evidence: EvolutionEvidence | None) -> BetaPosterior:
    if evidence is None:
        return posterior(None, None, None, None)
    return posterior(
        evidence.alpha,
        evidence.beta,
        evidence.posterior_mean,
        evidence.effective_sample_size,
    )
