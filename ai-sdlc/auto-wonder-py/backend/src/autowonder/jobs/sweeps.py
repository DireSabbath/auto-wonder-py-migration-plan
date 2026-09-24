"""其余 16 个定时任务的一轮扫描。锁和间隔与 Java ``@Scheduled`` 一致。"""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.ai.models import AiSession
from autowonder.config import get_settings
from autowonder.conversations.elicitation import notify_canceled, notify_expired
from autowonder.conversations.models import (
    AgentConversation,
    AgentConversationElicitation,
    AgentConversationTurn,
)
from autowonder.conversations.turns import recover_stale_turn, send_prepared_turn
from autowonder.core.clock import SHANGHAI, now_local
from autowonder.core.locks import release_lock, try_acquire_lock
from autowonder.core.redis import redis_client
from autowonder.db.rows import rowcount
from autowonder.db.session import SessionLocal
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.pending import (
    fail_and_drive,
    on_unacknowledged_timeout,
    run_pending,
)
from autowonder.dispatch.recovery import reconcile, retry_packaging, transition
from autowonder.executors.catalog import (
    catalog_snapshot_json,
    clear_catalog_exchange,
    put_catalog_ticket,
    take_catalog_result,
)
from autowonder.executors.models import Executor, ExecutorUpdateTask
from autowonder.executors.presence import current_version, executor_online, supports_feature
from autowonder.executors.upgrade import deliver_task
from autowonder.executors.version import is_behind
from autowonder.insights.service import request_refresh
from autowonder.integrations.aone_codec import aone_enabled
from autowonder.integrations.aone_outbox import dispatch_pending
from autowonder.integrations.aone_service import crypto
from autowonder.integrations.aone_sync import reconcile_linked_workitems, sync_binding_increment
from autowonder.integrations.dingtalk_bindings import start_stream_if_eligible
from autowonder.integrations.models import (
    DingtalkRobotBinding,
    ExternalProjectBinding,
    FeishuMessageInbox,
    FeishuRobotBinding,
    IntegrationOutbox,
)
from autowonder.integrations.receipts_sanitize import sanitize_error
from autowonder.jobs.account_deactivation import account_deactivation_expiry as _expire_accounts
from autowonder.jobs.cluster import under_lock
from autowonder.jobs.debug_log_reconciliation import reconcile_debug_logs
from autowonder.repos.models import Repo
from autowonder.workspaces.models import Org
from autowonder.ws.mailbox import deliver_executor_frame
from autowonder.ws.presence import presence_manager

logger = logging.getLogger(__name__)

_AI_LOCK = "ai:compensation:lock"
_WORKSPACE_LOCK = "workspace:cleanup:sweep:lock"
_DISPATCH_LOCK = "dispatch:compensation:lock"
_LOCK_TTL_MILLIS = 60_000
_STALE_TURN = timedelta(minutes=5)
_MAX_DISPATCH_ATTEMPTS = 3
_RECOVERY_BATCH = 100
_ELICITATION_TIMEOUT = timedelta(minutes=30)
_ELICITATION_BATCH = 200
_EVENT_RETENTION_DAYS = 30
_EVENT_BATCH = 1000
_CLEANUP_RETENTION = timedelta(days=3)
_CLEANUP_BATCH = 200
_CLEANUP_FEATURE = "WORKITEM_WORKSPACE_CLEANUP"
_UPDATE_BATCH = 200
_UPDATE_FEATURE = "EXECUTOR_UPDATE_V1"
_OFFLINE_RECHECK = timedelta(seconds=300)
_AUTO_COOLDOWN = timedelta(seconds=1800)
_BACKOFF = (60, 300)
_ACTIVE_UPDATE = ("PENDING", "DRAINING", "UPDATING")
_MIN_POLL_SECONDS = 15
_POLL_ATTEMPTS: dict[int, datetime] = {}
_CATALOG_LOCK = "model-catalog:refresh:"
_CATALOG_COOLDOWN = "model-catalog:cooldown:"
_CATALOG_INFLIGHT = "model-catalog:inflight:"
_CATALOG_SNAPSHOT = "model-catalog:snapshot:"
_CATALOG_ACTIVE = ("PACKAGING", "DISPATCHED", "ACKED", "RUNNING", "PAUSING")
_CATALOG_WAIT_SECONDS = 20
_CATALOG_FEATURE = "QODER_MODEL_CATALOG_V1"
_CATALOG_KINDS = {"qoder": "QODER_CLI", "qodercn": "QODER_CN_CLI"}
_RUN_TIMEOUT = "TIMEOUT"
_PAUSE_TIMEOUT = "PAUSE_CONFIRMATION_MISSING: 暂停确认超时，平台未收到有效暂停检查点"
_READBACK_UNAVAILABLE = "readback unavailable for connector/event"


def recovery_action(reported: bool, active_turn_ids: set[int], turn_id: int, attempts: int) -> str:
    """运行时没上报就跳过；仍在执行就跳过；次数用尽则失败，否则重投。"""
    if not reported:
        return "skip_unknown"
    if turn_id in active_turn_ids:
        return "skip_active"
    if attempts >= _MAX_DISPATCH_ATTEMPTS:
        return "fail"
    return "redeliver"


def poll_interval_seconds(configured: int | None) -> int:
    """单个 Aone 绑定两次轮询至少间隔 15 秒。"""
    value = _MIN_POLL_SECONDS if configured is None else configured
    return max(value, _MIN_POLL_SECONDS)


def should_poll(
    binding_id: int,
    configured_seconds: int | None,
    last_success_at: datetime | None,
    now: datetime,
) -> bool:
    """本进程刚轮询过，或距离上次成功还没到间隔，就不再打 Aone。"""
    interval = timedelta(seconds=poll_interval_seconds(configured_seconds))
    attempted = _POLL_ATTEMPTS.get(binding_id)
    if attempted is not None and now < attempted + interval:
        return False
    if last_success_at is None:
        return True
    return now >= last_success_at + interval


async def executor_update_scan() -> None:
    """重新投递到期升级，并给在线旧版本安排自动升级。"""
    async with SessionLocal() as session:
        now = now_local()
        tasks = list(
            await session.scalars(
                select(ExecutorUpdateTask)
                .where(
                    ExecutorUpdateTask.is_deleted == 0,
                    ExecutorUpdateTask.status.in_(_ACTIVE_UPDATE),
                    (
                        ExecutorUpdateTask.next_attempt_at.is_(None)
                        | (ExecutorUpdateTask.next_attempt_at <= now)
                    ),
                )
                .order_by(ExecutorUpdateTask.next_attempt_at.asc(), ExecutorUpdateTask.id.asc())
                .limit(_UPDATE_BATCH)
            )
        )
        for task in tasks:
            try:
                await _reconcile_update(session, task, now)
            except Exception:
                logger.warning(
                    "executor update reconcile failed taskId=%s executorId=%s",
                    task.id,
                    task.executor_id,
                    exc_info=True,
                )
        if get_settings().executor_auto_update_enabled:
            await _auto_schedule(session, now)
        await session.commit()


async def provider_model_catalog_refresh() -> None:
    """qoder 和 qodercn 的目录过期后向在线执行器要一份新快照。"""
    for provider in ("qoder", "qodercn"):
        await _refresh_catalog_if_due(provider)


async def account_deactivation_expiry() -> None:
    """注销冷静期结束的账号。单轮失败留到下一分钟。"""
    try:
        processed = await _expire_accounts()
    except Exception:
        logger.error("Failed to process expired account deactivations", exc_info=True)
        return
    if processed > 0:
        logger.info("Processed %s expired account deactivations", processed)


async def conversation_turn_event_cleanup() -> None:
    """删掉超过保留天数的会话事件，按批循环直到这一轮删不满。"""
    cutoff = now_local() - timedelta(days=_EVENT_RETENTION_DAYS)
    total = 0
    deleted = _EVENT_BATCH
    async with SessionLocal() as session:
        while deleted >= _EVENT_BATCH:
            result = await session.execute(
                text(
                    "DELETE FROM agent_conversation_turn_event "
                    "WHERE gmt_create < :cutoff LIMIT :limit"
                ),
                {"cutoff": cutoff, "limit": _EVENT_BATCH},
            )
            deleted = rowcount(result)
            total += deleted
        await session.commit()
    if total > 0:
        logger.info(
            "conversation turn event cleanup deleted=%s rows older than %s days",
            total,
            _EVENT_RETENTION_DAYS,
        )


async def agent_conversation_recovery() -> None:
    """收回超过 5 分钟仍在处理、且执行器不再报告该轮次的入站消息。"""
    try:
        await _recover_stale_turns()
    except Exception:
        logger.warning("conversation stale turn recovery scan failed", exc_info=True)


async def conversation_elicitation_expiry() -> None:
    """把超过 30 分钟仍挂起的问答卡片标成过期。"""
    cutoff = now_local() - _ELICITATION_TIMEOUT
    try:
        await _expire_elicitations(cutoff)
    except Exception:
        logger.warning(
            "conversation elicitation expiry sweep failed cutoff=%s",
            cutoff,
            exc_info=True,
        )


async def debug_log_reconciliation() -> None:
    """对账超过 24 小时的 PENDING debug_log。锁在对账函数里。"""
    await reconcile_debug_logs()


async def ai_compensation() -> None:
    """把卡住 10 分钟的 RUNNING 会话标成失败。"""
    await under_lock(_AI_LOCK, _LOCK_TTL_MILLIS, _ai_sweep)


async def workspace_cleanup() -> None:
    """向在线执行器发送已完成工单的工作区清理帧。"""
    await under_lock(_WORKSPACE_LOCK, _LOCK_TTL_MILLIS, _cleanup_sweep)


async def dispatch_compensation() -> None:
    """重试卡住的派发，并把超时的执行收成 TIMEOUT。"""
    await under_lock(_DISPATCH_LOCK, _LOCK_TTL_MILLIS, _dispatch_sweep)


async def aone_outbox_dispatch() -> None:
    """发送一批外部写回。Aone 关闭时跳过 AONE 行。"""
    async with SessionLocal() as session:
        await dispatch_pending(session, 20)
        await session.commit()


async def aone_inbound_poll() -> None:
    """Aone 打开时按绑定间隔拉取项目工单。"""
    if not aone_enabled():
        return
    now = now_local()
    async with SessionLocal() as session:
        bindings = list(
            await session.scalars(
                select(ExternalProjectBinding).where(
                    ExternalProjectBinding.provider == "AONE",
                    ExternalProjectBinding.enabled == 1,
                    ExternalProjectBinding.is_deleted == 0,
                )
            )
        )
        due = [
            binding
            for binding in bindings
            if should_poll(binding.id, binding.poll_interval_seconds, binding.last_success_at, now)
        ]
        for binding in due:
            _POLL_ATTEMPTS[binding.id] = now
        await session.commit()
    client_holder = _aone_client()
    for binding_id in [binding.id for binding in due]:
        await _poll_binding(binding_id, client_holder)


async def feishu_inbox_drain() -> None:
    """把飞书收件箱里到点的消息送进会话。抢不到的行留给别的节点。"""
    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(FeishuMessageInbox)
                .where(
                    FeishuMessageInbox.status.in_(("PENDING", "PROCESSING")),
                    FeishuMessageInbox.available_at <= now_local(),
                )
                .order_by(FeishuMessageInbox.id.asc())
                .limit(20)
            )
        )
        for row in rows:
            previous_attempts = row.attempts
            claimed = await session.execute(
                update(FeishuMessageInbox)
                .where(
                    FeishuMessageInbox.id == row.id,
                    FeishuMessageInbox.attempts == previous_attempts,
                    FeishuMessageInbox.status.in_(("PENDING", "PROCESSING")),
                    FeishuMessageInbox.available_at <= now_local(),
                )
                .values(
                    status="PROCESSING",
                    attempts=previous_attempts + 1,
                    available_at=now_local() + timedelta(seconds=120),
                )
            )
            if rowcount(claimed) != 1:
                continue
            await _deliver_feishu(session, row, previous_attempts)
        await session.commit()


async def dingtalk_stream_reconcile() -> None:
    """拉起启用中的钉钉 Stream 绑定。开关关闭时什么都不做。"""
    if not get_settings().dingtalk_stream_enabled:
        return
    async with SessionLocal() as session:
        bindings = list(
            await session.scalars(
                select(DingtalkRobotBinding).where(
                    DingtalkRobotBinding.status == "ENABLED",
                    DingtalkRobotBinding.transport_mode == "STREAM",
                )
            )
        )
    for binding in bindings:
        await start_stream_if_eligible(binding)


async def external_operation_recovery() -> None:
    """接管超时的发送中回执。没有回读实现时标成 UNKNOWN。"""
    before = now_local() - timedelta(seconds=30)
    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(IntegrationOutbox)
                .where(
                    (
                        (IntegrationOutbox.status == "SENDING")
                        | (
                            (IntegrationOutbox.status == "UNKNOWN")
                            & (IntegrationOutbox.event_type == "COMMENT_CREATE")
                        )
                    ),
                    IntegrationOutbox.gmt_modified <= before,
                )
                .order_by(IntegrationOutbox.gmt_modified.asc(), IntegrationOutbox.id.asc())
                .limit(20)
            )
        )
        for receipt in rows:
            expected = 0 if receipt.lock_version is None else receipt.lock_version
            taken = await session.execute(
                update(IntegrationOutbox)
                .where(
                    IntegrationOutbox.id == receipt.id,
                    IntegrationOutbox.lock_version == expected,
                    IntegrationOutbox.status.in_(("SENDING", "UNKNOWN")),
                    IntegrationOutbox.gmt_modified <= before,
                )
                .values(
                    status="UNKNOWN",
                    lock_version=IntegrationOutbox.lock_version + 1,
                    next_retry_at=None,
                    gmt_modified=now_local(),
                )
            )
            if rowcount(taken) != 1:
                continue
            await session.execute(
                update(IntegrationOutbox)
                .where(
                    IntegrationOutbox.id == receipt.id,
                    IntegrationOutbox.lock_version == expected + 1,
                    IntegrationOutbox.status.in_(("SENDING", "UNKNOWN")),
                )
                .values(
                    status="UNKNOWN",
                    last_error=sanitize_error(_READBACK_UNAVAILABLE),
                    next_retry_at=None,
                    gmt_modified=now_local(),
                )
            )
        await session.commit()


async def human_agent_participation_snapshot() -> None:
    """每天为未删除的工作空间排队一份参与度快照。

    与 Java ``WorkspaceDao.listActive`` 一样，停用（status=1）但仍未删除的空间也要刷新。
    """
    data_through = datetime.now(SHANGHAI).date() - timedelta(days=1)
    async with SessionLocal() as session:
        tenants = list(await session.scalars(select(Org).where(Org.is_deleted == 0)))
    logger.info(
        "Participation nightly rebuild starting tenants=%s dataThrough=%s",
        len(tenants),
        data_through,
    )
    for tenant in tenants:
        await request_refresh(tenant.id)


async def _reconcile_update(
    session: AsyncSession,
    task: ExecutorUpdateTask,
    now: datetime,
) -> None:
    newest = await session.scalar(
        select(ExecutorUpdateTask)
        .where(
            ExecutorUpdateTask.tenant_id == task.tenant_id,
            ExecutorUpdateTask.executor_id == task.executor_id,
            ExecutorUpdateTask.is_deleted == 0,
            ExecutorUpdateTask.status.in_(_ACTIVE_UPDATE),
        )
        .order_by(ExecutorUpdateTask.id.desc())
        .limit(1)
    )
    if newest is not None and newest.id != task.id:
        await _record_update_failure(session, task, "已被新的升级任务取代", True)
        return
    if not await executor_online(task.executor_id):
        await session.execute(
            update(ExecutorUpdateTask)
            .where(
                ExecutorUpdateTask.id == task.id,
                ExecutorUpdateTask.is_deleted == 0,
                ExecutorUpdateTask.status.in_(_ACTIVE_UPDATE),
            )
            .values(next_attempt_at=now + _OFFLINE_RECHECK)
        )
        return
    if not await supports_feature(task.executor_id, _UPDATE_FEATURE):
        await _record_update_failure(
            session,
            task,
            "客户端不支持远程升级，请在本地升级客户端",
            True,
        )
        return
    if task.delivered_at is None:
        await deliver_task(session, task)
        return
    await _record_update_failure(session, task, "等待客户端响应超时", False)


async def _record_update_failure(
    session: AsyncSession,
    task: ExecutorUpdateTask,
    reason: str,
    terminal: bool,
) -> None:
    attempts = (0 if task.attempt_count is None else task.attempt_count) + 1
    budget = _MAX_DISPATCH_ATTEMPTS if task.max_attempts is None else task.max_attempts
    exhausted = terminal or attempts >= budget
    nxt = None
    if not exhausted:
        delay = _BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)]
        nxt = now_local() + timedelta(seconds=delay)
    target = "FAILED" if exhausted else "PENDING"
    await session.execute(
        update(ExecutorUpdateTask)
        .where(
            ExecutorUpdateTask.id == task.id,
            ExecutorUpdateTask.is_deleted == 0,
            ExecutorUpdateTask.status.in_(_ACTIVE_UPDATE),
        )
        .values(
            status=target,
            attempt_count=ExecutorUpdateTask.attempt_count + 1,
            last_error=reason[:1000],
            next_attempt_at=nxt,
            delivered_at=None,
            completed_at=now_local() if target == "FAILED" else None,
        )
    )


async def _auto_schedule(session: AsyncSession, now: datetime) -> None:
    target = get_settings().recommended_runtime_version
    cooldown_start = now - _AUTO_COOLDOWN
    executors = list(
        await session.scalars(
            select(Executor).where(Executor.is_deleted == 0).order_by(Executor.id.asc())
        )
    )
    for executor in executors:
        if not await executor_online(executor.id):
            continue
        if not await supports_feature(executor.id, _UPDATE_FEATURE):
            continue
        reported = await current_version(executor.id)
        if not is_behind(reported, target):
            continue
        active = await session.scalar(
            select(ExecutorUpdateTask.id)
            .where(
                ExecutorUpdateTask.tenant_id == executor.tenant_id,
                ExecutorUpdateTask.executor_id == executor.id,
                ExecutorUpdateTask.is_deleted == 0,
                ExecutorUpdateTask.status.in_(_ACTIVE_UPDATE),
            )
            .limit(1)
        )
        if active is not None:
            continue
        recent = await session.scalar(
            select(func.count())
            .select_from(ExecutorUpdateTask)
            .where(
                ExecutorUpdateTask.tenant_id == executor.tenant_id,
                ExecutorUpdateTask.executor_id == executor.id,
                ExecutorUpdateTask.is_deleted == 0,
                ExecutorUpdateTask.source == "AUTO",
                ExecutorUpdateTask.status == "FAILED",
                ExecutorUpdateTask.completed_at.is_not(None),
                ExecutorUpdateTask.completed_at >= cooldown_start,
            )
        )
        if recent:
            continue
        created = ExecutorUpdateTask(
            tenant_id=executor.tenant_id,
            executor_id=executor.id,
            request_id=str(uuid.uuid4()),
            current_version=reported,
            target_version=target,
            status="PENDING",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at=now,
            source="AUTO",
            requested_at=now,
            is_deleted=0,
        )
        session.add(created)
        await session.flush()
        await deliver_task(session, created)


async def _refresh_catalog_if_due(provider: str) -> None:
    client = redis_client()
    raw = await client.get(_CATALOG_SNAPSHOT + provider)
    if raw and not _catalog_due(raw):
        return
    if await client.exists(_CATALOG_COOLDOWN + provider):
        return
    owner = str(uuid.uuid4())
    locked = await try_acquire_lock(_CATALOG_LOCK + provider, owner, 120_000)
    if not locked:
        return
    try:
        await client.set(_CATALOG_INFLIGHT + provider, "1", ex=120)
        await _request_catalog(provider)
    finally:
        await client.delete(_CATALOG_INFLIGHT + provider)
        await release_lock(_CATALOG_LOCK + provider, owner)


def _catalog_due(raw: str) -> bool:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return True
    if not isinstance(payload, dict):
        return True
    millis = payload.get("lastSuccessfulAt")
    if not isinstance(millis, int) or isinstance(millis, bool):
        return True
    age = datetime.now(UTC).timestamp() * 1000 - millis
    return age >= 86_400_000


async def _request_catalog(provider: str) -> None:
    kind = _CATALOG_KINDS[provider]
    async with SessionLocal() as session:
        executors = list(
            await session.scalars(
                select(Executor)
                .where(Executor.client_kind == kind, Executor.is_deleted == 0)
                .order_by(Executor.id.asc())
            )
        )
    sent = 0
    for executor in executors:
        if sent >= 3:
            break
        if not await executor_online(executor.id):
            continue
        if not await supports_feature(executor.id, _CATALOG_FEATURE):
            continue
        if await _catalog_executor_busy(executor.id):
            continue
        request_id = str(uuid.uuid4())
        await put_catalog_ticket(request_id, executor.tenant_id, executor.id, provider)
        frame = {
            "type": "QODER_MODEL_CATALOG_REQUEST",
            "requestId": request_id,
            "executorId": executor.id,
            "provider": provider,
        }
        await deliver_executor_frame(
            executor.id,
            json.dumps(frame, ensure_ascii=False, separators=(",", ":")),
        )
        result = await _wait_catalog_result(request_id)
        await clear_catalog_exchange(request_id)
        sent += 1
        models = result.get("models") if result is not None else None
        if result is not None and result.get("success") is True and isinstance(models, list):
            await redis_client().set(
                _CATALOG_SNAPSHOT + provider,
                catalog_snapshot_json(provider, executor.id, models),
            )
            return


async def _catalog_executor_busy(executor_id: int) -> bool:
    async with SessionLocal() as session:
        active = await session.scalar(
            select(func.count())
            .select_from(Dispatch)
            .where(
                Dispatch.executor_id == executor_id,
                Dispatch.status.in_(_CATALOG_ACTIVE),
                Dispatch.is_deleted == 0,
            )
        )
    return active != 0


async def _wait_catalog_result(request_id: str) -> dict[str, object] | None:
    deadline = time.monotonic() + _CATALOG_WAIT_SECONDS
    while True:
        result = await take_catalog_result(request_id)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def _recover_stale_turns() -> None:
    cutoff = now_local() - _STALE_TURN
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(AgentConversationTurn, AgentConversation)
                    .join(
                        AgentConversation,
                        (AgentConversation.id == AgentConversationTurn.conversation_id)
                        & (AgentConversation.tenant_id == AgentConversationTurn.tenant_id),
                    )
                    .where(
                        AgentConversationTurn.direction == "IN",
                        AgentConversationTurn.status == "PROCESSING",
                        AgentConversation.executor_id.is_not(None),
                        (
                            (AgentConversationTurn.last_dispatch_at <= cutoff)
                            | (
                                AgentConversationTurn.last_dispatch_at.is_(None)
                                & (AgentConversationTurn.gmt_create <= cutoff)
                            )
                        ),
                    )
                    .order_by(
                        func.coalesce(
                            AgentConversationTurn.last_dispatch_at,
                            AgentConversationTurn.gmt_create,
                        ).asc(),
                        AgentConversationTurn.id.asc(),
                    )
                    .limit(_RECOVERY_BATCH)
                )
            ).all()
        )
        settled: list[AgentConversationElicitation] = []
        pending_sends = []
        for turn, conversation in rows:
            if conversation.executor_id is None:
                continue
            try:
                async with session.begin_nested():
                    reported, active = await _runtime_activity(conversation.executor_id)
                    action = recovery_action(reported, active, turn.id, turn.dispatch_attempt)
                    effect = await recover_stale_turn(session, turn, conversation, cutoff, action)
            except Exception:
                logger.warning(
                    "conversation stale turn recovery failed conversationId=%s turnId=%s",
                    turn.conversation_id,
                    turn.id,
                    exc_info=True,
                )
                continue
            settled.extend(effect.settled)
            if effect.pending is not None:
                pending_sends.append(effect.pending)
        await session.commit()
        await notify_canceled(session, settled)
        for pending in pending_sends:
            await send_prepared_turn(session, pending)


async def _runtime_activity(executor_id: int) -> tuple[bool, set[int]]:
    if not await presence_manager.is_executor_online(executor_id):
        return False, set()
    snapshot = await presence_manager.current_dispatch_snapshot(executor_id)
    if snapshot is None or not snapshot.has_conversation_activity_report():
        return False, set()
    turns = snapshot.running_conversation_turn_ids
    if turns is None:
        return False, set()
    return True, set(turns)


async def _expire_elicitations(cutoff: datetime) -> None:
    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(AgentConversationElicitation)
                .where(
                    AgentConversationElicitation.status == "PENDING",
                    AgentConversationElicitation.gmt_create < cutoff,
                )
                .order_by(AgentConversationElicitation.id.asc())
                .limit(_ELICITATION_BATCH)
            )
        )
        expired: list[AgentConversationElicitation] = []
        for row in rows:
            result = await session.execute(
                update(AgentConversationElicitation)
                .where(
                    AgentConversationElicitation.tenant_id == row.tenant_id,
                    AgentConversationElicitation.conversation_id == row.conversation_id,
                    AgentConversationElicitation.request_id == row.request_id,
                    AgentConversationElicitation.status == "PENDING",
                )
                .values(status="EXPIRED", answer_json=None, gmt_modified=func.now())
            )
            if rowcount(result) == 1:
                expired.append(row)
        await session.commit()
        await notify_expired(session, expired)


async def _ai_sweep() -> None:
    cutoff = now_local() - timedelta(minutes=10)
    logger.info("ai compensation sweep started")
    async with SessionLocal() as session:
        stuck = list(
            await session.scalars(
                select(AiSession)
                .where(
                    AiSession.status == "RUNNING",
                    AiSession.gmt_modified < cutoff,
                    AiSession.is_deleted == 0,
                )
                .limit(100)
            )
        )
        logger.info("ai compensation found stuck=%s", len(stuck))
        for row in stuck:
            result = await session.execute(
                update(AiSession)
                .where(
                    AiSession.id == row.id,
                    AiSession.tenant_id == row.tenant_id,
                    AiSession.version == row.version,
                )
                .values(
                    status="FAILED",
                    error="session stuck (node may have crashed)",
                    version=AiSession.version + 1,
                )
            )
            if rowcount(result) != 1:
                continue
            if (
                row.scene == "REPO_SCAN"
                and row.biz_ref_type == "REPO"
                and row.biz_ref_id is not None
            ):
                repo = await session.get(Repo, row.biz_ref_id)
                if repo is not None and repo.tenant_id == row.tenant_id:
                    await session.execute(
                        update(Repo)
                        .where(
                            Repo.id == repo.id,
                            Repo.tenant_id == row.tenant_id,
                            Repo.version == repo.version,
                        )
                        .values(scan_status="FAILED", version=Repo.version + 1)
                    )
            logger.info("ai compensation: marked stuck session FAILED id=%s", row.id)
        await session.commit()


async def _cleanup_sweep() -> None:
    cutoff = now_local() - _CLEANUP_RETENTION
    async with SessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT w.tenant_id AS tenant_id,
                       w.id AS workitem_id,
                       d.executor_id AS executor_id,
                       w.version AS workitem_version,
                       MAX(e.gmt_create) AS published_at
                FROM workitem w
                INNER JOIN status_node sn ON sn.id = w.status_node_id
                INNER JOIN workitem_event e
                        ON e.tenant_id = w.tenant_id
                       AND e.workitem_id = w.id
                       AND e.event_type = 'STATUS_CHANGE'
                       AND UPPER(e.to_val) = UPPER(sn.code)
                INNER JOIN dispatch d
                        ON d.tenant_id = w.tenant_id
                       AND d.workitem_id = w.id
                       AND d.executor_id IS NOT NULL
                       AND d.is_deleted = 0
                WHERE w.is_deleted = 0
                  AND (
                      UPPER(sn.category) = 'DONE'
                      OR UPPER(sn.code) LIKE '%DONE%'
                      OR UPPER(sn.code) LIKE '%CLOSED%'
                      OR UPPER(sn.code) LIKE '%RELEASED%'
                      OR UPPER(sn.code) LIKE '%PUBLISHED%'
                      OR sn.name LIKE '%完成%'
                      OR sn.name LIKE '%关闭%'
                      OR sn.name LIKE '%发布%'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM dispatch active_dispatch
                      WHERE active_dispatch.tenant_id = w.tenant_id
                        AND active_dispatch.workitem_id = w.id
                        AND active_dispatch.is_deleted = 0
                        AND active_dispatch.status IN (
                            'PENDING', 'PACKAGING', 'DISPATCHED', 'ACKED', 'RUNNING',
                            'PAUSING', 'PAUSED', 'PAUSE_FAILED', 'WAITING_FOR_PAUSE'
                        )
                  )
                GROUP BY w.tenant_id, w.id, d.executor_id, w.version
                HAVING MAX(e.gmt_create) <= :cutoff
                ORDER BY MAX(e.gmt_create) ASC, w.id ASC
                LIMIT :limit
                """
            ),
            {"cutoff": cutoff, "limit": _CLEANUP_BATCH},
        )
        candidates = result.mappings().all()
    client = redis_client()
    for candidate in candidates:
        executor_id = candidate["executor_id"]
        if executor_id is None or candidate["workitem_id"] is None:
            continue
        if not await presence_manager.is_executor_online(int(executor_id)):
            continue
        if not await presence_manager.supports_protocol_feature(
            int(executor_id),
            _CLEANUP_FEATURE,
        ):
            continue
        marker = (
            "workspace:cleanup:sent:"
            + str(executor_id)
            + ":"
            + str(candidate["workitem_id"])
            + ":"
            + str(candidate["workitem_version"])
        )
        if await client.exists(marker):
            continue
        published = candidate["published_at"]
        published_ms = int(published.replace(tzinfo=SHANGHAI).timestamp() * 1000)
        frame = {
            "type": "WORKITEM_WORKSPACE_CLEANUP",
            "executorId": int(executor_id),
            "tenantId": int(candidate["tenant_id"]),
            "workitemId": int(candidate["workitem_id"]),
            "workitemVersion": int(candidate["workitem_version"]),
            "publishedAt": published_ms,
        }
        try:
            await deliver_executor_frame(
                int(executor_id),
                json.dumps(frame, separators=(",", ":")),
            )
        except Exception:
            logger.warning(
                "workspace cleanup request failed tenantId=%s workitemId=%s executorId=%s",
                candidate["tenant_id"],
                candidate["workitem_id"],
                executor_id,
                exc_info=True,
            )
            continue
        await client.set(marker, "1", ex=3600)


async def _dispatch_sweep() -> None:
    logger.info("compensation sweep started")
    async with SessionLocal() as session:
        try:
            await reconcile(session)
        except Exception:
            logger.warning("compensation recovery reconciliation failed", exc_info=True)
        now = now_local()
        phases = {
            "pending": (["PENDING"], now - timedelta(seconds=60)),
            "packaging": (["PACKAGING"], now - timedelta(minutes=5)),
            "unacknowledged": (["DISPATCHED"], now - timedelta(minutes=2)),
            "pausing": (["PAUSING", "PAUSE_FAILED"], now - timedelta(minutes=2)),
            "inflight": (["ACKED", "RUNNING"], now - timedelta(minutes=60)),
        }
        loaded = {
            name: await _load_stuck(session, statuses, cutoff)
            for name, (statuses, cutoff) in phases.items()
        }
        logger.info(
            "compensation found pending=%s packaging=%s unacknowledged=%s pausing=%s inflight=%s",
            len(loaded["pending"]),
            len(loaded["packaging"]),
            len(loaded["unacknowledged"]),
            len(loaded["pausing"]),
            len(loaded["inflight"]),
        )
        for row in loaded["pending"]:
            try:
                await run_pending(session, row.id)
            except Exception:
                logger.warning(
                    "compensation re-drive failed dispatchId=%s",
                    row.id,
                    exc_info=True,
                )
        for row in loaded["packaging"]:
            try:
                accepted = await retry_packaging(session, row, "PACKAGING_DEADLINE_EXCEEDED")
                if not accepted:
                    await fail_and_drive(
                        session,
                        row,
                        "TASK_PACKAGE_RETRIES_EXHAUSTED: 打包重试次数已耗尽",
                    )
            except Exception:
                logger.warning(
                    "compensation packaging-requeue failed dispatchId=%s",
                    row.id,
                    exc_info=True,
                )
        for row in loaded["unacknowledged"]:
            try:
                await on_unacknowledged_timeout(session, row)
            except Exception:
                logger.warning(
                    "compensation unacknowledged requeue failed dispatchId=%s",
                    row.id,
                    exc_info=True,
                )
        pausing_cutoff = now - timedelta(minutes=2)
        for row in loaded["pausing"]:
            try:
                if row.status == "PAUSING":
                    await session.execute(
                        update(Dispatch)
                        .where(
                            Dispatch.id == row.id,
                            Dispatch.tenant_id == row.tenant_id,
                            Dispatch.status == "PAUSING",
                            Dispatch.gmt_modified < pausing_cutoff,
                            Dispatch.is_deleted == 0,
                        )
                        .values(
                            status="PAUSE_FAILED",
                            error=_PAUSE_TIMEOUT[:512],
                            version=Dispatch.version + 1,
                            modifier_id=0,
                        )
                    )
            except Exception:
                logger.warning(
                    "compensation pause-expire failed dispatchId=%s",
                    row.id,
                    exc_info=True,
                )
        for row in loaded["inflight"]:
            try:
                await transition(session, row, "TIMEOUT", None, None, None, None, _RUN_TIMEOUT)
            except Exception:
                logger.warning("compensation timeout failed dispatchId=%s", row.id, exc_info=True)
        await session.commit()


async def _load_stuck(
    session: AsyncSession,
    statuses: list[str],
    cutoff: datetime,
) -> list[Dispatch]:
    try:
        return list(
            await session.scalars(
                select(Dispatch)
                .where(
                    Dispatch.is_deleted == 0,
                    Dispatch.gmt_modified < cutoff,
                    Dispatch.status.in_(statuses),
                )
                .order_by(Dispatch.gmt_modified.asc())
                .limit(200)
            )
        )
    except Exception:
        logger.warning("compensation query failed statuses=%s", statuses, exc_info=True)
        return []


def _aone_client() -> object:
    from autowonder.integrations.aone_api import AoneClient

    return AoneClient()


async def _poll_binding(binding_id: int, client: object) -> None:
    from autowonder.integrations.aone_api import AoneClient

    if not isinstance(client, AoneClient):
        return
    async with SessionLocal() as session:
        binding = await session.get(ExternalProjectBinding, binding_id)
        if binding is None:
            return
        tenant_id = binding.tenant_id
        project_id = binding.external_project_id
        try:
            await sync_binding_increment(session, client, crypto(), binding, _actor_id(binding))
            await session.commit()
        except Exception as error:
            await _mark_poll_failure(session, binding_id, tenant_id, project_id, error)
            return
    async with SessionLocal() as session:
        binding = await session.get(ExternalProjectBinding, binding_id)
        if binding is None:
            return
        tenant_id = binding.tenant_id
        project_id = binding.external_project_id
        try:
            reconciled = await reconcile_linked_workitems(
                session,
                client,
                crypto(),
                binding,
                _actor_id(binding),
                100,
            )
            await session.commit()
            if reconciled > 0:
                logger.info(
                    "Aone linked workitem reconciliation success bindingId=%s reconciledCount=%s",
                    binding_id,
                    reconciled,
                )
        except Exception as error:
            await _mark_poll_failure(session, binding_id, tenant_id, project_id, error)


async def _mark_poll_failure(
    session: AsyncSession,
    binding_id: int,
    tenant_id: int,
    project_id: str,
    error: Exception,
) -> None:
    await session.rollback()
    await session.execute(
        update(ExternalProjectBinding)
        .where(
            ExternalProjectBinding.id == binding_id,
            ExternalProjectBinding.tenant_id == tenant_id,
        )
        .values(last_error=str(error)[:4000])
    )
    await session.commit()
    logger.warning(
        "Aone inbound poll failed bindingId=%s tenantId=%s projectId=%s",
        binding_id,
        tenant_id,
        project_id,
        exc_info=True,
    )


def _actor_id(binding: ExternalProjectBinding) -> int:
    if binding.modifier_id is not None:
        return binding.modifier_id
    if binding.creator_id is not None:
        return binding.creator_id
    return 0


async def _deliver_feishu(
    session: AsyncSession,
    row: FeishuMessageInbox,
    previous_attempts: int,
) -> None:
    binding = await session.get(FeishuRobotBinding, row.binding_id)
    attempts = previous_attempts + 1
    try:
        if (
            binding is not None
            and binding.status == "ENABLED"
            and binding.agent_id == row.agent_id
        ):
            await _dispatch_feishu_text(session, binding, row.payload)
            await _feishu_health(session, binding, None)
        await _finish_feishu(session, row, attempts, "DONE", None)
    except Exception:
        error = "飞书消息处理失败，请检查应用凭据、权限和数字人的在线执行器"
        logger.warning(
            "Feishu inbox delivery failed bindingId=%s inboxId=%s attempt=%s",
            row.binding_id,
            row.id,
            attempts,
        )
        if binding is not None:
            await _feishu_health(session, binding, error)
        status = "FAILED" if previous_attempts >= 4 else "PENDING"
        await _finish_feishu(session, row, attempts, status, error)


async def _dispatch_feishu_text(
    session: AsyncSession,
    binding: FeishuRobotBinding,
    payload: str,
) -> None:
    from autowonder.conversations.turns import submit_inbound

    event = json.loads(payload)
    message = event.get("message")
    if not isinstance(message, dict):
        return
    content = json.loads(str(message.get("content") or "{}"))
    text_body = str(content.get("text") or "")
    if str(message.get("chat_type") or "") == "group":
        return
    if text_body.strip() == "":
        return
    chat_id = str(message.get("chat_id") or "")
    message_id = str(message.get("message_id") or "")
    thread_id = str(message.get("thread_id") or "")
    conversation_id = str(binding.id) + ":" + chat_id
    if thread_id.strip() != "":
        conversation_id = conversation_id + ":" + thread_id
    external_id = "FEISHU:" + str(binding.id) + ":" + message_id
    await submit_inbound(
        session,
        binding.tenant_id,
        binding.agent_id,
        "FEISHU",
        conversation_id,
        text_body.strip(),
        external_id,
    )


async def _feishu_health(
    session: AsyncSession,
    binding: FeishuRobotBinding,
    error: str | None,
) -> None:
    if error is None:
        await session.execute(
            update(FeishuRobotBinding)
            .where(
                FeishuRobotBinding.tenant_id == binding.tenant_id,
                FeishuRobotBinding.id == binding.id,
            )
            .values(last_success_at=now_local(), last_error=None)
        )
        return
    await session.execute(
        update(FeishuRobotBinding)
        .where(
            FeishuRobotBinding.tenant_id == binding.tenant_id,
            FeishuRobotBinding.id == binding.id,
        )
        .values(last_error=error)
    )


async def _finish_feishu(
    session: AsyncSession,
    row: FeishuMessageInbox,
    attempts: int,
    status: str,
    error: str | None,
) -> None:
    await session.execute(
        update(FeishuMessageInbox)
        .where(
            FeishuMessageInbox.id == row.id,
            FeishuMessageInbox.attempts == attempts,
            FeishuMessageInbox.status == "PROCESSING",
        )
        .values(
            status=status,
            last_error=error,
            available_at=now_local() + timedelta(seconds=30 * attempts),
        )
    )
