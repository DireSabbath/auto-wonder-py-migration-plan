"""审计日志写入。``record_required`` 失败时让当前事务回滚。"""

import json
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.audits.models import AuditLog

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
