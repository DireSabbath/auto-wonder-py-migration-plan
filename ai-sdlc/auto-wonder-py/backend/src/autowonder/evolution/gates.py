"""把门禁结论写进提案 lifecycle 的 gates 阶段。不单独落运行表。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.jsontext import as_dict, blank, parse_json
from autowonder.evolution.lifecycle import _advance, _put_stage, _require_proposal

_GATE_TYPES = {"BENCHMARK", "SHADOW", "CANARY"}
_VERDICTS = {"PASS", "FAIL", "INCONCLUSIVE"}


async def record_gate(
    session: AsyncSession,
    proposal_id: int | None,
    gate_type: str | None,
    verdict: str | None,
    result_json: str | None,
    tenant_id: int,
    user_id: int,
) -> dict[str, object]:
    """校验门禁类型和结论后，按类型覆盖写入 gates。"""
    if proposal_id is None or blank(gate_type) or blank(verdict) or blank(result_json):
        raise BizError(ErrorCode.PARAM_INVALID)
    if gate_type not in _GATE_TYPES:
        raise BizError(ErrorCode.PARAM_INVALID)
    if verdict not in _VERDICTS:
        raise BizError(ErrorCode.PARAM_INVALID)
    if result_json is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    result = parse_json(result_json)
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    gates = _gates(proposal.lifecycle_json)
    gate = {
        "proposalId": proposal_id,
        "gateType": gate_type,
        "verdict": verdict,
        "result": result,
        "creatorId": user_id,
    }
    gates[gate_type] = gate
    lifecycle = _put_stage(proposal.lifecycle_json, "gates", gates)
    await _advance(session, proposal, tenant_id, proposal.status, lifecycle, user_id)
    return {
        "id": None,
        "tenantId": tenant_id,
        "proposalId": proposal_id,
        "gateType": gate_type,
        "verdict": verdict,
        "resultJson": result_json,
        "gmtCreate": None,
        "creatorId": user_id,
    }


def _gates(lifecycle: object) -> dict[str, object]:
    if not isinstance(lifecycle, dict):
        return {}
    value = lifecycle.get("gates")
    if value is None:
        return {}
    if isinstance(value, str) and blank(value):
        return {}
    if isinstance(value, dict):
        return dict(value)
    parsed = as_dict(value)
    if parsed is None:
        raise BizError(ErrorCode.CONFLICT)
    return parsed
