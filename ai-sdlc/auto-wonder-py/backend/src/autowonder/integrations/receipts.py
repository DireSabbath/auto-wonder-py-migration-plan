"""人工重试或确认外部操作回执。"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.audits.service import AuditRecord, record_required
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel
from autowonder.db.rows import rowcount
from autowonder.integrations.models import IntegrationOutbox
from autowonder.integrations.receipts_sanitize import sanitize_text

_MAX_REASON = 512
_RETRY = "EXTERNAL_OPERATION_MANUAL_RETRY"
_CONFIRM = "EXTERNAL_OPERATION_MANUAL_CONFIRM_SUCCEEDED"


class ManualReceiptRequest(ApiModel):
    """人工处理原因。缺省请求体时原因按空处理。"""

    reason: str | None = None


async def manual_retry(
    session: AsyncSession,
    receipt_id: int,
    tenant_id: int,
    operator_id: int,
    reason: str | None,
) -> None:
    """把已终止或结果不明的回执重新放回待发送，并记审计。"""
    normalized = _required_text(reason, "人工重试原因")
    receipt = await _require_actionable(session, receipt_id, tenant_id)
    changed = await _apply(session, receipt, tenant_id, "PENDING", False)
    if changed != 1:
        raise _state_changed()
    await record_required(
        session,
        _audit(receipt, tenant_id, operator_id, _RETRY, normalized)
        .add("previousRetryCount", receipt.retry_count)
        .add("retryCountResetTo", 0),
    )


async def manual_confirm_succeeded(
    session: AsyncSession,
    receipt_id: int,
    tenant_id: int,
    operator_id: int,
    reason: str | None,
) -> None:
    """人工确认回执已经成功，不再自动重试。"""
    normalized = _required_text(reason, "人工确认原因")
    receipt = await _require_actionable(session, receipt_id, tenant_id)
    changed = await _apply(session, receipt, tenant_id, "SUCCEEDED", True)
    if changed != 1:
        raise _state_changed()
    await record_required(
        session,
        _audit(receipt, tenant_id, operator_id, _CONFIRM, normalized),
    )


async def _apply(
    session: AsyncSession,
    receipt: IntegrationOutbox,
    tenant_id: int,
    status: str,
    clear_error: bool,
) -> int:
    expected = 0 if receipt.lock_version is None else receipt.lock_version
    values: dict[str, object] = {
        "status": status,
        "lock_version": IntegrationOutbox.lock_version + 1,
        "next_retry_at": None,
        "gmt_modified": now_local(),
    }
    if status == "PENDING":
        values["retry_count"] = 0
    if clear_error:
        values["last_error"] = None
    result = await session.execute(
        update(IntegrationOutbox)
        .where(
            IntegrationOutbox.id == receipt.id,
            IntegrationOutbox.tenant_id == tenant_id,
            IntegrationOutbox.lock_version == expected,
            (
                (IntegrationOutbox.status == "UNKNOWN")
                | (
                    (IntegrationOutbox.status == "FAILED")
                    & (IntegrationOutbox.next_retry_at.is_(None))
                )
            ),
        )
        .values(**values)
    )
    return rowcount(result)


async def _require_actionable(
    session: AsyncSession,
    receipt_id: int,
    tenant_id: int,
) -> IntegrationOutbox:
    if receipt_id <= 0:
        raise BizError(ErrorCode.PARAM_INVALID, "Receipt ID 必须为正整数")
    receipt = await session.get(IntegrationOutbox, receipt_id)
    if receipt is None or receipt.tenant_id != tenant_id:
        raise BizError(ErrorCode.NOT_FOUND, "外部操作回执不存在")
    terminal_failure = receipt.status == "FAILED" and receipt.next_retry_at is None
    if not terminal_failure and receipt.status != "UNKNOWN":
        raise BizError(ErrorCode.CONFLICT, "仅可处理结果不明或已终止且不再自动重试的回执")
    return receipt


def _required_text(value: str | None, field: str) -> str:
    if value is None or value.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, field + "不能为空")
    normalized = sanitize_text(value.strip()) or ""
    if len(normalized) > _MAX_REASON:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            field + "长度不能超过 " + str(_MAX_REASON) + " 个字符",
        )
    return normalized


def _state_changed() -> BizError:
    return BizError(ErrorCode.CONFLICT, "回执状态已变化，请刷新后重试")


def _audit(
    receipt: IntegrationOutbox,
    tenant_id: int,
    operator_id: int,
    action: str,
    reason: str,
) -> AuditRecord:
    return (
        AuditRecord(
            tenant_id=tenant_id,
            actor_id=operator_id,
            actor_type="HUMAN",
            module="INTEGRATION",
            action=action,
            target_type="EXTERNAL_OPERATION_RECEIPT",
            target_id=receipt.id,
            trigger_type="MANUAL",
            trigger_source="ADMIN_API",
            event_type=action,
        )
        .add("reason", reason)
        .add("provider", receipt.provider)
        .add("receiptEventType", receipt.event_type)
        .add("operationKey", receipt.operation_key)
        .add("previousStatus", receipt.status)
    )
