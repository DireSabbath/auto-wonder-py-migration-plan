"""把提案、证据和编排结果收成与 Jackson 相同的 camelCase 字段。"""

from autowonder.evolution.commands import OrchestrateResult, PolicyDecision, TrialDecision
from autowonder.evolution.jsontext import dump_json
from autowonder.evolution.models import EvolutionEvidence, EvolutionProposal


def proposal_data(row: EvolutionProposal) -> dict[str, object]:
    """列里的 JSON 以文本返回，生命周期阶段同时展开成独立字段。"""
    lifecycle = row.lifecycle_json
    return {
        "id": row.id,
        "tenantId": row.tenant_id,
        "assetType": row.asset_type,
        "assetId": row.asset_id,
        "triggerType": row.trigger_type,
        "rootEvidenceJson": _json_text(row.root_evidence_json),
        "policyJson": _json_text(row.policy_json),
        "candidatePatchJson": _json_text(row.candidate_patch_json),
        "status": row.status,
        "lifecycleJson": _json_text(lifecycle),
        "gmtCreate": row.gmt_create,
        "gmtModified": row.gmt_modified,
        "creatorId": row.creator_id,
        "modifierId": row.modifier_id,
        "isDeleted": row.is_deleted,
        "version": row.version,
        "validationJson": _stage_text(lifecycle, "validation"),
        "replayJson": _stage_text(lifecycle, "replay"),
        "gateJson": _stage_text(lifecycle, "gates"),
        "releaseJson": _stage_text(lifecycle, "release"),
        "rollbackJson": _stage_text(lifecycle, "rollback"),
        "trialJson": _stage_text(lifecycle, "trial"),
    }


def evidence_data(row: EvolutionEvidence) -> dict[str, object]:
    """证据 JSON 保持文本，时间留给信封转成毫秒。"""
    return {
        "id": row.id,
        "tenantId": row.tenant_id,
        "assetType": row.asset_type,
        "assetId": row.asset_id,
        "posteriorType": row.posterior_type,
        "contextKey": row.context_key,
        "sourceType": row.source_type,
        "sourceRef": row.source_ref,
        "outcome": row.outcome,
        "weight": row.weight,
        "evidenceJson": _json_text(row.evidence_json),
        "dependencyGroup": row.dependency_group,
        "idempotencyKey": row.idempotency_key,
        "alpha": row.alpha,
        "beta": row.beta,
        "posteriorMean": row.posterior_mean,
        "effectiveSampleSize": row.effective_sample_size,
        "gmtCreate": row.gmt_create,
        "creatorId": row.creator_id,
    }


def orchestrate_data(result: OrchestrateResult) -> dict[str, object]:
    """编排结果。当前编排器不填写回放结论。"""
    return {
        "evidenceId": result.evidence_id,
        "policyDecision": _policy_data(result.policy),
        "proposalId": result.proposal_id,
        "proposalStatus": result.proposal_status,
        "replayVerdict": None,
        "trialDecision": _trial_data(result.trial),
        "action": result.action,
    }


def trial_data(decision: TrialDecision) -> dict[str, object]:
    """试验决策。未计算的概率保持 null。"""
    return _trial_fields(decision)


def _policy_data(policy: PolicyDecision) -> dict[str, object]:
    return {
        "action": policy.action,
        "shouldEvolve": policy.should_evolve,
        "confidence": policy.confidence,
        "reasonCode": policy.reason_code,
        "reason": policy.reason,
        "targetContextKey": policy.target_context_key,
        "dominantFailureMode": policy.dominant_failure_mode,
        "rewriteBrief": policy.rewrite_brief,
        "posteriorMean": policy.posterior_mean,
        "effectiveSampleSize": policy.effective_sample_size,
        "policyJson": policy.policy_json,
    }


def _trial_data(decision: TrialDecision | None) -> dict[str, object] | None:
    if decision is None:
        return None
    return _trial_fields(decision)


def _trial_fields(decision: TrialDecision) -> dict[str, object]:
    return {
        "proposalId": decision.proposal_id,
        "decision": decision.decision,
        "proposalStatus": decision.proposal_status,
        "reasonCode": decision.reason_code,
        "taskPatternKey": decision.task_pattern_key,
        "baselinePosteriorMean": decision.baseline_posterior_mean,
        "baselineEffectiveSampleSize": decision.baseline_effective_sample_size,
        "candidatePosteriorMean": decision.candidate_posterior_mean,
        "candidateEffectiveSampleSize": decision.candidate_effective_sample_size,
        "posteriorWinProbability": decision.posterior_win_probability,
        "posteriorLoseProbability": decision.posterior_lose_probability,
        "expectedLift": decision.expected_lift,
        "targetPosteriorType": decision.target_posterior_type,
        "reliabilityGuardProbability": decision.reliability_guard_probability,
    }


def _json_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return dump_json(value)


def _stage_text(lifecycle: object, name: str) -> str | None:
    if not isinstance(lifecycle, dict):
        return None
    value = lifecycle.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return dump_json(value)
