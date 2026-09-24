"""演进编排在服务之间传递的命令和结果。字段与 Java bean 对齐，未赋值即为 null。"""

from dataclasses import dataclass


@dataclass
class EvidenceEvent:
    """账本入账事件。"""

    asset_type: str | None = None
    asset_id: int | None = None
    posterior_type: str | None = None
    context_key: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    raw_outcome: str | None = None
    observation: float | None = None
    raw_event_json: str | None = None
    weight: float | None = None
    dependency_group: str | None = None
    idempotency_key: str | None = None


@dataclass
class EvidenceCommand:
    """一条已经归一化的贝叶斯证据。"""

    asset_type: str | None = None
    asset_id: int | None = None
    posterior_type: str | None = None
    context_key: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    outcome: str | None = None
    observation: float | None = None
    weight: float | None = None
    evidence_json: str | None = None
    dependency_group: str | None = None
    idempotency_key: str | None = None


@dataclass
class PolicyRequest:
    """策略只看技能相对同群的后验。"""

    asset_type: str | None = None
    asset_id: int | None = None
    posterior_type: str | None = None
    context_key: str | None = None
    min_effective_sample_size: float | None = None


@dataclass
class PolicyDecision:
    """一次策略选择。``policy_json`` 是交给提案和试验的原文。"""

    action: str
    should_evolve: bool
    reason_code: str
    reason: str
    target_context_key: str
    rewrite_brief: str
    policy_json: str
    confidence: float
    dominant_failure_mode: str | None = None
    posterior_mean: float | None = None
    effective_sample_size: float | None = None


@dataclass
class OrchestrateCommand:
    """一次从证据到试验的编排输入。"""

    evidence_event: EvidenceEvent | None = None
    candidate_asset_type: str | None = None
    candidate_asset_id: int | None = None
    root_evidence_json: str | None = None
    failure_summary: str | None = None
    suggested_patch_json: str | None = None
    draft_delta_json: str | None = None
    context_key: str | None = None
    source_agent_id: int | None = None
    auto_validate_before_replay: bool | None = None
    replay_suite_json: str | None = None


@dataclass
class RunCommand:
    """草案交给资产路由的运行命令。"""

    asset_type: str | None = None
    asset_id: int | None = None
    root_evidence_json: str | None = None
    policy_json: str | None = None
    failure_summary: str | None = None
    suggested_patch_json: str | None = None
    context_key: str | None = None
    source_agent_id: int | None = None


@dataclass
class ProposalCommand:
    """写入 ``evolution_proposal`` 的提案。"""

    asset_type: str | None = None
    asset_id: int | None = None
    trigger_type: str | None = None
    root_evidence_json: str | None = None
    policy_json: str | None = None
    candidate_patch_json: str | None = None


@dataclass
class RunResult:
    """路由创建提案后的标识。"""

    proposal_id: int
    status: str
    asset_type: str
    asset_id: int | None


@dataclass
class TrialDecision:
    """试验当前的采纳、拒绝或继续。"""

    proposal_id: int
    decision: str
    proposal_status: str
    reason_code: str
    task_pattern_key: str | None = None
    target_posterior_type: str | None = None
    baseline_posterior_mean: float | None = None
    baseline_effective_sample_size: float | None = None
    candidate_posterior_mean: float | None = None
    candidate_effective_sample_size: float | None = None
    reliability_guard_probability: float | None = None
    posterior_win_probability: float | None = None
    posterior_lose_probability: float | None = None
    expected_lift: float | None = None


@dataclass
class TrialEvidenceCommand:
    """试验臂上的一条结果。"""

    raw_outcome: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    weight: float | None = None
    evidence_json: str | None = None
    idempotency_key: str | None = None


@dataclass
class OrchestrateResult:
    """编排停在探索，或已经打开试验。"""

    evidence_id: int | None
    action: str
    policy: PolicyDecision
    proposal_id: int | None = None
    proposal_status: str | None = None
    trial: TrialDecision | None = None
