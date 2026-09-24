"""``/api/insights``。查看要求只读，回填用量和刷新人机协作数据要求读写。"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.aiusage.dispatch_usage import backfill_usage_artifacts
from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import fail, ok
from autowonder.db.session import get_session
from autowonder.insights.service import (
    force_participation_refresh,
    get_audit,
    get_delivery,
    get_metrics,
    get_participation,
    get_slow_tail,
    get_workers,
)
from autowonder.storage.objects import get_object_storage

router = APIRouter(
    prefix="/api/insights",
    tags=["insights"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看洞察"))],
)

member_router = APIRouter(
    prefix="/api/insights/member-delivery",
    tags=["insights"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看项目成员交付统计"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("/metrics")
async def metrics(
    worker_id: Annotated[int | None, Query(alias="worker_id")] = None,
    time_range: Annotated[str, Query(alias="time_range")] = "30d",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """成本、效率、稳定性和安全指标。"""
    return ok(await get_metrics(session, _workspace_id(), worker_id, time_range))


@router.get("/audit")
async def audit(
    page: Annotated[int, Query()] = 1,
    page_size: Annotated[int, Query(alias="page_size")] = 50,
    risk_level: Annotated[str | None, Query(alias="risk_level")] = None,
    worker_id: Annotated[int | None, Query(alias="worker_id")] = None,
    time_range: Annotated[str, Query(alias="time_range")] = "30d",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """审计明细分页。"""
    return ok(
        await get_audit(
            session,
            _workspace_id(),
            risk_level,
            worker_id,
            time_range,
            page,
            page_size,
        )
    )


@router.get("/workers")
async def workers(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """有调度记录的数字员工。"""
    return ok(await get_workers(session, _workspace_id()))


@router.post(
    "/usage/backfill",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "回填AI用量数据"))],
)
async def backfill_usage(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按当前空间回填已登记的用量产物。"""
    counts = await backfill_usage_artifacts(session, get_object_storage(), _workspace_id())
    return ok(
        {
            "scanned": counts.scanned,
            "succeeded": counts.succeeded,
            "skipped": counts.skipped,
            "failed": counts.failed,
        }
    )


@router.get("/human-agent-participation")
async def participation(
    start_date: Annotated[date, Query(alias="start_date")],
    end_date: Annotated[date, Query(alias="end_date")],
    granularity: Annotated[str, Query()] = "DAY",
) -> dict[str, Any]:
    """人机协作时长。日期按快照覆盖日校验。"""
    return ok(await get_participation(_workspace_id(), start_date, end_date, granularity))


@router.get("/human-agent-participation/slowest")
async def slow_tail(
    start_date: Annotated[date, Query(alias="start_date")],
    end_date: Annotated[date, Query(alias="end_date")],
    page: Annotated[int, Query()] = 1,
    page_size: Annotated[int, Query(alias="page_size")] = 20,
) -> dict[str, Any]:
    """最慢尾部。每页最多 100 条。"""
    capped = page_size
    if capped > 100:
        capped = 100
    return ok(await get_slow_tail(_workspace_id(), start_date, end_date, page, capped))


@router.post(
    "/human-agent-participation/refresh",
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "强制刷新人机协作数据"))
    ],
)
async def refresh_participation() -> dict[str, Any]:
    """强制刷新。拿不到锁时返回系统错误信封。"""
    accepted = await force_participation_refresh(_workspace_id())
    if not accepted:
        return fail(ErrorCode.SYSTEM_ERROR)
    return ok(None)


@member_router.get("")
async def member_delivery(
    start_date: Annotated[str | None, Query(alias="start_date")] = None,
    end_date: Annotated[str | None, Query(alias="end_date")] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """项目成员交付统计。"""
    return ok(await get_delivery(session, _workspace_id(), start_date, end_date))
