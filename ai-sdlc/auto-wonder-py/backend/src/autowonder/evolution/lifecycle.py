"""提案从校验、回放、通过走到发布或驳回。发布才写入记忆、仓库关系或技能。"""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.evolution.commands import EvidenceCommand
from autowonder.evolution.evidence import record_evidence
from autowonder.evolution.jsontext import (
    as_dict,
    blank,
    dump_json,
    java_trim,
    long_field,
    parse_object,
    text_field,
)
from autowonder.evolution.models import EvolutionProposal
from autowonder.evolution.store import find_proposal, mark_proposal
from autowonder.memories.schemas import CreateMemoryRequest, MemoryView
from autowonder.memories.service import create_from_evolution_proposal, get_memory
from autowonder.repos.schemas import CreateRelationRequest, RepoRelationView
from autowonder.repos.service import create_relation
from autowonder.skills.schemas import CreateSkillRequest, SkillView, UpdateSkillRequest
from autowonder.skills.service import (
    PackageReference,
    create_from_package_reference,
    create_skill,
    get_skill,
    update_from_package_reference,
    update_skill,
)

_RETIRE_MESSAGE = "RETIRE requires an explicit agent-version unbind/release decision"


async def validate(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """PROPOSED 提案校验通过后进入 VALIDATED。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    if proposal.status != "PROPOSED":
        raise BizError(ErrorCode.CONFLICT)
    _require_evidence(proposal.root_evidence_json)
    _require_object(proposal.candidate_patch_json)
    validation = {
        "verdict": "PASS",
        "checks": ["schema", "traceableEvidence", "candidatePatch"],
    }
    lifecycle = _put_stage(proposal.lifecycle_json, "validation", validation)
    await _advance(session, proposal, tenant_id, "VALIDATED", lifecycle, user_id)


async def record_replay(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    replay_json: str | None,
    user_id: int,
) -> None:
    """VALIDATED 提案记下回放结论。有资产编号时同步写入 UPLIFT 证据。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    if proposal.status != "VALIDATED":
        raise BizError(ErrorCode.CONFLICT)
    replay = _require_object(replay_json)
    verdict = text_field(replay, "verdict")
    if verdict == "PASS":
        status = "REPLAY_PASSED"
    elif verdict == "FAIL" or verdict == "INCONCLUSIVE":
        status = "REPLAY_" + verdict
    else:
        raise BizError(ErrorCode.PARAM_INVALID)
    lifecycle = _put_stage(proposal.lifecycle_json, "replay", replay)
    await _advance(session, proposal, tenant_id, status, lifecycle, user_id)
    await _record_replay_evidence(session, proposal, replay_json, verdict, tenant_id, user_id)


async def approve(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """回放通过或试验采纳之后，才能进入 APPROVED。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    if proposal.status != "REPLAY_PASSED" and proposal.status != "TRIAL_ADOPTED":
        raise BizError(ErrorCode.CONFLICT)
    await _advance(
        session,
        proposal,
        tenant_id,
        "APPROVED",
        proposal.lifecycle_json,
        user_id,
    )


async def release(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """APPROVED 且回放通过或试验采纳后，把补丁写成正式资产。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    if proposal.status != "APPROVED" or not _release_evidence_passed(proposal):
        raise BizError(ErrorCode.CONFLICT)
    patch = _require_object(proposal.candidate_patch_json)
    before_json = await _capture_before(session, proposal)
    released, after_json = await _release_asset(
        session,
        proposal,
        patch,
        tenant_id,
        proposal_id,
        user_id,
    )
    release_body: dict[str, object] = {
        "proposalId": proposal_id,
        "assetType": proposal.asset_type,
    }
    for key, value in released.items():
        release_body[key] = value
    if before_json is not None:
        release_body["beforeJson"] = before_json
    release_body["afterJson"] = after_json
    lifecycle = _put_stage(proposal.lifecycle_json, "release", release_body)
    await _advance(session, proposal, tenant_id, "RELEASED", lifecycle, user_id)


async def reject(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    reason: str | None,
    user_id: int,
) -> None:
    """驳回提案。原因缺省写成空字符串。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    reason_text = ""
    if reason is not None:
        reason_text = reason
    release_body = {"verdict": "REJECT", "reason": reason_text}
    lifecycle = _put_stage(proposal.lifecycle_json, "release", release_body)
    await _advance(session, proposal, tenant_id, "REJECTED", lifecycle, user_id)


async def _require_proposal(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
) -> EvolutionProposal:
    proposal = await find_proposal(session, proposal_id)
    if proposal is None or proposal.tenant_id != tenant_id:
        raise BizError(ErrorCode.NOT_FOUND)
    return proposal


async def _advance(
    session: AsyncSession,
    proposal: EvolutionProposal,
    tenant_id: int,
    status: str,
    lifecycle: object,
    user_id: int,
) -> None:
    version = proposal.version
    updated = await mark_proposal(
        session,
        proposal.id,
        tenant_id,
        status,
        lifecycle,
        version,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.CONFLICT)
    proposal.status = status
    proposal.lifecycle_json = lifecycle
    proposal.version = version + 1
    proposal.modifier_id = user_id


async def _record_replay_evidence(
    session: AsyncSession,
    proposal: EvolutionProposal,
    replay_json: str | None,
    verdict: str,
    tenant_id: int,
    user_id: int,
) -> None:
    if proposal.asset_id is None or verdict == "INCONCLUSIVE":
        return
    patch = _require_object(proposal.candidate_patch_json)
    context_key = text_field(patch, "contextKey")
    if context_key is None:
        context_key = proposal.asset_type + ":" + str(proposal.asset_id)
    outcome = "NEGATIVE"
    if verdict == "PASS":
        outcome = "POSITIVE"
    await record_evidence(
        session,
        EvidenceCommand(
            asset_type=proposal.asset_type,
            asset_id=proposal.asset_id,
            posterior_type="UPLIFT",
            context_key=context_key,
            source_type="REPLAY_RESULT",
            source_ref="proposal:" + str(proposal.id) + ":replay",
            outcome=outcome,
            evidence_json=replay_json,
        ),
        tenant_id,
        user_id,
    )


def _release_evidence_passed(proposal: EvolutionProposal) -> bool:
    replay = _stage(proposal.lifecycle_json, "replay")
    if not _stage_blank(replay) and _verdict_pass(replay):
        return True
    return _trial_adopted(_stage(proposal.lifecycle_json, "trial"))


def _verdict_pass(value: object) -> bool:
    return text_field(_require_object(value), "verdict") == "PASS"


def _trial_adopted(value: object) -> bool:
    if _stage_blank(value):
        return False
    parsed = as_dict(value)
    if parsed is None:
        return False
    return text_field(parsed, "decision") == "ADOPT"


async def _capture_before(session: AsyncSession, proposal: EvolutionProposal) -> str | None:
    asset_id = proposal.asset_id
    if proposal.asset_type == "SKILL" and asset_id is not None and asset_id > 0:
        return _view_json(await get_skill(session, asset_id))
    if proposal.asset_type == "MEMORY" and asset_id is not None:
        return _view_json(await get_memory(session, asset_id))
    return None


async def _release_asset(
    session: AsyncSession,
    proposal: EvolutionProposal,
    patch: dict[str, object],
    tenant_id: int,
    proposal_id: int,
    user_id: int,
) -> tuple[dict[str, object], str]:
    if proposal.asset_type == "MEMORY":
        memory = await create_from_evolution_proposal(
            session,
            _memory_request(patch),
            tenant_id,
            proposal_id,
            user_id,
        )
        return {"assetId": memory.id}, _view_json(memory)
    if proposal.asset_type == "REPO_RELATION":
        relation = await create_relation(session, _relation_request(patch), tenant_id, user_id)
        return {"assetId": relation.id}, _view_json(relation)
    if proposal.asset_type == "SKILL":
        return await _release_skill(session, proposal, patch, tenant_id, user_id)
    raise BizError(ErrorCode.PARAM_INVALID)


async def _release_skill(
    session: AsyncSession,
    proposal: EvolutionProposal,
    patch: dict[str, object],
    tenant_id: int,
    user_id: int,
) -> tuple[dict[str, object], str]:
    action = text_field(patch, "policyAction")
    if action is not None and action.upper() == "RETIRE":
        raise BizError(ErrorCode.CONFLICT, _RETIRE_MESSAGE)
    mode = _skill_mode(text_field(patch, "mode"))
    package = _package(patch)
    skill: SkillView
    if mode == "CREATE":
        if package is None:
            skill = await create_skill(session, _skill_create(patch), tenant_id, user_id)
        else:
            skill = await create_from_package_reference(
                session,
                _skill_create(patch),
                package,
                tenant_id,
                user_id,
            )
    else:
        asset_id = proposal.asset_id
        if asset_id is None:
            raise BizError(ErrorCode.PARAM_INVALID)
        if package is None:
            skill = await update_skill(session, asset_id, _skill_update(patch), tenant_id, user_id)
        else:
            skill = await update_from_package_reference(
                session,
                asset_id,
                _skill_update(patch),
                package,
                tenant_id,
                user_id,
            )
    return {
        "mode": mode,
        "assetId": skill.id,
        "assetVersion": skill.version,
    }, _view_json(skill)


def _skill_mode(value: str | None) -> str:
    if value is None or blank(value):
        return "UPDATE"
    normalized = java_trim(value).upper()
    if normalized != "CREATE" and normalized != "UPDATE":
        raise BizError(ErrorCode.PARAM_INVALID)
    return normalized


def _package(patch: dict[str, object]) -> PackageReference | None:
    oss_ref = text_field(patch, "packageOssRef")
    if oss_ref is None or blank(oss_ref):
        return None
    return PackageReference(
        oss_ref=oss_ref,
        file_name=text_field(patch, "packageFileName"),
        size=long_field(patch, "packageSize"),
        md5=text_field(patch, "packageMd5"),
    )


def _memory_request(patch: dict[str, object]) -> CreateMemoryRequest:
    return CreateMemoryRequest(
        scope=text_field(patch, "scope"),
        owner_ref=long_field(patch, "ownerRef"),
        type=text_field(patch, "type"),
        title=text_field(patch, "title"),
        content_md=text_field(patch, "contentMd"),
    )


def _relation_request(patch: dict[str, object]) -> CreateRelationRequest:
    return CreateRelationRequest(
        from_repo_id=long_field(patch, "fromRepoId"),
        to_repo_id=long_field(patch, "toRepoId"),
        relation_type=text_field(patch, "relationType"),
        description=text_field(patch, "description"),
        ai_session_id=long_field(patch, "aiSessionId"),
    )


def _skill_create(patch: dict[str, object]) -> CreateSkillRequest:
    return CreateSkillRequest(
        name=text_field(patch, "name"),
        type=text_field(patch, "type"),
        install_spec=text_field(patch, "installSpec"),
        description=text_field(patch, "description"),
    )


def _skill_update(patch: dict[str, object]) -> UpdateSkillRequest:
    return UpdateSkillRequest(
        name=text_field(patch, "name"),
        type=text_field(patch, "type"),
        install_spec=text_field(patch, "installSpec"),
        description=text_field(patch, "description"),
    )


def _view_json(view: MemoryView | SkillView | RepoRelationView) -> str:
    return dump_json(dump_data(view))


def _require_evidence(value: object) -> None:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise BizError(ErrorCode.PARAM_INVALID) from error
    if not isinstance(parsed, list) or len(parsed) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    for item in parsed:
        if not isinstance(item, dict):
            raise BizError(ErrorCode.PARAM_INVALID)
        source_type = text_field(item, "sourceType")
        source_ref = text_field(item, "sourceRef")
        if source_type is None or blank(source_type):
            raise BizError(ErrorCode.PARAM_INVALID)
        if source_ref is None or blank(source_ref):
            raise BizError(ErrorCode.PARAM_INVALID)


def _require_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        if len(value) == 0:
            raise BizError(ErrorCode.PARAM_INVALID)
        return value
    if isinstance(value, str):
        return parse_object(value)
    raise BizError(ErrorCode.PARAM_INVALID)


def _stage(lifecycle: object, name: str) -> object | None:
    if not isinstance(lifecycle, dict):
        return None
    return lifecycle.get(name)


def _stage_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return blank(value)
    return False


def _put_stage(lifecycle: object, name: str, value: object) -> dict[str, object]:
    stored: dict[str, object] = {}
    if isinstance(lifecycle, dict):
        stored = dict(lifecycle)
    stored[name] = value
    return stored
