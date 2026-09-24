"""执行器是否还有可派发容量。

``hasAvailableExecutor`` 只读 ``agent:execs:{agentId}``，不推进轮询游标。
畸形成员跳过。快照为空、清单未就绪或容量已满时，该执行器不可用。
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from autowonder.core.redis import redis_client
from autowonder.executors.registry import DispatchSnapshot, current_snapshot, is_available


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
    """容量占用查询尚未接到调度表。没有快照时探测不会用到这份集合。"""
    return set()
