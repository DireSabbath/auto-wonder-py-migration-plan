"""执行器控制帧。WebSocket 接入前，暂停帧没有通道可发。"""

from autowonder.dispatch.models import Dispatch

PAUSE_SEND_FAILURE = "暂停请求发送失败，请重试暂停"


def deliver_pause(_dispatch: Dispatch) -> None:
    """向执行器发送暂停。当前没有连接，调用方按发送失败处理。"""
    raise RuntimeError(PAUSE_SEND_FAILURE)
