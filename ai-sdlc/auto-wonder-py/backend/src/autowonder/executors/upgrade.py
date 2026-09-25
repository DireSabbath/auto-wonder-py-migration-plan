"""手动和批量升级。任务写入 executor_update_task，指令经调度广播下发。"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.executors.models import Executor, ExecutorUpdateTask
from autowonder.executors.presence import current_version, executor_online, supports_feature
from autowonder.executors.restart import instant_text
from autowonder.executors.schemas import (
    ExecutorUpdateAllResultView,
    ExecutorUpdateSkipView,
    ExecutorUpdateView,
)
from autowonder.executors.store import list_executors, require_executor
from autowonder.executors.updates import runtime_auto_update_view
from autowonder.executors.version import compare_versions
from autowonder.ws.mailbox import deliver_executor_frame

logger = logging.getLogger(__name__)

_ACTIVE = ("PENDING", "DRAINING", "UPDATING")
_SOURCE_MANUAL = "MANUAL"
_SOURCE_BATCH = "BATCH"
_PROTOCOL_FEATURE = "EXECUTOR_UPDATE_V1"
_MAX_ATTEMPTS = 3
_ATTEMPT_TIMEOUT_SECONDS = 1800


def update_view(task: ExecutorUpdateTask) -> ExecutorUpdateView:
    """把一条升级任务收成列表和升级接口使用的视图。"""
    return ExecutorUpdateView(
        task_id=task.id,
        request_id=task.request_id,
        status=task.status,
        current_version=task.current_version,
        target_version=task.target_version,
        attempt_count=task.attempt_count,
        max_attempts=task.max_attempts,
        last_error=task.last_error,
        source=task.source,
        requested_at=task.requested_at,
        next_attempt_at=task.next_attempt_at,
        completed_at=task.completed_at,
    )


async def latest_updates(
    session: AsyncSession,
    tenant_id: int,
    executor_ids: list[int],
) -> dict[int, ExecutorUpdateView]:
    """每个执行器只取 id 最大的一条升级任务。"""
    if not executor_ids:
        return {}
    latest = (
        select(func.max(ExecutorUpdateTask.id).label("id"))
        .where(
            ExecutorUpdateTask.tenant_id == tenant_id,
            ExecutorUpdateTask.is_deleted == 0,
            ExecutorUpdateTask.executor_id.in_(executor_ids),
        )
        .group_by(ExecutorUpdateTask.executor_id)
        .subquery()
    )
    rows = await session.scalars(
        select(ExecutorUpdateTask).join(latest, ExecutorUpdateTask.id == latest.c.id)
    )
    return {task.executor_id: update_view(task) for task in rows}


async def update_one(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
    user_id: int,
) -> ExecutorUpdateView:
    """为单台执行器创建手动升级。已达目标版本、客户端不支持或已有任务时拒绝。"""
    executor = await require_executor(session, executor_id, tenant_id)
    target = runtime_auto_update_view().target_version
    reported = await current_version(executor_id)
    comparison = compare_versions(reported, target)
    if comparison is not None and comparison >= 0:
        raise BizError(ErrorCode.PARAM_INVALID, f"执行器版本不低于目标版本 {target}，无需升级")
    if await executor_online(executor_id) and not await _supports_upgrade(executor_id):
        raise BizError(ErrorCode.PARAM_INVALID, "当前客户端不支持远程升级，请先在本地升级客户端")
    if await _active_task(session, tenant_id, executor_id) is not None:
        raise BizError(ErrorCode.PARAM_INVALID, "已有升级任务进行中，请等待结果")
    task = await _insert_task(session, executor, target, reported, _SOURCE_MANUAL, user_id)
    await _deliver(session, task)
    return update_view(task)


async def update_all(
    session: AsyncSession,
    tenant_id: int,
    squad_ids: list[int] | None,
    user_id: int,
) -> ExecutorUpdateAllResultView:
    """给当前可见的执行器排升级。已最新、进行中或不支持的记入跳过，不让整批失败。"""
    target = runtime_auto_update_view().target_version
    executors = await list_executors(session, tenant_id, None, squad_ids)
    result = ExecutorUpdateAllResultView(target_version=target, total=len(executors))
    already_up_to_date = 0
    scheduled = 0
    skipped: list[ExecutorUpdateSkipView] = []
    for executor in executors:
        reported = await current_version(executor.id)
        comparison = compare_versions(reported, target)
        if comparison is not None and comparison >= 0:
            already_up_to_date = already_up_to_date + 1
            continue
        if await _active_task(session, tenant_id, executor.id) is not None:
            skipped.append(
                ExecutorUpdateSkipView(
                    executor_id=executor.id,
                    executor_name=executor.name,
                    reason="已有升级任务进行中",
                )
            )
            continue
        if await executor_online(executor.id) and not await _supports_upgrade(executor.id):
            skipped.append(
                ExecutorUpdateSkipView(
                    executor_id=executor.id,
                    executor_name=executor.name,
                    reason="客户端不支持远程升级",
                )
            )
            continue
        task = await _insert_task(session, executor, target, reported, _SOURCE_BATCH, user_id)
        await _deliver(session, task)
        scheduled = scheduled + 1
    result.already_up_to_date = already_up_to_date
    result.scheduled = scheduled
    result.skipped = skipped
    return result


async def _supports_upgrade(executor_id: int) -> bool:
    return await supports_feature(executor_id, _PROTOCOL_FEATURE)


async def _active_task(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
) -> ExecutorUpdateTask | None:
    return await session.scalar(
        select(ExecutorUpdateTask)
        .where(
            ExecutorUpdateTask.tenant_id == tenant_id,
            ExecutorUpdateTask.executor_id == executor_id,
            ExecutorUpdateTask.is_deleted == 0,
            ExecutorUpdateTask.status.in_(_ACTIVE),
        )
        .order_by(ExecutorUpdateTask.id.desc())
        .limit(1)
    )


async def _insert_task(
    session: AsyncSession,
    executor: Executor,
    target: str,
    reported: str | None,
    source: str,
    user_id: int,
) -> ExecutorUpdateTask:
    now = now_local()
    task = ExecutorUpdateTask(
        tenant_id=executor.tenant_id,
        executor_id=executor.id,
        request_id=str(uuid.uuid4()),
        current_version=reported,
        target_version=target,
        status="PENDING",
        attempt_count=0,
        max_attempts=_MAX_ATTEMPTS,
        next_attempt_at=now,
        source=source,
        requested_by=user_id,
        requested_at=now,
        creator_id=user_id,
        is_deleted=0,
    )
    session.add(task)
    await session.commit()
    return task


def upgrade_phase_target(phase: object) -> str | None:
    """客户端阶段对应推迟截止时间、推进状态、成功或失败。未知阶段忽略。"""
    if phase == "accepted":
        return "postpone"
    if phase == "draining":
        return "DRAINING"
    if phase in {"downloading", "applying"}:
        return "UPDATING"
    if phase == "success":
        return "SUCCESS"
    if phase == "failed":
        return "FAILED"
    return None


async def on_upgrade_result(
    session: AsyncSession,
    executor_id: int,
    frame: dict[str, object],
) -> None:
    """按 requestId 推进仍在进行的升级。阶段上报都会把截止时间再推迟 30 分钟。"""
    request_id = frame.get("requestId")
    if not isinstance(request_id, str) or request_id.strip() == "":
        return
    target = upgrade_phase_target(frame.get("phase"))
    if target is None:
        return
    task = await session.scalar(
        select(ExecutorUpdateTask)
        .where(
            ExecutorUpdateTask.request_id == request_id.strip(),
            ExecutorUpdateTask.is_deleted == 0,
        )
        .limit(1)
    )
    if task is None or task.executor_id != executor_id or task.status not in _ACTIVE:
        return
    if target == "postpone":
        await _postpone_attempt(session, task)
        return
    if target == "FAILED":
        await _fail_attempt(session, task, _upgrade_error(frame.get("error")), False)
        return
    if target == "SUCCESS":
        version = frame.get("currentVersion")
        if isinstance(version, str) and version.strip() != "":
            from autowonder.ws.presence import presence_manager

            await presence_manager.record_version(task.executor_id, version.strip())
        await session.execute(
            update(ExecutorUpdateTask)
            .where(
                ExecutorUpdateTask.id == task.id,
                ExecutorUpdateTask.is_deleted == 0,
                ExecutorUpdateTask.status.in_(_ACTIVE),
            )
            .values(status="SUCCESS", completed_at=now_local())
        )
        await session.commit()
        return
    await session.execute(
        update(ExecutorUpdateTask)
        .where(
            ExecutorUpdateTask.id == task.id,
            ExecutorUpdateTask.is_deleted == 0,
            ExecutorUpdateTask.status.in_(_ACTIVE),
        )
        .values(status=target)
    )
    await _postpone_attempt(session, task)


def _upgrade_error(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[:1000]


async def _postpone_attempt(session: AsyncSession, task: ExecutorUpdateTask) -> None:
    deadline = now_local() + timedelta(seconds=_ATTEMPT_TIMEOUT_SECONDS)
    await session.execute(
        update(ExecutorUpdateTask)
        .where(ExecutorUpdateTask.id == task.id, ExecutorUpdateTask.is_deleted == 0)
        .values(next_attempt_at=deadline)
    )
    await session.commit()


async def _fail_attempt(
    session: AsyncSession,
    task: ExecutorUpdateTask,
    reason: str,
    terminal: bool,
) -> None:
    attempts = task.attempt_count + 1
    budget = _MAX_ATTEMPTS if task.max_attempts is None else task.max_attempts
    exhausted = terminal or attempts >= budget
    nxt = None
    if not exhausted:
        delay = (60, 300)[min(attempts - 1, 1)]
        nxt = now_local() + timedelta(seconds=delay)
    status = "FAILED" if exhausted else "PENDING"
    await session.execute(
        update(ExecutorUpdateTask)
        .where(
            ExecutorUpdateTask.id == task.id,
            ExecutorUpdateTask.is_deleted == 0,
            ExecutorUpdateTask.status.in_(_ACTIVE),
        )
        .values(
            status=status,
            attempt_count=ExecutorUpdateTask.attempt_count + 1,
            last_error=reason,
            next_attempt_at=nxt,
            delivered_at=None,
            completed_at=now_local() if status == "FAILED" else None,
        )
    )
    await session.commit()


async def deliver_task(session: AsyncSession, task: ExecutorUpdateTask) -> bool:
    """把升级指令发给在线执行器，并写下发时间。"""
    return await _deliver(session, task)


async def _deliver(session: AsyncSession, task: ExecutorUpdateTask) -> bool:
    if not await executor_online(task.executor_id):
        return False
    frame = {
        "type": "EXECUTOR_UPGRADE",
        "executorId": task.executor_id,
        "requestId": task.request_id,
        "targetVersion": task.target_version,
        "issuedAt": instant_text(datetime.now(UTC)),
    }
    payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    try:
        await deliver_executor_frame(task.executor_id, payload)
    except Exception:
        logger.warning(
            "executor upgrade delivery failed executorId=%s requestId=%s",
            task.executor_id,
            task.request_id,
            exc_info=True,
        )
        return False
    sent_at = now_local()
    deadline = sent_at + timedelta(seconds=_ATTEMPT_TIMEOUT_SECONDS)
    await session.execute(
        update(ExecutorUpdateTask)
        .where(
            ExecutorUpdateTask.id == task.id,
            ExecutorUpdateTask.is_deleted == 0,
            ExecutorUpdateTask.status.in_(_ACTIVE),
        )
        .values(delivered_at=sent_at, next_attempt_at=deadline)
    )
    await session.commit()
    task.delivered_at = sent_at
    task.next_attempt_at = deadline
    return True
