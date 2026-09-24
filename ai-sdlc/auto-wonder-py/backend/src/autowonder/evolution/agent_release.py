"""显式允许后，把门禁通过的提案审批并发布。"""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.jsontext import as_dict, blank, text_field
from autowonder.evolution.lifecycle import _require_proposal, _stage, approve, release


async def release_agent(
    session: AsyncSession,
    proposal_id: int,
    allow_release: bool | None,
    required_gate_types: list[str] | None,
    allow_canary_inconclusive: bool | None,
    tenant_id: int,
    user_id: int,
) -> dict[str, object]:
    """回放通过或试验采纳，且要求的门禁通过时，才发布。失败的灰度一律拦住。"""
    if allow_release is not True:
        raise BizError(ErrorCode.PARAM_INVALID)
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    replay_ready = proposal.status == "REPLAY_PASSED" and _marked(
        _stage(proposal.lifecycle_json, "replay"),
        "verdict",
        "PASS",
    )
    trial_ready = (
        proposal.status == "TRIAL_ADOPTED" or proposal.status == "APPROVED"
    ) and _marked(_stage(proposal.lifecycle_json, "trial"), "decision", "ADOPT")
    if not replay_ready and not trial_ready:
        raise BizError(ErrorCode.CONFLICT)
    gates = _gates(_stage(proposal.lifecycle_json, "gates"))
    _require_gates(gates, required_gate_types, allow_canary_inconclusive)
    _block_failed_canary(gates)
    if replay_ready or proposal.status == "TRIAL_ADOPTED":
        await approve(session, proposal_id, tenant_id, user_id)
    await release(session, proposal_id, tenant_id, user_id)
    return {"proposalId": proposal_id, "action": "RELEASED", "status": "RELEASED"}


def _marked(value: object, key: str, expected: str) -> bool:
    parsed = as_dict(value)
    if parsed is None:
        return False
    return text_field(parsed, key) == expected


def _gates(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and blank(value):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise BizError(ErrorCode.CONFLICT) from error
        if parsed is None:
            return {}
        if isinstance(parsed, dict):
            return parsed
    raise BizError(ErrorCode.CONFLICT)


def _require_gates(
    gates: dict[str, object],
    required_gate_types: list[str] | None,
    allow_canary_inconclusive: bool | None,
) -> None:
    if required_gate_types is None or len(required_gate_types) == 0:
        return
    for gate_type in required_gate_types:
        if blank(gate_type):
            raise BizError(ErrorCode.PARAM_INVALID)
        latest = gates.get(gate_type)
        if not isinstance(latest, dict):
            raise BizError(ErrorCode.CONFLICT)
        verdict = text_field(latest, "verdict")
        if verdict == "PASS":
            continue
        if (
            gate_type == "CANARY"
            and verdict == "INCONCLUSIVE"
            and allow_canary_inconclusive is True
        ):
            continue
        raise BizError(ErrorCode.CONFLICT)


def _block_failed_canary(gates: dict[str, object]) -> None:
    canary = gates.get("CANARY")
    if isinstance(canary, dict) and text_field(canary, "verdict") == "FAIL":
        raise BizError(ErrorCode.CONFLICT)
