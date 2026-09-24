"""执行器控制帧。本机有连接就直发，否则广播给持有会话的节点。"""

from autowonder.dispatch.models import Dispatch
from autowonder.ws.frames import task_pause_frame
from autowonder.ws.mailbox import deliver_executor_frame

PAUSE_SEND_FAILURE = "暂停请求发送失败，请重试暂停"


async def deliver_pause(dispatch: Dispatch) -> None:
    """向已分配的执行器发送 ``TASK_PAUSE``。"""
    executor_id = dispatch.executor_id
    if executor_id is None:
        raise RuntimeError("pause requires an assigned executor")
    try:
        await deliver_executor_frame(executor_id, task_pause_frame(dispatch.id, executor_id))
    except Exception as error:
        raise RuntimeError("WebSocket pause send failed") from error
