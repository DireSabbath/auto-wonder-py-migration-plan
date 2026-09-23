"""执行器在线登记。

Java ``ExecutorRegistry.isOnline`` 要求 Redis ``exec:online:{id}`` 与当前调度快照同时存在。
WebSocket 接入尚未迁入时，这里只保留进程内会话集合：没有登记的执行器一律离线。
``isAvailable`` 还要求未删除且不在故障转移冷却中，目前没有这些登记，调度侧一律不可用。
"""

from dataclasses import dataclass

_ONLINE: set[int] = set()


@dataclass(frozen=True)
class DispatchSnapshot:
    """一次调度容量快照，字段对应 ``ExecutorDispatchSnapshot`` 里参与容量判断的部分。"""

    capacity: int
    authoritative_inventory: bool
    inventory_ready: bool
    inventory_error: str | None
    running_dispatch_ids: frozenset[int]
    running_conversation_turn_ids: frozenset[int]


def is_online(executor_id: int) -> bool:
    """该执行器是否有活着的接入会话。"""
    return executor_id in _ONLINE


def register_session(executor_id: int) -> None:
    """登记一次接入。后续 WebSocket 上线时调用。"""
    _ONLINE.add(executor_id)


def drop_session(executor_id: int) -> None:
    """接入断开后移出在线集合。"""
    _ONLINE.discard(executor_id)


def presence(executor_id: int) -> str:
    """小队卡片上的 ONLINE / OFFLINE。"""
    if is_online(executor_id):
        return "ONLINE"
    return "OFFLINE"


def is_available(executor_id: int) -> bool:
    """该执行器当前能否接受调度。没有心跳、快照和冷却登记时不可用。"""
    return False


def current_snapshot(executor_id: int) -> DispatchSnapshot | None:
    """当前调度快照。WebSocket 未接入时没有快照。"""
    return None
