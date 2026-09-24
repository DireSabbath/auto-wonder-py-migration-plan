"""运行时 evolution_delta 的解析。编排链尚未接入时，合法候选仍会抛出。"""

import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank


@dataclass
class EvolutionCommand:
    """一条候选。资产类型和补丁在进入编排前就必须存在。"""

    asset_type: str
    asset_id: int
    suggested_patch: dict[str, object]
    dispatch_id: int
    index: int
    mode: str


async def ingest_evolution_delta(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    dispatch_id: int,
    payload: bytes,
    mode: str,
) -> None:
    """解析 candidates。缺字段时按参数不合法拒绝，合法候选交给编排。"""
    for index, candidate in enumerate(_candidates(payload)):
        command = _command(candidate, agent_id, dispatch_id, index, mode)
        await orchestrate_delta(session, tenant_id, agent_id, command)


async def orchestrate_delta(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    command: EvolutionCommand,
) -> None:
    """证据账本和策略链尚未迁移。调用方会记下跳过并保留产物回执。"""
    raise RuntimeError(
        "evolution delta orchestrator is not ported tenant="
        + str(tenant_id)
        + " agent="
        + str(agent_id)
        + " asset="
        + command.asset_type
        + " dispatch="
        + str(command.dispatch_id)
        + " index="
        + str(command.index)
        + " mode="
        + command.mode
        + " bound="
        + str(session.bind)
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


def _command(
    candidate: dict[str, object],
    agent_id: int,
    dispatch_id: int,
    index: int,
    mode: str,
) -> EvolutionCommand:
    asset_type = candidate.get("assetType")
    if not isinstance(asset_type, str) or java_is_blank(asset_type):
        raise BizError(ErrorCode.PARAM_INVALID)
    patch = _suggested_patch(candidate)
    _apply_memory_defaults(candidate, patch, agent_id)
    asset_id = _asset_id(candidate, asset_type, patch)
    return EvolutionCommand(asset_type, asset_id, patch, dispatch_id, index, mode)


def _apply_memory_defaults(
    candidate: dict[str, object],
    patch: dict[str, object],
    agent_id: int,
) -> None:
    raw_type = candidate.get("candidateAssetType")
    if not isinstance(raw_type, str) or java_is_blank(raw_type):
        raw_type = candidate.get("assetType")
    if not isinstance(raw_type, str) or raw_type.upper() != "MEMORY":
        return
    scope = patch.get("scope")
    if not isinstance(scope, str) or java_is_blank(scope):
        patch["scope"] = "AGENT"
        scope = "AGENT"
    if patch.get("ownerRef") is None and isinstance(scope, str) and scope.upper() == "AGENT":
        patch["ownerRef"] = agent_id


def _asset_id(candidate: dict[str, object], asset_type: str, patch: dict[str, object]) -> int:
    if "assetId" not in candidate or candidate.get("assetId") is None:
        mode = patch.get("mode")
        if asset_type.upper() == "SKILL" and isinstance(mode, str) and mode.upper() == "CREATE":
            return 0
        raise BizError(ErrorCode.PARAM_INVALID)
    value = candidate.get("assetId")
    if isinstance(value, bool) or not isinstance(value, int):
        raise BizError(ErrorCode.PARAM_INVALID)
    return value


def _suggested_patch(candidate: dict[str, object]) -> dict[str, object]:
    patch = candidate.get("suggestedPatch")
    if isinstance(patch, dict) and len(patch) > 0:
        return patch
    raw = candidate.get("suggestedPatchJson")
    if isinstance(raw, str) and not java_is_blank(raw):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise BizError(ErrorCode.PARAM_INVALID) from error
        if isinstance(parsed, dict) and len(parsed) > 0:
            return parsed
    raise BizError(ErrorCode.PARAM_INVALID)
