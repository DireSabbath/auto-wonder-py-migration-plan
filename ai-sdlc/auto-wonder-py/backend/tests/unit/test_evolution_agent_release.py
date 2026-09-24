"""智能体发布和资产清单，向量对齐 Java Lite 服务测试。"""

import importlib.util
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from autowonder.core.errors import BizError
from autowonder.core.page import PageResult
from autowonder.evolution.agent_release import release_agent
from autowonder.evolution.jsontext import dump_json
from autowonder.evolution.manifest import manifest
from autowonder.evolution.models import EvolutionEvidence, EvolutionProposal
from autowonder.memories.schemas import MemoryView
from autowonder.repos.schemas import RepoRelationView
from autowonder.skills.schemas import SkillView

_fixture_path = Path(__file__).with_name("test_evolution_orchestrator.py")
_fixture_spec = importlib.util.spec_from_file_location(
    "evolution_agent_release_fixture",
    _fixture_path,
)
assert _fixture_spec is not None
assert _fixture_spec.loader is not None
_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture)
MemorySession = _fixture.MemorySession
_remember = _fixture._remember

_MEMORY_PATCH = {
    "scope": "ORG",
    "type": "ENGINEERING_RULE",
    "title": "Use pnpm",
    "contentMd": "Use pnpm for this repo.",
}
_HEAVY_MEMORY = "VERY_LONG_MEMORY_CONTENT_SHOULD_NOT_APPEAR"
_HEAVY_INSTALL = "VERY_LONG_INSTALL_SPEC_SHOULD_NOT_APPEAR"


class _NoQuery(MemorySession):
    """显式拒绝发布时不应读取提案。"""

    async def execute(self, statement: object) -> object:
        raise AssertionError("should not query")


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_release", [False, None])
async def test_agent_release_requires_explicit_allow(allow_release: bool | None) -> None:
    """allowRelease 不是 true 时直接拒绝，不读提案。"""
    with pytest.raises(BizError) as raised:
        await release_agent(_NoQuery(), 100, allow_release, None, None, 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10001"


@pytest.mark.asyncio
async def test_agent_release_approves_replay_when_required_gates_pass() -> None:
    """回放通过且要求的门禁都通过时，先审批再发布。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.lifecycle_json = {
        "replay": {"verdict": "PASS"},
        "gates": {
            "BENCHMARK": {"verdict": "PASS"},
            "SHADOW": {"verdict": "PASS"},
        },
    }
    session.proposals.append(proposal)
    created = AsyncMock(return_value=MemoryView(id=301))
    with patch("autowonder.evolution.lifecycle.create_from_evolution_proposal", created):
        result = await release_agent(
            session,  # type: ignore[arg-type]
            100,
            True,
            ["BENCHMARK", "SHADOW"],
            None,
            1,
            2,
        )
    created.assert_awaited_once()
    assert result == {"proposalId": 100, "action": "RELEASED", "status": "RELEASED"}
    assert session.proposals[0].status == "RELEASED"
    assert session.proposals[0].version == 2


@pytest.mark.asyncio
async def test_agent_release_accepts_gate_json_text() -> None:
    """门禁列仍是 JSON 文本时，按对象解析后再判断。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.lifecycle_json = {
        "replay": '{"verdict":"PASS"}',
        "gates": '{"BENCHMARK":{"verdict":"PASS"}}',
    }
    session.proposals.append(proposal)
    created = AsyncMock(return_value=MemoryView(id=301))
    with patch("autowonder.evolution.lifecycle.create_from_evolution_proposal", created):
        await release_agent(session, 100, True, ["BENCHMARK"], None, 1, 2)  # type: ignore[arg-type]
    assert session.proposals[0].status == "RELEASED"


@pytest.mark.asyncio
async def test_agent_release_approves_trial_adoption_before_writing_asset() -> None:
    """试验采纳会先审批，再写成正式资产。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.status = "TRIAL_ADOPTED"
    proposal.lifecycle_json = {"trial": {"decision": "ADOPT", "taskPatternKey": "repo-checkout"}}
    session.proposals.append(proposal)
    created = AsyncMock(return_value=MemoryView(id=301))
    with patch("autowonder.evolution.lifecycle.create_from_evolution_proposal", created):
        result = await release_agent(session, 100, True, None, None, 1, 2)  # type: ignore[arg-type]
    created.assert_awaited_once()
    assert result["status"] == "RELEASED"
    assert session.proposals[0].version == 2


@pytest.mark.asyncio
async def test_agent_release_skips_approve_when_already_approved() -> None:
    """已经通过且试验采纳的提案只发布一次，版本只加一。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.status = "APPROVED"
    proposal.lifecycle_json = {"trial": {"decision": "ADOPT"}}
    session.proposals.append(proposal)
    created = AsyncMock(return_value=MemoryView(id=301))
    with patch("autowonder.evolution.lifecycle.create_from_evolution_proposal", created):
        await release_agent(session, 100, True, None, None, 1, 2)  # type: ignore[arg-type]
    assert session.proposals[0].status == "RELEASED"
    assert session.proposals[0].version == 1


@pytest.mark.asyncio
async def test_agent_release_blocks_when_required_gate_is_not_pass() -> None:
    """要求的门禁失败或缺失时保持回放通过，不审批。"""
    session = MemorySession()
    failed = _replay_passed()
    failed.lifecycle_json = {
        "replay": {"verdict": "PASS"},
        "gates": {"BENCHMARK": {"verdict": "FAIL"}},
    }
    missing = _replay_passed()
    missing.id = 101
    missing.lifecycle_json = {
        "replay": {"verdict": "PASS"},
        "gates": {"BENCHMARK": {"verdict": "PASS"}},
    }
    session.proposals.extend([failed, missing])
    with pytest.raises(BizError) as failed_gate:
        await release_agent(session, 100, True, ["BENCHMARK"], None, 1, 2)  # type: ignore[arg-type]
    assert failed_gate.value.code == "10409"
    with pytest.raises(BizError) as missing_gate:
        await release_agent(session, 101, True, ["SHADOW"], None, 1, 2)  # type: ignore[arg-type]
    assert missing_gate.value.code == "10409"
    assert failed.status == "REPLAY_PASSED"
    assert failed.version == 0
    assert missing.status == "REPLAY_PASSED"


@pytest.mark.asyncio
async def test_agent_release_blocks_failed_canary_without_required_gates() -> None:
    """灰度失败即使不在要求列表里也拦住。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.lifecycle_json = {
        "replay": {"verdict": "PASS"},
        "gates": {"CANARY": {"verdict": "FAIL"}},
    }
    session.proposals.append(proposal)
    with pytest.raises(BizError) as raised:
        await release_agent(session, 100, True, None, True, 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10409"
    assert proposal.status == "REPLAY_PASSED"
    assert proposal.version == 0


@pytest.mark.asyncio
async def test_agent_release_allows_inconclusive_canary_only_when_asked() -> None:
    """灰度无结论只有显式允许时才算通过。"""
    blocked = MemorySession()
    blocked.proposals.append(_canary_inconclusive())
    with pytest.raises(BizError) as raised:
        await release_agent(blocked, 100, True, ["CANARY"], False, 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10409"
    assert blocked.proposals[0].status == "REPLAY_PASSED"

    allowed = MemorySession()
    allowed.proposals.append(_canary_inconclusive())
    created = AsyncMock(return_value=MemoryView(id=301))
    with patch("autowonder.evolution.lifecycle.create_from_evolution_proposal", created):
        await release_agent(allowed, 100, True, ["CANARY"], True, 1, 2)  # type: ignore[arg-type]
    assert allowed.proposals[0].status == "RELEASED"


@pytest.mark.asyncio
async def test_agent_release_rejects_blank_gate_type_and_corrupt_gates() -> None:
    """空白门禁类型是参数错误；门禁 JSON 损坏是冲突。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.lifecycle_json = {"replay": {"verdict": "PASS"}, "gates": "{"}
    session.proposals.append(proposal)
    with pytest.raises(BizError) as corrupt:
        await release_agent(session, 100, True, ["BENCHMARK"], None, 1, 2)  # type: ignore[arg-type]
    assert corrupt.value.code == "10409"
    proposal.lifecycle_json = {"replay": {"verdict": "PASS"}, "gates": {}}
    with pytest.raises(BizError) as blank:
        await release_agent(session, 100, True, [" "], None, 1, 2)  # type: ignore[arg-type]
    assert blank.value.code == "10001"
    assert proposal.status == "REPLAY_PASSED"
    assert proposal.version == 0


@pytest.mark.asyncio
async def test_agent_release_requires_replay_pass_verdict() -> None:
    """回放结论不是通过时冲突，提案保持原状态。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.lifecycle_json = {"replay": {"verdict": "FAIL"}}
    session.proposals.append(proposal)
    with pytest.raises(BizError) as raised:
        await release_agent(session, 100, True, None, None, 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10409"
    assert proposal.status == "REPLAY_PASSED"
    assert proposal.version == 0


@pytest.mark.asyncio
async def test_agent_release_treats_broken_replay_json_as_not_ready() -> None:
    """回放 JSON 损坏时视为未通过，不向外抛解析错误。"""
    session = MemorySession()
    proposal = _replay_passed()
    proposal.lifecycle_json = {"replay": "{"}
    session.proposals.append(proposal)
    with pytest.raises(BizError) as raised:
        await release_agent(session, 100, True, None, None, 1, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10409"


@pytest.mark.asyncio
async def test_agent_release_hides_another_tenants_proposal() -> None:
    """提案不属于当前工作空间时按不存在处理。"""
    session = MemorySession()
    session.proposals.append(_replay_passed())
    with pytest.raises(BizError) as raised:
        await release_agent(session, 100, True, None, None, 9, 2)  # type: ignore[arg-type]
    assert raised.value.code == "10404"
    assert session.proposals[0].status == "REPLAY_PASSED"


@pytest.mark.asyncio
async def test_manifest_returns_cards_without_heavy_asset_bodies() -> None:
    """清单只有卡片和效用后验，不带回记忆正文或安装规格。"""
    session = MemorySession()
    _remember(
        session,
        EvolutionEvidence(
            tenant_id=1,
            asset_type="SKILL",
            asset_id=22,
            posterior_type="UTILITY",
            context_key="multi_repo_refactor",
            source_type="REPLAY_RESULT",
            source_ref="artifact:test",
            outcome="POSITIVE",
            weight=1.0,
            evidence_json=None,
            alpha=11.48,
            beta=2.52,
            posterior_mean=0.82,
            effective_sample_size=12.0,
            gmt_create=datetime(2026, 9, 24, 8, 0, 0),
        ),
    )
    memory = MemoryView(
        id=11,
        title="用户偏好：lean engine",
        content_md=_HEAVY_MEMORY,
        type="PREFERENCE",
        scope="GLOBAL",
        version=3,
    )
    skill = SkillView(
        id=22,
        name="multi-repo-refactor-safety",
        type="CODING",
        description="Before multi-repo edits, load repo-map.",
        install_spec=_HEAVY_INSTALL,
        version=4,
    )
    relation = RepoRelationView(
        id=33,
        from_repo_id=100,
        to_repo_id=200,
        relation_type="CONSUMES_API",
        description="frontend consumes backend api",
    )
    memories = AsyncMock(return_value=[memory])
    skills = AsyncMock(return_value=_page([skill]))
    relations = AsyncMock(return_value=[relation])
    with (
        patch("autowonder.evolution.manifest.list_memories", memories),
        patch("autowonder.evolution.manifest.list_skills", skills),
        patch("autowonder.evolution.manifest.list_relations", relations),
    ):
        result = await manifest(session, 1, None, "multi_repo_refactor", 10)  # type: ignore[arg-type]
    assert memories.await_args.args == (
        session,
        1,
        None,
        None,
        None,
        "ADOPTED",
        None,
        None,
        1,
        10,
    )
    assert skills.await_args.args == (session, 1, None, None, False, False, 1, 10)
    assert relations.await_args.args == (session, 1)
    cards = result["cards"]
    assert isinstance(cards, list)
    assert len(cards) == 3
    skill_card = next(card for card in cards if card["assetType"] == "SKILL")
    assert skill_card["assetId"] == 22
    assert skill_card["name"] == "multi-repo-refactor-safety"
    assert skill_card["lazyLoadRef"] == "/api/skills/22"
    assert skill_card["posteriorMean"] == 0.82
    assert skill_card["effectiveSampleSize"] == 12.0
    memory_card = next(card for card in cards if card["assetType"] == "MEMORY")
    assert memory_card["category"] == "GLOBAL/PREFERENCE"
    assert memory_card["triggerHint"] == "用户偏好：lean engine"
    assert memory_card["lazyLoadRef"] == "/api/memories/11"
    assert memory_card["posteriorMean"] is None
    relation_card = next(card for card in cards if card["assetType"] == "REPO_RELATION")
    assert relation_card["name"] == "100 CONSUMES_API 200"
    assert relation_card["lazyLoadRef"] == "/api/repos/relations?repoId=100"
    assert relation_card["version"] is None
    rendered = dump_json(result)
    assert _HEAVY_MEMORY not in rendered
    assert _HEAVY_INSTALL not in rendered


@pytest.mark.asyncio
async def test_manifest_normalizes_type_and_clamps_limit() -> None:
    """类型去空白并大写；条数落在 1 到 50。未知类型不查资产。"""
    session = MemorySession()
    memories = AsyncMock(return_value=[])
    skills = AsyncMock(return_value=_page([]))
    relations = AsyncMock(return_value=[])
    with (
        patch("autowonder.evolution.manifest.list_memories", memories),
        patch("autowonder.evolution.manifest.list_skills", skills),
        patch("autowonder.evolution.manifest.list_relations", relations),
    ):
        only_skill = await manifest(session, 1, " skill ", "ctx", 80)  # type: ignore[arg-type]
        everything = await manifest(session, 1, "  ", None, None)  # type: ignore[arg-type]
        unknown = await manifest(session, 1, "WIDGET", None, 0)  # type: ignore[arg-type]
    assert only_skill["limit"] == 50
    assert only_skill["contextKey"] == "ctx"
    assert only_skill["cards"] == []
    assert skills.await_args_list[0].args[-1] == 50
    assert memories.await_count == 1
    assert relations.await_count == 1
    assert skills.await_count == 2
    assert everything["limit"] == 20
    assert unknown == {"contextKey": None, "limit": 1, "cards": []}


@pytest.mark.asyncio
async def test_manifest_truncates_hints_by_utf16_units_and_null_relation_text() -> None:
    """提示按 UTF-16 码元截到 160；空的关系编号按 Java 拼成 null。"""
    session = MemorySession()
    emoji = "😀"
    description = "a" * 159 + emoji
    skill = SkillView(id=22, name="safety", type="CODING", description=description, version=1)
    relation = RepoRelationView(id=33, from_repo_id=None, to_repo_id=None, relation_type=None)
    skills = AsyncMock(return_value=_page([skill]))
    relations = AsyncMock(return_value=[relation])
    with (
        patch("autowonder.evolution.manifest.list_skills", skills),
        patch("autowonder.evolution.manifest.list_relations", relations),
    ):
        skill_only = await manifest(session, 1, "SKILL", None, 1)  # type: ignore[arg-type]
        relation_only = await manifest(session, 1, "REPO_RELATION", None, 1)  # type: ignore[arg-type]
    skill_cards = skill_only["cards"]
    assert isinstance(skill_cards, list)
    hint = skill_cards[0]["triggerHint"]
    assert isinstance(hint, str)
    encoded = hint.encode("utf-16-le", errors="surrogatepass")
    assert len(encoded) // 2 == 160
    assert encoded[-2:] == emoji.encode("utf-16-le")[:2]
    relation_cards = relation_only["cards"]
    assert isinstance(relation_cards, list)
    assert relation_cards[0]["name"] == "null null null"
    assert relation_cards[0]["lazyLoadRef"] == "/api/repos/relations?repoId=null"


def _replay_passed() -> EvolutionProposal:
    return EvolutionProposal(
        id=100,
        tenant_id=1,
        asset_type="MEMORY",
        asset_id=None,
        trigger_type="USER_CORRECTION",
        root_evidence_json=[],
        policy_json=None,
        candidate_patch_json=_MEMORY_PATCH,
        status="REPLAY_PASSED",
        lifecycle_json={"replay": {"verdict": "PASS"}},
        is_deleted=0,
        version=0,
        creator_id=2,
        gmt_create=datetime(2026, 9, 24, 8, 0, 0),
        gmt_modified=datetime(2026, 9, 24, 8, 0, 0),
    )


def _canary_inconclusive() -> EvolutionProposal:
    proposal = _replay_passed()
    proposal.lifecycle_json = {
        "replay": {"verdict": "PASS"},
        "gates": {"CANARY": {"verdict": "INCONCLUSIVE"}},
    }
    return proposal


def _page(items: list[object]) -> PageResult:
    return PageResult.model_validate(
        {"list": items, "total": len(items), "pageNum": 1, "pageSize": 10}
    )
