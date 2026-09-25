"""执行器在线状态。键名、TTL 和会话替换语义对齐 ``PresenceManager``。

调度快照以 JSON 存在 ``exec:dispatch-snapshot:{id}``。Java 用 JDK 序列化，
两边不能互读同一条快照。
"""

import json
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from autowonder.core.redis import redis_client
from autowonder.ws.frames import BROADCAST_CHANNEL

logger = logging.getLogger(__name__)

TTL_SEC = 90
LEGACY_DEFAULT_CAPACITY = 3
INVALID_CAPACITY = 1
MAX_CAPACITY = 50
MAX_VERSION_LENGTH = 64
MAX_MODEL_LENGTH = 128
MAX_PROTOCOL_FEATURES = 20

_REPLACE_SET = (
    "redis.call('del', KEYS[1]); "
    "if #ARGV > 1 then "
    "for i = 2, #ARGV do redis.call('sadd', KEYS[1], ARGV[i]) end; "
    "redis.call('expire', KEYS[1], ARGV[1]); end; return 1"
)


class SessionMutationResult(Enum):
    """一次心跳能否写回当前会话。"""

    APPLIED = "APPLIED"
    STALE_SESSION = "STALE_SESSION"
    DELETED = "DELETED"
    RETRY = "RETRY"


@dataclass
class DispatchPresence:
    """一次心跳里的调度清单，绑定到一条 WebSocket 会话。"""

    session_id: str
    capacity: int
    authoritative_inventory: bool
    inventory_ready: bool
    running_dispatch_ids: list[int]
    owned_dispatch_ids: list[int]
    running_conversation_turn_ids: list[int] | None
    protocol_features: list[str]
    inventory_error: str | None
    reported_at: int

    def has_conversation_activity_report(self) -> bool:
        """没有上报过会话轮次时，不能把它当成空闲。"""
        return self.running_conversation_turn_ids is not None

    def to_json(self) -> str:
        """写成一帧快照。会话轮次用 null 表示这次心跳没带报告。"""
        body = {
            "sessionId": self.session_id,
            "capacity": self.capacity,
            "authoritativeInventory": self.authoritative_inventory,
            "inventoryReady": self.inventory_ready,
            "runningDispatchIds": self.running_dispatch_ids,
            "ownedDispatchIds": self.owned_dispatch_ids,
            "runningConversationTurnIds": self.running_conversation_turn_ids,
            "protocolFeatures": self.protocol_features,
            "inventoryError": self.inventory_error,
            "reportedAt": self.reported_at,
        }
        return json.dumps(body, separators=(",", ":"))


def normalize_capacity(raw: str | None) -> int:
    """缺省按旧客户端 3；非正数或非数字为 1；上限 50。"""
    if raw is None:
        return LEGACY_DEFAULT_CAPACITY
    try:
        parsed = int(raw)
    except ValueError:
        return INVALID_CAPACITY
    if parsed <= 0:
        return INVALID_CAPACITY
    if parsed > MAX_CAPACITY:
        return MAX_CAPACITY
    return parsed


def snapshot_key(executor_id: int) -> str:
    """调度快照键。"""
    return "exec:dispatch-snapshot:" + str(executor_id)


def closed_session_key(executor_id: int, session_id: str) -> str:
    """这条会话已经关闭，不能再被心跳写回。"""
    return "exec:closed-session:" + str(executor_id) + ":" + session_id


def deleted_key(executor_id: int) -> str:
    """执行器已删除的墓碑。"""
    return "exec:deleted:" + str(executor_id)


def session_key(executor_id: int) -> str:
    """当前会话 id。心跳不能把更旧的会话写回来。"""
    return "exec:session:" + str(executor_id)


def protocol_features_key(executor_id: int) -> str:
    """心跳声明的协议能力。"""
    return "exec:protocol-features:" + str(executor_id)


def version_key(executor_id: int) -> str:
    """客户端上报的 runtime 版本。"""
    return "exec:version:" + str(executor_id)


def model_key(executor_id: int) -> str:
    """客户端上报的有效模型。"""
    return "exec:model:" + str(executor_id)


def capacity_key(executor_id: int) -> str:
    """当前并发上限。"""
    return "exec:capacity:" + str(executor_id)


def online_key(executor_id: int) -> str:
    """在线标记，值是本节点 id。"""
    return "exec:online:" + str(executor_id)


def route_key(executor_id: int) -> str:
    """持有连接的节点。"""
    return "exec:route:" + str(executor_id)


class NodeIdentity:
    """本进程的 12 位节点号，用来标记在线执行器落在哪台机器。"""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


class PresenceManager:
    """刷新 ``exec:online`` / ``exec:route``，并发布会话替换。"""

    def __init__(self, node: NodeIdentity) -> None:
        self.node = node

    async def unregister(self, executor_id: int, agent_id: int) -> None:
        """删掉该执行器的在线、路由、能力和快照。显式删除时调用。"""
        client = redis_client()
        await client.delete(
            online_key(executor_id),
            route_key(executor_id),
            capacity_key(executor_id),
            _conversation_turn_report_key(executor_id),
            _active_conversation_turn_key(executor_id),
            session_key(executor_id),
            protocol_features_key(executor_id),
            snapshot_key(executor_id),
            version_key(executor_id),
            model_key(executor_id),
        )
        await cast(Awaitable[int], client.srem("agent:execs:" + str(agent_id), str(executor_id)))
        logger.info("presence unregister executorId=%s agentId=%s", executor_id, agent_id)

    async def announce_session(self, executor_id: int, session_id: str) -> bool:
        """记下新会话并广播 ``SESSION_REPLACED``。空会话 id 不写。"""
        if session_id.strip() == "":
            return False
        client = redis_client()
        await client.set(session_key(executor_id), session_id)
        await client.publish(
            BROADCAST_CHANNEL,
            '{"type":"SESSION_REPLACED","executorId":' + str(executor_id) + "}",
        )
        return True

    async def publish_heartbeat(
        self,
        executor_id: int,
        agent_id: int,
        session_id: str,
        snapshot: DispatchPresence,
        protocol_features: list[str] | None,
        version: str | None,
        model: str | None,
    ) -> SessionMutationResult:
        """把心跳写回 Redis。会话不一致、已删除或已关闭时不覆盖新连接。"""
        if session_id != snapshot.session_id:
            return SessionMutationResult.STALE_SESSION
        client = redis_client()
        if await client.exists(deleted_key(executor_id)):
            return SessionMutationResult.DELETED
        if not await self.is_current_session(executor_id, session_id):
            return SessionMutationResult.STALE_SESSION
        if await client.exists(closed_session_key(executor_id, session_id)):
            return SessionMutationResult.STALE_SESSION
        features = _normalize_features(protocol_features)
        stored = DispatchPresence(
            session_id=snapshot.session_id,
            capacity=snapshot.capacity,
            authoritative_inventory=snapshot.authoritative_inventory,
            inventory_ready=snapshot.inventory_ready,
            running_dispatch_ids=snapshot.running_dispatch_ids,
            owned_dispatch_ids=snapshot.owned_dispatch_ids,
            running_conversation_turn_ids=(
                snapshot.running_conversation_turn_ids
                if snapshot.has_conversation_activity_report()
                else None
            ),
            protocol_features=features,
            inventory_error=snapshot.inventory_error,
            reported_at=snapshot.reported_at,
        )
        wrote = await client.set(snapshot_key(executor_id), stored.to_json(), ex=TTL_SEC)
        if not wrote:
            return SessionMutationResult.RETRY
        if snapshot.has_conversation_activity_report():
            turns = snapshot.running_conversation_turn_ids
            members = []
            if turns is not None:
                members = [str(item) for item in sorted(turns)]
            await _replace_set(_active_conversation_turn_key(executor_id), members)
            await client.set(_conversation_turn_report_key(executor_id), "1", ex=TTL_SEC)
        await _replace_set(protocol_features_key(executor_id), features)
        await self.record_version(executor_id, version)
        await self.record_model(executor_id, model)
        await client.set(
            capacity_key(executor_id),
            str(normalize_capacity(str(snapshot.capacity))),
            ex=TTL_SEC,
        )
        await client.set(online_key(executor_id), self.node.node_id, ex=TTL_SEC)
        await client.set(route_key(executor_id), self.node.node_id, ex=TTL_SEC)
        await cast(Awaitable[int], client.sadd("agent:execs:" + str(agent_id), str(executor_id)))
        await self.clear_protocol_error(executor_id, agent_id, session_id)
        return SessionMutationResult.APPLIED

    async def record_protocol_error(
        self,
        executor_id: int,
        agent_id: int,
        session_id: str,
        error: str,
    ) -> bool:
        """只给当前会话记下协议错误。旧会话写不进去。"""
        if not await self.is_current_session(executor_id, session_id):
            return False
        client = redis_client()
        await client.set(_protocol_error_key(executor_id, session_id), error, ex=TTL_SEC)
        await cast(Awaitable[int], client.sadd("agent:execs:" + str(agent_id), str(executor_id)))
        return True

    async def clear_protocol_error(
        self,
        executor_id: int,
        agent_id: int,
        session_id: str,
    ) -> bool:
        """当前会话心跳成功后清掉协议错误。``agent_id`` 与 Java 签名一致，这里不改成员集合。"""
        del agent_id
        if not await self.is_current_session(executor_id, session_id):
            return False
        await redis_client().delete(_protocol_error_key(executor_id, session_id))
        return True

    async def record_version(self, executor_id: int, version: str | None) -> None:
        """记下 runtime 版本。空白表示旧客户端没报，超长截到 64。"""
        trimmed = _trim_limit(version, MAX_VERSION_LENGTH)
        if trimmed is None:
            return
        await redis_client().set(version_key(executor_id), trimmed, ex=TTL_SEC)

    async def record_model(self, executor_id: int, model: str | None) -> None:
        """记下有效模型。空白跳过，超长截到 128。"""
        trimmed = _trim_limit(model, MAX_MODEL_LENGTH)
        if trimmed is None:
            return
        await redis_client().set(model_key(executor_id), trimmed, ex=TTL_SEC)

    async def unregister_if_current(
        self,
        executor_id: int,
        agent_id: int,
        session_id: str,
    ) -> bool:
        """给这条会话打关闭标记。不删掉后继连接已经写上的在线状态。"""
        del agent_id
        if session_id.strip() == "":
            return False
        await redis_client().set(closed_session_key(executor_id, session_id), "1", ex=TTL_SEC)
        return await self.is_current_session(executor_id, session_id)

    async def current_session_id(self, executor_id: int) -> str | None:
        """Redis 里登记的当前会话。"""
        return await redis_client().get(session_key(executor_id))

    async def is_current_session(self, executor_id: int, session_id: str) -> bool:
        """``session_id`` 是否仍是该执行器的当前会话。"""
        current = await self.current_session_id(executor_id)
        return current == session_id

    async def current_dispatch_snapshot(self, executor_id: int) -> DispatchPresence | None:
        """当前会话的快照。关闭标记或会话号对不上时当作没有。"""
        try:
            raw = await redis_client().get(snapshot_key(executor_id))
            if raw is None:
                return None
            snapshot = _snapshot_from_json(raw)
            session_id = await self.current_session_id(executor_id)
            if session_id is None or session_id != snapshot.session_id:
                return None
            closed = await redis_client().exists(closed_session_key(executor_id, session_id))
            if closed:
                return None
            return snapshot
        except (RuntimeError, ValueError, TypeError):
            return None

    async def supports_protocol_feature(self, executor_id: int, feature: str) -> bool:
        """当前快照是否声明了这项能力。"""
        snapshot = await self.current_dispatch_snapshot(executor_id)
        if snapshot is None:
            return False
        return feature in snapshot.protocol_features

    async def is_executor_online(self, executor_id: int) -> bool:
        """在线键和当前快照都在，才算执行器在线。"""
        exists = await redis_client().exists(online_key(executor_id))
        if not exists:
            return False
        return await self.current_dispatch_snapshot(executor_id) is not None

    async def is_executor_available(self, executor_id: int) -> bool:
        """在线且未删除、不在故障转移冷却中，才能接受新派发。"""
        client = redis_client()
        try:
            if await client.exists(deleted_key(executor_id)):
                return False
            if not await self.is_executor_online(executor_id):
                return False
            cooldown = "exec:provider-cooldown:" + str(executor_id)
            if not await client.exists(cooldown):
                return True
            marker = _redis_text(await client.get(cooldown))
            if marker is None or not marker.startswith("failover:"):
                await client.delete(cooldown)
                return True
            return False
        except Exception:
            return False

    async def current_protocol_error(self, executor_id: int) -> str | None:
        """当前会话上报的协议错误。没有会话时没有错误。"""
        session_id = await self.current_session_id(executor_id)
        if session_id is None:
            return None
        return _redis_text(await redis_client().get(_protocol_error_key(executor_id, session_id)))

    async def current_agent_protocol_error(self, agent_id: int) -> str | None:
        """该数字员工任一在线成员上的协议错误。"""
        raw = await cast(
            Awaitable[set[Any]],
            redis_client().smembers("agent:execs:" + str(agent_id)),
        )
        for member in raw:
            text = _redis_text(member)
            if text is None:
                continue
            try:
                executor_id = int(text)
            except ValueError:
                continue
            error = await self.current_protocol_error(executor_id)
            if error is not None and error.strip() != "":
                return error
        return None


def _normalize_features(protocol_features: list[str] | None) -> list[str]:
    if protocol_features is None:
        return []
    kept: list[str] = []
    for feature in protocol_features:
        if feature.strip() == "":
            continue
        if feature not in kept:
            kept.append(feature)
    kept.sort()
    return kept


def _trim_limit(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if trimmed == "":
        return None
    if len(trimmed) > limit:
        return trimmed[:limit]
    return trimmed


def _redis_text(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode()
    return str(raw)


def _protocol_error_key(executor_id: int, session_id: str) -> str:
    return "exec:protocol-error:" + str(executor_id) + ":" + session_id


def _conversation_turn_report_key(executor_id: int) -> str:
    return "exec:conversation-turn-report:" + str(executor_id)


def _active_conversation_turn_key(executor_id: int) -> str:
    return "exec:conversation-turns:" + str(executor_id)


async def _replace_set(key: str, members: list[str]) -> None:
    args = [str(TTL_SEC), *members]
    await cast(Awaitable[object], redis_client().eval(_REPLACE_SET, 1, key, *args))


def _snapshot_from_json(raw: str) -> DispatchPresence:
    body = json.loads(raw)
    turns = body["runningConversationTurnIds"]
    turn_ids = None
    if turns is not None:
        turn_ids = [int(item) for item in turns]
    return DispatchPresence(
        session_id=body["sessionId"],
        capacity=int(body["capacity"]),
        authoritative_inventory=bool(body["authoritativeInventory"]),
        inventory_ready=bool(body["inventoryReady"]),
        running_dispatch_ids=[int(item) for item in body["runningDispatchIds"]],
        owned_dispatch_ids=[int(item) for item in body["ownedDispatchIds"]],
        running_conversation_turn_ids=turn_ids,
        protocol_features=[str(item) for item in body["protocolFeatures"]],
        inventory_error=body["inventoryError"],
        reported_at=int(body["reportedAt"]),
    )


def new_node_identity() -> NodeIdentity:
    """生成本进程节点号：去掉连字符的 UUID 前 12 位。"""
    return NodeIdentity(uuid.uuid4().hex[:12])


node_identity = new_node_identity()
presence_manager = PresenceManager(node_identity)
