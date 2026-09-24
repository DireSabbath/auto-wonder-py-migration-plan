"""没有模型草案时，用失败摘要或证据里的结构化字段补一张补丁。"""

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.commands import (
    EvidenceEvent,
    OrchestrateCommand,
    PolicyDecision,
    RunCommand,
)
from autowonder.evolution.jsontext import blank, dump_json, long_field, parse_object, text_field


def draft(
    command: OrchestrateCommand,
    event: EvidenceEvent,
    policy: PolicyDecision | None,
) -> RunCommand:
    """优先采用已有补丁；仓库关系和技能在缺补丁时从证据还原。"""
    if blank(command.candidate_asset_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    run = RunCommand(
        policy_json=None if policy is None else policy.policy_json,
        asset_type=command.candidate_asset_type,
        asset_id=command.candidate_asset_id,
        root_evidence_json=_root_evidence(command, event),
        failure_summary=command.failure_summary,
        context_key=_context_key(command, event),
        source_agent_id=command.source_agent_id,
    )
    sourced = resolve_patch(command, event)
    if not blank(sourced):
        run.suggested_patch_json = sourced
        return run
    if command.candidate_asset_type == "REPO_RELATION":
        run.suggested_patch_json = _repo_relation_patch(event)
        return run
    if command.candidate_asset_type == "SKILL":
        run.suggested_patch_json = _skill_patch(command, event)
        return run
    raise BizError(ErrorCode.PARAM_INVALID)


def resolve_patch(command: OrchestrateCommand, event: EvidenceEvent) -> str | None:
    """显式补丁优先，其次从 draft delta 取出 patch。"""
    if not blank(command.suggested_patch_json):
        return command.suggested_patch_json
    if not blank(command.draft_delta_json):
        return _patch_from_draft_delta(command.draft_delta_json or "")
    return _memory_fallback(command, event)


def _patch_from_draft_delta(draft_delta_json: str) -> str:
    delta = parse_object(draft_delta_json)
    patch = delta.get("patch")
    if not isinstance(patch, dict):
        patch = delta.get("draftPatch")
    if not isinstance(patch, dict) or len(patch) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    return dump_json(patch)


def _memory_fallback(command: OrchestrateCommand, event: EvidenceEvent) -> str | None:
    if command.candidate_asset_type != "MEMORY":
        return None
    summary = command.failure_summary
    if blank(summary):
        summary = (
            "Evidence from " + _java_null(event.source_type) + ":" + _java_null(event.source_ref)
        )
    patch = {
        "title": "Learning from " + _topic(command, event) + " failure",
        "contentMd": summary,
        "scope": "GLOBAL",
        "type": "FACT",
    }
    return dump_json(patch)


def _topic(command: OrchestrateCommand, event: EvidenceEvent) -> str:
    context = command.context_key
    if blank(context):
        context = event.context_key
    if context is None or blank(context):
        return "evolution"
    index = context.rfind(":")
    if index >= 0 and index + 1 < len(context):
        topic = context[index + 1 :]
    else:
        topic = context
    if blank(topic):
        return "evolution"
    return topic


def _repo_relation_patch(event: EvidenceEvent) -> str:
    raw = _raw_object(event)
    from_repo_id = long_field(raw, "fromRepoId")
    to_repo_id = long_field(raw, "toRepoId")
    relation_type = text_field(raw, "relationType")
    description = text_field(raw, "description")
    if from_repo_id is None or to_repo_id is None or blank(relation_type) or blank(description):
        raise BizError(ErrorCode.PARAM_INVALID)
    return dump_json(
        {
            "fromRepoId": from_repo_id,
            "toRepoId": to_repo_id,
            "relationType": relation_type,
            "description": description,
        }
    )


def _skill_patch(command: OrchestrateCommand, event: EvidenceEvent) -> str:
    if command.candidate_asset_id is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    raw = _raw_object(event)
    name = text_field(raw, "name")
    skill_type = text_field(raw, "type")
    install_spec = text_field(raw, "installSpec")
    description = text_field(raw, "description")
    if blank(name) or blank(skill_type) or blank(install_spec) or blank(description):
        raise BizError(ErrorCode.PARAM_INVALID)
    return dump_json(
        {
            "name": name,
            "type": skill_type,
            "installSpec": install_spec,
            "description": description,
        }
    )


def _root_evidence(command: OrchestrateCommand, event: EvidenceEvent) -> str:
    if not blank(command.root_evidence_json):
        return command.root_evidence_json or ""
    return dump_json([{"sourceType": event.source_type, "sourceRef": event.source_ref}])


def _context_key(command: OrchestrateCommand, event: EvidenceEvent) -> str | None:
    if blank(command.context_key):
        return event.context_key
    return command.context_key


def _raw_object(event: EvidenceEvent) -> dict[str, object]:
    if event.raw_event_json is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    return parse_object(event.raw_event_json)


def _java_null(value: str | None) -> str:
    if value is None:
        return "null"
    return value
