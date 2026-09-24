"""从 observability/usage.json 写入派发级用量。解析失败只记日志。"""

import json
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.aiusage.models import DispatchAiUsage
from autowonder.core.clock import now_local
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.models import Dispatch

logger = logging.getLogger(__name__)

USAGE_ARTIFACT = "observability/usage.json"


def is_usage_artifact(name: str | None) -> bool:
    """逻辑名本身或任意父目录下的 usage.json。"""
    if name is None:
        return False
    normalized = name.replace("\\", "/")
    if normalized == USAGE_ARTIFACT:
        return True
    return normalized.endswith("/" + USAGE_ARTIFACT)


def usage_upsert_statement(
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    agent_id: int | None,
    executor_id: int | None,
    artifact_id: int | None,
    step_id: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    reasoning_tokens: int,
    credits: Decimal | None,
    total_tokens: int,
    raw_json: dict[str, Any],
) -> Any:
    """与 Java upsert 相同：执行器空值不覆盖已有执行器，产物 id 同理。"""
    statement = mysql_insert(DispatchAiUsage).values(
        tenant_id=tenant_id,
        workitem_id=workitem_id,
        dispatch_id=dispatch_id,
        agent_id=agent_id,
        executor_id=executor_id,
        artifact_id=artifact_id,
        step_id=step_id,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        credits=credits,
        total_tokens=total_tokens,
        raw_json=raw_json,
        usage_at=now_local(),
    )
    return statement.on_duplicate_key_update(
        id=text("LAST_INSERT_ID(id)"),
        workitem_id=statement.inserted.workitem_id,
        agent_id=statement.inserted.agent_id,
        executor_id=func.coalesce(statement.inserted.executor_id, DispatchAiUsage.executor_id),
        artifact_id=func.coalesce(statement.inserted.artifact_id, DispatchAiUsage.artifact_id),
        input_tokens=statement.inserted.input_tokens,
        output_tokens=statement.inserted.output_tokens,
        cache_read_tokens=statement.inserted.cache_read_tokens,
        cache_write_tokens=statement.inserted.cache_write_tokens,
        reasoning_tokens=statement.inserted.reasoning_tokens,
        credits=statement.inserted.credits,
        total_tokens=statement.inserted.total_tokens,
        raw_json=statement.inserted.raw_json,
        usage_at=statement.inserted.usage_at,
        gmt_modified=text("NOW(3)"),
    )


async def ingest_usage_artifact(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    dispatch_id: int,
    artifact_id: int,
    artifact_name: str,
    oss_ref: str,
    content: bytes,
) -> None:
    """不是用量文件时直接返回。条目或调度对不上时跳过，不抛给上报接口。"""
    if not is_usage_artifact(artifact_name):
        return
    try:
        entries = _entries(content)
        if len(entries) == 0:
            logger.warning(
                "usage artifact ingest skipped artifactId=%s ossRef=%s workitemId=%s "
                "dispatchId=%s reason=no_entries",
                artifact_id,
                oss_ref,
                workitem_id,
                dispatch_id,
            )
            return
        dispatch = await session.get(Dispatch, dispatch_id)
        if dispatch is None or dispatch.tenant_id != tenant_id or dispatch.is_deleted != 0:
            logger.warning(
                "usage artifact ingest skipped artifactId=%s ossRef=%s workitemId=%s "
                "dispatchId=%s reason=dispatch_not_found",
                artifact_id,
                oss_ref,
                workitem_id,
                dispatch_id,
            )
            return
        for entry in entries:
            await _persist(session, dispatch, artifact_id, entry)
    except Exception:
        logger.warning(
            "usage artifact ingest failed artifactId=%s ossRef=%s workitemId=%s dispatchId=%s",
            artifact_id,
            oss_ref,
            workitem_id,
            dispatch_id,
            exc_info=True,
        )


async def _persist(
    session: AsyncSession,
    dispatch: Dispatch,
    artifact_id: int,
    entry: dict[str, Any],
) -> None:
    provider = _name(entry.get("provider"))
    model = _name(entry.get("model"))
    input_tokens = _non_negative(entry.get("input_tokens"))
    output_tokens = _non_negative(entry.get("output_tokens"))
    cache_read = _non_negative(entry.get("cache_read_tokens"))
    cache_write = _non_negative(entry.get("cache_write_tokens"))
    reasoning = _non_negative(entry.get("reasoning_tokens"))
    total = input_tokens + output_tokens + cache_read + cache_write + reasoning
    await session.execute(
        usage_upsert_statement(
            dispatch.tenant_id,
            dispatch.workitem_id,
            dispatch.id,
            dispatch.agent_id,
            dispatch.executor_id,
            artifact_id,
            _step_id(entry),
            provider,
            model,
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
            reasoning,
            _credits(entry.get("credits")),
            total,
            entry,
        )
    )
    logger.info(
        "dispatch usage recorded tenantId=%s workitemId=%s dispatchId=%s "
        "provider=%s model=%s totalTokens=%s",
        dispatch.tenant_id,
        dispatch.workitem_id,
        dispatch.id,
        provider,
        model,
        total,
    )


def _entries(content: bytes) -> list[dict[str, Any]]:
    parsed = json.loads(content.decode("utf-8"))
    raw: object
    if isinstance(parsed, list):
        raw = parsed
    elif isinstance(parsed, dict):
        raw = parsed.get("usage")
    else:
        return []
    if not isinstance(raw, list) or len(raw) == 0:
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("usage entry")
        rows.append(item)
    return rows


def _name(value: object) -> str:
    if not isinstance(value, str) or java_is_blank(value):
        return "unknown"
    return _trim(value)


def _step_id(entry: dict[str, Any]) -> str:
    value = entry.get("step_id")
    if value is None:
        value = entry.get("stepId")
    if not isinstance(value, str):
        return ""
    return value


def _non_negative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if value < 0:
        return 0
    return value


def _credits(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return Decimal(str(value))


def _trim(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start += 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end -= 1
    return value[start:end]
