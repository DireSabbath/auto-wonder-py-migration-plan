"""提案校验、回放、发布和演进 HTTP，向量对齐 Java 提案与自动化测试。"""

import importlib.util
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from autowonder.core.errors import BizError
from autowonder.evolution.admin import overview
from autowonder.evolution.canary import postprocess_canary
from autowonder.evolution.gates import record_gate
from autowonder.evolution.lifecycle import approve, record_replay, release, validate
from autowonder.evolution.models import EvolutionProposal
from autowonder.evolution.replay import execute_replay
from autowonder.evolution.rollback import rollback
from autowonder.evolution.trigger import check_trigger
from autowonder.main import create_app
from autowonder.memories.schemas import MemoryView
from autowonder.skills.schemas import SkillView
from autowonder.skills.service import PackageReference

_fixture_path = Path(__file__).with_name("test_evolution_orchestrator.py")
_fixture_spec = importlib.util.spec_from_file_location(
    "evolution_orchestrator_fixture",
    _fixture_path,
)
assert _fixture_spec is not None
assert _fixture_spec.loader is not None
_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture)
MemorySession = _fixture.MemorySession
_evidence = _fixture._evidence
_remember = _fixture._remember

_EVIDENCE = [{"sourceType": "HUMAN_REVIEW", "sourceRef": "comment:77"}]
_MEMORY_PATCH = {
    "scope": "ORG",
    "type": "ENGINEERING_RULE",
    "title": "Use pnpm",
    "contentMd": "Use pnpm for this repo.",
}


def test_evolution_routes_match_java_and_require_login() -> None:
    """已接入的演进路径要求登录。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    expected = [
        "/api/evolution/proposals",
        "/api/evolution/proposals/{id}/validate",
        "/api/evolution/proposals/{id}/replay",
        "/api/evolution/proposals/{id}/approve",
        "/api/evolution/proposals/{id}/release",
        "/api/evolution/proposals/{id}/agent-release",
        "/api/evolution/proposals/{id}/reject",
        "/api/evolution/proposals/{id}/trial/start",
        "/api/evolution/proposals/{id}/trial/evidence",
        "/api/evolution/proposals/{id}/trial/decide",
        "/api/evolution/run",
        "/api/evolution/evidence",
        "/api/evolution/evidence/events",
        "/api/evolution/evidence/trigger-check",
        "/api/evolution/gates",
        "/api/evolution/orchestrate",
        "/api/evolution/replay/execute",
        "/api/evolution/canary/postprocess",
        "/api/evolution/rollback/{proposalId}",
        "/api/evolution/admin/overview",
        "/api/evolution/admin/asset-manifest",
    ]
    for path in expected:
        assert path in paths
    response = client.post("/api/evolution/proposals", json={})
    assert response.status_code == 401
    assert response.json()["code"] == "10401"


@pytest.mark.asyncio
async def test_validate_replay_and_approve_only_advance_the_proposal() -> None:
    """校验、回放通过和审批只改提案状态，不写记忆。"""
    session = MemorySession()
    session.proposals.append(_proposal("PROPOSED"))
    await validate(session, 101, 1, 2)  # type: ignore[arg-type]
    assert session.proposals[0].status == "VALIDATED"
    lifecycle = session.proposals[0].lifecycle_json
    assert isinstance(lifecycle, dict)
    validation = lifecycle["validation"]
    assert isinstance(validation, dict)
    assert validation["verdict"] == "PASS"
    assert validation["checks"] == ["schema", "traceableEvidence", "candidatePatch"]

    await record_replay(
        session,  # type: ignore[arg-type]
        101,
        1,
        '{"verdict":"PASS","evidenceRefs":["artifact:replay-1"]}',
        2,
    )
    assert session.proposals[0].status == "REPLAY_PASSED"
    assert session.evidence == []
    await approve(session, 101, 1, 2)  # type: ignore[arg-type]
    assert session.proposals[0].status == "APPROVED"
    assert session.proposals[0].version == 3


@pytest.mark.asyncio
async def test_replay_pass_records_uplift_without_releasing_the_asset() -> None:
    """有资产编号的回放通过会写 UPLIFT 证据，仍然不发布资产。"""
    session = MemorySession()
    proposal = _proposal("VALIDATED")
    proposal.asset_type = "SKILL"
    proposal.asset_id = 9
    proposal.candidate_patch_json = {
        "contextKey": "repo:checkout",
        "description": "Better checkout flow",
    }
    session.proposals.append(proposal)
    await record_replay(
        session,  # type: ignore[arg-type]
        101,
        1,
        '{"verdict":"PASS","evidenceRefs":["artifact:replay-1"]}',
        2,
    )
    assert len(session.evidence) == 1
    evidence = session.evidence[0]
    assert evidence.asset_type == "SKILL"
    assert evidence.asset_id == 9
    assert evidence.posterior_type == "UPLIFT"
    assert evidence.context_key == "repo:checkout"
    assert evidence.source_type == "REPLAY_RESULT"
    assert evidence.source_ref == "proposal:101:replay"
    assert evidence.outcome == "POSITIVE"


@pytest.mark.asyncio
async def test_release_requires_approved_proposal_and_passing_replay() -> None:
    """没通过的提案不能发布，也不会去创建资产。"""
    session = MemorySession()
    session.proposals.append(_proposal("VALIDATED"))
    with pytest.raises(BizError) as raised:
        await release(session, 101, 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10409"


@pytest.mark.asyncio
async def test_release_memory_is_the_only_writer_of_the_adopted_memory() -> None:
    """发布记忆提案时才调用进化记忆入口，并把资产编号写入发布快照。"""
    session = MemorySession()
    proposal = _proposal("APPROVED")
    proposal.lifecycle_json = {
        "replay": {"verdict": "PASS", "evidenceRefs": ["artifact:replay-1"]}
    }
    session.proposals.append(proposal)
    created = AsyncMock(return_value=MemoryView(id=301))
    with patch("autowonder.evolution.lifecycle.create_from_evolution_proposal", created):
        await release(session, 101, 1, 2)  # type: ignore[arg-type]
    request = created.await_args.args[1]
    assert request.title == "Use pnpm"
    assert request.content_md == "Use pnpm for this repo."
    assert request.type == "ENGINEERING_RULE"
    assert created.await_args.args[2:] == (1, 101, 2)
    release_body = _release_of(session)
    assert release_body["assetId"] == 301
    assert release_body["afterJson"] == '{"id":301}'
    assert session.proposals[0].status == "RELEASED"


@pytest.mark.asyncio
async def test_release_can_use_trial_adoption_instead_of_replay() -> None:
    """试验采纳可以代替回放通过。"""
    session = MemorySession()
    proposal = _proposal("APPROVED")
    proposal.lifecycle_json = {"trial": {"decision": "ADOPT", "taskPatternKey": "repo-checkout"}}
    session.proposals.append(proposal)
    created = AsyncMock(return_value=MemoryView(id=301))
    with patch("autowonder.evolution.lifecycle.create_from_evolution_proposal", created):
        await release(session, 101, 1, 2)  # type: ignore[arg-type]
    created.assert_awaited()
    assert _release_of(session)["assetId"] == 301


@pytest.mark.asyncio
async def test_release_skill_create_does_not_update_an_existing_skill() -> None:
    """CREATE 模式新建技能，不走更新。"""
    session = MemorySession()
    proposal = _proposal("APPROVED")
    proposal.asset_type = "SKILL"
    proposal.asset_id = None
    proposal.lifecycle_json = {"replay": {"verdict": "PASS"}}
    proposal.candidate_patch_json = {
        "mode": "CREATE",
        "name": "multi-repo-triage",
        "type": "CODEX_SKILL",
        "installSpec": "skill://multi-repo-triage",
        "description": "Use repo-map before multi-repo edits.",
    }
    session.proposals.append(proposal)
    created = AsyncMock(return_value=SkillView(id=501, version=0))
    updated = AsyncMock()
    with (
        patch("autowonder.evolution.lifecycle.create_skill", created),
        patch("autowonder.evolution.lifecycle.update_skill", updated),
    ):
        await release(session, 101, 1, 2)  # type: ignore[arg-type]
    request = created.await_args.args[1]
    assert request.name == "multi-repo-triage"
    assert request.type == "CODEX_SKILL"
    assert request.install_spec == "skill://multi-repo-triage"
    assert request.description == "Use repo-map before multi-repo edits."
    updated.assert_not_awaited()
    release_body = _release_of(session)
    assert release_body["assetId"] == 501
    assert release_body["mode"] == "CREATE"


@pytest.mark.asyncio
async def test_release_skill_package_keeps_the_package_reference() -> None:
    """带对象存储引用的技能补丁走技能包更新。"""
    session = MemorySession()
    proposal = _proposal("APPROVED")
    proposal.asset_type = "SKILL"
    proposal.asset_id = 9
    proposal.lifecycle_json = {"replay": {"verdict": "PASS"}}
    proposal.candidate_patch_json = {
        "mode": "UPDATE",
        "name": "checkout",
        "type": "SKILL",
        "installSpec": "{}",
        "description": "candidate",
        "packageOssRef": "oss://skills/candidate.zip",
        "packageMd5": "abc",
    }
    session.proposals.append(proposal)
    updated = AsyncMock(return_value=SkillView(id=9, version=4))
    with (
        patch("autowonder.evolution.lifecycle.get_skill", AsyncMock(return_value=SkillView(id=9))),
        patch("autowonder.evolution.lifecycle.update_from_package_reference", updated),
    ):
        await release(session, 101, 1, 2)  # type: ignore[arg-type]
    package = updated.await_args.args[3]
    assert isinstance(package, PackageReference)
    assert package.oss_ref == "oss://skills/candidate.zip"
    assert package.md5 == "abc"
    assert updated.await_args.args[1] == 9


@pytest.mark.asyncio
async def test_release_retire_cannot_masquerade_as_skill_update() -> None:
    """RETIRE 不能借技能更新发布，必须走智能体解绑。"""
    session = MemorySession()
    proposal = _proposal("APPROVED")
    proposal.asset_type = "SKILL"
    proposal.asset_id = 9
    proposal.lifecycle_json = {"replay": {"verdict": "PASS"}}
    proposal.candidate_patch_json = {
        "mode": "UPDATE",
        "policyAction": "RETIRE",
        "name": "checkout",
        "type": "SKILL",
        "installSpec": "{}",
        "description": "candidate",
    }
    session.proposals.append(proposal)
    with (
        patch("autowonder.evolution.lifecycle.get_skill", AsyncMock(return_value=SkillView(id=9))),
        pytest.raises(BizError) as raised,
    ):
        await release(session, 101, 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10409"
    assert str(raised.value) == "RETIRE requires an explicit agent-version unbind/release decision"


@pytest.mark.asyncio
async def test_replay_executor_validates_then_records_forced_pass() -> None:
    """自动校验后再按套件里的强制结论记回放。"""
    session = MemorySession()
    session.proposals.append(_proposal("PROPOSED"))
    result = await execute_replay(
        session,  # type: ignore[arg-type]
        101,
        True,
        '{"forceVerdict":"PASS","checks":[{"name":"suite","status":"PASS"}]}',
        1,
        2,
    )
    assert result["verdict"] == "PASS"
    assert session.proposals[0].status == "REPLAY_PASSED"
    assert session.proposals[0].version == 2


@pytest.mark.asyncio
async def test_canary_failure_records_gate_and_rejects_when_asked() -> None:
    """灰度失败建议回滚，并在要求驳回时把提案标成 REJECTED。"""
    session = MemorySession()
    session.proposals.append(_proposal("TRIAL"))
    result = await postprocess_canary(
        session,  # type: ignore[arg-type]
        101,
        "SKILL",
        9,
        "repo:checkout",
        "FAIL",
        '{"summary":"regressed"}',
        True,
        1,
        2,
    )
    assert result["action"] == "ROLLBACK_RECOMMENDED"
    assert session.proposals[0].status == "REJECTED"
    lifecycle = session.proposals[0].lifecycle_json
    assert isinstance(lifecycle, dict)
    gates = lifecycle["gates"]
    assert isinstance(gates, dict)
    canary = gates["CANARY"]
    assert isinstance(canary, dict)
    assert canary["verdict"] == "FAIL"
    release_body = lifecycle["release"]
    assert isinstance(release_body, dict)
    assert release_body["reason"] == "CANARY_FAIL_ROLLBACK_RECOMMENDED"
    assert session.evidence[0].source_type == "CANARY_RESULT"
    assert session.evidence[0].outcome == "NEGATIVE"


@pytest.mark.asyncio
async def test_gate_rejects_unknown_type() -> None:
    """门禁类型只能是基准、影子或灰度。"""
    session = MemorySession()
    session.proposals.append(_proposal("PROPOSED"))
    with pytest.raises(BizError) as raised:
        await record_gate(session, 101, "LOAD", "PASS", "{}", 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10001"


@pytest.mark.asyncio
async def test_trigger_investigates_a_weak_posterior() -> None:
    """负向样本足够且可信上界低于阈值时需要调查。"""
    session = MemorySession()
    _remember(session, _evidence("SKILL", "repo:checkout", 1, 40, posterior_type="UTILITY"))
    quiet = await check_trigger(session, 1, "SKILL", 9, "UTILITY", "missing", None, None)  # type: ignore[arg-type]
    assert quiet["shouldInvestigate"] is False
    decision = await check_trigger(
        session,  # type: ignore[arg-type]
        1,
        "SKILL",
        9,
        "UTILITY",
        "repo:checkout",
        None,
        None,
    )
    assert decision["shouldInvestigate"] is True
    assert decision["credibleUpperBound90"] is not None
    assert float(decision["credibleUpperBound90"]) < 0.30


@pytest.mark.asyncio
async def test_overview_returns_the_newest_rows_inside_the_cap() -> None:
    """概览最多 20 条，并且新的在前。"""
    session = MemorySession()
    session.proposals.append(_proposal("PROPOSED", proposal_id=1))
    newer = _proposal("PROPOSED", proposal_id=8)
    session.proposals.append(newer)
    proposals, evidence = await overview(session, 1, 100)  # type: ignore[arg-type]
    assert [row.id for row in proposals] == [8, 1]
    assert evidence == []
    newest, _ = await overview(session, 1, 1)  # type: ignore[arg-type]
    assert [row.id for row in newest] == [8]


@pytest.mark.asyncio
async def test_rollback_deletes_the_memory_created_by_release() -> None:
    """回滚已发布的记忆就是删掉那条记忆。"""
    session = MemorySession()
    proposal = _proposal("RELEASED")
    proposal.lifecycle_json = {
        "release": {"proposalId": 101, "assetType": "MEMORY", "assetId": 301, "afterJson": "{}"}
    }
    session.proposals.append(proposal)
    deleted = AsyncMock()
    with patch("autowonder.evolution.rollback.delete_memory", deleted):
        result = await rollback(session, 101, 1, 2)  # type: ignore[arg-type]
    deleted.assert_awaited_once()
    assert deleted.await_args.args == (session, 301, 1, 2)
    assert result["action"] == "DELETE_CREATED_MEMORY"
    assert session.proposals[0].status == "ROLLED_BACK"


def _proposal(status: str, proposal_id: int = 101) -> EvolutionProposal:
    return EvolutionProposal(
        id=proposal_id,
        tenant_id=1,
        asset_type="MEMORY",
        asset_id=None,
        trigger_type="USER_CORRECTION",
        root_evidence_json=_EVIDENCE,
        policy_json=None,
        candidate_patch_json=_MEMORY_PATCH,
        status=status,
        lifecycle_json=None,
        is_deleted=0,
        version=0,
        creator_id=2,
        gmt_create=datetime(2026, 9, 24, 8, 0, 0),
        gmt_modified=datetime(2026, 9, 24, 8, 0, 0),
    )


def _release_of(session: MemorySession) -> dict[str, object]:
    lifecycle = session.proposals[0].lifecycle_json
    assert isinstance(lifecycle, dict)
    release_body = lifecycle["release"]
    assert isinstance(release_body, dict)
    return release_body
