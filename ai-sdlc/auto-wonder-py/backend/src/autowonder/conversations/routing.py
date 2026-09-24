"""为会话选择执行器，并读取当前调度快照上的在线与协议能力。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.constants import (
    ACP_INTERACTION,
    ACTION_PLAN_V1,
    AGENT_ENVIRONMENT_VARIABLES_V1,
    ARTIFACT_OUTPUT_V1,
    ATTACHMENT_MANIFEST_V1,
    TURN_CANCEL,
    TURN_EVENT,
)
from autowonder.conversations.records import count_environment_refs
from autowonder.dispatch.selector import ProtocolCompatibilityError, select_dispatch_executor
from autowonder.ws.presence import presence_manager


class ProtocolUnsupported(Exception):
    """在线执行器有容量，但没有声明会话要求的协议。"""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__("Executor runtime does not support required protocol feature " + feature)


async def executor_online(executor_id: int | None) -> bool:
    """在线键和当前调度快照都在，才算执行器在线。"""
    if executor_id is None:
        return False
    return await presence_manager.is_executor_online(executor_id)


async def protocol_features(executor_id: int | None) -> set[str]:
    """协议能力在当前调度快照上。没有快照时前端看不到任何能力位。"""
    if executor_id is None:
        return set()
    snapshot = await presence_manager.current_dispatch_snapshot(executor_id)
    if snapshot is None:
        return set()
    return set(snapshot.protocol_features)


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
    try:
        return await select_dispatch_executor(
            session,
            agent_id,
            preferred_executor_id,
            False,
            required,
        )
    except ProtocolCompatibilityError as error:
        raise ProtocolUnsupported(error.feature) from error
