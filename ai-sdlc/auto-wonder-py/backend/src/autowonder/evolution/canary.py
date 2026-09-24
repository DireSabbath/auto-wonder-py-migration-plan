"""灰度结论先记门禁。通过或失败再入账，失败且要求驳回时驳回提案。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.commands import EvidenceEvent
from autowonder.evolution.evidence import record_event
from autowonder.evolution.gates import record_gate
from autowonder.evolution.jsontext import blank
from autowonder.evolution.lifecycle import reject


async def postprocess_canary(
    session: AsyncSession,
    proposal_id: int | None,
    asset_type: str | None,
    asset_id: int | None,
    context_key: str | None,
    verdict: str | None,
    result_json: str | None,
    reject_on_fail: bool | None,
    tenant_id: int,
    user_id: int,
) -> dict[str, object]:
    """INCONCLUSIVE 只观察。FAIL 且要求驳回时，原因固定为灰度回滚建议。"""
    if (
        proposal_id is None
        or blank(asset_type)
        or asset_id is None
        or blank(context_key)
        or blank(verdict)
        or blank(result_json)
    ):
        raise BizError(ErrorCode.PARAM_INVALID)
    await record_gate(
        session,
        proposal_id,
        "CANARY",
        verdict,
        result_json,
        tenant_id,
        user_id,
    )
    action = "OBSERVE"
    if verdict == "PASS" or verdict == "FAIL":
        outcome = "NEGATIVE"
        if verdict == "PASS":
            outcome = "POSITIVE"
        await record_event(
            session,
            EvidenceEvent(
                asset_type=asset_type,
                asset_id=asset_id,
                posterior_type="UPLIFT",
                context_key=context_key,
                source_type="CANARY_RESULT",
                source_ref="proposal:" + str(proposal_id) + ":canary",
                raw_outcome=outcome,
                raw_event_json=result_json,
                idempotency_key="proposal:" + str(proposal_id) + ":canary:" + verdict,
            ),
            tenant_id,
            user_id,
        )
        action = "KEEP"
        if verdict == "FAIL":
            action = "ROLLBACK_RECOMMENDED"
    if action == "ROLLBACK_RECOMMENDED" and reject_on_fail is True:
        await reject(
            session,
            proposal_id,
            tenant_id,
            "CANARY_FAIL_ROLLBACK_RECOMMENDED",
            user_id,
        )
    return {"proposalId": proposal_id, "verdict": verdict, "action": action}
