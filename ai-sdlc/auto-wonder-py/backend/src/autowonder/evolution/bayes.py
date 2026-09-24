"""技能相对同群的贝叶斯策略，以及试验动作的探索分数。"""

import math
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.commands import PolicyDecision, PolicyRequest
from autowonder.evolution.jsontext import as_dict, blank, dump_json, java_trim
from autowonder.evolution.models import EvolutionEvidence
from autowonder.evolution.store import find_latest_evidence, list_recent_evidence

DECISION_PROBABILITY = 0.80
RELIABILITY_MARGIN = 0.05
DEFAULT_MIN_SAMPLE = 5.0
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0
ACTION_ASSET_TYPE = "SKILL_ACTION"
ACTION_ASSET_ID = 0
DIMENSIONS = (
    "RELIABILITY",
    "TOKEN_EFFICIENCY",
    "TURN_EFFICIENCY",
    "REPAIR_EFFICIENCY",
    "TOOL_EFFICIENCY",
    "ALIGNMENT",
)


@dataclass
class BetaPosterior:
    """Beta 后验的两个参数。"""

    alpha: float
    beta: float

    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def effective_sample_size(self) -> float:
        return self.alpha + self.beta - PRIOR_ALPHA - PRIOR_BETA

    def variance(self) -> float:
        total = self.alpha + self.beta
        return (self.alpha * self.beta) / (total * total * (total + 1.0))


@dataclass
class PosteriorComparison:
    """候选相对基线的胜负概率和期望提升。"""

    win_probability: float
    lose_probability: float
    expected_lift: float


@dataclass
class ActionSelection:
    """合格动作里探索分数最高的一个。"""

    action: str
    posterior_mean: float
    effective_sample_size: float
    exploration_score: float


@dataclass
class ContextShape:
    """同一技能在多个任务模式上的可靠性形状。"""

    poor_context_count: int
    healthy_context_count: int
    divergence_probability: float


def posterior(
    alpha: float | None,
    beta: float | None,
    mean: float | None,
    effective_sample_size: float | None,
) -> BetaPosterior:
    """有正的 alpha/beta 时直接用；否则用均值和样本量还原。"""
    if alpha is not None and beta is not None and alpha > 0 and beta > 0:
        return BetaPosterior(alpha, beta)
    safe_mean = 0.5
    if mean is not None:
        safe_mean = mean
        if safe_mean < 0.01:
            safe_mean = 0.01
        if safe_mean > 0.99:
            safe_mean = 0.99
    sample = 0.0
    if effective_sample_size is not None and effective_sample_size > 0.0:
        sample = effective_sample_size
    total = sample + PRIOR_ALPHA + PRIOR_BETA
    return BetaPosterior(safe_mean * total, (1.0 - safe_mean) * total)


def compare(
    baseline: BetaPosterior,
    candidate: BetaPosterior,
    min_lift: float,
) -> PosteriorComparison:
    """正态近似下，候选超过基线至少 ``min_lift`` 的概率。"""
    expected_lift = candidate.mean() - baseline.mean()
    stddev = math.sqrt(max(1e-9, baseline.variance() + candidate.variance()))
    win_probability = 1.0 - _normal_cdf((min_lift - expected_lift) / stddev)
    lose_probability = _normal_cdf((-min_lift - expected_lift) / stddev)
    return PosteriorComparison(win_probability, lose_probability, expected_lift)


def action_posterior_type(action: str) -> str:
    """动作效用在证据表里的后验类型。"""
    return "ACTION_" + action


async def select_action(
    session: AsyncSession,
    tenant_id: int,
    task_pattern_key: str,
    eligible_actions: list[str],
) -> ActionSelection:
    """未试过的动作靠后验标准差上界获得探索机会。"""
    if blank(task_pattern_key) or len(eligible_actions) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    best = await _selection(session, tenant_id, task_pattern_key, eligible_actions[0])
    for action in eligible_actions[1:]:
        current = await _selection(session, tenant_id, task_pattern_key, action)
        if current.exploration_score > best.exploration_score:
            best = current
    return best


async def decide(
    session: AsyncSession,
    tenant_id: int,
    request: PolicyRequest,
) -> PolicyDecision:
    """样本不够就继续探索；可信劣化才进入修补、拆分、退役或压缩。"""
    _validate_request(request)
    context_key = request.context_key
    asset_id = request.asset_id
    if context_key is None or asset_id is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    min_sample = DEFAULT_MIN_SAMPLE
    if request.min_effective_sample_size is not None:
        min_sample = request.min_effective_sample_size
    skill = await _load(session, tenant_id, "SKILL", asset_id)
    cohort = await _load(session, tenant_id, "SKILL_COHORT", 0)
    reliability = _pair(skill, cohort, "RELIABILITY", context_key)
    if not _comparable(reliability, min_sample):
        return _explore(
            "insufficient_comparative_evidence",
            context_key,
            reliability,
            {},
            0.0,
            ContextShape(0, 0, 0.0),
        )
    deficits: dict[str, float] = {}
    for dimension in DIMENSIONS:
        current = _pair(skill, cohort, dimension, context_key)
        if _comparable(current, min_sample):
            deficits[dimension] = _deficit_probability(current)
    reliability_deficit = _deficit_of(deficits, "RELIABILITY")
    repair_deficit = _deficit_of(deficits, "REPAIR_EFFICIENCY")
    tool_deficit = _deficit_of(deficits, "TOOL_EFFICIENCY")
    token_deficit = _deficit_of(deficits, "TOKEN_EFFICIENCY")
    turn_deficit = _deficit_of(deficits, "TURN_EFFICIENCY")
    non_inferior = _non_inferior_probability(reliability)
    shape = _reliability_shape(
        skill.get("RELIABILITY"),
        cohort.get("RELIABILITY"),
        context_key,
        min_sample,
    )
    eligible: list[str] = []
    reason = ""
    if _highest(reliability_deficit, repair_deficit, tool_deficit) >= DECISION_PROBABILITY:
        eligible.append("PATCH")
        if shape.poor_context_count >= 2 and shape.healthy_context_count == 0:
            eligible.append("RETIRE")
            reason = "credible_multi_context_deficit"
        elif shape.divergence_probability >= DECISION_PROBABILITY:
            eligible.append("SPLIT")
            reason = "credible_context_divergence"
        else:
            reason = "credible_localized_deficit"
    elif (
        non_inferior >= DECISION_PROBABILITY
        and _highest(token_deficit, turn_deficit) >= DECISION_PROBABILITY
    ):
        eligible.append("COMPRESS")
        reason = "reliability_non_inferior_efficiency_deficit"
    else:
        return _explore(
            "posterior_not_actionable",
            context_key,
            reliability,
            deficits,
            non_inferior,
            shape,
        )
    selected = await select_action(session, tenant_id, context_key, eligible)
    return _action_decision(
        selected,
        eligible,
        reason,
        context_key,
        reliability,
        deficits,
        non_inferior,
        shape,
    )


async def _selection(
    session: AsyncSession,
    tenant_id: int,
    task_pattern_key: str,
    action: str,
) -> ActionSelection:
    normalized = java_trim(action).upper()
    if blank(normalized):
        raise BizError(ErrorCode.PARAM_INVALID)
    latest = await find_latest_evidence(
        session,
        tenant_id,
        ACTION_ASSET_TYPE,
        ACTION_ASSET_ID,
        action_posterior_type(normalized),
        task_pattern_key,
    )
    drawn = _posterior_of(latest)
    exploration_score = drawn.mean() + math.sqrt(max(0.0, drawn.variance()))
    if exploration_score > 1.0:
        exploration_score = 1.0
    return ActionSelection(
        normalized,
        drawn.mean(),
        drawn.effective_sample_size(),
        exploration_score,
    )


async def _load(
    session: AsyncSession,
    tenant_id: int,
    asset_type: str,
    asset_id: int,
) -> dict[str, dict[str, EvolutionEvidence]]:
    result: dict[str, dict[str, EvolutionEvidence]] = {}
    for dimension in DIMENSIONS:
        rows = await list_recent_evidence(session, tenant_id, asset_type, asset_id, dimension)
        by_context: dict[str, EvolutionEvidence] = {}
        for row in rows:
            if blank(row.context_key):
                continue
            if row.context_key not in by_context:
                by_context[row.context_key] = row
        result[dimension] = by_context
    return result


def _reliability_shape(
    skill: dict[str, EvolutionEvidence] | None,
    cohort: dict[str, EvolutionEvidence] | None,
    target_context: str,
    min_sample: float,
) -> ContextShape:
    if skill is None or cohort is None:
        return ContextShape(0, 0, 0.0)
    poor = 0
    healthy = 0
    divergence = 0.0
    target = skill.get(target_context)
    for context in skill:
        if context not in cohort:
            continue
        current = (skill.get(context), cohort.get(context))
        if not _comparable(current, min_sample):
            continue
        if _deficit_probability(current) >= DECISION_PROBABILITY:
            poor += 1
        if _non_inferior_probability(current) >= DECISION_PROBABILITY:
            healthy += 1
        other = skill.get(context)
        if target is None or context == target_context or other is None:
            continue
        if _sample(target) < min_sample or _sample(other) < min_sample:
            continue
        gap = compare(_posterior_of(target), _posterior_of(other), 0.0).win_probability
        if gap > divergence:
            divergence = gap
    return ContextShape(poor, healthy, divergence)


def _pair(
    skill: dict[str, dict[str, EvolutionEvidence]],
    cohort: dict[str, dict[str, EvolutionEvidence]],
    dimension: str,
    context: str,
) -> tuple[EvolutionEvidence | None, EvolutionEvidence | None]:
    skill_row = None
    cohort_row = None
    skill_dimension = skill.get(dimension)
    cohort_dimension = cohort.get(dimension)
    if skill_dimension is not None:
        skill_row = skill_dimension.get(context)
    if cohort_dimension is not None:
        cohort_row = cohort_dimension.get(context)
    return skill_row, cohort_row


def _comparable(
    pair: tuple[EvolutionEvidence | None, EvolutionEvidence | None],
    min_sample: float,
) -> bool:
    skill_row, cohort_row = pair
    if skill_row is None or cohort_row is None:
        return False
    if _sample(skill_row) < min_sample:
        return False
    return _sample(cohort_row) >= min_sample


def _deficit_probability(
    pair: tuple[EvolutionEvidence | None, EvolutionEvidence | None],
) -> float:
    skill_row, cohort_row = pair
    return compare(_posterior_of(skill_row), _posterior_of(cohort_row), 0.0).win_probability


def _non_inferior_probability(
    pair: tuple[EvolutionEvidence | None, EvolutionEvidence | None],
) -> float:
    skill_row, cohort_row = pair
    return compare(
        _posterior_of(cohort_row),
        _posterior_of(skill_row),
        -RELIABILITY_MARGIN,
    ).win_probability


def _posterior_of(row: EvolutionEvidence | None) -> BetaPosterior:
    if row is None:
        return posterior(None, None, None, None)
    return posterior(row.alpha, row.beta, row.posterior_mean, row.effective_sample_size)


def _action_decision(
    selection: ActionSelection,
    eligible_actions: list[str],
    reason: str,
    context: str,
    reliability: tuple[EvolutionEvidence | None, EvolutionEvidence | None],
    deficits: dict[str, float],
    non_inferior: float,
    shape: ContextShape,
) -> PolicyDecision:
    decision = _base_decision(selection.action, True, reason, context, reliability)
    policy = as_dict(decision.policy_json)
    if policy is None:
        policy = {}
    policy.update(_comparison_json(deficits, non_inferior, shape))
    policy["eligibleActions"] = eligible_actions
    policy["targetPosteriorType"] = _target_dimension(selection.action, deficits)
    policy["actionPosteriorMean"] = selection.posterior_mean
    policy["actionEffectiveSampleSize"] = selection.effective_sample_size
    policy["actionExplorationScore"] = selection.exploration_score
    decision.policy_json = dump_json(policy)
    return decision


def _target_dimension(action: str, deficits: dict[str, float]) -> str:
    if action == "COMPRESS":
        candidates = ["TOKEN_EFFICIENCY", "TURN_EFFICIENCY"]
    elif action == "PATCH":
        candidates = ["RELIABILITY", "REPAIR_EFFICIENCY", "TOOL_EFFICIENCY"]
    else:
        candidates = ["RELIABILITY"]
    best = candidates[0]
    for current in candidates:
        if _deficit_of(deficits, current) > _deficit_of(deficits, best):
            best = current
    return best


def _explore(
    reason: str,
    context: str,
    reliability: tuple[EvolutionEvidence | None, EvolutionEvidence | None],
    deficits: dict[str, float],
    non_inferior: float,
    shape: ContextShape,
) -> PolicyDecision:
    decision = _base_decision("EXPLORE", False, reason, context, reliability)
    policy = as_dict(decision.policy_json)
    if policy is None:
        policy = {}
    policy.update(_comparison_json(deficits, non_inferior, shape))
    decision.policy_json = dump_json(policy)
    return decision


def _base_decision(
    action: str,
    evolve: bool,
    reason: str,
    context: str,
    reliability: tuple[EvolutionEvidence | None, EvolutionEvidence | None],
) -> PolicyDecision:
    skill_row, _cohort_row = reliability
    failure = _failure_mode(skill_row)
    brief = _rewrite_brief(action, context, failure)
    posterior_mean = None
    sample_size = None
    if skill_row is not None:
        posterior_mean = skill_row.posterior_mean
        sample_size = skill_row.effective_sample_size
    sample = _sample(skill_row)
    confidence = sample / (sample + 10.0)
    if confidence > 0.99:
        confidence = 0.99
    policy = {
        "action": action,
        "reasonCode": reason,
        "targetContextKey": context,
        "rewriteBrief": brief,
        "posteriorMean": posterior_mean,
        "effectiveSampleSize": sample_size,
        "decisionProbability": DECISION_PROBABILITY,
        "reliabilityNonInferiorityMargin": RELIABILITY_MARGIN,
    }
    return PolicyDecision(
        action=action,
        should_evolve=evolve,
        reason_code=reason,
        reason=reason,
        target_context_key=context,
        rewrite_brief=brief,
        policy_json=dump_json(policy),
        confidence=confidence,
        dominant_failure_mode=failure,
        posterior_mean=posterior_mean,
        effective_sample_size=sample_size,
    )


def _comparison_json(
    deficits: dict[str, float],
    non_inferior: float,
    shape: ContextShape,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for dimension, value in deficits.items():
        key = dimension.lower() + "DeficitProbability"
        if dimension == "RELIABILITY":
            key = "reliabilityDeficitProbability"
        result[key] = value
    result["reliabilityNonInferiorProbability"] = non_inferior
    result["poorContextCount"] = shape.poor_context_count
    result["healthyContextCount"] = shape.healthy_context_count
    result["contextDivergenceProbability"] = shape.divergence_probability
    return result


def _rewrite_brief(action: str, context: str, failure_mode: str | None) -> str:
    if action == "EXPLORE":
        return "Collect more comparable session evidence before changing the Skill."
    focus = ""
    if failure_mode is not None and not blank(failure_mode):
        focus = " Focus on " + failure_mode + "."
    if action == "PATCH":
        return "Patch the Skill for " + context + "." + focus
    if action == "SPLIT":
        return (
            "Split the Skill at the "
            + context
            + " boundary and preserve healthy contexts."
            + focus
        )
    if action == "RETIRE":
        return "Trial the bundle without this Skill because it is poor across contexts." + focus
    if action == "COMPRESS":
        return "Compress the Skill while preserving reliability for " + context + "."
    return "Prepare a bounded Skill candidate from posterior evidence."


def _failure_mode(evidence: EvolutionEvidence | None) -> str | None:
    if evidence is None:
        return None
    parsed = as_dict(evidence.evidence_json)
    if parsed is None:
        return None
    category = parsed.get("failureCategory")
    if isinstance(category, str):
        return category
    return None


def _sample(evidence: EvolutionEvidence | None) -> float:
    if evidence is None:
        return 0.0
    return evidence.effective_sample_size


def _deficit_of(deficits: dict[str, float], dimension: str) -> float:
    value = deficits.get(dimension)
    if value is None:
        return 0.0
    return value


def _highest(*values: float) -> float:
    highest = 0.0
    for value in values:
        if value > highest:
            highest = value
    return highest


def _validate_request(request: PolicyRequest) -> None:
    asset_type = request.asset_type
    if asset_type is None or asset_type.upper() != "SKILL":
        raise BizError(ErrorCode.PARAM_INVALID)
    if request.asset_id is None or request.asset_id <= 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    if blank(request.context_key):
        raise BizError(ErrorCode.PARAM_INVALID)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + _erf(value / math.sqrt(2.0)))


def _erf(x: float) -> float:
    sign = 1.0
    if x < 0:
        sign = -1.0
    abs_x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * abs_x)
    polynomial = (
        (((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736
    ) * t + 0.254829592
    return sign * (1.0 - polynomial * t * math.exp(-abs_x * abs_x))
