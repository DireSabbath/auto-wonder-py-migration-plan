"""AI 用量查询、配额和按月计数。计数先写 Redis，再累加到用量表。"""

from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.aiusage.models import AiQuota, AiUsage
from autowonder.aiusage.schemas import AiQuotaView, AiUsageView, UpdateQuotaRequest
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.redis import redis_client

PERIOD_EXPIRE_SEC = 40 * 24 * 3600
_CHECK_AND_INCR_LUA = (
    "local callKey = KEYS[1]\n"
    "local tokenKey = KEYS[2]\n"
    "local maxCalls = tonumber(ARGV[1])\n"
    "local maxTokens = tonumber(ARGV[2])\n"
    "local addTokens = tonumber(ARGV[3])\n"
    "local expireSec = tonumber(ARGV[4])\n"
    "local curCalls = tonumber(redis.call('GET', callKey) or '0')\n"
    "if maxCalls > 0 and curCalls >= maxCalls then return -1 end\n"
    "if maxTokens > 0 then\n"
    "  local curTokens = tonumber(redis.call('GET', tokenKey) or '0')\n"
    "  if curTokens >= maxTokens then return -2 end\n"
    "end\n"
    "local newCalls = redis.call('INCRBY', callKey, 1)\n"
    "if newCalls == 1 then redis.call('EXPIRE', callKey, expireSec) end\n"
    "local newTokens = redis.call('INCRBY', tokenKey, addTokens)\n"
    "if newTokens == addTokens then redis.call('EXPIRE', tokenKey, expireSec) end\n"
    "return newCalls\n"
)


def current_period() -> str:
    """当前上海本地月份，格式 yyyy-MM。"""
    return now_local().strftime("%Y-%m")


def resolved_period(period: str | None) -> str:
    """省略周期时用当前月。空字符串仍按传入值查询。"""
    if period is None:
        return current_period()
    return period


def calls_key(tenant_id: int, period: str) -> str:
    """周期调用次数的 Redis 键。"""
    return "ai:usage:" + str(tenant_id) + ":" + period + ":calls"


def tokens_key(tenant_id: int, period: str) -> str:
    """周期 token 数的 Redis 键。"""
    return "ai:usage:" + str(tenant_id) + ":" + period + ":tokens"


def stored_limit(value: int | None) -> int:
    """写入 Lua 的上限。空值按 0，表示这段计数不设限。"""
    if value is None:
        return 0
    return value


def quota_blocks(
    max_calls: int | None,
    current_calls: int,
    max_tokens: int | None,
    current_tokens: int,
) -> bool:
    """读取路径上的配额判断。上限为 0 时，当前用量大于等于 0 即拒绝。"""
    if max_calls is None and max_tokens is None:
        return False
    if max_calls is not None and current_calls >= max_calls:
        return True
    if max_tokens is not None and current_tokens >= max_tokens:
        return True
    return False


def quota_view(quota: AiQuota | None) -> AiQuotaView:
    """没有配额行时只返回 MONTH。"""
    if quota is None:
        return AiQuotaView(period_type="MONTH")
    return AiQuotaView(
        period_type=quota.period_type,
        max_calls=quota.max_calls,
        max_tokens=quota.max_tokens,
        concurrency_limit=quota.concurrency_limit,
    )


async def list_usage(
    session: AsyncSession,
    tenant_id: int,
    period: str | None,
) -> list[AiUsageView]:
    """按周期列出用量，周期倒序、场景正序。"""
    shown = resolved_period(period)
    rows = await session.scalars(
        select(AiUsage)
        .where(AiUsage.tenant_id == tenant_id, AiUsage.period == shown)
        .order_by(AiUsage.period.desc(), AiUsage.scene)
    )
    return [
        AiUsageView(
            period=row.period,
            scene=row.scene,
            call_count=row.call_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
        )
        for row in rows
    ]


async def get_quota(session: AsyncSession, tenant_id: int) -> AiQuotaView:
    """读取工作空间配额。"""
    return quota_view(await _find_quota(session, tenant_id))


async def update_quota(
    session: AsyncSession,
    request: UpdateQuotaRequest,
    tenant_id: int,
) -> None:
    """没有配额行时插入，已有行时按请求覆盖三个上限。"""
    existing = await _find_quota(session, tenant_id)
    if existing is None:
        session.add(
            AiQuota(
                tenant_id=tenant_id,
                period_type="MONTH",
                max_calls=request.max_calls,
                max_tokens=request.max_tokens,
                concurrency_limit=request.concurrency_limit,
            )
        )
    else:
        await session.execute(
            update(AiQuota)
            .where(AiQuota.tenant_id == tenant_id)
            .values(
                max_calls=request.max_calls,
                max_tokens=request.max_tokens,
                concurrency_limit=request.concurrency_limit,
            )
        )
    await session.commit()


async def check_quota(session: AsyncSession, tenant_id: int) -> None:
    """调用前检查 Redis 计数。两个上限都为空时不限制。"""
    quota = await _find_quota(session, tenant_id)
    if quota is None:
        return
    if quota.max_calls is None and quota.max_tokens is None:
        return
    period = current_period()
    client = redis_client()
    current_calls = await _counter(client, calls_key(tenant_id, period))
    current_tokens = await _counter(client, tokens_key(tenant_id, period))
    if quota_blocks(quota.max_calls, current_calls, quota.max_tokens, current_tokens):
        raise BizError(ErrorCode.AI_QUOTA_EXCEEDED)


async def check_and_record_usage(
    session: AsyncSession,
    tenant_id: int,
    scene: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """原子检查并加一。Lua 返回 -1 或 -2 时拒绝，不再写用量表。"""
    quota = await _find_quota(session, tenant_id)
    max_calls = stored_limit(None if quota is None else quota.max_calls)
    max_tokens = stored_limit(None if quota is None else quota.max_tokens)
    add_tokens = input_tokens + output_tokens
    period = current_period()
    result = await cast(
        Awaitable[int],
        redis_client().eval(
            _CHECK_AND_INCR_LUA,
            2,
            calls_key(tenant_id, period),
            tokens_key(tenant_id, period),
            str(max_calls),
            str(max_tokens),
            str(add_tokens),
            str(PERIOD_EXPIRE_SEC),
        ),
    )
    code = 0
    if result is not None:
        code = int(result)
    if code == -1 or code == -2:
        raise BizError(ErrorCode.AI_QUOTA_EXCEEDED)
    await _upsert_usage(session, tenant_id, period, scene, 1, input_tokens, output_tokens)


async def record_usage(
    session: AsyncSession,
    tenant_id: int,
    scene: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """不检查上限，直接累加 Redis 和用量表。"""
    period = current_period()
    await _ex_incr_by(calls_key(tenant_id, period), 1, PERIOD_EXPIRE_SEC)
    added_tokens = input_tokens + output_tokens
    await _ex_incr_by(tokens_key(tenant_id, period), added_tokens, PERIOD_EXPIRE_SEC)
    await _upsert_usage(session, tenant_id, period, scene, 1, input_tokens, output_tokens)


async def _find_quota(session: AsyncSession, tenant_id: int) -> AiQuota | None:
    return await session.scalar(select(AiQuota).where(AiQuota.tenant_id == tenant_id).limit(1))


async def _upsert_usage(
    session: AsyncSession,
    tenant_id: int,
    period: str,
    scene: str,
    call_count: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    statement = mysql_insert(AiUsage).values(
        tenant_id=tenant_id,
        period=period,
        scene=scene,
        call_count=call_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    statement = statement.on_duplicate_key_update(
        call_count=AiUsage.call_count + call_count,
        input_tokens=AiUsage.input_tokens + input_tokens,
        output_tokens=AiUsage.output_tokens + output_tokens,
    )
    await session.execute(statement)
    await session.commit()


async def _counter(client: Redis, key: str) -> int:
    raw = await client.get(key)
    if raw is None:
        return 0
    return int(raw)


async def _ex_incr_by(key: str, increment: int, expire_seconds: int) -> int:
    client = redis_client()
    value = int(await cast(Awaitable[int], client.incrby(key, increment)))
    if value == increment:
        await client.expire(key, expire_seconds)
    return value
