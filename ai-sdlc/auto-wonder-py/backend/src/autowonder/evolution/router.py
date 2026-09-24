"""``/api/evolution``。查看要求只读，推进提案要求读写。"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.evolution.admin import overview
from autowonder.evolution.agent_release import release_agent
from autowonder.evolution.canary import postprocess_canary
from autowonder.evolution.commands import (
    EvidenceCommand,
    EvidenceEvent,
    OrchestrateCommand,
    ProposalCommand,
    RunCommand,
    TrialEvidenceCommand,
)
from autowonder.evolution.evidence import record_event, record_evidence
from autowonder.evolution.gates import record_gate
from autowonder.evolution.jsontext import bool_field, double_field, long_field, text_field
from autowonder.evolution.lifecycle import approve, record_replay, reject, release, validate
from autowonder.evolution.manifest import manifest
from autowonder.evolution.orchestrator import orchestrate
from autowonder.evolution.present import (
    evidence_data,
    orchestrate_data,
    proposal_data,
    trial_data,
)
from autowonder.evolution.replay import execute_replay
from autowonder.evolution.rollback import rollback
from autowonder.evolution.routing import propose, route
from autowonder.evolution.trial import decide, record_outcome, start_trial
from autowonder.evolution.trigger import check_trigger

router = APIRouter(prefix="/api/evolution", tags=["evolution"])


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


async def _body(request: Request) -> dict[str, object]:
    raw = await request.body()
    if not raw:
        raise BizError(ErrorCode.PARAM_INVALID)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    if not isinstance(parsed, dict):
        raise BizError(ErrorCode.PARAM_INVALID)
    return parsed


@router.post(
    "/proposals",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建演进提案"))],
)
async def create_proposal(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """校验证据和补丁后创建 PROPOSED 提案。"""
    body = await _body(request)
    proposal = await propose(
        session,
        ProposalCommand(
            asset_type=text_field(body, "assetType"),
            asset_id=long_field(body, "assetId"),
            trigger_type=text_field(body, "triggerType"),
            root_evidence_json=text_field(body, "rootEvidenceJson"),
            policy_json=text_field(body, "policyJson"),
            candidate_patch_json=text_field(body, "candidatePatchJson"),
        ),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(proposal_data(proposal))


@router.post(
    "/proposals/{id}/validate",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "校验演进提案"))],
)
async def validate_proposal(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """校验提案结构、证据和补丁。"""
    await validate(session, id, _workspace_id(), _user_id())
    await session.commit()
    return ok(None)


@router.post(
    "/proposals/{id}/replay",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "记录演进回放"))],
)
async def replay_proposal(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """记录调用方给出的回放 JSON。"""
    body = await _body(request)
    await record_replay(
        session,
        id,
        _workspace_id(),
        text_field(body, "replayJson"),
        _user_id(),
    )
    await session.commit()
    return ok(None)


@router.post(
    "/proposals/{id}/approve",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "通过演进提案"))],
)
async def approve_proposal(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """回放通过或试验采纳后通过提案。"""
    await approve(session, id, _workspace_id(), _user_id())
    await session.commit()
    return ok(None)


@router.post(
    "/proposals/{id}/release",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "发布演进提案"))],
)
async def release_proposal(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把已通过提案发布成记忆、仓库关系或技能。"""
    await release(session, id, _workspace_id(), _user_id())
    await session.commit()
    return ok(None)


@router.post(
    "/proposals/{id}/agent-release",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "发布演进智能体"))],
)
async def release_agent_proposal(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """门禁通过后审批并发布提案。必须显式允许发布。"""
    body = await _body(request)
    result = await release_agent(
        session,
        id,
        bool_field(body, "allowRelease"),
        _gate_types(body),
        bool_field(body, "allowCanaryInconclusive"),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(result)


@router.post(
    "/proposals/{id}/reject",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "驳回演进提案"))],
)
async def reject_proposal(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """驳回提案并记下原因。"""
    body = await _body(request)
    await reject(
        session,
        id,
        _workspace_id(),
        text_field(body, "reason"),
        _user_id(),
    )
    await session.commit()
    return ok(None)


@router.post(
    "/run",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "执行演进运行"))],
)
async def run_evolution(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按资产类型起草并创建提案。"""
    body = await _body(request)
    result = await route(session, _run_command(body), _workspace_id(), _user_id())
    await session.commit()
    return ok(
        {
            "proposalId": result.proposal_id,
            "status": result.status,
            "assetType": result.asset_type,
            "assetId": result.asset_id,
        }
    )


@router.post(
    "/proposals/{id}/trial/start",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "启动演进试验"))],
)
async def start_proposal_trial(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """打开试验并写下任务模式。"""
    body = await _body(request)
    decision = await start_trial(
        session,
        id,
        text_field(body, "taskPatternKey"),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(trial_data(decision))


@router.post(
    "/proposals/{id}/trial/evidence",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "记录演进试验结果"))],
)
async def record_trial_evidence(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把一条结果记到候选臂并重新裁决。"""
    body = await _body(request)
    decision = await record_outcome(
        session,
        id,
        TrialEvidenceCommand(
            raw_outcome=text_field(body, "rawOutcome"),
            source_type=text_field(body, "sourceType"),
            source_ref=text_field(body, "sourceRef"),
            evidence_json=text_field(body, "evidenceJson"),
            idempotency_key=text_field(body, "idempotencyKey"),
            weight=double_field(body, "weight"),
        ),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(trial_data(decision))


@router.post(
    "/proposals/{id}/trial/decide",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "决策演进试验"))],
)
async def decide_proposal_trial(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按当前样本采纳、拒绝或继续试验。"""
    decision = await decide(session, id, _workspace_id(), _user_id())
    await session.commit()
    return ok(trial_data(decision))


@router.post(
    "/evidence",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "记录贝叶斯证据"))],
)
async def record_bayesian_evidence(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """直接累加一条已经归一化的证据。"""
    body = await _body(request)
    row = await record_evidence(
        session,
        EvidenceCommand(
            asset_type=text_field(body, "assetType"),
            asset_id=long_field(body, "assetId"),
            posterior_type=text_field(body, "posteriorType"),
            context_key=text_field(body, "contextKey"),
            source_type=text_field(body, "sourceType"),
            source_ref=text_field(body, "sourceRef"),
            outcome=text_field(body, "outcome"),
            observation=double_field(body, "observation"),
            weight=double_field(body, "weight"),
            evidence_json=text_field(body, "evidenceJson"),
            dependency_group=text_field(body, "dependencyGroup"),
            idempotency_key=text_field(body, "idempotencyKey"),
        ),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(evidence_data(row))


@router.post(
    "/evidence/events",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "记录演进证据事件"))],
)
async def record_evidence_event(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按幂等键记录证据事件。"""
    body = await _body(request)
    row = await record_event(session, _event(body), _workspace_id(), _user_id())
    await session.commit()
    return ok(evidence_data(row))


@router.post(
    "/evidence/trigger-check",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "检查演进触发条件"))],
)
async def trigger_check(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """查看最新后验是否低到需要调查。"""
    body = await _body(request)
    decision = await check_trigger(
        session,
        _workspace_id(),
        text_field(body, "assetType"),
        long_field(body, "assetId"),
        text_field(body, "posteriorType"),
        text_field(body, "contextKey"),
        double_field(body, "minEffectiveSampleSize"),
        double_field(body, "credibleUpperBoundBelow"),
    )
    return ok(decision)


@router.post(
    "/gates",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "记录演进门禁运行"))],
)
async def record_gate_run(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """记录基准、影子或灰度门禁。"""
    body = await _body(request)
    recorded = await record_gate(
        session,
        long_field(body, "proposalId"),
        text_field(body, "gateType"),
        text_field(body, "verdict"),
        text_field(body, "resultJson"),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(recorded)


@router.post(
    "/orchestrate",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "编排演进自动化"))],
)
async def orchestrate_evolution(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """从证据走到探索或试验。"""
    body = await _body(request)
    result = await orchestrate(session, _orchestrate_command(body), _workspace_id(), _user_id())
    await session.commit()
    return ok(orchestrate_data(result))


@router.post(
    "/replay/execute",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "执行演进回放"))],
)
async def execute_evolution_replay(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """执行回放套件并写入提案。"""
    body = await _body(request)
    result = await execute_replay(
        session,
        long_field(body, "proposalId"),
        bool_field(body, "autoValidate"),
        text_field(body, "replaySuiteJson"),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(result)


@router.post(
    "/canary/postprocess",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "处理演进灰度结果"))],
)
async def postprocess_evolution_canary(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """处理灰度结论，必要时驳回提案。"""
    body = await _body(request)
    result = await postprocess_canary(
        session,
        long_field(body, "proposalId"),
        text_field(body, "assetType"),
        long_field(body, "assetId"),
        text_field(body, "contextKey"),
        text_field(body, "verdict"),
        text_field(body, "resultJson"),
        bool_field(body, "rejectProposalOnFail"),
        _workspace_id(),
        _user_id(),
    )
    await session.commit()
    return ok(result)


@router.post(
    "/rollback/{proposalId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "回滚演进变更"))],
)
async def rollback_proposal(
    proposalId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """撤回已发布的资产变更。"""
    result = await rollback(session, proposalId, _workspace_id(), _user_id())
    await session.commit()
    return ok(result)


@router.get(
    "/admin/overview",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看演进管理信息"))],
)
async def admin_overview(
    limit: Annotated[int | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """返回最近的提案和证据。"""
    proposals, evidence = await overview(session, _workspace_id(), limit)
    return ok(
        {
            "proposals": [proposal_data(row) for row in proposals],
            "evidence": [evidence_data(row) for row in evidence],
        }
    )


@router.get(
    "/admin/asset-manifest",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看演进管理信息"))],
)
async def admin_asset_manifest(
    asset_type: Annotated[str | None, Query(alias="assetType")] = None,
    context_key: Annotated[str | None, Query(alias="contextKey")] = None,
    limit: Annotated[int | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """返回记忆、技能和仓库关系的轻量卡片。"""
    return ok(await manifest(session, _workspace_id(), asset_type, context_key, limit))


def _gate_types(body: dict[str, object]) -> list[str] | None:
    raw = body.get("requiredGateTypes")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise BizError(ErrorCode.PARAM_INVALID)
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise BizError(ErrorCode.PARAM_INVALID)
        names.append(item)
    return names


def _run_command(body: dict[str, object]) -> RunCommand:
    return RunCommand(
        asset_type=text_field(body, "assetType"),
        asset_id=long_field(body, "assetId"),
        root_evidence_json=text_field(body, "rootEvidenceJson"),
        policy_json=text_field(body, "policyJson"),
        failure_summary=text_field(body, "failureSummary"),
        suggested_patch_json=text_field(body, "suggestedPatchJson"),
        context_key=text_field(body, "contextKey"),
        source_agent_id=long_field(body, "sourceAgentId"),
    )


def _event(body: dict[str, object]) -> EvidenceEvent:
    return EvidenceEvent(
        asset_type=text_field(body, "assetType"),
        asset_id=long_field(body, "assetId"),
        posterior_type=text_field(body, "posteriorType"),
        context_key=text_field(body, "contextKey"),
        source_type=text_field(body, "sourceType"),
        source_ref=text_field(body, "sourceRef"),
        raw_outcome=text_field(body, "rawOutcome"),
        observation=double_field(body, "observation"),
        raw_event_json=text_field(body, "rawEventJson"),
        weight=double_field(body, "weight"),
        dependency_group=text_field(body, "dependencyGroup"),
        idempotency_key=text_field(body, "idempotencyKey"),
    )


def _orchestrate_command(body: dict[str, object]) -> OrchestrateCommand:
    event = None
    raw_event = body.get("evidenceEvent")
    if isinstance(raw_event, dict):
        event = _event(raw_event)
    return OrchestrateCommand(
        evidence_event=event,
        candidate_asset_type=text_field(body, "candidateAssetType"),
        candidate_asset_id=long_field(body, "candidateAssetId"),
        root_evidence_json=text_field(body, "rootEvidenceJson"),
        failure_summary=text_field(body, "failureSummary"),
        suggested_patch_json=text_field(body, "suggestedPatchJson"),
        draft_delta_json=text_field(body, "draftDeltaJson"),
        context_key=text_field(body, "contextKey"),
        source_agent_id=long_field(body, "sourceAgentId"),
        auto_validate_before_replay=bool_field(body, "autoValidateBeforeReplay"),
        replay_suite_json=text_field(body, "replaySuiteJson"),
    )
