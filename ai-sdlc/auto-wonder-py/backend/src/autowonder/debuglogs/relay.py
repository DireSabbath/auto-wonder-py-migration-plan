"""调试日志中转。对象键与直传签发使用同一套命名。"""

import logging
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import AgentVersion
from autowonder.debuglogs.models import DebugLog
from autowonder.debuglogs.naming import (
    sanitize_role_code,
    scheduled_object_key,
    workitem_object_key,
)
from autowonder.debuglogs.sanitizer import java_is_blank, sanitize_sha256
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.query import execution_source_type
from autowonder.scheduledtasks.models import ScheduledTaskRun

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})


async def lookup_relay_target(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> tuple[str, int] | None:
    """调度不属于该租户，或打包时没有打开调试日志时，不接受中转。"""
    dispatch = await _dispatch(session, dispatch_id)
    if dispatch is None or dispatch.tenant_id != tenant_id or dispatch.debug_log_enabled != 1:
        return None
    run_no = await run_number(session, dispatch)
    return await canonical_object_key(session, dispatch, run_no), run_no


async def record_relay_upload(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    object_key: str,
    run_no: int,
    size_bytes: int,
    files_metadata: dict[str, Any] | None,
) -> None:
    """把中转成功写成 UPLOADED/RELAY。已有行只在尚未 UPLOADED 时更新。"""
    dispatch = await _dispatch(session, dispatch_id)
    if dispatch is None or dispatch.tenant_id != tenant_id or dispatch.debug_log_enabled != 1:
        return
    sha256 = None
    if files_metadata is not None:
        raw_sha = files_metadata.get("sha256")
        if isinstance(raw_sha, str):
            sha256 = sanitize_sha256(raw_sha)
        else:
            sha256 = sanitize_sha256(None)
    if dispatch.status in _TERMINAL:
        dispatch_status: str | None = dispatch.status
    else:
        dispatch_status = None
    existing = await session.scalar(
        select(DebugLog).where(DebugLog.dispatch_id == dispatch_id).limit(1)
    )
    if existing is not None:
        await _apply_result(
            session,
            dispatch_id,
            existing.id,
            size_bytes,
            sha256,
            dispatch_status,
        )
        return
    if dispatch_status is not None:
        stored_status = dispatch_status
    else:
        stored_status = dispatch.status
    row = DebugLog(
        tenant_id=dispatch.tenant_id,
        source_type=dispatch.source_type,
        source_id=dispatch.workitem_id,
        dispatch_id=dispatch.id,
        agent_id=dispatch.agent_id,
        agent_version_id=dispatch.agent_version_id,
        run_no=run_no,
        dispatch_status=stored_status,
        object_key=object_key,
        size_bytes=size_bytes,
        sha256=sha256,
        truncated=0,
        upload_channel="RELAY",
        status="UPLOADED",
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as error:
        if not _duplicate_key(error):
            raise
        winner = await session.scalar(
            select(DebugLog).where(DebugLog.dispatch_id == dispatch_id).limit(1)
        )
        if winner is None:
            logger.warning(
                "debug log relay insert race winner unreadable dispatchId=%s "
                "reason=DEBUG_LOG_INSERT_RACE_UNREADABLE",
                dispatch_id,
            )
            return
        logger.warning(
            "debug log insert race fell back to update dispatchId=%s winnerId=%s "
            "reason=DEBUG_LOG_INSERT_RACE",
            dispatch_id,
            winner.id,
        )
        await _apply_result(session, dispatch_id, winner.id, size_bytes, sha256, None)


async def run_number(session: AsyncSession, dispatch: Dispatch) -> int:
    """同一来源、同一数字员工按创建时间数轮次，从 1 开始。"""
    source = execution_source_type(dispatch.source_type)
    rows = list(
        await session.scalars(
            select(Dispatch).where(
                Dispatch.tenant_id == dispatch.tenant_id,
                Dispatch.source_type == source,
                Dispatch.workitem_id == dispatch.workitem_id,
                Dispatch.is_deleted == 0,
            )
        )
    )
    same_agent = [row for row in rows if row.agent_id == dispatch.agent_id]
    same_agent.sort(key=_sibling_key)
    for index, row in enumerate(same_agent):
        if row.id == dispatch.id:
            return index + 1
    fallback = len(same_agent) + 1
    logger.warning(
        "debug log run_no fallback dispatchId=%s agentId=%s runNo=%s siblings=%s "
        "reason=RUN_NO_SELF_NOT_IN_SIBLINGS",
        dispatch.id,
        dispatch.agent_id,
        fallback,
        len(same_agent),
    )
    return fallback


async def canonical_object_key(session: AsyncSession, dispatch: Dispatch, run_no: int) -> str:
    """工单和定时任务使用不同的目录。任务 id 缺失时退化为 0。"""
    role = sanitize_role_code(await _role_code(session, dispatch), dispatch.agent_id)
    if execution_source_type(dispatch.source_type) == "SCHEDULED_TASK_RUN":
        run = await session.scalar(
            select(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.workspace_id == dispatch.tenant_id,
                ScheduledTaskRun.id == dispatch.workitem_id,
            )
            .limit(1)
        )
        task_id = 0
        if run is not None and run.scheduled_task_id is not None:
            task_id = run.scheduled_task_id
        else:
            logger.warning(
                "debug log object key degraded dispatchId=%s runId=%s "
                "reason=SCHEDULED_TASK_ID_MISSING",
                dispatch.id,
                dispatch.workitem_id,
            )
        return scheduled_object_key(task_id, dispatch.workitem_id, role, run_no)
    return workitem_object_key(dispatch.workitem_id, role, run_no)


async def _role_code(session: AsyncSession, dispatch: Dispatch) -> str | None:
    if dispatch.agent_version_id is None:
        logger.warning(
            "debug log role code fallback dispatchId=%s agentId=%s "
            "reason=AGENT_VERSION_ID_MISSING",
            dispatch.id,
            dispatch.agent_id,
        )
        return None
    version = await session.scalar(
        select(AgentVersion)
        .where(AgentVersion.id == dispatch.agent_version_id, AgentVersion.is_deleted == 0)
        .limit(1)
    )
    if version is None:
        logger.warning(
            "debug log role code fallback dispatchId=%s agentVersionId=%s "
            "reason=AGENT_VERSION_NOT_FOUND",
            dispatch.id,
            dispatch.agent_version_id,
        )
        return None
    if version.tenant_id != dispatch.tenant_id:
        logger.warning(
            "debug log role code fallback dispatchId=%s agentVersionId=%s "
            "versionTenantId=%s reason=ROLE_CODE_TENANT_MISMATCH",
            dispatch.id,
            dispatch.agent_version_id,
            version.tenant_id,
        )
        return None
    if version.role_code is None or java_is_blank(version.role_code):
        logger.warning(
            "debug log role code fallback dispatchId=%s agentVersionId=%s reason=ROLE_CODE_BLANK",
            dispatch.id,
            dispatch.agent_version_id,
        )
        return None
    return version.role_code


async def _apply_result(
    session: AsyncSession,
    dispatch_id: int,
    debug_log_id: int,
    size_bytes: int,
    sha256: str | None,
    dispatch_status: str | None,
) -> None:
    values: dict[str, object] = {
        "status": "UPLOADED",
        "upload_channel": "RELAY",
        "size_bytes": size_bytes,
        "error_message": None,
    }
    if sha256 is not None:
        values["sha256"] = sha256
    if dispatch_status is not None:
        values["dispatch_status"] = dispatch_status
    result = await session.execute(
        update(DebugLog)
        .where(DebugLog.id == debug_log_id, DebugLog.status != "UPLOADED")
        .values(**values)
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        logger.info(
            "debug log report converged on uploaded row dispatchId=%s debugLogId=%s "
            "status=UPLOADED reason=DEBUG_LOG_RESULT_UPDATE_NO_ROW",
            dispatch_id,
            debug_log_id,
        )


async def _dispatch(session: AsyncSession, dispatch_id: int) -> Dispatch | None:
    return await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )


def _sibling_key(row: Dispatch) -> tuple[int, datetime, int]:
    if row.gmt_create is None:
        return (0, datetime.min, row.id)
    return (1, row.gmt_create, row.id)


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None or not origin.args:
        return False
    return origin.args[0] == 1062
