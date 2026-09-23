"""执行器在线登记。

Java ``ExecutorRegistry.isOnline`` 要求 Redis ``exec:online:{id}`` 与当前调度快照同时存在。
WebSocket 接入尚未迁入时，这里只保留进程内会话集合：没有登记的执行器一律离线。
"""

_ONLINE: set[int] = set()


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
