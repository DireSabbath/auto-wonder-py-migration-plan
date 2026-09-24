"""把 learning_delta/evolution_delta.json 收成编排命令并执行。"""

import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.evolution.commands import EvidenceEvent, OrchestrateCommand
from autowonder.evolution.jsontext import (
    bool_field,
    dump_json,
    first_text,
    java_trim,
    long_field,
    parse_json,
    text_field,
)
from autowonder.evolution.orchestrator import orchestrate

_KEY_PART = re.compile(r"[^a-z0-9]+")
_EDGE_DASH = re.compile(r"(^-+|-+$)")


async def ingest_evolution_delta(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    dispatch_id: int,
    payload: bytes,
    mode: str,
) -> None:
    """逐条候选编排。缺字段时按参数不合法拒绝。"""
    for index, candidate in enumerate(_candidates(payload)):
        command = build_orchestrate_command(candidate, agent_id, dispatch_id, index, mode)
        await orchestrate(session, command, tenant_id, agent_id)


def build_orchestrate_command(
    candidate: dict[str, object],
    agent_id: int,
    dispatch_id: int,
    index: int,
    mode: str,
) -> OrchestrateCommand:
    """把工人增量里的一个候选收成编排命令。"""
    asset_type = _required_text(candidate, "assetType")
    asset_id = long_field(candidate, "assetId")
    patch = _suggested_patch(candidate)
    if asset_id is None:
        mode_text = text_field(patch, "mode")
        if (
            asset_type.upper() == "SKILL"
            and isinstance(mode_text, str)
            and mode_text.upper() == "CREATE"
        ):
            asset_id = 0
        else:
            raise BizError(ErrorCode.PARAM_INVALID)
    _apply_memory_defaults(candidate, patch, agent_id)
    pattern = _task_pattern_key(candidate)
    return OrchestrateCommand(
        evidence_event=_event(candidate, asset_type, asset_id, pattern, dispatch_id, index),
        candidate_asset_type=_text_or(text_field(candidate, "candidateAssetType"), asset_type),
        candidate_asset_id=_candidate_asset_id(candidate, asset_id),
        context_key=pattern,
        source_agent_id=agent_id,
        failure_summary=text_field(candidate, "failureSummary"),
        root_evidence_json=text_field(candidate, "rootEvidenceJson"),
        suggested_patch_json=dump_json(patch),
        draft_delta_json=text_field(candidate, "draftDeltaJson"),
        replay_suite_json=text_field(candidate, "replaySuiteJson"),
        auto_validate_before_replay=_auto_validate(candidate, mode),
    )


def _candidates(payload: bytes) -> list[dict[str, object]]:
    try:
        root = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    if not isinstance(root, dict) or len(root) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    candidates = root.get("candidates")
    if not isinstance(candidates, list) or len(candidates) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or len(candidate) == 0:
            raise BizError(ErrorCode.PARAM_INVALID)
        rows.append(candidate)
    return rows


def _event(
    candidate: dict[str, object],
    asset_type: str,
    asset_id: int,
    task_pattern_key: str,
    dispatch_id: int,
    index: int,
) -> EvidenceEvent:
    source_ref = "dispatch:" + str(dispatch_id) + ":evolution:" + str(index)
    return EvidenceEvent(
        asset_type=asset_type,
        asset_id=asset_id,
        posterior_type=_text_or(text_field(candidate, "posteriorType"), "UTILITY"),
        context_key=task_pattern_key,
        source_type=_text_or(text_field(candidate, "sourceType"), "MODEL_SELF_REPORT"),
        source_ref=_text_or(text_field(candidate, "sourceRef"), source_ref),
        raw_outcome=_text_or(
            text_field(candidate, "rawOutcome"),
            _text_or(text_field(candidate, "outcome"), "FAIL"),
        ),
        raw_event_json=_raw_event_json(candidate, task_pattern_key),
        dependency_group=text_field(candidate, "dependencyGroup"),
        idempotency_key=_text_or(text_field(candidate, "idempotencyKey"), source_ref),
    )


def _candidate_asset_id(candidate: dict[str, object], asset_id: int) -> int | None:
    if "candidateAssetId" in candidate:
        return long_field(candidate, "candidateAssetId")
    return asset_id


def _auto_validate(candidate: dict[str, object], mode: str) -> bool:
    explicit = bool_field(candidate, "autoValidateBeforeReplay")
    if explicit is not None:
        return explicit
    replay = text_field(candidate, "replaySuiteJson")
    return mode == "AUTO_PROPOSAL" and isinstance(replay, str) and not java_is_blank(replay)


def _apply_memory_defaults(
    candidate: dict[str, object],
    patch: dict[str, object],
    agent_id: int,
) -> None:
    raw_type = _text_or(
        text_field(candidate, "candidateAssetType"),
        text_field(candidate, "assetType"),
    )
    if raw_type is None or raw_type.upper() != "MEMORY":
        return
    scope = text_field(patch, "scope")
    if scope is None or java_is_blank(scope):
        patch["scope"] = "AGENT"
        scope = "AGENT"
    if patch.get("ownerRef") is None and scope.upper() == "AGENT":
        patch["ownerRef"] = agent_id


def _suggested_patch(candidate: dict[str, object]) -> dict[str, object]:
    patch = candidate.get("suggestedPatch")
    if isinstance(patch, dict) and len(patch) > 0:
        return patch
    raw = text_field(candidate, "suggestedPatchJson")
    if raw is not None and not java_is_blank(raw):
        parsed = parse_json(raw)
        if isinstance(parsed, dict) and len(parsed) > 0:
            return parsed
    raise BizError(ErrorCode.PARAM_INVALID)


def _raw_event_json(candidate: dict[str, object], task_pattern_key: str) -> str:
    raw = candidate.get("rawEventJson")
    if isinstance(raw, dict) and len(raw) > 0:
        return dump_json(raw)
    if isinstance(raw, str) and not java_is_blank(raw):
        parse_json(raw)
        return raw
    features: dict[str, object] = {}
    _put_text(features, "failureMode", text_field(candidate, "failureMode"))
    _put_text(features, "taskType", text_field(candidate, "taskType"))
    _put_text(features, "primaryRepoGroup", text_field(candidate, "primaryRepoGroup"))
    _put_text(features, "operation", text_field(candidate, "operation"))
    _put_text(features, "harness", text_field(candidate, "harness"))
    _put_text(features, "toolUsePattern", text_field(candidate, "toolUsePattern"))
    _put_text(
        features,
        "participation",
        first_text(
            text_field(candidate, "participation"),
            text_field(candidate, "participationLevel"),
            text_field(candidate, "assetRole"),
        ),
    )
    _put_text(features, "outcomeQuality", text_field(candidate, "outcomeQuality"))
    event: dict[str, object] = {
        "source": "learning_delta/evolution_delta.json",
        "taskPatternKey": task_pattern_key,
    }
    _put_text(event, "summary", text_field(candidate, "failureSummary"))
    if len(features) > 0:
        event["features"] = features
    metrics = candidate.get("metrics")
    if isinstance(metrics, dict) and len(metrics) > 0:
        event["metrics"] = metrics
    return dump_json(event)


def _task_pattern_key(candidate: dict[str, object]) -> str:
    explicit = _text_or(
        text_field(candidate, "taskPatternKey"),
        text_field(candidate, "contextKey"),
    )
    if explicit is not None and not java_is_blank(explicit):
        return java_trim(explicit)
    task_type = _required_text(candidate, "taskType")
    primary_repo_group = _required_text(candidate, "primaryRepoGroup")
    operation = _required_text(candidate, "operation")
    return (
        _normalize_key_part(task_type)
        + ":"
        + _normalize_key_part(primary_repo_group)
        + ":"
        + _normalize_key_part(operation)
    )


def _normalize_key_part(value: str) -> str:
    lowered = java_trim(value).lower()
    dashed = _KEY_PART.sub("-", lowered)
    return _EDGE_DASH.sub("", dashed)


def _required_text(candidate: dict[str, object], key: str) -> str:
    value = text_field(candidate, key)
    if value is None or java_is_blank(value):
        raise BizError(ErrorCode.PARAM_INVALID)
    return value


def _text_or(value: str | None, fallback: str | None) -> str | None:
    if value is None or java_is_blank(value):
        return fallback
    return value


def _put_text(target: dict[str, object], key: str, value: str | None) -> None:
    if value is None or java_is_blank(value):
        return
    target[key] = value
