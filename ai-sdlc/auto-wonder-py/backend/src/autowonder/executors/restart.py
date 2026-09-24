"""远程重启。状态放在 Redis，命令通过调度广播交给持有连接的节点。"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import SHANGHAI
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.redis import redis_client
from autowonder.executors.presence import executor_online, supports_feature
from autowonder.executors.store import require_executor
from autowonder.ws.mailbox import deliver_executor_frame

_ACTIVE = {"REQUESTED", "UPDATING", "RESTARTING"}
_LOCK_SECONDS = 600
_STATE_SECONDS = 86400
_FEATURE_RESTART = "EXECUTOR_RESTART_V1"
_FEATURE_UPDATE_RESTART = "EXECUTOR_UPDATE_RESTART_V1"


def apply_restart_timeout(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """进行中的重启超过 10 分钟仍无结果时，读出来记为超时，不回写。"""
    if state.get("status") not in _ACTIVE:
        return state
    issued = datetime.fromisoformat(str(state["issuedAt"]).replace("Z", "+00:00"))
    if issued < now - timedelta(seconds=_LOCK_SECONDS):
        state["status"] = "TIMED_OUT"
        state["message"] = "未收到重启后的心跳，请检查客户端"
    return state


def instant_text(moment: datetime) -> str:
    """写成 Java Instant 的 UTC 文本，供重启和升级帧使用。"""
    utc = moment.astimezone(UTC)
    seconds = utc.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{utc.microsecond:06d}".rstrip("0")
    if fraction == "":
        return seconds + "Z"
    return f"{seconds}.{fraction}Z"


async def restart_status(executor_id: int) -> dict[str, Any] | None:
    """读取一台执行器的重启状态。没有记录时为空。"""
    raw = await redis_client().get(_state_key(executor_id))
    if raw is None or raw.strip() == "":
        return None
    state = json.loads(raw)
    return apply_restart_timeout(state, datetime.now(UTC))


async def request_restart(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
    user_id: int,
    update: bool,
) -> dict[str, Any]:
    """向在线且声明了对应能力的执行器发送重启。已有进行中的请求时拒绝。"""
    executor = await require_executor(session, executor_id, tenant_id)
    if not await executor_online(executor_id):
        raise BizError(ErrorCode.PARAM_INVALID, "执行器离线，无法远程重启")
    feature = _FEATURE_UPDATE_RESTART if update else _FEATURE_RESTART
    if not await supports_feature(executor_id, feature):
        if update:
            message = "当前客户端不支持发布版更新，请在本地更新源码或升级客户端"
        else:
            message = "当前客户端不支持远程重启，请先在本地升级客户端"
        raise BizError(ErrorCode.PARAM_INVALID, message)
    if executor.last_started_at is None:
        raise BizError(ErrorCode.PARAM_INVALID, "等待客户端首次上报启动时间后再重试")
    acquired = await redis_client().set(_lock_key(executor_id), "1", nx=True, ex=_LOCK_SECONDS)
    if acquired is not True:
        raise BizError(ErrorCode.PARAM_INVALID, "已有重启请求，请等待结果")
    result: dict[str, Any] = {
        "requestId": str(uuid.uuid4()),
        "status": "REQUESTED",
        "message": "已发送，等待客户端响应",
        "issuedAt": instant_text(datetime.now(UTC)),
        "update": update,
        "requestedBy": user_id,
        "previousStartedAt": _epoch_millis(executor.last_started_at),
    }
    await _save(executor_id, result)
    frame = dict(result)
    frame["type"] = "EXECUTOR_RESTART"
    frame["executorId"] = executor_id
    try:
        await deliver_executor_frame(executor_id, _frame_json(frame))
    except Exception:
        result["status"] = "FAILED"
        result["message"] = "发送失败，请稍后重试"
        await _save(executor_id, result)
        await redis_client().delete(_lock_key(executor_id))
    return result


async def _save(executor_id: int, state: dict[str, Any]) -> None:
    await redis_client().set(
        _state_key(executor_id),
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        ex=_STATE_SECONDS,
    )


def _frame_json(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def _state_key(executor_id: int) -> str:
    return f"exec:restart:{executor_id}"


def _lock_key(executor_id: int) -> str:
    return f"exec:restart-lock:{executor_id}"


def _epoch_millis(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=SHANGHAI)
    return int(aware.timestamp() * 1000)
