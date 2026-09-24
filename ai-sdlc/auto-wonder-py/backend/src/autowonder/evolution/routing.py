"""按资产类型把草案收成可追溯提案，并写入 evolution_proposal。"""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.commands import ProposalCommand, RunCommand, RunResult
from autowonder.evolution.jsontext import (
    blank,
    dump_json,
    java_trim,
    long_field,
    parse_object,
    text_field,
)
from autowonder.evolution.models import EvolutionProposal
from autowonder.evolution.store import insert_proposal


async def route(
    session: AsyncSession,
    command: RunCommand,
    tenant_id: int,
    user_id: int,
) -> RunResult:
    """MEMORY、REPO_RELATION、SKILL 各自建提案。其他类型直接拒绝。"""
    asset_type = command.asset_type
    root_evidence = command.root_evidence_json
    suggested_patch = command.suggested_patch_json
    if asset_type is None or blank(asset_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    if root_evidence is None or blank(root_evidence):
        raise BizError(ErrorCode.PARAM_INVALID)
    if suggested_patch is None or blank(suggested_patch):
        raise BizError(ErrorCode.PARAM_INVALID)
    if asset_type == "MEMORY":
        proposal_command = build_memory_proposal(command)
    elif asset_type == "REPO_RELATION":
        proposal_command = build_repo_proposal(command)
    elif asset_type == "SKILL":
        proposal_command = build_skill_proposal(command)
    else:
        raise BizError(ErrorCode.PARAM_INVALID)
    proposal = await propose(session, proposal_command, tenant_id, user_id)
    return RunResult(
        proposal.id,
        proposal.status,
        asset_type,
        proposal_command.asset_id,
    )


async def propose(
    session: AsyncSession,
    command: ProposalCommand,
    tenant_id: int,
    user_id: int,
) -> EvolutionProposal:
    """校验证据和补丁后插入 PROPOSED 提案。"""
    asset_type = command.asset_type
    trigger_type = command.trigger_type
    if asset_type is None or blank(asset_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    if trigger_type is None or blank(trigger_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    evidence = _require_evidence(command.root_evidence_json)
    patch = _require_patch(command.candidate_patch_json)
    policy: object | None = None
    if command.policy_json is not None and not blank(command.policy_json):
        policy = json.loads(command.policy_json)
    row = EvolutionProposal(
        tenant_id=tenant_id,
        asset_type=asset_type,
        asset_id=command.asset_id,
        trigger_type=trigger_type,
        root_evidence_json=evidence,
        policy_json=policy,
        candidate_patch_json=patch,
        status="PROPOSED",
        creator_id=user_id,
        is_deleted=0,
        version=0,
        gmt_create=now_local(),
        gmt_modified=now_local(),
    )
    return await insert_proposal(session, row)


def build_memory_proposal(command: RunCommand) -> ProposalCommand:
    """记忆提案不绑定已有资产，归属默认落到报告它的数字员工。"""
    suggested = _suggested(command)
    title = text_field(suggested, "title")
    content_md = text_field(suggested, "contentMd")
    if blank(title) or blank(content_md):
        raise BizError(ErrorCode.PARAM_INVALID)
    scope = text_field(suggested, "scope")
    if blank(scope):
        if command.source_agent_id is None:
            scope = "ORG"
        else:
            scope = "AGENT"
    patch: dict[str, object] = {"scope": scope}
    owner_ref = long_field(suggested, "ownerRef")
    if "ownerRef" in suggested and suggested.get("ownerRef") is not None:
        patch["ownerRef"] = owner_ref
    elif (
        isinstance(scope, str) and scope.upper() == "AGENT" and command.source_agent_id is not None
    ):
        patch["ownerRef"] = command.source_agent_id
    patch["type"] = _text_or_default(text_field(suggested, "type"), "FACT")
    patch["title"] = title
    patch["contentMd"] = content_md
    _put_context(patch, command.context_key)
    patch["proposalBuilder"] = "MEMORY_LITE"
    patch["failureSummary"] = command.failure_summary
    patch["policyAction"] = _policy_action(command.policy_json)
    return _proposal(command, "MEMORY", None, patch)


def build_repo_proposal(command: RunCommand) -> ProposalCommand:
    """仓库关系提案记录两端仓库和关系类型。"""
    suggested = _suggested(command)
    from_repo_id = long_field(suggested, "fromRepoId")
    to_repo_id = long_field(suggested, "toRepoId")
    relation_type = text_field(suggested, "relationType")
    description = text_field(suggested, "description")
    if from_repo_id is None or to_repo_id is None or blank(relation_type) or blank(description):
        raise BizError(ErrorCode.PARAM_INVALID)
    patch: dict[str, object] = {
        "fromRepoId": from_repo_id,
        "toRepoId": to_repo_id,
        "relationType": relation_type,
        "description": description,
    }
    if suggested.get("aiSessionId") is not None:
        patch["aiSessionId"] = long_field(suggested, "aiSessionId")
    _put_context(patch, command.context_key)
    patch["proposalBuilder"] = "REPO_MAP_LITE"
    patch["failureSummary"] = command.failure_summary
    patch["policyAction"] = _policy_action(command.policy_json)
    proposal = _proposal(command, "REPO_RELATION", None, patch)
    return proposal


def build_skill_proposal(command: RunCommand) -> ProposalCommand:
    """动作策略决定 CREATE 还是 UPDATE，覆盖工人补丁里的 mode。"""
    suggested = _suggested(command)
    action = _policy_action(command.policy_json)
    mode = _skill_mode(text_field(suggested, "mode"), action)
    if mode == "UPDATE" and command.asset_id is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    name = text_field(suggested, "name")
    skill_type = text_field(suggested, "type")
    install_spec = text_field(suggested, "installSpec")
    description = text_field(suggested, "description")
    if blank(name) or blank(skill_type) or blank(install_spec) or blank(description):
        raise BizError(ErrorCode.PARAM_INVALID)
    patch: dict[str, object] = {
        "mode": mode,
        "name": name,
        "type": skill_type,
        "installSpec": install_spec,
        "description": description,
    }
    _put_text(patch, "packageOssRef", text_field(suggested, "packageOssRef"))
    _put_text(patch, "packageMd5", text_field(suggested, "packageMd5"))
    _put_text(patch, "packageFileName", text_field(suggested, "packageFileName"))
    package_size = long_field(suggested, "packageSize")
    if package_size is not None:
        patch["packageSize"] = package_size
    _put_context(patch, command.context_key)
    patch["proposalBuilder"] = "SKILL_LITE"
    patch["failureSummary"] = command.failure_summary
    patch["policyAction"] = action
    return _proposal(command, "SKILL", command.asset_id, patch)


def _proposal(
    command: RunCommand,
    asset_type: str,
    asset_id: int | None,
    patch: dict[str, object],
) -> ProposalCommand:
    return ProposalCommand(
        asset_type=asset_type,
        asset_id=asset_id,
        trigger_type="PROPOSAL_BUILDER_LITE",
        root_evidence_json=command.root_evidence_json,
        policy_json=command.policy_json,
        candidate_patch_json=dump_json(patch),
    )


def _skill_mode(value: str | None, policy_action: str | None) -> str:
    if policy_action == "CREATE" or policy_action == "SPLIT":
        return "CREATE"
    if policy_action == "PATCH" or policy_action == "COMPRESS" or policy_action == "RETIRE":
        return "UPDATE"
    if value is None or blank(value):
        return "UPDATE"
    normalized = java_trim(value).upper()
    if normalized != "CREATE" and normalized != "UPDATE":
        raise BizError(ErrorCode.PARAM_INVALID)
    return normalized


def _policy_action(policy_json: str | None) -> str | None:
    parsed = _policy_object(policy_json)
    if parsed is None:
        return None
    action = parsed.get("action")
    if isinstance(action, str):
        return action
    return None


def _policy_object(policy_json: str | None) -> dict[str, object] | None:
    if policy_json is None or blank(policy_json):
        return None
    try:
        parsed = json.loads(policy_json)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _suggested(command: RunCommand) -> dict[str, object]:
    if command.suggested_patch_json is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    return parse_object(command.suggested_patch_json)


def _text_or_default(value: str | None, fallback: str) -> str:
    if value is None or blank(value):
        return fallback
    return value


def _put_context(patch: dict[str, object], context_key: str | None) -> None:
    if not blank(context_key):
        patch["contextKey"] = context_key


def _put_text(patch: dict[str, object], key: str, value: str | None) -> None:
    if value is None or blank(value):
        return
    patch[key] = value


def _require_evidence(raw: str | None) -> list[object]:
    if raw is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    if not isinstance(parsed, list) or len(parsed) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    for item in parsed:
        if not isinstance(item, dict):
            raise BizError(ErrorCode.PARAM_INVALID)
        source_type = item.get("sourceType")
        source_ref = item.get("sourceRef")
        if not isinstance(source_type, str) or blank(source_type):
            raise BizError(ErrorCode.PARAM_INVALID)
        if not isinstance(source_ref, str) or blank(source_ref):
            raise BizError(ErrorCode.PARAM_INVALID)
    return parsed


def _require_patch(raw: str | None) -> dict[str, object]:
    if raw is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    return parse_object(raw)
