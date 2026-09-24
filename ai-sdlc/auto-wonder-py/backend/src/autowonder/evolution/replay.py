"""按回放套件给出结论，并把它记到提案上。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.jsontext import blank, dump_json, parse_json, text_field
from autowonder.evolution.lifecycle import record_replay, validate

_VERDICTS = {"PASS", "FAIL", "INCONCLUSIVE"}


async def execute_replay(
    session: AsyncSession,
    proposal_id: int | None,
    auto_validate: bool | None,
    replay_suite_json: str | None,
    tenant_id: int,
    user_id: int,
) -> dict[str, object]:
    """套件可以强制结论；否则有失败检查就是 FAIL，没有检查则无法下结论。"""
    if proposal_id is None or blank(replay_suite_json):
        raise BizError(ErrorCode.PARAM_INVALID)
    if replay_suite_json is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    chosen = _verdict(replay_suite_json)
    replay = {"verdict": chosen, "suite": parse_json(replay_suite_json)}
    replay_json = dump_json(replay)
    if auto_validate is True:
        await validate(session, proposal_id, tenant_id, user_id)
    await record_replay(session, proposal_id, tenant_id, replay_json, user_id)
    return {"proposalId": proposal_id, "verdict": chosen, "replayJson": replay_json}


def _verdict(suite_json: str) -> str:
    parsed = parse_json(suite_json)
    if not isinstance(parsed, dict) or len(parsed) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    forced = text_field(parsed, "forceVerdict")
    if forced in _VERDICTS:
        return forced
    checks = parsed.get("checks")
    if not isinstance(checks, list) or len(checks) == 0:
        return "INCONCLUSIVE"
    for check in checks:
        if not isinstance(check, dict):
            return "FAIL"
        if text_field(check, "status") == "FAIL":
            return "FAIL"
    return "PASS"
