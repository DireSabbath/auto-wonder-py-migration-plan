"""洞察指标、审计、成员交付和人机协作快照。"""

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Mapping
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import SHANGHAI, now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.locks import release_lock, try_acquire_lock
from autowonder.core.redis import redis_client
from autowonder.dashboards.service import java_round, round1
from autowonder.db.session import SessionLocal
from autowonder.insights.participation import (
    Granularity,
    ParsedSnapshot,
    ParticipationEvent,
    ParticipationFact,
    instant_text,
    parse_granularity,
    parse_snapshot,
    reconstruct,
    require_date_range,
    snapshot_document,
    summarize,
)
from autowonder.insights.schemas import (
    CostMetrics,
    DeliveryCounts,
    DeliveryMember,
    DeliveryReport,
    DurationSummary,
    EfficiencyMetrics,
    InsightAuditItem,
    InsightAuditPage,
    InsightMetricsView,
    InsightWorker,
    P90Workitem,
    ParticipationView,
    SecurityMetrics,
    SlowTailPage,
    StabilityMetrics,
    TrendEntry,
)
from autowonder.insights.sql import (
    COUNT_AUDIT_BLOCKS,
    COUNT_AUDIT_LOGS,
    COUNT_COMPLETED_WORKITEMS,
    COUNT_HIGH_RISK_AUDITS,
    COUNT_WORKITEMS,
    LIST_ACTIVE_WORKERS,
    LIST_MEMBERS,
    MEMBER_COUNTS,
    PARTICIPATION_EVENTS,
    RISK_CASE,
    audit_items,
    avg_dispatch_minutes,
    count_audit_items,
    count_dispatches,
    count_usage_workitems,
    daily_trend,
    usage_since,
)

logger = logging.getLogger(__name__)

_EMPTY_TREND = [0, 0, 0, 0, 0, 0, 0]
_SNAPSHOT_PREFIX = "autowonder:insights:human-agent:v1:"
_LOCK_PREFIX = "autowonder:insights:human-agent:refresh-lock:"
_INFLIGHT_PREFIX = "autowonder:insights:human-agent:refresh-inflight:"
_CACHE_TTL_SECONDS = 97200
_LOCK_TTL_MILLIS = 3600000
_CACHE_MISS_WAIT_MS = 300000
_POLL_MS = 2000
_PAGE_SIZE = 5000
_INFLIGHT_TTL_SECONDS = 600
_QUEUE_LIMIT = 22
_TASKS: set[asyncio.Task[None]] = set()
_ZONE = ZoneInfo("Asia/Shanghai")


def days_from_range(time_range: str) -> int:
    """7 天、90 天，其余按 30 天。"""
    if time_range == "7d":
        return 7
    if time_range == "90d":
        return 90
    return 30


def compute_since(time_range: str) -> datetime:
    """从当前上海时间往回推整天，保留钟点。"""
    return now_local() - timedelta(days=days_from_range(time_range))


def divide_credits(total: Decimal, divisor: int) -> Decimal:
    """按 Java ``divide(..., 2, HALF_UP)`` 计算单项均值。"""
    if divisor <= 0:
        return Decimal(0)
    return (total / Decimal(divisor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def delivery_bounds(
    today: date,
    start_text: str | None,
    end_text: str | None,
) -> tuple[date, date, date]:
    """缺省从本周一到今天。非法日期或跨度超过 365 天拒绝。"""
    monday = today - timedelta(days=today.weekday())
    try:
        if start_text is None:
            start = monday
        else:
            start = date.fromisoformat(start_text)
        if end_text is None:
            end = today
        else:
            end = date.fromisoformat(end_text)
    except ValueError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    if start > end or end > today or start + timedelta(days=365) < end:
        raise BizError(ErrorCode.PARAM_INVALID)
    return start, end, monday


async def get_metrics(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int | None,
    time_range: str,
) -> InsightMetricsView:
    """汇总成本、效率、稳定性和安全。"""
    since = compute_since(time_range)
    params = _params(tenant_id, since, agent_id)
    agent_sql = _agent_sql(agent_id)
    total_tokens = _int(
        await _scalar(session, usage_since("COALESCE(SUM(total_tokens), 0)", agent_sql), params)
    )
    total_tasks = _int(await _scalar(session, COUNT_WORKITEMS, params))
    usage_workitems = _int(await _scalar(session, count_usage_workitems(agent_sql), params))
    days = days_from_range(time_range)
    total_credits = _decimal(
        await _scalar(session, usage_since("COALESCE(SUM(credits), 0)", agent_sql), params)
    )
    token_sql = daily_trend("COALESCE(SUM(total_tokens), 0)", "tokens", agent_sql)
    credit_sql = daily_trend("COALESCE(SUM(credits), 0)", "credits", agent_sql)
    token_rows = await _rows(session, token_sql, params)
    credit_rows = await _rows(session, credit_sql, params)
    completed = _int(await _scalar(session, COUNT_COMPLETED_WORKITEMS, params))
    completion_rate = 0.0
    if total_tasks > 0:
        completion_rate = round1(completed / total_tasks * 100.0)
    avg_duration = _int(await _scalar(session, avg_dispatch_minutes(agent_sql), params))
    first_pass_sql = count_dispatches(" AND status = 'SUCCEEDED' AND attempt = 0", agent_sql)
    first_pass = _int(await _scalar(session, first_pass_sql, params))
    total_dispatches = _int(await _scalar(session, count_dispatches("", agent_sql), params))
    success_rate = 100.0
    if total_dispatches > 0:
        success_rate = round1(first_pass / total_dispatches * 100.0)
    high_risk = _int(await _scalar(session, COUNT_HIGH_RISK_AUDITS, params))
    total_audit = _int(await _scalar(session, COUNT_AUDIT_LOGS, params))
    compliance = 100.0
    if total_audit > 0:
        compliance = round1((total_audit - high_risk) / total_audit * 100.0)
    avg_tokens = 0
    if usage_workitems > 0:
        avg_tokens = total_tokens // usage_workitems
    daily_tokens = 0
    if days > 0:
        daily_tokens = total_tokens // days
    return InsightMetricsView(
        cost=CostMetrics(
            total_tokens=total_tokens,
            avg_tokens_per_task=avg_tokens,
            daily_avg=daily_tokens,
            trend=_token_trend(token_rows),
            total_credits=float(total_credits),
            avg_credits_per_task=float(divide_credits(total_credits, usage_workitems)),
            daily_avg_credits=float(divide_credits(total_credits, days)),
            credits_trend=_credit_trend(credit_rows),
        ),
        efficiency=EfficiencyMetrics(
            completion_rate=completion_rate,
            total_tasks=total_tasks,
            completed_tasks=completed,
            avg_duration_minutes=avg_duration,
            trend=[java_round(completion_rate)] * 7,
        ),
        stability=StabilityMetrics(
            success_rate=success_rate,
            retry_count=_int(
                await _scalar(session, count_dispatches(" AND attempt > 0", agent_sql), params)
            ),
            blocked_count=_int(
                await _scalar(
                    session,
                    count_dispatches(" AND status IN ('FAILED', 'TIMEOUT')", agent_sql),
                    params,
                )
            ),
            trend=[java_round(success_rate)] * 7,
        ),
        security=SecurityMetrics(
            high_risk_ops=high_risk,
            compliance_rate=compliance,
            audit_blocks=_int(await _scalar(session, COUNT_AUDIT_BLOCKS, params)),
            trend=[high_risk] * 7,
        ),
    )


async def get_audit(
    session: AsyncSession,
    tenant_id: int,
    risk_level: str | None,
    worker_id: int | None,
    time_range: str,
    page: int,
    page_size: int,
) -> InsightAuditPage:
    """按风险和数字员工分页审计。时间窗跟数据库 NOW()。"""
    risk_sql = ""
    worker_sql = ""
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "days": days_from_range(time_range),
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    if risk_level is not None and risk_level != "":
        risk_sql = " AND " + RISK_CASE + " = :risk_level"
        params["risk_level"] = risk_level
    if worker_id is not None:
        worker_sql = " AND a.id = :worker_id"
        params["worker_id"] = worker_id
    rows = await _rows(session, audit_items(risk_sql, worker_sql), params)
    total = _int(await _scalar(session, count_audit_items(risk_sql, worker_sql), params))
    return InsightAuditPage(
        items=[
            InsightAuditItem(
                timestamp=_text(row.get("timestamp")),
                worker=_text(row.get("worker")),
                event_type=_text(row.get("eventType")),
                detail=_text(row.get("detail")),
                risk_level=_text(row.get("riskLevel")),
            )
            for row in rows
        ],
        total=total,
    )


async def get_workers(session: AsyncSession, tenant_id: int) -> list[InsightWorker]:
    """列出有调度记录的数字员工。"""
    rows = await _rows(session, LIST_ACTIVE_WORKERS, {"tenant_id": tenant_id})
    return [InsightWorker(id=str(row["id"]), name=_text(row.get("name"))) for row in rows]


async def get_delivery(
    session: AsyncSession,
    tenant_id: int,
    start_text: str | None,
    end_text: str | None,
) -> DeliveryReport:
    """统计成员在日期窗内的交付。本周需求在窗口不是本周时单独再查。"""
    today = now_local().date()
    start, end, monday = delivery_bounds(today, start_text, end_text)
    members = await _member_map(session, tenant_id)
    summary = DeliveryCounts()
    for counts in await _delivery_counts(session, tenant_id, start, end):
        summary.total = summary.total + counts.total
        summary.completed = summary.completed + counts.completed
        summary.in_progress = summary.in_progress + counts.in_progress
        summary.requirements = summary.requirements + counts.requirements
        member = members.get(counts.member_id)
        if member is None:
            member = DeliveryMember(
                member_id=counts.member_id,
                member_name=_historical_name(counts.member_id),
            )
            members[counts.member_id] = member
        member.total = member.total + counts.total
        member.completed = member.completed + counts.completed
        member.in_progress = member.in_progress + counts.in_progress
        member.requirements = member.requirements + counts.requirements
    if start == monday and end == today:
        week_requirements = summary.requirements
    else:
        week_rows = await _delivery_counts(session, tenant_id, monday, today)
        week_requirements = sum(row.requirements for row in week_rows)
    return DeliveryReport(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        timezone="Asia/Shanghai",
        summary=summary,
        week_requirements=week_requirements,
        members=list(members.values()),
    )


async def get_participation(
    tenant_id: int,
    start: date,
    end: date,
    granularity: str,
) -> ParticipationView:
    """读取快照并汇总。没有快照时触发刷新，仍没有则返回不可用。"""
    snapshot = await _read_snapshot(tenant_id)
    if snapshot is None:
        await request_refresh(tenant_id)
        await _wait_for_refresh(tenant_id, _CACHE_MISS_WAIT_MS)
        snapshot = await _read_snapshot(tenant_id)
        if snapshot is None:
            return ParticipationView(available=False, refresh_triggered=True, sample_size=0)
    return _participation_view(snapshot, start, end, parse_granularity(granularity))


async def get_slow_tail(
    tenant_id: int,
    start: date,
    end: date,
    page: int,
    page_size: int,
) -> SlowTailPage:
    """最慢的百分之十。没有快照时返回空页。"""
    snapshot = await _read_snapshot(tenant_id)
    if snapshot is None:
        await request_refresh(tenant_id)
        await _wait_for_refresh(tenant_id, _CACHE_MISS_WAIT_MS)
        snapshot = await _read_snapshot(tenant_id)
        if snapshot is None:
            return SlowTailPage(tail_size=0, total=0, page=page, page_size=page_size, items=[])
    data_through = date.fromisoformat(snapshot.data_through)
    require_date_range(start, end, data_through)
    summary = summarize(snapshot.items, start, end, Granularity.DAY, _ZONE)
    total = len(summary.slow_tail)
    start_index = min((page - 1) * page_size, total)
    end_index = min(start_index + page_size, total)
    return SlowTailPage(
        tail_size=total,
        total=total,
        page=page,
        page_size=page_size,
        items=[_p90_item(fact) for fact in summary.slow_tail[start_index:end_index]],
    )


async def force_participation_refresh(tenant_id: int) -> bool:
    """清掉进行中标记后重新排队。拿不到锁时返回 false。"""
    client = redis_client()
    inflight = _INFLIGHT_PREFIX + str(tenant_id)
    await client.delete(inflight)
    token = str(uuid.uuid4())
    lock_key = _LOCK_PREFIX + str(tenant_id)
    locked = await try_acquire_lock(lock_key, token, _LOCK_TTL_MILLIS)
    if not locked:
        logger.info("Participation force refresh lock contention tenantId=%s", tenant_id)
        return False
    await client.set(inflight, "1", ex=_INFLIGHT_TTL_SECONDS)
    accepted = _submit(_refresh_guard(tenant_id, token, lock_key, inflight, forced=True))
    if not accepted:
        logger.warning("Force participation refresh rejected tenantId=%s queueFull", tenant_id)
        await client.delete(inflight)
        await release_lock(lock_key, token)
        return False
    return True


async def request_refresh(tenant_id: int) -> bool:
    """已有刷新或锁被占用时直接返回。队列满时释放锁并返回 false。"""
    client = redis_client()
    inflight = _INFLIGHT_PREFIX + str(tenant_id)
    if await client.exists(inflight):
        return True
    token = str(uuid.uuid4())
    lock_key = _LOCK_PREFIX + str(tenant_id)
    locked = await try_acquire_lock(lock_key, token, _LOCK_TTL_MILLIS)
    if not locked:
        return True
    await client.set(inflight, "1", ex=_INFLIGHT_TTL_SECONDS)
    accepted = _submit(_refresh_guard(tenant_id, token, lock_key, inflight, forced=False))
    if not accepted:
        logger.warning("Participation refresh rejected tenantId=%s queueFull", tenant_id)
        await client.delete(inflight)
        await release_lock(lock_key, token)
        return False
    return True


def _participation_view(
    snapshot: ParsedSnapshot,
    start: date,
    end: date,
    granularity: Granularity,
) -> ParticipationView:
    data_through = date.fromisoformat(snapshot.data_through)
    require_date_range(start, end, data_through)
    summary = summarize(snapshot.items, start, end, granularity, _ZONE)
    p90 = None
    if summary.p90 is not None:
        p90 = _p90_item(summary.p90)
    return ParticipationView(
        available=True,
        generated_at=snapshot.generated_at,
        data_through=snapshot.data_through,
        refresh_triggered=False,
        sample_size=len(summary.eligible_facts),
        average=DurationSummary(
            total_duration_seconds=summary.average_total_seconds,
            human_duration_seconds=summary.average_human_seconds,
            agent_duration_seconds=summary.average_agent_seconds,
        ),
        p90=p90,
        trend=[
            TrendEntry(
                label=bucket.label,
                average_total_seconds=bucket.average_total_seconds,
                average_human_seconds=bucket.average_human_seconds,
                average_agent_seconds=bucket.average_agent_seconds,
            )
            for bucket in summary.trend
        ],
    )


def _p90_item(fact: ParticipationFact) -> P90Workitem:
    return P90Workitem(
        workitem_id=fact.workitem_id,
        title=fact.title,
        completed_at=instant_text(fact.completed_at),
        total_duration_seconds=fact.total_duration_seconds,
        human_duration_seconds=fact.human_duration_seconds,
        agent_duration_seconds=fact.agent_duration_seconds,
    )


async def _refresh_guard(
    tenant_id: int,
    token: str,
    lock_key: str,
    inflight: str,
    forced: bool,
) -> None:
    try:
        async with SessionLocal() as session:
            await _refresh(session, tenant_id, _data_through())
    except Exception:
        if forced:
            logger.warning(
                "Force participation refresh failed tenantId=%s",
                tenant_id,
                exc_info=True,
            )
        else:
            logger.warning(
                "Async participation refresh failed tenantId=%s",
                tenant_id,
                exc_info=True,
            )
    finally:
        client = redis_client()
        await client.delete(inflight)
        await release_lock(lock_key, token)


async def _refresh(session: AsyncSession, tenant_id: int, data_through: date) -> None:
    cutoff = datetime.combine(data_through + timedelta(days=1), time.min)
    rows: list[ParticipationEvent] = []
    offset = 0
    while True:
        page = await _event_page(session, tenant_id, cutoff, offset, _PAGE_SIZE)
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset = offset + _PAGE_SIZE
    facts = reconstruct(rows, cutoff.replace(tzinfo=SHANGHAI))
    document = snapshot_document(facts, data_through.isoformat(), now_local())
    await redis_client().set(
        _SNAPSHOT_PREFIX + str(tenant_id),
        document,
        ex=_CACHE_TTL_SECONDS,
    )
    logger.info(
        "Participation refresh completed tenantId=%s dataThrough=%s events=%s eligible=%s",
        tenant_id,
        data_through.isoformat(),
        len(rows),
        len(facts),
    )


async def _event_page(
    session: AsyncSession,
    tenant_id: int,
    cutoff: datetime,
    offset: int,
    limit: int,
) -> list[ParticipationEvent]:
    rows = await _rows(
        session,
        PARTICIPATION_EVENTS,
        {"tenant_id": tenant_id, "cutoff": cutoff, "offset": offset, "limit": limit},
    )
    return [_event(row) for row in rows]


def _event(row: Mapping[str, Any]) -> ParticipationEvent:
    event_at = row["eventAt"]
    if not isinstance(event_at, datetime):
        event_at = datetime.fromisoformat(str(event_at))
    terminal = row["terminal"]
    title = row["title"]
    inferred = row["inferredToType"]
    title_text = None
    if title is not None:
        title_text = str(title)
    inferred_text = None
    if inferred is not None:
        inferred_text = str(inferred)
    return ParticipationEvent(
        workitem_id=int(row["workitemId"]),
        title=title_text,
        event_type=str(row["eventType"]),
        detail_json=row["detailJson"],
        inferred_to_type=inferred_text,
        event_at=event_at,
        terminal=terminal is True or terminal == 1,
    )


async def _read_snapshot(tenant_id: int) -> ParsedSnapshot | None:
    try:
        raw = await redis_client().get(_SNAPSHOT_PREFIX + str(tenant_id))
    except RedisError:
        logger.warning(
            "Failed to read participation snapshot tenantId=%s",
            tenant_id,
            exc_info=True,
        )
        return None
    if raw is None or str(raw).strip() == "":
        return None
    parsed = parse_snapshot(str(raw))
    if parsed is None:
        logger.warning("Failed to parse participation snapshot tenantId=%s", tenant_id)
    return parsed


async def _wait_for_refresh(tenant_id: int, timeout_ms: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    inflight = _INFLIGHT_PREFIX + str(tenant_id)
    while asyncio.get_running_loop().time() < deadline:
        if await _read_snapshot(tenant_id) is not None:
            return True
        if not await redis_client().exists(inflight):
            return await _read_snapshot(tenant_id) is not None
        await asyncio.sleep(_POLL_MS / 1000)
    return await _read_snapshot(tenant_id) is not None


def _submit(work: Coroutine[Any, Any, None]) -> bool:
    if len(_TASKS) >= _QUEUE_LIMIT:
        work.close()
        return False
    task = asyncio.create_task(work)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return True


def _data_through() -> date:
    return now_local().date() - timedelta(days=1)


async def _member_map(session: AsyncSession, tenant_id: int) -> dict[int | None, DeliveryMember]:
    rows = await _rows(session, LIST_MEMBERS, {"tenant_id": tenant_id})
    members: dict[int | None, DeliveryMember] = {}
    for row in rows:
        member_id = _optional_int(row.get("memberId"))
        members[member_id] = DeliveryMember(
            member_id=member_id,
            member_name=_text(row.get("memberName")),
        )
    return members


async def _delivery_counts(
    session: AsyncSession,
    tenant_id: int,
    start: date,
    end: date,
) -> list[DeliveryCounts]:
    rows = await _rows(
        session,
        MEMBER_COUNTS,
        {
            "tenant_id": tenant_id,
            "start": datetime.combine(start, time.min),
            "end": datetime.combine(end + timedelta(days=1), time.min),
        },
    )
    return [
        DeliveryCounts(
            member_id=_optional_int(row.get("memberId")),
            total=_int(row.get("total")),
            completed=_int(row.get("completed")),
            in_progress=_int(row.get("inProgress")),
            requirements=_int(row.get("requirements")),
        )
        for row in rows
    ]


def _historical_name(member_id: int | None) -> str:
    if member_id is None:
        return "未归属成员"
    return "历史成员 #" + str(member_id)


def _token_trend(rows: list[Mapping[str, Any]]) -> list[int]:
    if len(rows) == 0:
        return list(_EMPTY_TREND)
    return [_int(row.get("tokens")) for row in rows]


def _credit_trend(rows: list[Mapping[str, Any]]) -> list[float]:
    if len(rows) == 0:
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    return [float(_decimal(row.get("credits"))) for row in rows]


def _agent_sql(agent_id: int | None) -> str:
    if agent_id is None:
        return ""
    return " AND agent_id = :agent_id"


def _params(tenant_id: int, since: datetime, agent_id: int | None) -> dict[str, Any]:
    params: dict[str, Any] = {"tenant_id": tenant_id, "since": since}
    if agent_id is not None:
        params["agent_id"] = agent_id
    return params


async def _scalar(session: AsyncSession, sql: str, params: Mapping[str, Any]) -> Any:
    result = await session.execute(text(sql), params)
    return result.scalar_one()


async def _rows(
    session: AsyncSession,
    sql: str,
    params: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    result = await session.execute(text(sql), params)
    return cast(list[Mapping[str, Any]], list(result.mappings().all()))


def _int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _decimal(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
