"""审计日志写入与查询。``record_required`` 失败时让当前事务回滚。"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import case, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.agents.models import Agent
from autowonder.audits.models import AuditLog
from autowonder.audits.schemas import AuditLogView
from autowonder.users.models import User

logger = logging.getLogger(__name__)

MAX_DETAIL_JSON_CHARS = 4000
MAX_COLUMN_CHARS = 64
MAX_TEXT_CHARS = 512


@dataclass
class AuditRecord:
    """一次必须落库的审计。``detail`` 保持写入顺序。"""

    tenant_id: int
    actor_id: int | None
    actor_type: str | None
    module: str
    action: str
    target_type: str | None
    target_id: int | None
    trigger_type: str | None = None
    trigger_source: str | None = None
    event_type: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def add(self, key: str, value: object) -> "AuditRecord":
        """追加一条细节。空键或空值不写入。"""
        if value is not None:
            self.detail[key] = value
        return self


async def record_required(session: AsyncSession, record: AuditRecord) -> None:
    """写入审计行。模块或动作为空时中断调用方事务。"""
    if record.tenant_id <= 0 or _blank(record.module) or _blank(record.action):
        raise RuntimeError("Required audit record is invalid")
    session.add(
        AuditLog(
            tenant_id=record.tenant_id,
            actor_id=record.actor_id,
            module=_clamp(record.module),
            action=_clamp(record.action),
            target_type=_clamp(record.target_type),
            target_id=record.target_id,
            detail_json=_detail_payload(record),
        )
    )
    await session.flush()


def _detail_payload(record: AuditRecord) -> dict[str, object]:
    detail: dict[str, object] = {}
    _put(detail, "actorType", record.actor_type)
    _put(detail, "triggerType", record.trigger_type)
    _put(detail, "triggerSource", record.trigger_source)
    _put(detail, "eventType", record.event_type)
    for key, value in record.detail.items():
        _put(detail, key, _sanitize(value))
    encoded = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= MAX_DETAIL_JSON_CHARS:
        return detail
    truncated: dict[str, object] = {}
    _put(truncated, "actorType", record.actor_type)
    _put(truncated, "triggerType", record.trigger_type)
    _put(truncated, "triggerSource", record.trigger_source)
    _put(truncated, "eventType", record.event_type)
    truncated["truncated"] = True
    truncated["originalLength"] = len(encoded)
    return truncated


def _put(detail: dict[str, object], key: str, value: object) -> None:
    if isinstance(value, str) and value.strip() == "":
        return
    if value is not None:
        detail[key] = value


def _sanitize(value: object) -> object:
    if isinstance(value, str) and len(value) > MAX_TEXT_CHARS:
        return value[:MAX_TEXT_CHARS]
    return value


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _clamp(value: str | None) -> str | None:
    if value is None or len(value) <= MAX_COLUMN_CHARS:
        return value
    return value[:MAX_COLUMN_CHARS]


def page_window(page: int, size: int) -> tuple[int, int]:
    """页码小于 1 按第 1 页，每页至少 1 条、至多 100 条。返回偏移和页大小。"""
    if page < 1:
        page = 1
    if size < 1:
        size = 1
    if size > 100:
        size = 100
    return (page - 1) * size, size


def actor_type_of(detail: object) -> str | None:
    """从细节 JSON 读取 ``actorType``。空文本或无法解析时为空。"""
    parsed = _parsed_detail(detail)
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("actorType")
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        if value:
            return "true"
        return "false"
    return str(value)


def detail_text(detail: object) -> str | None:
    """把库存细节还原成 JSON 文本。字典按键名排序，贴近 MySQL JSON 读出顺序。"""
    if detail is None:
        return None
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def human_display_name(nickname: str | None, username: str | None) -> str | None:
    """有非空白昵称时用昵称，否则用登录名。"""
    if nickname is not None and nickname.strip() != "":
        return nickname
    return username


def apply_filters(
    statement: Select[Any],
    tenant_id: int,
    module: str | None,
    action: str | None,
    actor_type: str | None,
    actor_id: int | None,
    target_type: str | None,
    target_id: int | None,
    start_time: str | None,
    end_time: str | None,
    keyword: str | None,
) -> Select[Any]:
    """拼上与 ``AuditLogDao.searchCondition`` 相同的条件。空的 actorType 不参与过滤。"""
    statement = statement.where(AuditLog.tenant_id == tenant_id)
    if module is not None:
        statement = statement.where(AuditLog.module == module)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    if actor_type is not None and actor_type != "":
        payload = case(
            (func.json_valid(AuditLog.detail_json), AuditLog.detail_json),
            else_=literal("{}"),
        )
        extracted = func.json_unquote(func.json_extract(payload, "$.actorType"))
        statement = statement.where(extracted == actor_type)
    if actor_id is not None:
        statement = statement.where(AuditLog.actor_id == actor_id)
    if target_type is not None:
        statement = statement.where(AuditLog.target_type == target_type)
    if target_id is not None:
        statement = statement.where(AuditLog.target_id == target_id)
    if start_time is not None:
        statement = statement.where(
            text("gmt_create >= :start_time").bindparams(start_time=start_time)
        )
    if end_time is not None:
        statement = statement.where(text("gmt_create <= :end_time").bindparams(end_time=end_time))
    if keyword is not None:
        statement = statement.where(AuditLog.detail_json.like("%" + keyword + "%"))
    return statement


async def search_logs(
    session: AsyncSession,
    tenant_id: int,
    module: str | None,
    action: str | None,
    actor_type: str | None,
    actor_id: int | None,
    target_type: str | None,
    target_id: int | None,
    start_time: str | None,
    end_time: str | None,
    keyword: str | None,
    page: int,
    size: int,
) -> list[AuditLogView]:
    """按创建时间倒序分页查询。"""
    offset, limit = page_window(page, size)
    statement = apply_filters(
        select(AuditLog),
        tenant_id,
        module,
        action,
        actor_type,
        actor_id,
        target_type,
        target_id,
        start_time,
        end_time,
        keyword,
    )
    statement = statement.order_by(AuditLog.gmt_create.desc()).offset(offset).limit(limit)
    result = await session.scalars(statement)
    views: list[AuditLogView] = []
    for row in result.all():
        views.append(await _to_view(session, row))
    return views


async def count_logs(
    session: AsyncSession,
    tenant_id: int,
    module: str | None,
    action: str | None,
    actor_type: str | None,
    actor_id: int | None,
    target_type: str | None,
    target_id: int | None,
    start_time: str | None,
    end_time: str | None,
    keyword: str | None,
) -> int:
    """符合条件的审计条数。"""
    statement = apply_filters(
        select(func.count()).select_from(AuditLog),
        tenant_id,
        module,
        action,
        actor_type,
        actor_id,
        target_type,
        target_id,
        start_time,
        end_time,
        keyword,
    )
    return cast(int, await session.scalar(statement))


def _parsed_detail(detail: object) -> object:
    if detail is None:
        return None
    if isinstance(detail, str):
        if detail.strip() == "":
            return None
        try:
            return json.loads(detail)
        except json.JSONDecodeError:
            return None
    return detail


async def _to_view(session: AsyncSession, row: AuditLog) -> AuditLogView:
    actor_type = actor_type_of(row.detail_json)
    return AuditLogView(
        id=row.id,
        actor_id=row.actor_id,
        actor_type=actor_type,
        actor_name=await _actor_name(session, actor_type, row.actor_id),
        module=row.module,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        detail_json=detail_text(row.detail_json),
        gmt_create=row.gmt_create,
    )


async def _actor_name(
    session: AsyncSession,
    actor_type: str | None,
    actor_id: int | None,
) -> str | None:
    if actor_id is None or actor_type is None:
        return None
    try:
        if actor_type == "AGENT":
            return await _agent_name(session, actor_id)
        if actor_type == "HUMAN":
            return await _human_name(session, actor_id)
    except Exception:
        logger.warning(
            "audit actor name resolve skipped actorType=%s actorId=%s",
            actor_type,
            actor_id,
            exc_info=True,
        )
        return None
    return None


async def _agent_name(session: AsyncSession, actor_id: int) -> str | None:
    agent = await session.scalar(
        select(Agent).where(Agent.id == actor_id, Agent.is_deleted == 0).limit(1)
    )
    if agent is None:
        return None
    return agent.name


async def _human_name(session: AsyncSession, actor_id: int) -> str | None:
    user = await session.scalar(
        select(User).where(User.id == actor_id, User.is_deleted == 0).limit(1)
    )
    if user is None:
        return None
    return human_display_name(user.nickname, user.username)


def compiled_filter_sql(
    tenant_id: int,
    module: str | None,
    action: str | None,
    actor_type: str | None,
    actor_id: int | None,
    target_type: str | None,
    target_id: int | None,
    start_time: str | None,
    end_time: str | None,
    keyword: str | None,
) -> str:
    """把筛选条件编译成 MySQL 文本，供契约测试核对。"""
    from sqlalchemy.dialects import mysql

    statement = apply_filters(
        select(AuditLog),
        tenant_id,
        module,
        action,
        actor_type,
        actor_id,
        target_type,
        target_id,
        start_time,
        end_time,
        keyword,
    )
    return str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
