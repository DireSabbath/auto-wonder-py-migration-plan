"""为会话选择执行器，并读取当前进程能看到的在线与协议能力。"""

import logging
from collections.abc import Awaitable
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.constants import (
    ACP_INTERACTION,
    ACTION_PLAN_V1,
    AGENT_ENVIRONMENT_VARIABLES_V1,
    ARTIFACT_OUTPUT_V1,
    ATTACHMENT_MANIFEST_V1,
    ROUND_ROBIN_TTL_SEC,
    TURN_CANCEL,
    TURN_EVENT,
)
from autowonder.conversations.records import count_environment_refs
from autowonder.core.redis import redis_client
from autowonder.dispatch.selector import execs_key, has_remaining_capacity
from autowonder.executors.registry import current_snapshot, is_available, is_online

logger = logging.getLogger(__name__)


class ProtocolUnsupported(Exception):
    """在线执行器有容量，但没有声明会话要求的协议。"""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__("Executor runtime does not support required protocol feature " + feature)


def executor_online(executor_id: int | None) -> bool:
    """没有绑定执行器，或当前进程没有它的接入会话时视为离线。"""
    if executor_id is None:
        return False
    return is_online(executor_id)


def protocol_features(executor_id: int | None) -> set[str]:
    """协议能力挂在调度快照上。当前快照没有该字段，所以读到的集合是空的。"""
    if executor_id is None or current_snapshot(executor_id) is None:
        return set()
    return set()


def protocol_supported(online: bool, features: set[str], feature: str) -> bool:
    """执行器在线且声明了该能力时，前端才打开对应入口。"""
    return online and feature in features


def runtime_capabilities(online: bool, features: set[str]) -> dict[str, bool]:
    """平台会话详情上的六项能力位。"""
    return {
        "streaming_supported": protocol_supported(online, features, TURN_EVENT),
        "cancel_supported": protocol_supported(online, features, TURN_CANCEL),
        "acp_interaction_supported": protocol_supported(online, features, ACP_INTERACTION),
        "attachment_manifest_supported": protocol_supported(
            online, features, ATTACHMENT_MANIFEST_V1
        ),
        "artifact_output_supported": protocol_supported(online, features, ARTIFACT_OUTPUT_V1),
        "action_plan_supported": protocol_supported(online, features, ACTION_PLAN_V1),
    }


async def select_executor(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    agent_version_id: int,
    preferred_executor_id: int | None,
) -> int | None:
    """版本绑了环境变量时，只选声明了该协议的执行器。没有候选人时返回 None。"""
    required = None
    if await count_environment_refs(session, tenant_id, agent_version_id) > 0:
        required = AGENT_ENVIRONMENT_VARIABLES_V1
    return await _select(agent_id, preferred_executor_id, required)


async def _select(
    agent_id: int,
    preferred_executor_id: int | None,
    required_feature: str | None,
) -> int | None:
    raw = await cast(Awaitable[set[Any]], redis_client().smembers(execs_key(agent_id)))
    members = {str(member) for member in raw}
    if not members:
        return None
    ids = sorted(_executor_ids(members))
    supporting = False
    unsupported_with_capacity = False
    if (
        preferred_executor_id is not None
        and preferred_executor_id in ids
        and is_available(preferred_executor_id)
    ):
        supports = _supports(preferred_executor_id, required_feature)
        has_capacity = _has_capacity(preferred_executor_id)
        supporting = supports
        if has_capacity and supports:
            return preferred_executor_id
        unsupported_with_capacity = not supports and has_capacity
    eligible: list[int] = []
    for executor_id in ids:
        if executor_id == preferred_executor_id or not is_available(executor_id):
            continue
        supports = _supports(executor_id, required_feature)
        if supports:
            supporting = True
        if not _has_capacity(executor_id):
            continue
        if not supports:
            unsupported_with_capacity = True
            continue
        eligible.append(executor_id)
    if eligible:
        sequence = await _round_robin(agent_id)
        index = (sequence - 1) % len(eligible) if sequence > 0 else 0
        return eligible[index]
    if not supporting and unsupported_with_capacity:
        raise ProtocolUnsupported(required_feature or "")
    return None


def _executor_ids(members: set[str]) -> list[int]:
    ids: list[int] = []
    for member in members:
        try:
            ids.append(int(member))
        except ValueError:
            continue
    return ids


def _supports(executor_id: int, required_feature: str | None) -> bool:
    if required_feature is None:
        return True
    return required_feature in protocol_features(executor_id)


def _has_capacity(executor_id: int) -> bool:
    if not is_available(executor_id):
        return False
    return has_remaining_capacity(current_snapshot(executor_id), set(), False)


async def _round_robin(agent_id: int) -> int:
    key = f"agent:executor-round-robin:{agent_id}"
    try:
        value = int(await cast(Awaitable[int], redis_client().incr(key)))
        await redis_client().expire(key, ROUND_ROBIN_TTL_SEC)
    except Exception:
        logger.warning("executor round-robin cursor unavailable agentId=%s", agent_id)
        return 1
    return value
