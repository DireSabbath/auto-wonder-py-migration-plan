"""已发布提案按资产类型撤回：删掉新建资产，或把技能恢复到发布前。"""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.jsontext import blank, long_field, text_field
from autowonder.evolution.lifecycle import _advance, _put_stage, _require_proposal, _stage
from autowonder.memories.service import delete_memory
from autowonder.repos.service import delete_relation
from autowonder.skills.schemas import UpdateSkillRequest
from autowonder.skills.service import delete_skill, update_skill


async def rollback(
    session: AsyncSession,
    proposal_id: int,
    tenant_id: int,
    user_id: int,
) -> dict[str, object]:
    """只接受 RELEASED 且带发布快照的提案。"""
    proposal = await _require_proposal(session, proposal_id, tenant_id)
    release = _stage(proposal.lifecycle_json, "release")
    if proposal.status != "RELEASED" or release is None or _blank_text(release):
        raise BizError(ErrorCode.CONFLICT)
    if not isinstance(release, dict):
        raise BizError(ErrorCode.CONFLICT)
    asset_id = long_field(release, "assetId")
    if asset_id is None:
        raise BizError(ErrorCode.CONFLICT)
    action = await _undo(session, proposal.asset_type, release, asset_id, tenant_id, user_id)
    rollback_body = {"action": action, "assetId": asset_id}
    lifecycle = _put_stage(proposal.lifecycle_json, "rollback", rollback_body)
    await _advance(session, proposal, tenant_id, "ROLLED_BACK", lifecycle, user_id)
    return {
        "proposalId": proposal_id,
        "assetId": asset_id,
        "assetType": proposal.asset_type,
        "status": "ROLLED_BACK",
        "action": action,
    }


async def _undo(
    session: AsyncSession,
    asset_type: str,
    release: dict[str, object],
    asset_id: int,
    tenant_id: int,
    user_id: int,
) -> str:
    if asset_type == "MEMORY":
        await delete_memory(session, asset_id, tenant_id, user_id)
        return "DELETE_CREATED_MEMORY"
    if asset_type == "REPO_RELATION":
        await delete_relation(session, asset_id, tenant_id)
        return "DELETE_CREATED_REPO_RELATION"
    if asset_type == "SKILL":
        if text_field(release, "mode") == "CREATE":
            await delete_skill(session, asset_id, tenant_id, user_id)
            return "DELETE_CREATED_SKILL"
        before = _before(release)
        await update_skill(
            session,
            asset_id,
            UpdateSkillRequest(
                name=text_field(before, "name"),
                type=text_field(before, "type"),
                install_spec=text_field(before, "installSpec"),
                description=text_field(before, "description"),
            ),
            tenant_id,
            user_id,
        )
        return "RESTORE_SKILL"
    raise BizError(ErrorCode.PARAM_INVALID)


def _before(release: dict[str, object]) -> dict[str, object]:
    raw = text_field(release, "beforeJson")
    if raw is None:
        raise BizError(ErrorCode.CONFLICT)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise BizError(ErrorCode.CONFLICT)
    return parsed


def _blank_text(value: object) -> bool:
    if isinstance(value, str):
        return blank(value)
    return False
