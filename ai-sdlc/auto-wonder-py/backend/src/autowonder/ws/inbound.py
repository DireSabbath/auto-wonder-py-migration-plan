"""入站帧路由，对齐 ``InboundFrameRouter``。

类型化帧会丢掉 Bean 上没有的键，所以这里按原始 JSON 分发。
调度状态只在 Java 允许的来源集合里前进。SDLC 驱动、交接、引导、
会话和升级回执还没有接到对应服务。
"""

import json
import logging
import time
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from autowonder.artifacts.service import record_reported_artifact
from autowonder.db.rows import rowcount
from autowonder.db.session import SessionLocal
from autowonder.debuglogs.issue import record_task_result_report
from autowonder.dispatch.checkpoint import CheckpointEngine, SqlCheckpointRepo
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery import execution_source
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.ws.frames import task_result_ack
from autowonder.ws.presence import (
    DispatchPresence,
    PresenceManager,
    SessionMutationResult,
    presence_manager,
)
from autowonder.ws.session import ExecutorSession, session_registry

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
ACK_ALLOWED_STATUSES = frozenset({"DISPATCHED"})
PROGRESS_ALLOWED_STATUSES = frozenset({"DISPATCHED", "ACKED"})
RESULT_MUTABLE_STATUSES = frozenset(
    {
        "PENDING",
        "PACKAGING",
        "DISPATCHED",
        "ACKED",
        "RUNNING",
        "WAITING_FOR_PAUSE",
    }
)
RESULT_BLOCKED_STATUSES = frozenset({"PAUSING", "PAUSED", "PAUSE_FAILED", "CANCELED"})
FAILOVER_SOURCE_STATUSES = frozenset({"DISPATCHED", "ACKED", "RUNNING"})
DISPATCH_FRAME_TYPES = frozenset(
    {
        "TASK_ACK",
        "TASK_PROGRESS",
        "TASK_RESULT",
        "TASK_BUSY",
        "TASK_PAUSED",
        "TASK_PAUSE_FAILED",
        "TASK_GUIDANCE_ACK",
        "ARTIFACT_UPLOADED",
        "TASK_HANDOFF",
    }
)
EXECUTOR_FAILURE_CATEGORIES = frozenset(
    {
        "agent_error.provider_auth_or_access",
        "agent_error.provider_quota_limit",
        "agent_error.provider_capacity_or_rate_limit",
        "agent_error.provider_server_error",
        "agent_error.provider_network",
        "agent_error.missing_config",
        "agent_error.model_not_found_or_unavailable",
        "agent_error.runtime_version_unsupported",
        "agent_error.runtime_missing_executable",
        "runtime_recovery",
    }
)
SYSTEM_USER_ID = 0
MAX_ERROR_CHARS = 512
_UNWIRED = (
    "EXECUTOR_RESTART_RESULT",
    "EXECUTOR_UPGRADE_RESULT",
    "TASK_BUSY",
    "TASK_PAUSED",
    "TASK_PAUSE_FAILED",
    "TASK_GUIDANCE_ACK",
    "CONVERSATION_TURN_ACK",
    "CONVERSATION_TURN_EVENT",
    "CONVERSATION_COMMANDS_RESULT",
    "QODER_MODEL_CATALOG_RESULT",
    "TASK_HANDOFF",
)


def ack_target(status: str) -> str | None:
    """只有 ``DISPATCHED`` 进入 ``ACKED``。终态和已确认的行保持不动。"""
    if status in ACK_ALLOWED_STATUSES:
        return "ACKED"
    return None


def progress_target(status: str) -> str | None:
    """``DISPATCHED`` 或 ``ACKED`` 进入 ``RUNNING``。已经在跑则不再改。"""
    if status in PROGRESS_ALLOWED_STATUSES:
        return "RUNNING"
    return None


def result_accepted(status: str, success: bool) -> bool:
    """暂停族和取消不接受。终态只在成败与当前状态一致时当作已经确认。"""
    if status in RESULT_BLOCKED_STATUSES:
        return False
    if status in TERMINAL_STATUSES:
        expected = "FAILED"
        if success:
            expected = "SUCCEEDED"
        return status == expected
    return status in RESULT_MUTABLE_STATUSES


def protocol_features(payload: dict[str, Any]) -> list[str] | None:
    """读取心跳 ``protocolFeatures``。缺省或空数组表示这次没声明，最多 20 项。"""
    raw = payload.get("protocolFeatures")
    if not isinstance(raw, list) or len(raw) == 0:
        return None
    features: list[str] = []
    for item in raw:
        if len(features) >= 20:
            break
        if isinstance(item, str) and item != "":
            features.append(item)
    if len(features) == 0:
        return None
    return features


def classify_executor_failure(error: str | None) -> str | None:
    """旧客户端没有结构化失败类别时，只认明确的提供者或会话错误。"""
    if error is None or error.strip() == "":
        return None
    normalized = error.lower()
    if _contains_any(
        normalized,
        (
            "no conversation found with session id",
            "no rollout found for thread id",
            "invalid session identifier",
            "session not found",
        ),
    ) or ("source session" in normalized and "does not exist" in normalized):
        return "runtime_recovery"
    if _contains_any(
        normalized,
        (
            "you've hit your usage limit",
            "you have hit your usage limit",
            "usage limit",
            "payment required",
            "insufficient_balance",
            "insufficient balance",
            "balance is too low",
            "purchase more credits",
            "quota exceeded",
        ),
    ):
        return "agent_error.provider_quota_limit"
    if _contains_any(
        normalized,
        (
            "not logged in",
            "login required",
            "please login again",
            "invalid api key",
            "invalid_api_key",
            "authentication failed",
            "access token has expired",
            "access token expired",
            "access token revoked",
        ),
    ):
        return "agent_error.provider_auth_or_access"
    if _contains_any(
        normalized,
        (
            "http 429",
            "status 429",
            "too many requests",
            "rate limit exceeded",
            "rate_limit_exceeded",
        ),
    ):
        return "agent_error.provider_capacity_or_rate_limit"
    if _contains_any(
        normalized,
        (
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "status 500",
            "status 502",
            "status 503",
            "status 504",
        ),
    ) and _contains_any(normalized, ("provider", " api", "service unavailable", "bad gateway")):
        return "agent_error.provider_server_error"
    return None


class InboundFrameRouter:
    """把一条执行器文本帧分到确认、进度、结果或心跳。"""

    def __init__(self, presence: PresenceManager) -> None:
        self.presence = presence

    async def route(self, executor_session: ExecutorSession, message: str) -> None:
        """解析并处理一帧。坏 JSON 丢掉，不关掉连接。"""
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("malformed frame from executor=%s", executor_session.executor_id)
            return
        if not isinstance(parsed, dict):
            return
        frame_type = parsed.get("type")
        if not isinstance(frame_type, str):
            return
        if frame_type == "HEARTBEAT":
            await self._heartbeat(executor_session, parsed)
            return
        if frame_type == "TASK_ACK":
            await self._ack(executor_session, parsed)
            return
        if frame_type == "TASK_PROGRESS":
            await self._progress(executor_session, parsed)
            return
        if frame_type == "TASK_RESULT":
            await self._result(executor_session, parsed)
            return
        if frame_type == "ARTIFACT_UPLOADED":
            await self._artifact(executor_session, parsed)
            return
        if frame_type == "MCP_CONNECTION_TEST_RESULT":
            await self._mcp_connection_test(executor_session, parsed)
            return
        if frame_type in _UNWIRED:
            logger.info(
                "inbound %s not connected executorId=%s",
                frame_type,
                executor_session.executor_id,
            )
            return
        logger.info(
            "inbound unknown frame type=%s executorId=%s",
            frame_type,
            executor_session.executor_id,
        )

    async def _mcp_connection_test(
        self, executor_session: ExecutorSession, payload: dict[str, Any]
    ) -> None:
        """把 Runtime 的连接测试结果写回 Redis。"""
        from autowonder.skills.runtime_mcp import complete_connection_test

        tools = payload.get("tools")
        if not isinstance(tools, list):
            tools = []
        duration = payload.get("durationMs")
        duration_ms = None
        if isinstance(duration, int) and not isinstance(duration, bool):
            duration_ms = duration
        await complete_connection_test(
            executor_session.tenant_id,
            executor_session.executor_id,
            payload.get("testId") if isinstance(payload.get("testId"), str) else None,
            payload.get("success") is True,
            payload.get("message") if isinstance(payload.get("message"), str) else None,
            duration_ms,
            tools,
        )

    async def _heartbeat(self, executor_session: ExecutorSession, payload: dict[str, Any]) -> None:
        logger.info("inbound HEARTBEAT executorId=%s", executor_session.executor_id)
        features = protocol_features(payload)
        inventory = features is not None and "dispatch_inventory_v1" in features
        try:
            snapshot = _heartbeat_snapshot(executor_session, payload, inventory)
        except ValueError as invalid:
            protocol_error = "EXECUTOR_PROTOCOL_INCOMPATIBLE: " + str(invalid)
            logger.warning(
                "executor dispatch inventory rejected executorId=%s reason=%s",
                executor_session.executor_id,
                invalid,
            )
            await self.presence.record_protocol_error(
                executor_session.executor_id,
                executor_session.agent_id,
                executor_session.session_id,
                protocol_error,
            )
            await _close_session(executor_session)
            return
        result = await self.presence.publish_heartbeat(
            executor_session.executor_id,
            executor_session.agent_id,
            executor_session.session_id,
            snapshot,
            features,
            _text(payload, "version"),
            _text(payload, "model"),
        )
        if result in (SessionMutationResult.DELETED, SessionMutationResult.STALE_SESSION):
            logger.warning(
                "heartbeat rejected executorId=%s result=%s; closing session",
                executor_session.executor_id,
                result.value,
            )
            await _close_session(executor_session)
            return
        if result == SessionMutationResult.RETRY:
            logger.warning(
                "heartbeat publication deferred executorId=%s sessionId=%s",
                executor_session.executor_id,
                executor_session.session_id,
            )
            return
        from autowonder.ws.executor import persist_heartbeat_if_needed

        await persist_heartbeat_if_needed(
            executor_session.executor_id,
            executor_session.tenant_id,
        )

    async def _ack(self, executor_session: ExecutorSession, payload: dict[str, Any]) -> None:
        dispatch_id = _long(payload, "dispatchId")
        logger.info(
            "inbound TASK_ACK dispatchId=%s executorId=%s",
            dispatch_id,
            executor_session.executor_id,
        )
        async with SessionLocal() as session:
            await _apply_ack(session, executor_session.tenant_id, dispatch_id)

    async def _progress(self, executor_session: ExecutorSession, payload: dict[str, Any]) -> None:
        dispatch_id = _long(payload, "dispatchId")
        logger.info(
            "inbound TASK_PROGRESS dispatchId=%s executorId=%s",
            dispatch_id,
            executor_session.executor_id,
        )
        async with SessionLocal() as session:
            await _apply_progress(session, executor_session.tenant_id, dispatch_id)

    async def _result(self, executor_session: ExecutorSession, payload: dict[str, Any]) -> None:
        dispatch_id = _long(payload, "dispatchId")
        success = payload.get("success") is True
        category, scope = _failure(payload, success)
        logger.info(
            "inbound TASK_RESULT dispatchId=%s success=%s executorId=%s "
            "failureCategory=%s failureScope=%s",
            dispatch_id,
            success,
            executor_session.executor_id,
            category,
            scope,
        )
        durable = _int(payload, "checkpointReceiptVersion") >= 1
        async with SessionLocal() as session:
            if success and durable:
                matched = await _durable_receipt(
                    session,
                    executor_session.tenant_id,
                    dispatch_id,
                    _long(payload, "checkpointSeq"),
                    _text(payload, "checkpointSha256"),
                )
                if not matched:
                    logger.warning(
                        "TASK_RESULT checkpoint is not durable dispatchId=%s executorId=%s "
                        "checkpointSeq=%s",
                        dispatch_id,
                        executor_session.executor_id,
                        _long(payload, "checkpointSeq"),
                    )
                    await _send_result_ack(executor_session, dispatch_id, False)
                    return
            if success and not durable:
                logger.warning(
                    "accepting legacy TASK_RESULT without durable checkpoint receipt "
                    "dispatchId=%s executorId=%s",
                    dispatch_id,
                    executor_session.executor_id,
                )
            failover = (
                not success and scope == "EXECUTOR" and category in EXECUTOR_FAILURE_CATEGORIES
            )
            if failover:
                accepted = await _apply_failover(
                    session,
                    executor_session.tenant_id,
                    executor_session.executor_id,
                    dispatch_id,
                )
            else:
                accepted = await _apply_result(
                    session,
                    executor_session.tenant_id,
                    executor_session.executor_id,
                    dispatch_id,
                    success,
                    _text(payload, "resultSummary"),
                    _text(payload, "error"),
                )
            await _record_debug_log(
                session,
                executor_session,
                dispatch_id,
                payload.get("debugLog"),
            )
        await _send_result_ack(executor_session, dispatch_id, accepted)

    async def _artifact(self, executor_session: ExecutorSession, payload: dict[str, Any]) -> None:
        dispatch_id = _long(payload, "dispatchId")
        logger.info(
            "inbound ARTIFACT_UPLOADED dispatchId=%s name=%s type=%s",
            dispatch_id,
            _text(payload, "name"),
            _text(payload, "artifactType"),
        )
        async with SessionLocal() as session:
            dispatch = await _load_dispatch(session, executor_session.tenant_id, dispatch_id, None)
            if dispatch is None or dispatch.workitem_id <= 0:
                logger.warning(
                    "rejecting artifact frame for unknown or foreign dispatchId=%s executorId=%s",
                    dispatch_id,
                    executor_session.executor_id,
                )
                return
            source = execution_source(dispatch)
            if source == "SCHEDULED_TASK_RUN":
                require_scheduled_capability()
            name = _text(payload, "name")
            artifact_type = _text(payload, "artifactType")
            oss_ref = _text(payload, "ossRef")
            if name is None or artifact_type is None or oss_ref is None:
                return
            await record_reported_artifact(
                session,
                executor_session.tenant_id,
                source,
                dispatch.workitem_id,
                dispatch_id,
                name,
                artifact_type,
                oss_ref,
                _long(payload, "size"),
            )
            await session.commit()


async def _apply_ack(session: AsyncSession, tenant_id: int, dispatch_id: int) -> None:
    dispatch = await _load_dispatch(session, tenant_id, dispatch_id, None)
    if dispatch is None or ack_target(dispatch.status) is None:
        return
    await _write_status(session, dispatch, "ACKED", None, None)
    await session.commit()


async def _apply_progress(session: AsyncSession, tenant_id: int, dispatch_id: int) -> None:
    dispatch = await _load_dispatch(session, tenant_id, dispatch_id, None)
    if dispatch is None or progress_target(dispatch.status) is None:
        return
    await _write_status(session, dispatch, "RUNNING", None, None)
    await session.commit()


async def _apply_result(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
    success: bool,
    result_summary: str | None,
    error: str | None,
) -> bool:
    dispatch = await _load_dispatch(session, tenant_id, dispatch_id, executor_id)
    if dispatch is None:
        return False
    if not result_accepted(dispatch.status, success):
        return False
    if dispatch.status in TERMINAL_STATUSES:
        return True
    target = "FAILED"
    if success:
        target = "SUCCEEDED"
    updated = await _write_status(session, dispatch, target, result_summary, error)
    await session.commit()
    if updated == 1:
        return True
    refreshed = await _load_dispatch(session, tenant_id, dispatch_id, executor_id)
    if refreshed is None:
        return False
    return result_accepted(refreshed.status, success)


async def _apply_failover(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
) -> bool:
    """执行器故障把仍在跑的调度退回 PENDING。乐观锁冲突时最多再读三次。"""
    for _attempt in range(3):
        dispatch = await _load_dispatch(session, tenant_id, dispatch_id, None)
        if dispatch is None:
            return False
        if dispatch.status in TERMINAL_STATUSES:
            return True
        if dispatch.status in {"PAUSING", "PAUSED", "PAUSE_FAILED"}:
            return False
        if dispatch.executor_id != executor_id:
            return True
        if dispatch.status not in FAILOVER_SOURCE_STATUSES:
            return False
        result = await session.execute(
            update(Dispatch)
            .where(
                Dispatch.id == dispatch.id,
                Dispatch.tenant_id == tenant_id,
                Dispatch.executor_id == executor_id,
                Dispatch.status.in_(FAILOVER_SOURCE_STATUSES),
                Dispatch.version == dispatch.version,
                Dispatch.is_deleted == 0,
            )
            .values(
                status="PENDING",
                executor_id=None,
                package_oss_ref=None,
                result_summary=None,
                error=None,
                version=Dispatch.version + 1,
                modifier_id=SYSTEM_USER_ID,
            )
        )
        if rowcount(result) == 1:
            await session.commit()
            return True
        session.expire(dispatch)
    return False


async def _write_status(
    session: AsyncSession,
    dispatch: Dispatch,
    status: str,
    result_summary: str | None,
    error: str | None,
) -> int:
    values: dict[str, object] = {
        "status": status,
        "version": Dispatch.version + 1,
        "modifier_id": SYSTEM_USER_ID,
    }
    if result_summary is not None:
        values["result_summary"] = result_summary
    if error is not None:
        values["error"] = error[:MAX_ERROR_CHARS]
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch.id,
            Dispatch.tenant_id == dispatch.tenant_id,
            Dispatch.version == dispatch.version,
            Dispatch.is_deleted == 0,
        )
        .values(**values)
    )
    return rowcount(result)


async def _load_dispatch(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    executor_id: int | None,
) -> Dispatch | None:
    dispatch = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    if dispatch is None or dispatch.tenant_id != tenant_id:
        return None
    if executor_id is not None and dispatch.executor_id != executor_id:
        return None
    return dispatch


async def _durable_receipt(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    checkpoint_seq: int,
    checkpoint_sha256: str | None,
) -> bool:
    def matches(sync_session: Session) -> bool:
        engine = CheckpointEngine.__new__(CheckpointEngine)
        return engine.matches_durable_receipt(
            tenant_id,
            dispatch_id,
            checkpoint_seq,
            checkpoint_sha256,
            SqlCheckpointRepo(sync_session),
        )

    return await session.run_sync(matches)


async def _record_debug_log(
    session: AsyncSession,
    executor_session: ExecutorSession,
    dispatch_id: int,
    debug_log: object,
) -> None:
    if not isinstance(debug_log, dict):
        return
    try:
        await record_task_result_report(
            session,
            executor_session.tenant_id,
            executor_session.executor_id,
            dispatch_id,
            debug_log,
        )
    except Exception:
        logger.warning(
            "debug log report ignored dispatchId=%s reason=DEBUG_LOG_REPORT_IGNORED",
            dispatch_id,
            exc_info=True,
        )


async def _send_result_ack(
    executor_session: ExecutorSession,
    dispatch_id: int,
    accepted: bool,
) -> None:
    try:
        await executor_session.send_text(task_result_ack(dispatch_id, accepted))
    except Exception:
        logger.warning(
            "result ack send failed dispatchId=%s executorId=%s",
            dispatch_id,
            executor_session.executor_id,
        )


async def _close_session(executor_session: ExecutorSession) -> None:
    try:
        await executor_session.websocket.close()
    except Exception:
        logger.warning(
            "failed to close executor session executorId=%s",
            executor_session.executor_id,
        )


def _heartbeat_snapshot(
    executor_session: ExecutorSession,
    payload: dict[str, Any],
    inventory: bool,
) -> DispatchPresence:
    reported_at = int(time.time() * 1000)
    if inventory:
        return _inventory_snapshot(executor_session, payload, reported_at)
    reported = _running_dispatch_ids(payload)
    running: list[int] = []
    if reported is not None:
        running = list(dict.fromkeys(reported))
    return DispatchPresence(
        session_id=executor_session.session_id,
        capacity=executor_session.max_concurrent_dispatches,
        authoritative_inventory=False,
        inventory_ready=False,
        running_dispatch_ids=running,
        owned_dispatch_ids=running,
        running_conversation_turn_ids=_conversation_turns(payload),
        protocol_features=[],
        inventory_error=None,
        reported_at=reported_at,
    )


def _inventory_snapshot(
    executor_session: ExecutorSession,
    payload: dict[str, Any],
    reported_at: int,
) -> DispatchPresence:
    capacity_value = payload.get("maxConcurrentDispatches")
    if isinstance(capacity_value, bool) or not isinstance(capacity_value, int):
        raise ValueError("INVALID_MAX_CONCURRENT_DISPATCHES")
    if capacity_value < 1 or capacity_value > 50:
        raise ValueError("INVALID_MAX_CONCURRENT_DISPATCHES")
    ready_value = payload.get("dispatchInventoryReady")
    if not isinstance(ready_value, bool):
        raise ValueError("DISPATCH_INVENTORY_READY_MISSING")
    running = _required_long_set(payload, "runningDispatchIds", 50)
    owned = _required_long_set(payload, "ownedDispatchIds", 1000)
    conversations = _required_long_set(payload, "runningConversationTurnIds", 50)
    inventory_error = _text(payload, "dispatchInventoryError")
    overflow = inventory_error == "OWNED_DISPATCH_LIMIT_EXCEEDED"
    if overflow:
        if ready_value or set(owned) != set(running):
            raise ValueError("INVALID_DISPATCH_INVENTORY_OVERFLOW")
    else:
        if inventory_error is not None and inventory_error.strip() != "":
            raise ValueError("UNKNOWN_DISPATCH_INVENTORY_ERROR")
        if not set(running).issubset(set(owned)):
            raise ValueError("RUNNING_DISPATCH_NOT_OWNED")
    return DispatchPresence(
        session_id=executor_session.session_id,
        capacity=capacity_value,
        authoritative_inventory=True,
        inventory_ready=ready_value,
        running_dispatch_ids=running,
        owned_dispatch_ids=owned,
        running_conversation_turn_ids=conversations,
        protocol_features=[],
        inventory_error=inventory_error,
        reported_at=reported_at,
    )


def _required_long_set(payload: dict[str, Any], field: str, max_size: int) -> list[int]:
    raw = payload.get(field)
    if not isinstance(raw, list):
        raise ValueError(field + "_MISSING")
    if len(raw) > max_size:
        raise ValueError(field + "_TOO_LARGE")
    ids: list[int] = []
    for element in raw:
        if isinstance(element, bool) or not isinstance(element, int) or element <= 0:
            raise ValueError(field + "_INVALID")
        if element in ids:
            raise ValueError(field + "_INVALID")
        ids.append(element)
    return ids


def _running_dispatch_ids(payload: dict[str, Any]) -> list[int] | None:
    if "runningDispatchIds" not in payload:
        return None
    raw = payload.get("runningDispatchIds")
    if not isinstance(raw, list):
        return None
    ids: list[int] = []
    for element in raw[:50]:
        if isinstance(element, bool) or not isinstance(element, int) or element <= 0:
            return None
        ids.append(element)
    return ids


def _conversation_turns(payload: dict[str, Any]) -> list[int] | None:
    if "runningConversationTurnIds" not in payload and "runningConversationTurns" not in payload:
        return None
    ids: list[int] = []
    raw_ids = payload.get("runningConversationTurnIds")
    if isinstance(raw_ids, list):
        for element in raw_ids:
            if len(ids) >= 50:
                break
            if isinstance(element, bool) or not isinstance(element, int) or element <= 0:
                continue
            if element not in ids:
                ids.append(element)
    raw_turns = payload.get("runningConversationTurns")
    if isinstance(raw_turns, list):
        for turn in raw_turns:
            if len(ids) >= 50:
                break
            if not isinstance(turn, dict):
                continue
            turn_id = turn.get("turnId")
            if isinstance(turn_id, bool) or not isinstance(turn_id, int) or turn_id <= 0:
                continue
            if turn_id not in ids:
                ids.append(turn_id)
    return ids


def _failure(payload: dict[str, Any], success: bool) -> tuple[str | None, str | None]:
    category = _text(payload, "failureCategory")
    scope = _text(payload, "failureScope")
    if success:
        return category, scope
    if category in EXECUTOR_FAILURE_CATEGORIES:
        return category, "EXECUTOR"
    if category is None or category.strip() == "":
        classified = classify_executor_failure(_text(payload, "error"))
        if classified is not None:
            return classified, "EXECUTOR"
    return category, scope


def _long(payload: dict[str, Any], key: str) -> int:
    raw = payload.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def _int(payload: dict[str, Any], key: str) -> int:
    return _long(payload, key)


def _text(payload: dict[str, Any], key: str) -> str | None:
    raw = payload.get(key)
    if isinstance(raw, str):
        return raw
    return None


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    for needle in needles:
        if needle in value:
            return True
    return False


inbound_router = InboundFrameRouter(presence_manager)


def current_session_is_registered(executor_session: ExecutorSession) -> bool:
    """入站帧只处理会话表里的当前连接。"""
    return session_registry.is_current(executor_session)
