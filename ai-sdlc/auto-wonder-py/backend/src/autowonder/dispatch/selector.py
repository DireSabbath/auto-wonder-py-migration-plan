"""执行器选择与容量。

轮询、偏好执行器和失败原因对齐 ``ExecutorSelector``。
``hasAvailableExecutor`` 只读 ``agent:execs:{agentId}``，不推进轮询游标。
畸形成员跳过。快照为空、清单未就绪或容量已满时，该执行器不可用。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.redis import redis_client
from autowonder.dispatch.models import Dispatch, DispatchRecovery
from autowonder.executors.registry import DispatchSnapshot, current_snapshot, is_available
from autowonder.ws.presence import DispatchPresence, presence_manager

logger = logging.getLogger(__name__)

ROUND_ROBIN_TTL_SECONDS = 7 * 24 * 60 * 60
_OCCUPYING = ("PACKAGING", "DISPATCHED", "ACKED", "RUNNING", "PAUSING")
WAITING_REASONS: dict[str, tuple[bool, str]] = {
    "NO_EXECUTOR_CAPACITY": (True, "等待可用执行器容量"),
    "CAPACITY_LOCK_BUSY": (True, "等待调度容量锁"),
    "EXECUTOR_AT_CAPACITY": (True, "执行器容量已满，等待重试"),
    "EXECUTOR_RECOVERING": (True, "执行器正在恢复本地任务"),
    "NO_EXECUTOR_ONLINE": (True, "等待执行器上线"),
    "AGENT_NOT_PUBLISHED": (False, "数字员工尚未发布"),
    "AGENT_VERSION_NOT_FOUND": (False, "发布版本不存在"),
    "RUNTIME_INCOMPATIBLE": (False, "执行器版本不兼容"),
    "SELECTION_INTERNAL_ERROR": (False, "调度器内部错误，任务未派发"),
}


class ProtocolCompatibilityError(Exception):
    """在线执行器有容量，但没有声明这次派发要求的协议。"""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__("Executor runtime does not support required protocol feature " + feature)


@dataclass(frozen=True)
class ExecutorView:
    """选择时看到的一台执行器。"""

    available: bool
    online: bool
    snapshot: DispatchSnapshot | None
    features: frozenset[str]
    occupying: frozenset[int]


def waiting_retryable(reason: str) -> bool:
    """该等待原因是否留给下一轮重试。"""
    return WAITING_REASONS[reason][0]


def waiting_error(reason: str) -> str:
    """写入派发失败原因的 ``名称: 说明``。"""
    return reason + ": " + WAITING_REASONS[reason][1]


def parse_member_ids(members: set[str] | None) -> list[int]:
    """把在线集合成升序执行器 id。非十进制成员跳过。"""
    if members is None:
        return []
    ids: list[int] = []
    for member in members:
        try:
            ids.append(int(member))
        except ValueError:
            continue
    ids.sort()
    return ids


def plan_selection(
    members: set[str] | None,
    views: dict[int, ExecutorView],
    preferred_executor_id: int | None,
    interaction: bool,
    required_feature: str | None,
) -> tuple[int | None, list[int]]:
    """偏好执行器有容量时直接选中，否则返回待轮询列表。"""
    ids = parse_member_ids(members)
    if not ids:
        return None, []
    supporting = False
    unsupported_with_capacity = False
    if preferred_executor_id is not None and preferred_executor_id in ids:
        preferred = views.get(preferred_executor_id)
        if preferred is not None and preferred.available:
            supports = _supports(preferred, required_feature)
            has_capacity = _view_has_capacity(preferred, interaction)
            supporting = supports
            if has_capacity and supports:
                return preferred_executor_id, []
            unsupported_with_capacity = not supports and has_capacity
    eligible: list[int] = []
    for executor_id in ids:
        if executor_id == preferred_executor_id:
            continue
        view = views.get(executor_id)
        if view is None or not view.available:
            continue
        supports = _supports(view, required_feature)
        if supports:
            supporting = True
        if not _view_has_capacity(view, interaction):
            continue
        if not supports:
            unsupported_with_capacity = True
            continue
        eligible.append(executor_id)
    if eligible:
        return None, eligible
    if not supporting and unsupported_with_capacity:
        raise ProtocolCompatibilityError(required_feature or "")
    return None, []


def round_robin_pick(eligible: list[int], sequence: int) -> int:
    """轮询序号从 1 开始。非正序号落在第一个候选人。"""
    if sequence > 0:
        return eligible[(sequence - 1) % len(eligible)]
    return eligible[0]


def choose_strict(
    members: set[str] | None,
    view: ExecutorView | None,
    executor_id: int,
    required_feature: str | None,
) -> int | None:
    """只接受指定执行器。连续会话不能改派到别的机器。"""
    if members is None or str(executor_id) not in members or view is None or not view.available:
        return None
    if not _supports(view, required_feature):
        raise ProtocolCompatibilityError(required_feature or "")
    if _view_has_capacity(view, False):
        return executor_id
    return None


def diagnose_unavailable(
    members: set[str] | None,
    views: dict[int, ExecutorView],
    protocol_error: str | None,
) -> str:
    """解释为什么选不到执行器。不推进轮询游标。"""
    if members is None or len(members) == 0:
        if protocol_error is None:
            return "NO_EXECUTOR_ONLINE"
        return "RUNTIME_INCOMPATIBLE"
    online = False
    recovering = False
    compatible = False
    for executor_id in parse_member_ids(members):
        view = views.get(executor_id)
        if view is None:
            continue
        if view.available or view.online:
            online = True
        if not view.available:
            continue
        snapshot = view.snapshot
        if snapshot is None or (
            snapshot.authoritative_inventory
            and (not snapshot.inventory_ready or snapshot.inventory_error is not None)
        ):
            recovering = True
        else:
            compatible = True
    if not online:
        if protocol_error is None:
            return "NO_EXECUTOR_ONLINE"
        return "RUNTIME_INCOMPATIBLE"
    if recovering:
        return "EXECUTOR_RECOVERING"
    if not compatible and protocol_error is not None:
        return "RUNTIME_INCOMPATIBLE"
    return "NO_EXECUTOR_CAPACITY"


def _supports(view: ExecutorView, required_feature: str | None) -> bool:
    if required_feature is None:
        return True
    return required_feature in view.features


def _view_has_capacity(view: ExecutorView, interaction: bool) -> bool:
    if not view.available:
        return False
    return has_remaining_capacity(view.snapshot, set(view.occupying), interaction)


def snapshot_from_presence(presence: DispatchPresence | None) -> DispatchSnapshot | None:
    """把心跳快照收成容量判断用的字段。未上报的会话轮次按空集合计。"""
    if presence is None:
        return None
    turns = presence.running_conversation_turn_ids
    return DispatchSnapshot(
        capacity=presence.capacity,
        authoritative_inventory=presence.authoritative_inventory,
        inventory_ready=presence.inventory_ready,
        inventory_error=presence.inventory_error,
        running_dispatch_ids=frozenset(presence.running_dispatch_ids),
        running_conversation_turn_ids=frozenset() if turns is None else frozenset(turns),
        owned_dispatch_ids=frozenset(presence.owned_dispatch_ids),
    )


def execs_key(agent_id: int) -> str:
    """在线执行器集合的 Redis 键。"""
    return f"agent:execs:{agent_id}"


def capacity_limit(capacity: int, interaction: bool) -> int:
    """交互派发占满容量；普通派发在容量大于 1 时留出一个槽。"""
    if interaction:
        return capacity
    if capacity <= 1:
        return capacity
    return capacity - 1


def has_remaining_capacity(
    snapshot: DispatchSnapshot | None,
    occupying_ids: set[int],
    interaction: bool,
) -> bool:
    """快照与已占用派发合并后，是否还低于容量上限。"""
    if snapshot is None:
        return False
    if snapshot.authoritative_inventory:
        if not snapshot.inventory_ready:
            return False
        if snapshot.inventory_error is not None:
            return False
    occupied = set(occupying_ids)
    occupied.update(snapshot.running_dispatch_ids)
    active = len(occupied) + len(snapshot.running_conversation_turn_ids)
    limit = capacity_limit(snapshot.capacity, interaction)
    return limit > 0 and active < limit


def probe_members(
    members: set[str] | None,
    runtime_available: Callable[[int], bool],
    snapshot_of: Callable[[int], DispatchSnapshot | None],
    occupying_of: Callable[[int], set[int]],
) -> bool:
    """集合里只要有一个执行器还有容量就可用。``None`` 与空集合都不可用。"""
    if members is None:
        return False
    for member in members:
        executor_id = _executor_id(member)
        if executor_id is None:
            continue
        if not runtime_available(executor_id):
            continue
        if has_remaining_capacity(
            snapshot_of(executor_id),
            occupying_of(executor_id),
            False,
        ):
            return True
    return False


async def has_available_executor(agent_id: int) -> bool:
    """当前进程登记下，该数字员工是否还有可调度执行器。"""
    raw = await cast(Awaitable[set[Any]], redis_client().smembers(execs_key(agent_id)))
    members = {str(member) for member in raw}
    return probe_members(members, is_available, current_snapshot, _empty_occupying)


def _executor_id(member: str) -> int | None:
    """非十进制成员与 Java ``Long.parseLong`` 一样跳过。"""
    try:
        return int(member)
    except ValueError:
        return None


def _empty_occupying(executor_id: int) -> set[int]:
    """只读探测不查调度表。没有快照时探测不会用到这份集合。"""
    return set()


async def capacity_occupying_ids(session: AsyncSession, executor_id: int) -> set[int]:
    """打包中、执行中，以及已请求停止的派发都占着容量。"""
    active = select(Dispatch.id).where(
        Dispatch.executor_id == executor_id,
        Dispatch.status.in_(_OCCUPYING),
        Dispatch.is_deleted == 0,
    )
    stopping = (
        select(Dispatch.id)
        .join(
            DispatchRecovery,
            and_(
                DispatchRecovery.tenant_id == Dispatch.tenant_id,
                DispatchRecovery.dispatch_id == Dispatch.id,
            ),
        )
        .where(
            Dispatch.executor_id == executor_id,
            Dispatch.is_deleted == 0,
            DispatchRecovery.stop_pending == 1,
        )
    )
    rows = await session.execute(active.union(stopping))
    return {int(row[0]) for row in rows.all()}


async def load_executor_view(session: AsyncSession, executor_id: int) -> ExecutorView:
    """读当前快照、在线状态和已经占住的派发。"""
    presence = await presence_manager.current_dispatch_snapshot(executor_id)
    features: frozenset[str] = frozenset()
    if presence is not None:
        features = frozenset(presence.protocol_features)
    occupying = await capacity_occupying_ids(session, executor_id)
    return ExecutorView(
        available=await presence_manager.is_executor_available(executor_id),
        online=await presence_manager.is_executor_online(executor_id),
        snapshot=snapshot_from_presence(presence),
        features=features,
        occupying=frozenset(occupying),
    )


async def advance_round_robin(agent_id: int) -> int:
    """推进轮询游标。游标不可用时从第一个候选人开始。"""
    key = "agent:executor-round-robin:" + str(agent_id)
    try:
        value = int(await cast(Awaitable[int], redis_client().incr(key)))
        await redis_client().expire(key, ROUND_ROBIN_TTL_SECONDS)
    except Exception:
        logger.warning("executor round-robin cursor unavailable agentId=%s", agent_id)
        return 1
    return value


async def select_dispatch_executor(
    session: AsyncSession,
    agent_id: int,
    preferred_executor_id: int | None,
    interaction: bool,
    required_feature: str | None,
) -> int | None:
    """按偏好、协议和轮询选一台执行器。没有候选人时返回 None。"""
    members = await _members(agent_id)
    views = await _views(session, parse_member_ids(members))
    chosen, eligible = plan_selection(
        members,
        views,
        preferred_executor_id,
        interaction,
        required_feature,
    )
    if chosen is not None:
        logger.info(
            "executor selected preferred agentId=%s executorId=%s",
            agent_id,
            chosen,
        )
        return chosen
    if not eligible:
        logger.info("executor select none agentId=%s", agent_id)
        return None
    sequence = await advance_round_robin(agent_id)
    selected = round_robin_pick(eligible, sequence)
    logger.info(
        "executor selected round-robin agentId=%s executorId=%s sequence=%s eligible=%s",
        agent_id,
        selected,
        sequence,
        len(eligible),
    )
    return selected


async def select_strict_executor(
    session: AsyncSession,
    agent_id: int,
    executor_id: int,
    required_feature: str | None,
) -> int | None:
    """连续会话只接受原来的执行器。"""
    members = await _members(agent_id)
    view = await load_executor_view(session, executor_id)
    return choose_strict(members, view, executor_id, required_feature)


async def unavailable_reason(session: AsyncSession, agent_id: int) -> str:
    """选不到执行器时的等待原因。诊断失败记为调度器内部错误。"""
    try:
        members = await _members(agent_id)
        views = await _views(session, parse_member_ids(members))
        error = await presence_manager.current_agent_protocol_error(agent_id)
        return diagnose_unavailable(members, views, error)
    except Exception:
        logger.error("executor selection diagnosis failed agentId=%s", agent_id, exc_info=True)
        return "SELECTION_INTERNAL_ERROR"


async def _members(agent_id: int) -> set[str]:
    raw = await cast(Awaitable[set[Any]], redis_client().smembers(execs_key(agent_id)))
    return {_member_text(member) for member in raw}


async def _views(session: AsyncSession, ids: list[int]) -> dict[int, ExecutorView]:
    views: dict[int, ExecutorView] = {}
    for executor_id in ids:
        views[executor_id] = await load_executor_view(session, executor_id)
    return views


def _member_text(member: object) -> str:
    if isinstance(member, bytes):
        return member.decode()
    return str(member)
