"""debug 日志只读查询，以及已上传对象的临时下载地址。"""

from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.clock import SHANGHAI
from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.models import DebugLog
from autowonder.debuglogs.schemas import DebugLogView
from autowonder.storage.objects import get_object_storage

FALLBACK_ARTIFACT_BUCKET = "autowonder-artifact-daily"
DOWNLOAD_URL_TTL_SECONDS = 600
UPLOADED = "UPLOADED"
_QUERY_SOURCES = frozenset({"WORKITEM", "SCHEDULED_TASK_RUN"})


def artifact_bucket(configured: str | None) -> str:
    """未配置产物桶时用日常桶名。空字符串仍是已配置的值。"""
    if configured is None:
        return FALLBACK_ARTIFACT_BUCKET
    return configured


def query_window(page: int, size: int) -> tuple[int, int]:
    """page 小于 1 视为 1，size 小于 1 视为 50，并且不超过 200。"""
    resolved_page = page
    resolved_size = size
    if resolved_page < 1:
        resolved_page = 1
    if resolved_size < 1:
        resolved_size = 50
    if resolved_size > 200:
        resolved_size = 200
    return resolved_size, (resolved_page - 1) * resolved_size


def require_query_source_type(source_type: str) -> str:
    """任务定义不是可执行主体，未知取值同样拒绝。"""
    if source_type not in _QUERY_SOURCES:
        raise BizError(ErrorCode.PARAM_INVALID)
    return source_type


def since_local(epoch_millis: int) -> datetime:
    """把查询起点从毫秒时间戳换成上海本地的 naive 时间。"""
    aware = datetime.fromtimestamp(epoch_millis / 1000, SHANGHAI)
    return aware.replace(tzinfo=None)


def logs_select(
    tenant_id: int,
    source_type: str,
    source_id: int,
    agent_id: int | None,
    since_epoch_millis: int | None,
    page: int,
    size: int,
) -> Select[tuple[DebugLog]]:
    """按来源、数字员工和创建时间过滤，按轮次和 id 升序。"""
    limit, offset = query_window(page, size)
    statement = select(DebugLog).where(
        DebugLog.tenant_id == tenant_id,
        DebugLog.source_type == source_type,
        DebugLog.source_id == source_id,
    )
    if agent_id is not None:
        statement = statement.where(DebugLog.agent_id == agent_id)
    if since_epoch_millis is not None:
        statement = statement.where(DebugLog.gmt_create >= since_local(since_epoch_millis))
    return statement.order_by(DebugLog.run_no.asc(), DebugLog.id.asc()).limit(limit).offset(offset)


def download_url(row: DebugLog) -> str | None:
    """只有 UPLOADED 行签发 600 秒的下载地址。"""
    if row.status != UPLOADED:
        return None
    bucket = artifact_bucket(get_settings().oss_artifact_bucket)
    oss_ref = bucket + "/" + row.object_key
    return get_object_storage().presign_get(oss_ref, DOWNLOAD_URL_TTL_SECONDS)


def to_view(row: DebugLog, url: str | None) -> DebugLogView:
    """登记行转成查询结果。截断标记按 0/1 写成布尔值。"""
    return DebugLogView(
        id=row.id,
        source_type=row.source_type,
        source_id=row.source_id,
        dispatch_id=row.dispatch_id,
        agent_id=row.agent_id,
        run_no=row.run_no,
        dispatch_status=row.dispatch_status,
        object_key=row.object_key,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        truncated=row.truncated != 0,
        upload_channel=row.upload_channel,
        status=row.status,
        error_message=row.error_message,
        gmt_create=row.gmt_create,
        download_url=url,
    )


async def list_debug_logs(
    session: AsyncSession,
    tenant_id: int,
    source_type: str,
    source_id: int,
    agent_id: int | None,
    since_epoch_millis: int | None,
    page: int,
    size: int,
) -> list[DebugLogView]:
    """查询当前工作空间的 debug 日志，并为已上传行附上下载地址。"""
    source = require_query_source_type(source_type)
    rows = await session.scalars(
        logs_select(
            tenant_id,
            source,
            source_id,
            agent_id,
            since_epoch_millis,
            page,
            size,
        )
    )
    return [to_view(row, download_url(row)) for row in rows]
