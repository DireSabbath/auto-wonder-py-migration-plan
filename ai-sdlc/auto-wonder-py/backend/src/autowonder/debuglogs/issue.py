"""调试日志直传签发、结果收尾和超期对账。"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.clock import SHANGHAI
from autowonder.debuglogs.models import DebugLog
from autowonder.debuglogs.relay import canonical_object_key, run_number
from autowonder.debuglogs.sanitizer import (
    accepted_upload_channel,
    java_is_blank,
    sanitize_sha256,
    truncate_error_message,
)
from autowonder.debuglogs.service import FALLBACK_ARTIFACT_BUCKET, artifact_bucket
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.trace import instant_text
from autowonder.storage.objects import get_object_storage

logger = logging.getLogger(__name__)

UPLOAD_URL_TTL_SECONDS = 20 * 60
PENDING_RECONCILE_AGE_MS = 24 * 60 * 60 * 1000
RECONCILE_BATCH = 200
PENDING_TIMEOUT_ERROR = "PENDING_TIMEOUT: object missing after 24h"
PENDING = "PENDING"
UPLOADED = "UPLOADED"
FAILED = "FAILED"
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
_REPORT_STATUSES = frozenset({UPLOADED, FAILED})


@dataclass
class IssueResult:
    """直传签发回执。已经上传过时地址和到期时间为空。"""

    object_key: str
    upload_url: str | None
    expires_at: datetime | None
    already_uploaded: bool


def expires_text(moment: datetime | None) -> str | None:
    """把签发到期时间写成 UTC 的 Instant 文本。"""
    if moment is None:
        return None
    return instant_text(moment)


async def issue_upload(
    session: AsyncSession,
    dispatch: Dispatch,
    size_bytes: int | None,
    sha256: str | None,
    truncated: bool,
    dispatch_status: str,
) -> IssueResult:
    """已上传的行直接返回原键。其余行刷新为 PENDING 后再签 20 分钟上传地址。

    登记先提交，再向对象存储要地址。地址签发失败时，PENDING 行仍然留下。
    """
    existing = await _log_by_dispatch(session, dispatch.id)
    if existing is not None and existing.status == UPLOADED:
        logger.info(
            "debug log re-issue after upload dispatchId=%s objectKey=%s",
            dispatch.id,
            existing.object_key,
        )
        return IssueResult(existing.object_key, None, None, True)
    run_no = await run_number(session, dispatch)
    object_key = await canonical_object_key(session, dispatch, run_no)
    await _upsert_pending(
        session,
        dispatch,
        existing,
        run_no,
        object_key,
        size_bytes,
        sha256,
        truncated,
        dispatch_status,
    )
    await session.commit()
    expires_at = datetime.now(UTC) + timedelta(seconds=UPLOAD_URL_TTL_SECONDS)
    bucket = artifact_bucket(get_settings().oss_artifact_bucket)
    upload_url = get_object_storage().presign_put(bucket, object_key, UPLOAD_URL_TTL_SECONDS)
    return IssueResult(object_key, upload_url, expires_at, False)


async def record_task_result_report(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    dispatch_id: int,
    debug_log: dict[str, Any],
) -> None:
    """收敛 TASK_RESULT 里的 debugLog。非法输入忽略，不抛给调用方。"""
    dispatch = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    if dispatch is None or dispatch.tenant_id != tenant_id or dispatch.executor_id != executor_id:
        return
    if dispatch.debug_log_enabled != 1:
        return
    status = debug_log.get("status")
    if not isinstance(status, str) or status not in _REPORT_STATUSES:
        logger.warning(
            "debug log report ignored dispatchId=%s status=%s "
            "reason=DEBUG_LOG_REPORT_BAD_STATUS",
            dispatch_id,
            status,
        )
        return
    existing = await _log_by_dispatch(session, dispatch_id)
    dispatch_status = None
    if dispatch.status in _TERMINAL:
        dispatch_status = dispatch.status
    if existing is None and dispatch_status is None:
        logger.warning(
            "debug log report skipped dispatchId=%s dispatchStatus=%s "
            "reason=DEBUG_LOG_REPORT_DISPATCH_NOT_TERMINAL",
            dispatch_id,
            dispatch.status,
        )
        return
    channel = accepted_upload_channel(dispatch_id, _text(debug_log.get("channel")))
    size_bytes = _long(debug_log.get("sizeBytes"))
    sha256 = sanitize_sha256(_text(debug_log.get("sha256")))
    truncated = _flag(debug_log.get("truncated"))
    error = truncate_error_message(_text(debug_log.get("error")))
    if existing is not None:
        await _apply_result_update(
            session,
            dispatch_id,
            existing.id,
            status,
            channel,
            size_bytes,
            sha256,
            truncated,
            dispatch_status,
            error,
        )
        await session.commit()
        return
    run_no = await run_number(session, dispatch)
    object_key = await canonical_object_key(session, dispatch, run_no)
    row = _new_row(dispatch, run_no, object_key)
    row.status = status
    row.upload_channel = channel
    row.size_bytes = size_bytes
    row.sha256 = sha256
    if truncated is True:
        row.truncated = 1
    stored_status = dispatch.status
    if dispatch_status is not None:
        stored_status = dispatch_status
    row.dispatch_status = stored_status
    row.error_message = error
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as error_info:
        if not _duplicate_key(error_info):
            raise
        winner = await _log_by_dispatch(session, dispatch_id)
        if winner is None:
            logger.warning(
                "debug log insert race winner unreadable dispatchId=%s "
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
        await _apply_result_update(
            session,
            dispatch_id,
            winner.id,
            status,
            channel,
            size_bytes,
            sha256,
            truncated,
            dispatch_status,
            error,
        )
    await session.commit()


async def reconcile_pending_once(session: AsyncSession) -> int:
    """把超过 24 小时仍为 PENDING 的行收成 UPLOADED 或 FAILED。

    产物桶没配置时整轮放弃。单行失败留下警告，继续扫后面的行。
    """
    configured = get_settings().oss_artifact_bucket
    if java_is_blank(configured):
        logger.error(
            "debug log reconciliation skipped: oss.artifact-bucket is not configured; "
            "refusing to sweep against fallback bucket %s because exists() would "
            "report every object missing and mass-mark stale rows FAILED "
            "reason=DEBUG_LOG_RECONCILE_BUCKET_UNCONFIGURED",
            FALLBACK_ARTIFACT_BUCKET,
        )
        return 0
    cutoff = _cutoff(int(time.time() * 1000) - PENDING_RECONCILE_AGE_MS)
    stale = list(
        await session.scalars(
            select(DebugLog)
            .where(DebugLog.status == PENDING, DebugLog.gmt_modified < cutoff)
            .order_by(DebugLog.gmt_modified.asc())
            .limit(RECONCILE_BATCH)
        )
    )
    storage = get_object_storage()
    marked_uploaded = 0
    marked_failed = 0
    skipped = 0
    for row in stale:
        try:
            if storage.exists(configured + "/" + row.object_key):
                marked_uploaded += await _mark(session, row.id, UPLOADED, None)
            else:
                changed = await _mark(session, row.id, FAILED, PENDING_TIMEOUT_ERROR)
                marked_failed += changed
                if changed > 0:
                    logger.info(
                        "debug log reconciled to FAILED debugLogId=%s dispatchId=%s "
                        "objectKey=%s reason=DEBUG_LOG_RECONCILE_MARK_FAILED",
                        row.id,
                        row.dispatch_id,
                        row.object_key,
                    )
            await session.commit()
        except Exception:
            await session.rollback()
            skipped += 1
            logger.warning(
                "debug log reconcile skipped dispatchId=%s debugLogId=%s objectKey=%s "
                "reason=DEBUG_LOG_RECONCILE_ROW_FAILED",
                row.dispatch_id,
                row.id,
                row.object_key,
                exc_info=True,
            )
    logger.info(
        "debug log reconciliation swept scanned=%s uploaded=%s failed=%s skipped=%s "
        "reason=DEBUG_LOG_RECONCILE_SWEEP",
        len(stale),
        marked_uploaded,
        marked_failed,
        skipped,
    )
    return marked_uploaded + marked_failed


async def _upsert_pending(
    session: AsyncSession,
    dispatch: Dispatch,
    existing: DebugLog | None,
    run_no: int,
    object_key: str,
    size_bytes: int | None,
    sha256: str | None,
    truncated: bool,
    dispatch_status: str,
) -> None:
    if existing is not None:
        await _refresh_on_issue(
            session,
            dispatch,
            existing.id,
            run_no,
            object_key,
            size_bytes,
            sha256,
            truncated,
            dispatch_status,
        )
        return
    row = _new_row(dispatch, run_no, object_key)
    row.size_bytes = size_bytes
    row.sha256 = sha256
    if truncated:
        row.truncated = 1
    row.dispatch_status = dispatch_status
    row.status = PENDING
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as error:
        if not _duplicate_key(error):
            raise
        winner = await _log_by_dispatch(session, dispatch.id)
        if winner is None:
            raise
        logger.warning(
            "debug log insert race fell back to update dispatchId=%s winnerId=%s "
            "reason=DEBUG_LOG_INSERT_RACE",
            dispatch.id,
            winner.id,
        )
        await _refresh_on_issue(
            session,
            dispatch,
            winner.id,
            run_no,
            object_key,
            size_bytes,
            sha256,
            truncated,
            dispatch_status,
        )


async def _refresh_on_issue(
    session: AsyncSession,
    dispatch: Dispatch,
    debug_log_id: int,
    run_no: int,
    object_key: str,
    size_bytes: int | None,
    sha256: str | None,
    truncated: bool,
    dispatch_status: str,
) -> None:
    stored_truncated = 0
    if truncated:
        stored_truncated = 1
    result = await session.execute(
        update(DebugLog)
        .where(DebugLog.id == debug_log_id, DebugLog.status != UPLOADED)
        .values(
            run_no=run_no,
            object_key=object_key,
            size_bytes=size_bytes,
            sha256=sha256,
            truncated=stored_truncated,
            dispatch_status=dispatch_status,
            status=PENDING,
            error_message=None,
        )
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        logger.warning(
            "debug log issue refresh matched no row dispatchId=%s debugLogId=%s "
            "objectKey=%s reason=DEBUG_LOG_ISSUE_UPDATE_NO_ROW",
            dispatch.id,
            debug_log_id,
            object_key,
        )


async def _apply_result_update(
    session: AsyncSession,
    dispatch_id: int,
    debug_log_id: int,
    status: object,
    channel: str | None,
    size_bytes: int | None,
    sha256: str | None,
    truncated: bool | None,
    dispatch_status: str | None,
    error: str | None,
) -> None:
    values: dict[str, object] = {"status": status, "error_message": error}
    if channel is not None:
        values["upload_channel"] = channel
    if size_bytes is not None:
        values["size_bytes"] = size_bytes
    if sha256 is not None:
        values["sha256"] = sha256
    if truncated is not None:
        stored_truncated = 0
        if truncated:
            stored_truncated = 1
        values["truncated"] = stored_truncated
    if dispatch_status is not None:
        values["dispatch_status"] = dispatch_status
    result = await session.execute(
        update(DebugLog)
        .where(DebugLog.id == debug_log_id, DebugLog.status != UPLOADED)
        .values(**values)
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        logger.info(
            "debug log report converged on uploaded row dispatchId=%s debugLogId=%s "
            "status=%s reason=DEBUG_LOG_RESULT_UPDATE_NO_ROW",
            dispatch_id,
            debug_log_id,
            status,
        )


async def _mark(
    session: AsyncSession,
    debug_log_id: int,
    status: str,
    error_message: str | None,
) -> int:
    result = await session.execute(
        update(DebugLog)
        .where(DebugLog.id == debug_log_id, DebugLog.status == PENDING)
        .values(status=status, error_message=error_message)
    )
    return int(cast(CursorResult[Any], result).rowcount)


def _new_row(dispatch: Dispatch, run_no: int, object_key: str) -> DebugLog:
    return DebugLog(
        tenant_id=dispatch.tenant_id,
        source_type=dispatch.source_type,
        source_id=dispatch.workitem_id,
        dispatch_id=dispatch.id,
        agent_id=dispatch.agent_id,
        agent_version_id=dispatch.agent_version_id,
        run_no=run_no,
        dispatch_status=dispatch.status,
        object_key=object_key,
        truncated=0,
        status=PENDING,
    )


async def _log_by_dispatch(session: AsyncSession, dispatch_id: int) -> DebugLog | None:
    return await session.scalar(
        select(DebugLog).where(DebugLog.dispatch_id == dispatch_id).limit(1)
    )


def _cutoff(epoch_millis: int) -> datetime:
    aware = datetime.fromtimestamp(epoch_millis / 1000, SHANGHAI)
    return aware.replace(tzinfo=None)


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _long(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None:
        return False
    if not origin.args:
        return False
    return origin.args[0] == 1062
