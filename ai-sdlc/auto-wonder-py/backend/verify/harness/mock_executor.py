"""可注入故障的执行器客户端。

故障：``no_ack`` 不确认，``timeout`` 空等不回帧，``duplicate`` 把 ACK 发两遍，
``disconnect`` 确认后断开。断线之后用 ``reconnect`` 带新的启动时间重新上线。
"""

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote, urlsplit

import websockets
from websockets.exceptions import ConnectionClosed

_RESTART_FEATURE = "EXECUTOR_RESTART_V1"


class MockExecutor:
    """连上 ``/ws/executor``，按故障策略回应派发和远程重启。"""

    def __init__(self, http_base: str, executor_id: int, token: str) -> None:
        self.http_base = http_base.rstrip("/")
        self.executor_id = executor_id
        self.token = token
        self.frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._socket: Any = None
        self._reader: asyncio.Task[None] | None = None

    def _url(self) -> str:
        parsed = urlsplit(self.http_base)
        scheme = "ws"
        if parsed.scheme == "https":
            scheme = "wss"
        query = (
            f"executorId={self.executor_id}"
            f"&token={quote(self.token, safe='')}"
            "&maxConcurrentDispatches=2"
        )
        return f"{scheme}://{parsed.netloc}/ws/executor?{query}"

    async def connect(self) -> None:
        """关掉旧连接后再握手。不走环境变量里的 HTTP 代理。"""
        await self.close()
        self._socket = await websockets.connect(
            self._url(),
            proxy=None,
            open_timeout=15,
            ping_interval=None,
        )
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        """关闭套接字并等读循环结束。"""
        socket = self._socket
        reader = self._reader
        self._socket = None
        self._reader = None
        if socket is not None:
            await socket.close()
        if reader is not None:
            await reader

    async def heartbeat(self, started_at: str, restart_request_id: str | None = None) -> None:
        """上报启动时间和远程重启能力。重连时带上同一次 ``restartRequestId``。"""
        payload: dict[str, Any] = {
            "type": "HEARTBEAT",
            "protocolFeatures": [_RESTART_FEATURE],
            "startedAt": started_at,
            "version": "0.2.163",
        }
        if restart_request_id is not None:
            payload["restartRequestId"] = restart_request_id
        await self._send(payload)

    async def hold(self, seconds: float) -> None:
        """故障：超时。这段时间里不回任何帧。"""
        await asyncio.sleep(seconds)

    async def answer_dispatch(self, frame: dict[str, Any], fault: str) -> None:
        """按故障策略回应一条 ``TASK_DISPATCH``。

        ``no_ack`` 与调用方另行 ``hold`` 的超时都不确认。
        ``duplicate`` 把 ACK 发两遍，然后仍回进度和旧式结果。
        ``disconnect`` 确认后断开，不再回进度和结果。
        """
        dispatch_id = int(frame["dispatchId"])
        if fault == "no_ack":
            return
        if fault == "timeout":
            return
        repeats = 1
        if fault == "duplicate":
            repeats = 2
        await self._ack(dispatch_id, repeats)
        if fault == "disconnect":
            await self.close()
            return
        await self._send(
            {"type": "TASK_PROGRESS", "dispatchId": dispatch_id, "log": "mock progress"}
        )
        await self._send(
            {
                "type": "TASK_RESULT",
                "dispatchId": dispatch_id,
                "success": True,
                "resultSummary": "mock executor completed",
            }
        )

    async def restart_result(self, request_id: str, status: str) -> None:
        """回一条重启阶段。``RESTARTING`` 之后等新进程心跳才算完成。"""
        await self._send(
            {
                "type": "EXECUTOR_RESTART_RESULT",
                "requestId": request_id,
                "status": status,
                "message": "mock restarting",
            }
        )

    async def reconnect(self, started_at: str, restart_request_id: str) -> None:
        """断线后用更晚的启动时间重新上线，并带上同一次重启请求。"""
        await self.connect()
        await self.heartbeat(started_at, restart_request_id)

    async def wait_type(self, frame_type: str, timeout: float) -> dict[str, Any] | None:
        """等到指定类型的帧。期间其他帧留在队列里被跳过。到点返回空。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                frame = await asyncio.wait_for(self.frames.get(), remaining)
            except TimeoutError:
                return None
            if frame.get("type") == frame_type:
                return frame

    async def _ack(self, dispatch_id: int, repeats: int) -> None:
        sent = 0
        payload = {"type": "TASK_ACK", "dispatchId": dispatch_id}
        while sent < repeats:
            await self._send(payload)
            sent += 1

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._socket.send(json.dumps(payload, separators=(",", ":")))

    async def _read(self) -> None:
        socket = self._socket
        try:
            async for message in socket:
                self.frames.put_nowait(json.loads(message))
        except ConnectionClosed:
            return
