"""调度列表与详情。时间窗按上海本地钟往回推整天。"""

import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.artifacts.service import list_by_dispatch
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.schemas import DispatchPage, DispatchView
from autowonder.executors.models import Executor
from autowonder.workitems.models import Workitem

_SOURCES = frozenset({"WORKITEM", "SCHEDULED_TASK", "SCHEDULED_TASK_RUN"})


def days_from_range(time_range: str | None) -> int:
    """7 天、90 天，其余按 30 天。"""
    if time_range == "7d":
        return 7
    if time_range == "90d":
        return 90
    return 30


def compute_since(time_range: str | None) -> datetime:
    """从当前上海时间往回推整天，保留钟点。"""
    return now_local() - timedelta(days=days_from_range(time_range))


def page_window(page: int, page_size: int) -> tuple[int, int, int]:
    """page 小于 1 视为 1，pageSize 小于 1 视为 1，并且不超过 100。"""
    safe_page = page
    safe_size = page_size
    if safe_page < 1:
        safe_page = 1
    if safe_size < 1:
        safe_size = 1
    if safe_size > 100:
        safe_size = 100
    return safe_page, safe_size, (safe_page - 1) * safe_size


def execution_source_type(value: str | None) -> str:
    """空来源按工单。其他取值必须是执行来源枚举。"""
    if value is None or _is_blank(value):
        return "WORKITEM"
    if value not in _SOURCES:
        raise ValueError(
            "No enum constant com.aliyun.autowonder.dispatch.ExecutionSourceType." + value
        )
    return value


def workitem_lookup_ids(rows: list[Dispatch]) -> list[int]:
    """只有工单来源才用 workitem_id 去取标题。定时任务的这个字段是运行 id。"""
    ids: list[int] = []
    seen: set[int] = set()
    for row in rows:
        if execution_source_type(row.source_type) != "WORKITEM":
            continue
        if row.workitem_id in seen:
            continue
        seen.add(row.workitem_id)
        ids.append(row.workitem_id)
    return ids


def require_same_tenant(row: Dispatch | None, tenant_id: int) -> Dispatch:
    """缺失或属于其他工作空间时都是调度不存在。"""
    if row is None or row.tenant_id != tenant_id:
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    return row


def build_view(
    row: Dispatch,
    titles: dict[int, str],
    agent_names: dict[int, str],
    version_nos: dict[int, int],
    executor_names: dict[int, str],
) -> DispatchView:
    """把登记行和名称索引收成一条查询结果。产物留给详情接口填写。"""
    source = execution_source_type(row.source_type)
    title = None
    if source == "WORKITEM":
        title = titles.get(row.workitem_id)
    version_no = None
    if row.agent_version_id is not None:
        version_no = version_nos.get(row.agent_version_id)
    executor_name = None
    if row.executor_id is not None:
        executor_name = executor_names.get(row.executor_id)
    return DispatchView(
        id=row.id,
        source_type=source,
        workitem_id=row.workitem_id,
        sdlc_step_id=row.sdlc_step_id,
        agent_id=row.agent_id,
        agent_version_id=row.agent_version_id,
        executor_id=row.executor_id,
        status=row.status,
        attempt=row.attempt,
        result_summary=row.result_summary,
        error=row.error,
        package_oss_ref=row.package_oss_ref,
        gmt_create=row.gmt_create,
        gmt_modified=row.gmt_modified,
        workitem_title=title,
        agent_name=agent_names.get(row.agent_id),
        agent_version_no=version_no,
        executor_name=executor_name,
        artifacts=None,
    )


def filtered_dispatches(
    tenant_id: int,
    status: str | None,
    agent_id: int | None,
    workitem_id: int | None,
    since: datetime,
) -> Select[tuple[Dispatch]]:
    """租户、未删除、时间窗，以及可选的状态、数字员工和工单。"""
    statement = select(Dispatch).where(
        Dispatch.tenant_id == tenant_id,
        Dispatch.is_deleted == 0,
        Dispatch.gmt_create >= since,
    )
    if status is not None:
        if status != "":
            statement = statement.where(Dispatch.status == status)
    if agent_id is not None:
        statement = statement.where(Dispatch.agent_id == agent_id)
    if workitem_id is not None:
        statement = statement.where(
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id == workitem_id,
        )
    return statement


def list_statement(
    tenant_id: int,
    status: str | None,
    agent_id: int | None,
    workitem_id: int | None,
    since: datetime,
    page: int,
    page_size: int,
) -> Select[tuple[Dispatch]]:
    """按创建时间倒序分页。"""
    _page, size, offset = page_window(page, page_size)
    return (
        filtered_dispatches(tenant_id, status, agent_id, workitem_id, since)
        .order_by(Dispatch.gmt_create.desc())
        .limit(size)
        .offset(offset)
    )


async def list_dispatches(
    session: AsyncSession,
    tenant_id: int,
    status: str | None,
    agent_id: int | None,
    workitem_id: int | None,
    time_range: str | None,
    page: int,
    page_size: int,
) -> DispatchPage:
    """查询当前工作空间的调度，并补上工单、数字员工和执行器名称。"""
    since = compute_since(time_range)
    safe_page, safe_size, _offset = page_window(page, page_size)
    rows = list(
        await session.scalars(
            list_statement(
                tenant_id,
                status,
                agent_id,
                workitem_id,
                since,
                page,
                page_size,
            )
        )
    )
    filtered = filtered_dispatches(tenant_id, status, agent_id, workitem_id, since)
    counted = await session.scalar(select(func.count()).select_from(filtered.subquery()))
    if counted is None:
        total = 0
    else:
        total = int(counted)
    views = await _enrich(session, tenant_id, rows)
    return DispatchPage(list=views, total=total, page=safe_page, page_size=safe_size)


async def get_dispatch(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> DispatchView:
    """读取一条调度。其他工作空间和不存在都返回调度不存在，并附上可见产物。"""
    row = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    found = require_same_tenant(row, tenant_id)
    view = (await _enrich(session, tenant_id, [found]))[0]
    view.artifacts = await list_by_dispatch(session, tenant_id, dispatch_id)
    return view


async def _enrich(
    session: AsyncSession,
    tenant_id: int,
    rows: list[Dispatch],
) -> list[DispatchView]:
    titles = await _workitem_titles(session, tenant_id, workitem_lookup_ids(rows))
    agent_names = await _agent_names(session, tenant_id, _ids(rows, "agent_id"))
    version_nos = await _version_nos(session, tenant_id, _optional_ids(rows, "agent_version_id"))
    executor_names = await _executor_names(session, tenant_id, _optional_ids(rows, "executor_id"))
    return [build_view(row, titles, agent_names, version_nos, executor_names) for row in rows]


def _ids(rows: list[Dispatch], field_name: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for row in rows:
        value = getattr(row, field_name)
        if value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return ids


def _optional_ids(rows: list[Dispatch], field_name: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for row in rows:
        value = getattr(row, field_name)
        if value is None:
            continue
        if value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return ids


async def _workitem_titles(
    session: AsyncSession,
    tenant_id: int,
    ids: list[int],
) -> dict[int, str]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(Workitem).where(
            Workitem.tenant_id == tenant_id,
            Workitem.is_deleted == 0,
            Workitem.id.in_(ids),
        )
    )
    titles: dict[int, str] = {}
    for row in rows:
        if row.id in titles:
            continue
        titles[row.id] = row.title
    return titles


async def _agent_names(
    session: AsyncSession,
    tenant_id: int,
    ids: list[int],
) -> dict[int, str]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(Agent).where(
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
            Agent.id.in_(ids),
        )
    )
    names: dict[int, str] = {}
    for row in rows:
        if row.id in names:
            continue
        names[row.id] = row.name
    return names


async def _version_nos(
    session: AsyncSession,
    tenant_id: int,
    ids: list[int],
) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(AgentVersion).where(
            AgentVersion.tenant_id == tenant_id,
            AgentVersion.is_deleted == 0,
            AgentVersion.id.in_(ids),
        )
    )
    numbers: dict[int, int] = {}
    for row in rows:
        if row.id in numbers:
            continue
        numbers[row.id] = row.version_no
    return numbers


async def _executor_names(
    session: AsyncSession,
    tenant_id: int,
    ids: list[int],
) -> dict[int, str]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(Executor).where(
            Executor.tenant_id == tenant_id,
            Executor.is_deleted == 0,
            Executor.id.in_(ids),
        )
    )
    names: dict[int, str] = {}
    for row in rows:
        if row.id in names:
            continue
        names[row.id] = row.name
    return names


def _is_blank(text: str) -> bool:
    for char in text:
        if not _is_java_whitespace(ord(char)):
            return False
    return True


def _is_java_whitespace(code_point: int) -> bool:
    if code_point in {0x00A0, 0x2007, 0x202F}:
        return False
    if code_point in {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F}:
        return True
    category = unicodedata.category(chr(code_point))
    if category == "Zs" or category == "Zl" or category == "Zp":
        return True
    return False
