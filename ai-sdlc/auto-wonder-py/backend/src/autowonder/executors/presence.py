"""执行器现场读口。版本和模型是心跳字符串，能力在当前调度快照里。"""

import json
from typing import Any

from autowonder.core.redis import redis_client
from autowonder.ws.frames import BROADCAST_CHANNEL
from autowonder.ws.presence import (
    deleted_key,
    model_key,
    presence_manager,
    version_key,
)

TOMBSTONE_TTL_SECONDS = 24 * 60 * 60


async def current_version(executor_id: int) -> str | None:
    """最近一次上报的运行时版本。没上报过则为空。"""
    return await redis_client().get(version_key(executor_id))


async def current_model(executor_id: int) -> str | None:
    """最近一次上报的生效模型。没上报过则为空。"""
    return await redis_client().get(model_key(executor_id))


async def supports_feature(executor_id: int, feature: str) -> bool:
    """当前会话快照是否声明了这项协议能力。"""
    return await presence_manager.supports_protocol_feature(executor_id, feature)


async def current_features(executor_id: int) -> list[str]:
    """当前快照里的协议能力。没有有效快照时为空。"""
    snapshot = await presence_manager.current_dispatch_snapshot(executor_id)
    if snapshot is None:
        return []
    return snapshot.protocol_features


async def executor_online(executor_id: int) -> bool:
    """在线键和当前快照都在，才算这台执行器在线。"""
    return await presence_manager.is_executor_online(executor_id)


async def mark_deleted(executor_id: int) -> None:
    """写下删除墓碑。已有墓碑时不延长它的有效期。"""
    await redis_client().set(deleted_key(executor_id), "1", nx=True, ex=TOMBSTONE_TTL_SECONDS)


async def unregister(executor_id: int, agent_id: int) -> None:
    """删除后清掉在线、路由、快照、版本和模型。"""
    await presence_manager.unregister(executor_id, agent_id)


async def publish(frame: dict[str, Any]) -> None:
    """把一帧发到调度广播通道，由持有连接的节点转给执行器。"""
    await redis_client().publish(
        BROADCAST_CHANNEL,
        json.dumps(frame, ensure_ascii=False, separators=(",", ":")),
    )
