"""证据账本：把运行事件归一成贝叶斯证据，并按幂等键复用已有行。"""

import math

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.evolution.commands import EvidenceCommand, EvidenceEvent
from autowonder.evolution.jsontext import (
    blank,
    double_field,
    dump_json,
    first_text,
    java_trim,
    long_field,
    parse_json,
    text_field,
)
from autowonder.evolution.models import EvolutionEvidence
from autowonder.evolution.store import find_evidence_by_key, find_latest_evidence, insert_evidence

PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0


async def record_event(
    session: AsyncSession,
    event: EvidenceEvent,
    tenant_id: int,
    user_id: int,
) -> EvolutionEvidence:
    """写入事件。多资产参与时返回第一条证据，其余照常入账。"""
    if blank(event.idempotency_key):
        raise BizError(ErrorCode.PARAM_INVALID)
    commands = evidence_commands(event)
    first = await _record_command(session, commands[0], tenant_id, user_id)
    for command in commands[1:]:
        await _record_command(session, command, tenant_id, user_id)
    return first


async def record_evidence(
    session: AsyncSession,
    command: EvidenceCommand,
    tenant_id: int,
    user_id: int,
) -> EvolutionEvidence:
    """从最新后验继续累加一条证据。"""
    asset_type, asset_id, posterior_type, context_key = _validated(command)
    source_type, source_ref, outcome = _validated_source(command)
    latest = await find_latest_evidence(
        session,
        tenant_id,
        asset_type,
        asset_id,
        posterior_type,
        context_key,
    )
    alpha = PRIOR_ALPHA
    beta = PRIOR_BETA
    if latest is not None:
        alpha = latest.alpha
        beta = latest.beta
    weight = _credit(command.weight)
    observation = _observation(command)
    alpha += weight * observation
    beta += weight * (1.0 - observation)
    row = EvolutionEvidence(
        tenant_id=tenant_id,
        asset_type=asset_type,
        asset_id=asset_id,
        posterior_type=posterior_type,
        context_key=context_key,
        source_type=source_type,
        source_ref=source_ref,
        outcome=outcome,
        weight=weight,
        evidence_json=_evidence_json(command.evidence_json, observation),
        dependency_group=command.dependency_group,
        idempotency_key=command.idempotency_key,
        alpha=alpha,
        beta=beta,
        posterior_mean=alpha / (alpha + beta),
        effective_sample_size=alpha + beta - PRIOR_ALPHA - PRIOR_BETA,
        creator_id=user_id,
        gmt_create=now_local(),
    )
    return await insert_evidence(session, row)


def evidence_commands(event: EvidenceEvent) -> list[EvidenceCommand]:
    """没有资产用量时记事件本身，否则每个参与资产各记一条。"""
    usage = _asset_usage(event.raw_event_json)
    if usage is None or len(usage) == 0:
        return [
            _command(
                event,
                event.asset_type,
                event.asset_id,
                event.raw_event_json,
                event.idempotency_key,
            )
        ]
    commands: list[EvidenceCommand] = []
    for item in usage:
        if not isinstance(item, dict):
            raise BizError(ErrorCode.PARAM_INVALID)
        asset_type = text_field(item, "assetType")
        asset_id = long_field(item, "assetId")
        if asset_type is None or java_is_blank(asset_type):
            raise BizError(ErrorCode.PARAM_INVALID)
        if asset_id is None:
            raise BizError(ErrorCode.PARAM_INVALID)
        event_key = event.idempotency_key
        if event_key is None:
            raise BizError(ErrorCode.PARAM_INVALID)
        key = event_key + ":" + asset_type + ":" + str(asset_id)
        commands.append(
            _command(event, asset_type, asset_id, _usage_json(event.raw_event_json, item), key)
        )
    return commands


def normalize_outcome(raw_outcome: str | None) -> str:
    """把 PASS/FAIL 等别名收成 POSITIVE 或 NEGATIVE。"""
    normalized = None
    if raw_outcome is not None:
        normalized = java_trim(raw_outcome).upper()
    if normalized == "PASS" or normalized == "SUCCESS" or normalized == "POSITIVE":
        return "POSITIVE"
    if normalized == "FAIL" or normalized == "FAILED" or normalized == "NEGATIVE":
        return "NEGATIVE"
    raise BizError(ErrorCode.PARAM_INVALID)


def resolve_dependency(event: EvidenceEvent) -> str | None:
    """调用方没给分组时，用来源或资产上下文拼一个。"""
    if not blank(event.dependency_group) and event.dependency_group is not None:
        return java_trim(event.dependency_group)
    if not blank(event.source_type) and not blank(event.source_ref):
        return java_trim(event.source_type or "") + ":" + java_trim(event.source_ref or "")
    if not blank(event.asset_type) and event.asset_id is not None and not blank(event.context_key):
        return (
            java_trim(event.asset_type or "")
            + ":"
            + str(event.asset_id)
            + ":"
            + java_trim(event.context_key or "")
        )
    return None


async def _record_command(
    session: AsyncSession,
    command: EvidenceCommand,
    tenant_id: int,
    user_id: int,
) -> EvolutionEvidence:
    existing = None
    if command.idempotency_key is not None:
        existing = await find_evidence_by_key(session, tenant_id, command.idempotency_key)
    if existing is None:
        return await record_evidence(session, command, tenant_id, user_id)
    return existing


def _command(
    event: EvidenceEvent,
    asset_type: str | None,
    asset_id: int | None,
    evidence_json: str | None,
    idempotency_key: str | None,
) -> EvidenceCommand:
    return EvidenceCommand(
        asset_type=asset_type,
        asset_id=asset_id,
        posterior_type=event.posterior_type,
        context_key=event.context_key,
        source_type=event.source_type,
        source_ref=event.source_ref,
        outcome=normalize_outcome(event.raw_outcome),
        observation=event.observation,
        weight=event.weight,
        evidence_json=evidence_json,
        dependency_group=resolve_dependency(event),
        idempotency_key=idempotency_key,
    )


def _asset_usage(raw_event_json: str | None) -> list[object] | None:
    root = _parse_event_object(raw_event_json)
    if root is None:
        return None
    usage = root.get("assetUsage")
    if not isinstance(usage, list):
        return None
    return usage


def _usage_json(raw_event_json: str | None, usage: dict[str, object]) -> str:
    root = _parse_event_object(raw_event_json)
    if root is None:
        root = {}
    else:
        root = dict(root)
    features_raw = root.get("features")
    if isinstance(features_raw, dict):
        features = dict(features_raw)
    else:
        features = {}
    root["features"] = features
    participation = first_text(
        text_field(usage, "participation"),
        text_field(usage, "participationLevel"),
        text_field(usage, "role"),
    )
    _put_text(features, "participation", participation)
    _put_text(features, "outcomeQuality", text_field(usage, "outcomeQuality"))
    confidence = double_field(usage, "outcomeConfidence")
    if confidence is not None:
        features["outcomeConfidence"] = confidence
    confidence = double_field(usage, "confidence")
    if confidence is not None:
        features["confidence"] = confidence
    root["assetUsageEntry"] = usage
    return dump_json(root)


def _parse_event_object(raw_event_json: str | None) -> dict[str, object] | None:
    if blank(raw_event_json):
        return None
    parsed = parse_json(raw_event_json or "")
    if isinstance(parsed, dict):
        return parsed
    return None


def _validated(command: EvidenceCommand) -> tuple[str, int, str, str]:
    asset_type = command.asset_type
    asset_id = command.asset_id
    posterior_type = command.posterior_type
    context_key = command.context_key
    if asset_type is None or java_is_blank(asset_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    if asset_id is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    if posterior_type is None or java_is_blank(posterior_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    if context_key is None or java_is_blank(context_key):
        raise BizError(ErrorCode.PARAM_INVALID)
    _validated_source(command)
    return asset_type, asset_id, posterior_type, context_key


def _validated_source(command: EvidenceCommand) -> tuple[str, str, str]:
    source_type = command.source_type
    source_ref = command.source_ref
    outcome = command.outcome
    if source_type is None or java_is_blank(source_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    if source_ref is None or java_is_blank(source_ref):
        raise BizError(ErrorCode.PARAM_INVALID)
    if outcome is None or java_is_blank(outcome):
        raise BizError(ErrorCode.PARAM_INVALID)
    if outcome != "POSITIVE" and outcome != "NEGATIVE":
        raise BizError(ErrorCode.PARAM_INVALID)
    if command.observation is not None:
        if math.isnan(command.observation):
            raise BizError(ErrorCode.PARAM_INVALID)
        if command.observation < 0.0 or command.observation > 1.0:
            raise BizError(ErrorCode.PARAM_INVALID)
    if command.evidence_json is not None and not blank(command.evidence_json):
        parse_json(command.evidence_json)
    return source_type, source_ref, outcome


def _credit(weight: float | None) -> float:
    if weight is None:
        return 1.0
    if weight < 0.0:
        return 0.0
    if weight > 1.0:
        return 1.0
    return weight


def _observation(command: EvidenceCommand) -> float:
    if command.observation is None:
        if command.outcome == "POSITIVE":
            return 1.0
        return 0.0
    return command.observation


def _evidence_json(raw: str | None, observation: float) -> dict[str, object]:
    evidence: dict[str, object] = {}
    if raw is not None and not blank(raw):
        parsed = parse_json(raw)
        if isinstance(parsed, dict):
            evidence.update(parsed)
        else:
            evidence["raw"] = parsed
    evidence["observation"] = observation
    return evidence


def _put_text(target: dict[str, object], key: str, value: str | None) -> None:
    if value is None or java_is_blank(value):
        return
    target[key] = value
