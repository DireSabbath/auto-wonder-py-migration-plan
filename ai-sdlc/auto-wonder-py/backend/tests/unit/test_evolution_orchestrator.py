"""演进增量从证据账本走到试验，向量对齐 Java lite 服务测试。"""

import json
from datetime import datetime

import pytest
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList

from autowonder.core.errors import BizError
from autowonder.evolution.bayes import decide
from autowonder.evolution.commands import (
    EvidenceEvent,
    OrchestrateCommand,
    PolicyDecision,
    PolicyRequest,
    RunCommand,
)
from autowonder.evolution.delta import build_orchestrate_command
from autowonder.evolution.drafting import draft
from autowonder.evolution.evidence import record_event
from autowonder.evolution.jsontext import dump_json
from autowonder.evolution.models import EvolutionEvidence, EvolutionProposal
from autowonder.evolution.orchestrator import orchestrate
from autowonder.evolution.routing import build_memory_proposal, build_skill_proposal, route
from autowonder.evolution.trial import decide as decide_trial
from autowonder.evolution.trial import decide_if_active


class MemorySession:
    """用等值条件回放证据和提案，不连接 MySQL。"""

    def __init__(self) -> None:
        self.evidence: list[EvolutionEvidence] = []
        self.proposals: list[EvolutionProposal] = []
        self._pending: list[object] = []
        self._next_id = 1

    def add(self, row: object) -> None:
        self._pending.append(row)

    async def flush(self) -> None:
        row = self._pending.pop()
        if getattr(row, "id", None) is None:
            row.id = self._next_id
            self._next_id += 1
        if isinstance(row, EvolutionEvidence):
            self.evidence.append(row)
        if isinstance(row, EvolutionProposal):
            self.proposals.append(row)

    async def execute(self, statement: object) -> object:
        if statement.__class__.__name__ == "Update":
            return _Cursor(self._update(statement))
        return _SelectResult(self._rows(statement))

    def _rows(self, statement: object) -> list[object]:
        entity = statement.column_descriptions[0]["entity"]
        comps = _comparisons(statement.whereclause)
        if entity is EvolutionEvidence:
            rows: list[object] = [row for row in self.evidence if _matches(row, comps)]
            rows.sort(key=lambda row: row.id, reverse=True)
        elif entity is EvolutionProposal:
            rows = [row for row in self.proposals if _matches(row, comps)]
        else:
            rows = []
        limit = _limit(statement)
        if limit is not None:
            return rows[:limit]
        return rows

    def _update(self, statement: object) -> int:
        values = {column.key: bind.value for column, bind in statement._values.items()}
        comps = _comparisons(statement.whereclause)
        count = 0
        for row in self.proposals:
            if not _matches(row, comps):
                continue
            for key, value in values.items():
                setattr(row, key, value)
            count += 1
        return count


class _SelectResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_Scalars":
        return _Scalars(self._rows)


class _Scalars:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def first(self) -> object | None:
        if len(self._rows) == 0:
            return None
        return self._rows[0]

    def all(self) -> list[object]:
        return list(self._rows)


class _Cursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def _comparisons(clause: object) -> list[tuple[str, str, object]]:
    found: list[tuple[str, str, object]] = []
    _walk(clause, found)
    return found


def _walk(node: object, found: list[tuple[str, str, object]]) -> None:
    if isinstance(node, BooleanClauseList):
        for child in node.clauses:
            _walk(child, found)
        return
    left = getattr(node, "left", None)
    key = getattr(left, "key", None)
    operator = getattr(getattr(node, "operator", None), "__name__", "")
    right = getattr(node, "right", None)
    if isinstance(key, str) and isinstance(right, BindParameter):
        found.append((key, operator, right.value))


def _matches(row: object, comps: list[tuple[str, str, object]]) -> bool:
    for key, operator, value in comps:
        if operator == "eq" and getattr(row, key) != value:
            return False
    return True


def _limit(statement: object) -> int | None:
    clause = getattr(statement, "_limit_clause", None)
    if clause is None:
        return None
    if isinstance(clause, BindParameter) and isinstance(clause.value, int):
        return clause.value
    value = getattr(clause, "value", None)
    if isinstance(value, int):
        return value
    element = getattr(clause, "element", None)
    if isinstance(element, BindParameter) and isinstance(element.value, int):
        return element.value
    return None


def _evidence(
    asset_type: str,
    context: str,
    alpha: float,
    beta: float,
    *,
    posterior_type: str = "RELIABILITY",
    asset_id: int | None = None,
) -> EvolutionEvidence:
    resolved_id = 9 if asset_type == "SKILL" else 0
    if asset_id is not None:
        resolved_id = asset_id
    return EvolutionEvidence(
        tenant_id=1,
        asset_type=asset_type,
        asset_id=resolved_id,
        posterior_type=posterior_type,
        context_key=context,
        source_type="REPLAY_RESULT",
        source_ref="artifact:test",
        outcome="NEGATIVE",
        weight=1.0,
        evidence_json={"failureCategory": "agent_execution"},
        alpha=alpha,
        beta=beta,
        posterior_mean=alpha / (alpha + beta),
        effective_sample_size=alpha + beta - 2,
        gmt_create=datetime(2026, 9, 24, 8, 0, 0),
    )


def _remember(session: MemorySession, row: EvolutionEvidence) -> None:
    row.id = session._next_id
    session._next_id += 1
    session.evidence.append(row)


def _pair(
    session: MemorySession,
    posterior_type: str,
    context: str,
    skill: tuple[float, float],
    cohort: tuple[float, float],
) -> None:
    _remember(
        session,
        _evidence("SKILL", context, skill[0], skill[1], posterior_type=posterior_type),
    )
    _remember(
        session,
        _evidence(
            "SKILL_COHORT",
            context,
            cohort[0],
            cohort[1],
            posterior_type=posterior_type,
        ),
    )


def _neutral(session: MemorySession, posterior_type: str, context: str) -> None:
    _pair(session, posterior_type, context, (8, 4), (8, 4))


def _request(context: str) -> PolicyRequest:
    return PolicyRequest(
        asset_type="SKILL",
        asset_id=9,
        posterior_type="RELIABILITY",
        context_key=context,
    )


def _event() -> EvidenceEvent:
    return EvidenceEvent(
        asset_type="SKILL",
        asset_id=9,
        posterior_type="UTILITY",
        context_key="repo:checkout",
        source_type="DETERMINISTIC_TEST",
        source_ref="artifact:test-log-1",
        raw_outcome="FAIL",
        raw_event_json='{"test":"checkout smoke","status":"FAIL"}',
        dependency_group="dispatch:44:test",
        idempotency_key="dispatch:44:event:test-failed",
    )


def test_delta_command_keeps_skill_patch_and_synthesized_event() -> None:
    """工人增量补上缺省来源、幂等键和原始事件。"""
    payload = json.loads(
        """
        {
          "candidates": [
            {
              "assetType": "SKILL",
              "assetId": 88,
              "posteriorType": "UTILITY",
              "contextKey": "multi_repo_refactor",
              "failureMode": "repo_map_not_loaded",
              "taskType": "code_change",
              "harness": "codex_worker",
              "failureSummary": "旧 Skill 在多 repo 修改时没有先加载 repo-map",
              "suggestedPatch": {
                "mode": "CREATE",
                "name": "multi-repo-refactor-safety",
                "type": "CODING",
                "installSpec": "skill://multi-repo-refactor-safety",
                "description": "Before multi-repo edits, load repo-map."
              }
            }
          ]
        }
        """
    )
    command = build_orchestrate_command(payload["candidates"][0], 30, 99, 0, "ASSISTED")
    assert command.candidate_asset_type == "SKILL"
    assert command.candidate_asset_id == 88
    assert command.failure_summary == "旧 Skill 在多 repo 修改时没有先加载 repo-map"
    assert command.suggested_patch_json is not None
    assert '"mode":"CREATE"' in command.suggested_patch_json
    event = command.evidence_event
    assert event is not None
    assert event.asset_type == "SKILL"
    assert event.asset_id == 88
    assert event.posterior_type == "UTILITY"
    assert event.context_key == "multi_repo_refactor"
    assert event.source_type == "MODEL_SELF_REPORT"
    assert event.source_ref == "dispatch:99:evolution:0"
    assert event.raw_outcome == "FAIL"
    assert event.idempotency_key == "dispatch:99:evolution:0"
    assert event.raw_event_json is not None
    assert '"failureMode":"repo_map_not_loaded"' in event.raw_event_json
    assert '"harness":"codex_worker"' in event.raw_event_json


def test_delta_defaults_memory_scope_to_the_reporting_agent() -> None:
    """记忆候选没写归属时，归到上报的数字员工。"""
    candidate = {
        "assetType": "MEMORY",
        "assetId": 1,
        "contextKey": "repo:auto-wonder",
        "suggestedPatch": {
            "title": "Prefer lean memory",
            "contentMd": "Default worker memory should belong to the reporting agent.",
        },
    }
    command = build_orchestrate_command(candidate, 30, 99, 0, "ASSISTED")
    assert command.source_agent_id == 30
    assert command.suggested_patch_json is not None
    assert '"scope":"AGENT"' in command.suggested_patch_json
    assert '"ownerRef":30' in command.suggested_patch_json


def test_delta_accepts_outcome_alias_and_derives_task_pattern() -> None:
    """outcome 别名和任务三元组都会进证据事件。"""
    positive = build_orchestrate_command(
        {
            "assetType": "SKILL",
            "assetId": 88,
            "contextKey": "repo:auto-wonder",
            "outcome": "POSITIVE",
            "suggestedPatch": {"mode": "UPDATE", "name": "safe-skill"},
        },
        30,
        99,
        0,
        "ASSISTED",
    )
    assert positive.evidence_event is not None
    assert positive.evidence_event.raw_outcome == "POSITIVE"
    derived = build_orchestrate_command(
        {
            "assetType": "SKILL",
            "assetId": 88,
            "taskType": "Coding",
            "primaryRepoGroup": "Monorepo",
            "operation": "Checkout",
            "suggestedPatch": {"mode": "UPDATE", "name": "repo-checkout-safety"},
        },
        30,
        99,
        0,
        "ASSISTED",
    )
    assert derived.context_key == "coding:monorepo:checkout"
    assert derived.evidence_event is not None
    assert derived.evidence_event.context_key == "coding:monorepo:checkout"
    assert derived.evidence_event.raw_event_json is not None
    assert '"taskPatternKey":"coding:monorepo:checkout"' in derived.evidence_event.raw_event_json


def test_delta_create_without_source_skill_uses_zero_and_auto_validates() -> None:
    """CREATE 可以没有具体技能编号；自动提案在有回放集时默认先验证。"""
    created = build_orchestrate_command(
        {
            "assetType": "SKILL",
            "posteriorType": "UTILITY",
            "contextKey": "coding:monorepo:checkout",
            "candidateAssetType": "SKILL",
            "suggestedPatch": {
                "mode": "CREATE",
                "name": "monorepo-checkout-safety",
                "type": "CODING",
                "installSpec": "skill://monorepo-checkout-safety",
                "description": "Checkout and validate monorepo workspaces before edits.",
            },
        },
        30,
        99,
        0,
        "ASSISTED",
    )
    assert created.candidate_asset_id == 0
    assert created.evidence_event is not None
    assert created.evidence_event.asset_id == 0
    validated = build_orchestrate_command(
        {
            "assetType": "SKILL",
            "assetId": 88,
            "contextKey": "repo:auto-wonder",
            "replaySuiteJson": '{"cases":["old-failure"]}',
            "suggestedPatch": {"mode": "UPDATE", "name": "safe-skill"},
        },
        30,
        99,
        0,
        "AUTO_PROPOSAL",
    )
    assert validated.auto_validate_before_replay is True


@pytest.mark.asyncio
async def test_ledger_normalizes_outcome_and_reuses_idempotency_key() -> None:
    """失败别名收成 NEGATIVE；相同幂等键不重复入账。"""
    session = MemorySession()
    evidence = await record_event(session, _event(), 1, 2)
    assert evidence.asset_type == "SKILL"
    assert evidence.outcome == "NEGATIVE"
    assert evidence.dependency_group == "dispatch:44:test"
    assert evidence.idempotency_key == "dispatch:44:event:test-failed"
    assert evidence.weight == 1.0
    assert evidence.alpha == 1.0
    assert evidence.beta == 2.0
    again = _event()
    again.dependency_group = None
    again.raw_outcome = " fail "
    again.idempotency_key = "dispatch:44:event:test-failed"
    derived = await record_event(session, again, 1, 2)
    assert derived.id == evidence.id
    assert len(session.evidence) == 1


@pytest.mark.asyncio
async def test_ledger_derives_dependency_group_for_a_new_key() -> None:
    """没给分组时用来源类型和来源引用拼接。"""
    session = MemorySession()
    event = _event()
    event.dependency_group = None
    event.idempotency_key = "dispatch:44:event:other"
    event.raw_outcome = " fail "
    evidence = await record_event(session, event, 1, 2)
    assert evidence.dependency_group == "DETERMINISTIC_TEST:artifact:test-log-1"
    assert evidence.outcome == "NEGATIVE"


@pytest.mark.asyncio
async def test_ledger_records_each_asset_usage_entry() -> None:
    """assetUsage 里每个参与资产各记一条，并带上参与度。"""
    session = MemorySession()
    event = _event()
    event.raw_event_json = json.dumps(
        {
            "taskPatternKey": "coding:monorepo:checkout",
            "assetUsage": [
                {
                    "assetType": "SKILL",
                    "assetId": 9,
                    "participation": "PROVEN",
                    "outcomeQuality": "VERIFIED",
                },
                {"assetType": "MEMORY", "assetId": 12, "participation": "EXPOSED"},
                {"assetType": "REPO_RELATION", "assetId": 33, "participation": "ENGAGED"},
            ],
        }
    )
    await record_event(session, event, 1, 2)
    assert len(session.evidence) == 3
    engaged = [
        row for row in session.evidence if row.asset_type == "REPO_RELATION" and row.asset_id == 33
    ]
    assert len(engaged) == 1
    assert engaged[0].weight > 0.99
    rendered = dump_json(engaged[0].evidence_json)
    assert '"participation":"ENGAGED"' in rendered


@pytest.mark.asyncio
async def test_soft_observation_updates_both_posterior_parameters() -> None:
    """0 到 1 的观察同时推动 alpha 和 beta。"""
    session = MemorySession()
    event = _event()
    event.observation = 0.8
    event.weight = 0.5
    evidence = await record_event(session, event, 1, 2)
    assert evidence.alpha == pytest.approx(1.4)
    assert evidence.beta == pytest.approx(1.1)


@pytest.mark.asyncio
async def test_policy_explores_when_comparative_evidence_is_sparse() -> None:
    """任一侧样本量不够时继续探索。"""
    session = MemorySession()
    _pair(session, "RELIABILITY", "coding:repo:test", (2, 2), (8, 2))
    decision = await decide(session, 1, _request("coding:repo:test"))
    assert decision.action == "EXPLORE"
    assert decision.reason_code == "insufficient_comparative_evidence"
    assert decision.should_evolve is False


@pytest.mark.asyncio
async def test_policy_patches_a_localized_reliability_deficit() -> None:
    """技能相对同群明显更差时只给出 PATCH。"""
    session = MemorySession()
    _pair(session, "RELIABILITY", "coding:repo:test", (2, 12), (12, 2))
    _neutral(session, "TOKEN_EFFICIENCY", "coding:repo:test")
    _neutral(session, "TURN_EFFICIENCY", "coding:repo:test")
    _neutral(session, "REPAIR_EFFICIENCY", "coding:repo:test")
    _neutral(session, "TOOL_EFFICIENCY", "coding:repo:test")
    decision = await decide(session, 1, _request("coding:repo:test"))
    assert decision.action == "PATCH"
    assert decision.should_evolve is True
    assert "reliabilityDeficitProbability" in decision.policy_json
    parsed = json.loads(decision.policy_json)
    assert parsed["eligibleActions"] == ["PATCH"]


@pytest.mark.asyncio
async def test_policy_splits_when_contexts_diverge_and_retire_when_all_poor() -> None:
    """上下文分化选 SPLIT；多个上下文都差选 RETIRE。"""
    split = MemorySession()
    _pair(split, "RELIABILITY", "coding:repo:test", (2, 12), (12, 2))
    _pair(split, "RELIABILITY", "coding:repo:review", (12, 2), (10, 3))
    _neutral(split, "TOKEN_EFFICIENCY", "coding:repo:test")
    _neutral(split, "TURN_EFFICIENCY", "coding:repo:test")
    _neutral(split, "REPAIR_EFFICIENCY", "coding:repo:test")
    _neutral(split, "TOOL_EFFICIENCY", "coding:repo:test")
    _remember(
        split,
        _evidence(
            "SKILL_ACTION",
            "coding:repo:test",
            40,
            2,
            posterior_type="ACTION_SPLIT",
            asset_id=0,
        ),
    )
    split_decision = await decide(split, 1, _request("coding:repo:test"))
    assert split_decision.action == "SPLIT"
    assert "contextDivergenceProbability" in split_decision.policy_json

    retire = MemorySession()
    _pair(retire, "RELIABILITY", "coding:repo:test", (2, 12), (12, 2))
    _pair(retire, "RELIABILITY", "coding:repo:review", (2, 11), (11, 2))
    _neutral(retire, "TOKEN_EFFICIENCY", "coding:repo:test")
    _neutral(retire, "TURN_EFFICIENCY", "coding:repo:test")
    _neutral(retire, "REPAIR_EFFICIENCY", "coding:repo:test")
    _neutral(retire, "TOOL_EFFICIENCY", "coding:repo:test")
    _remember(
        retire,
        _evidence(
            "SKILL_ACTION",
            "coding:repo:test",
            40,
            2,
            posterior_type="ACTION_RETIRE",
            asset_id=0,
        ),
    )
    retire_decision = await decide(retire, 1, _request("coding:repo:test"))
    assert retire_decision.action == "RETIRE"
    assert '"poorContextCount":2' in retire_decision.policy_json


@pytest.mark.asyncio
async def test_policy_compresses_when_reliability_holds_and_tokens_do_not() -> None:
    """可靠性不差但 token 明显更费时压缩。"""
    session = MemorySession()
    _pair(session, "RELIABILITY", "coding:repo:test", (12, 2), (10, 4))
    _pair(session, "TOKEN_EFFICIENCY", "coding:repo:test", (2, 12), (12, 2))
    _pair(session, "TURN_EFFICIENCY", "coding:repo:test", (8, 4), (8, 4))
    _neutral(session, "REPAIR_EFFICIENCY", "coding:repo:test")
    _neutral(session, "TOOL_EFFICIENCY", "coding:repo:test")
    decision = await decide(session, 1, _request("coding:repo:test"))
    assert decision.action == "COMPRESS"
    parsed = json.loads(decision.policy_json)
    assert parsed["eligibleActions"] == ["COMPRESS"]


def test_draft_builds_memory_repo_and_keeps_explicit_skill_patch() -> None:
    """没有补丁时记忆用失败摘要，仓库关系用证据字段，显式技能补丁原样保留。"""
    policy = PolicyDecision(
        action="PATCH",
        should_evolve=True,
        reason_code="repeated_context_failure",
        reason="repeated_context_failure",
        target_context_key="repo:checkout",
        rewrite_brief="Patch the Skill for repo:checkout.",
        policy_json='{"action":"PATCH"}',
        confidence=0.5,
    )
    event = EvidenceEvent(
        asset_type="SKILL",
        asset_id=9,
        posterior_type="UTILITY",
        context_key="repo:checkout",
        source_type="REPLAY_RESULT",
        source_ref="dispatch:44",
        raw_outcome="FAIL",
        raw_event_json='{"status":"FAIL"}',
        idempotency_key="dispatch:44:fail",
    )
    memory = OrchestrateCommand(
        candidate_asset_type="MEMORY",
        root_evidence_json='[{"sourceType":"REPLAY_RESULT","sourceRef":"dispatch:44"}]',
        context_key="repo:checkout",
        failure_summary="checkout fails because repo relation is stale",
    )
    drafted = draft(memory, event, policy)
    assert drafted.asset_type == "MEMORY"
    assert drafted.suggested_patch_json is not None
    assert "Learning from checkout failure" in drafted.suggested_patch_json
    assert "checkout fails because repo relation is stale" in drafted.suggested_patch_json
    assert '"type":"FACT"' in drafted.suggested_patch_json

    skill = OrchestrateCommand(
        candidate_asset_type="SKILL",
        candidate_asset_id=9,
        suggested_patch_json=(
            '{"name":"checkout","type":"CODEX_SKILL",'
            '"installSpec":"skill://checkout-v2","description":"Use repo map first"}'
        ),
        context_key="repo:checkout",
    )
    kept = draft(skill, event, policy)
    assert kept.suggested_patch_json == skill.suggested_patch_json
    assert kept.asset_id == 9

    repo_event = EvidenceEvent(
        raw_event_json=(
            '{"fromRepoId":10,"toRepoId":11,"relationType":"DEPENDS_ON",'
            '"description":"checkout frontend uses checkout api"}'
        ),
        context_key="repo:checkout",
        source_type="REPLAY_RESULT",
        source_ref="dispatch:44",
    )
    repo = draft(
        OrchestrateCommand(
            candidate_asset_type="REPO_RELATION",
            context_key="repo:checkout",
            root_evidence_json=memory.root_evidence_json,
        ),
        repo_event,
        policy,
    )
    assert repo.suggested_patch_json is not None
    assert '"fromRepoId":10' in repo.suggested_patch_json
    assert '"relationType":"DEPENDS_ON"' in repo.suggested_patch_json


def test_skill_policy_action_overrides_the_worker_mode() -> None:
    """SPLIT 和 CREATE 把补丁收成新建，不管工人写的是 UPDATE。"""
    command = RunCommand(
        asset_type="SKILL",
        asset_id=9,
        policy_json='{"action":"CREATE"}',
        root_evidence_json='[{"sourceType":"REPLAY_RESULT","sourceRef":"dispatch:44"}]',
        context_key="repo:checkout",
        suggested_patch_json=(
            '{"mode":"UPDATE","name":"checkout-recovery","type":"CODEX_SKILL",'
            '"installSpec":"skill://checkout-recovery","description":"Recover checkout."}'
        ),
    )
    proposal = build_skill_proposal(command)
    assert proposal.asset_id == 9
    assert proposal.candidate_patch_json is not None
    assert '"mode":"CREATE"' in proposal.candidate_patch_json
    memory = build_memory_proposal(
        RunCommand(
            asset_type="MEMORY",
            source_agent_id=30,
            policy_json='{"action":"PATCH","reasonCode":"repeated_context_failure"}',
            root_evidence_json='[{"sourceType":"REPLAY_RESULT","sourceRef":"dispatch:44"}]',
            failure_summary="checkout replay failed after repo rename",
            context_key="repo:checkout",
            suggested_patch_json=(
                '{"title":"Remember checkout failure",'
                '"contentMd":"Checkout fails when repo relation is stale."}'
            ),
        )
    )
    assert memory.asset_id is None
    assert memory.candidate_patch_json is not None
    assert '"proposalBuilder":"MEMORY_LITE"' in memory.candidate_patch_json
    assert '"ownerRef":30' in memory.candidate_patch_json


@pytest.mark.asyncio
async def test_router_rejects_unknown_asset_before_insert() -> None:
    """未知资产类型不写提案。"""
    session = MemorySession()
    with pytest.raises(BizError) as caught:
        await route(
            session,  # type: ignore[arg-type]
            RunCommand(
                asset_type="WORKER_PROFILE",
                root_evidence_json='[{"sourceType":"REPLAY_RESULT","sourceRef":"dispatch:44"}]',
                suggested_patch_json='{"title":"x","contentMd":"y"}',
            ),
            1,
            2,
        )
    assert caught.value.code == "10001"
    assert session.proposals == []


@pytest.mark.asyncio
async def test_orchestrator_stops_on_explore_and_starts_trial_on_patch() -> None:
    """探索只留证据；修补会写下提案并把状态推进到 TRIAL。"""
    sparse = MemorySession()
    explored = await orchestrate(sparse, _orchestrate_command(), 1, 2)  # type: ignore[arg-type]
    assert explored.action == "EXPLORE"
    assert explored.proposal_id is None
    assert len(sparse.evidence) == 1
    assert sparse.proposals == []

    ready = MemorySession()
    _pair(ready, "RELIABILITY", "repo:checkout", (2, 12), (12, 2))
    started = await orchestrate(ready, _orchestrate_command(), 1, 2)  # type: ignore[arg-type]
    assert started.action == "TRIAL_STARTED"
    assert started.proposal_status == "TRIAL"
    assert started.trial is not None
    assert started.trial.decision == "CONTINUE_TRIAL"
    assert started.trial.task_pattern_key == "repo:checkout"
    assert len(ready.proposals) == 1
    assert ready.proposals[0].status == "TRIAL"
    lifecycle = ready.proposals[0].lifecycle_json
    assert isinstance(lifecycle, dict)
    trial = lifecycle["trial"]
    assert isinstance(trial, dict)
    assert trial["targetPosteriorType"] == "RELIABILITY"
    assert trial["baselineArm"]["assetType"] == "TRIAL_BASELINE"


@pytest.mark.asyncio
async def test_trial_adopts_when_candidate_wins_and_reliability_holds() -> None:
    """目标维度打赢且可靠性不差时采纳，并把动作结果写回证据。"""
    session = MemorySession()
    proposal = EvolutionProposal(
        id=77,
        tenant_id=1,
        asset_type="SKILL",
        asset_id=9,
        trigger_type="PROPOSAL_BUILDER_LITE",
        root_evidence_json=[{"sourceType": "REPLAY_RESULT", "sourceRef": "dispatch:44"}],
        policy_json={"action": "COMPRESS", "targetPosteriorType": "TOKEN_EFFICIENCY"},
        candidate_patch_json={"mode": "UPDATE", "name": "lean"},
        status="TRIAL",
        lifecycle_json={
            "trial": {
                "taskPatternKey": "coding:repo:test",
                "targetPosteriorType": "TOKEN_EFFICIENCY",
            }
        },
        is_deleted=0,
        version=0,
        gmt_create=datetime(2026, 9, 24, 8, 0, 0),
        gmt_modified=datetime(2026, 9, 24, 8, 0, 0),
    )
    session.proposals.append(proposal)
    session._next_id = 80
    _remember(session, _arm("TRIAL_BASELINE", "TOKEN_EFFICIENCY", 20, 80))
    _remember(session, _arm("TRIAL_CANDIDATE", "TOKEN_EFFICIENCY", 80, 20))
    _remember(session, _arm("TRIAL_BASELINE", "RELIABILITY", 500, 100))
    _remember(session, _arm("TRIAL_CANDIDATE", "RELIABILITY", 500, 100))
    decision = await decide_trial(session, 77, 1, 2)  # type: ignore[arg-type]
    assert decision.decision == "ADOPT"
    assert decision.proposal_status == "TRIAL_ADOPTED"
    assert decision.posterior_win_probability is not None
    assert decision.posterior_win_probability > 0.9
    assert decision.reliability_guard_probability is not None
    assert decision.reliability_guard_probability > 0.9
    actions = [row for row in session.evidence if row.asset_type == "SKILL_ACTION"]
    assert len(actions) == 1
    assert actions[0].posterior_type == "ACTION_COMPRESS"
    assert actions[0].outcome == "POSITIVE"


@pytest.mark.asyncio
async def test_late_telemetry_does_not_reopen_a_finished_trial() -> None:
    """已经采纳的试验不再读证据。"""
    session = MemorySession()
    session.proposals.append(
        EvolutionProposal(
            id=77,
            tenant_id=1,
            asset_type="SKILL",
            asset_id=9,
            trigger_type="PROPOSAL_BUILDER_LITE",
            root_evidence_json=[{"sourceType": "REPLAY_RESULT", "sourceRef": "dispatch:44"}],
            policy_json={"action": "PATCH"},
            candidate_patch_json={"name": "checkout"},
            status="TRIAL_ADOPTED",
            is_deleted=0,
            version=1,
            gmt_create=datetime(2026, 9, 24, 8, 0, 0),
            gmt_modified=datetime(2026, 9, 24, 8, 0, 0),
        )
    )
    assert await decide_if_active(session, 77, 1, 2) is None  # type: ignore[arg-type]
    assert session.evidence == []


@pytest.mark.asyncio
async def test_zero_skill_id_is_recorded_then_rejected_by_policy() -> None:
    """覆盖假设的资产编号 0 可以入账，但策略不把它当成可比较技能。"""
    session = MemorySession()
    command = build_orchestrate_command(
        {
            "assetType": "SKILL",
            "contextKey": "coding:monorepo:checkout",
            "suggestedPatch": {
                "mode": "CREATE",
                "name": "monorepo-checkout-safety",
                "type": "CODING",
                "installSpec": "skill://monorepo-checkout-safety",
                "description": "Checkout first.",
            },
        },
        30,
        99,
        0,
        "ASSISTED",
    )
    with pytest.raises(BizError) as caught:
        await orchestrate(session, command, 1, 30)  # type: ignore[arg-type]
    assert caught.value.code == "10001"
    assert len(session.evidence) == 1
    assert session.evidence[0].asset_id == 0
    assert session.proposals == []


def _orchestrate_command() -> OrchestrateCommand:
    return OrchestrateCommand(
        evidence_event=EvidenceEvent(
            asset_type="SKILL",
            asset_id=9,
            posterior_type="UTILITY",
            context_key="repo:checkout",
            source_type="REPLAY_RESULT",
            source_ref="dispatch:44",
            raw_outcome="FAIL",
            raw_event_json='{"status":"FAIL"}',
            idempotency_key="dispatch:44:fail",
        ),
        candidate_asset_type="MEMORY",
        failure_summary="checkout failed",
        suggested_patch_json=(
            '{"title":"Remember checkout failure",'
            '"contentMd":"Checkout fails when repo relation is stale."}'
        ),
        context_key="repo:checkout",
        source_agent_id=30,
        root_evidence_json='[{"sourceType":"REPLAY_RESULT","sourceRef":"dispatch:44"}]',
    )


def _arm(asset_type: str, posterior_type: str, alpha: float, beta: float) -> EvolutionEvidence:
    return _evidence(
        asset_type,
        "coding:repo:test",
        alpha,
        beta,
        posterior_type=posterior_type,
        asset_id=77,
    )
