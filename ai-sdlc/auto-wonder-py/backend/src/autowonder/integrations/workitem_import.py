"""外部工单导入。字段映射、建单、重复和更新都按 Java 分支处理。"""

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.audits.service import AuditRecord, record_required
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel
from autowonder.integrations.models import ExternalWorkitemImportRecord, ExternalWorkitemLink
from autowonder.statemachines.models import StatusNode, StatusTemplate
from autowonder.workitems.models import Workitem, WorkitemEvent

_CREATED = "CREATED"
_UPDATED = "UPDATED"
_DUPLICATE = "DUPLICATE"
_FAILED = "FAILED"
_UNKNOWN = "UNKNOWN"


class ExternalAttachment(ApiModel):
    """导入请求里的附件。"""

    name: str | None = None
    url: str | None = None


class ExternalWorkitemImportRequest(ApiModel):
    """外部系统推送的工单。"""

    source_system: str | None = None
    external_workitem_id: str | None = None
    external_project_id: str | None = None
    title: str | None = None
    description: str | None = None
    type: str | None = None
    priority: int | None = None
    assignee: str | None = None
    creator: str | None = None
    status: str | None = None
    source_url: str | None = None
    request_id: str | None = None
    update_existing: bool | None = None
    attachments: list[ExternalAttachment] | None = None
    extensions: dict[str, Any] | None = None
    field_mappings: dict[str, str] | None = None


class ExternalWorkitemImportResult(ApiModel):
    """导入结果。created / updated / duplicate 三者按实际分支赋值。"""

    source_system: str | None = None
    external_workitem_id: str | None = None
    workitem_id: int | None = None
    import_record_id: int | None = None
    created: bool = False
    updated: bool = False
    duplicate: bool = False


class ExternalWorkitemImportRecordView(ApiModel):
    """导入记录列表项。"""

    id: int | None = None
    source_system: str | None = None
    external_workitem_id: str | None = None
    workitem_id: int | None = None
    request_id: str | None = None
    status: str | None = None
    failure_reason: str | None = None
    source_url: str | None = None
    gmt_create: Any = None
    gmt_modified: Any = None


def normalize_work_type(work_type: str) -> str:
    """把外部类型名收成 REQ / BUG / TASK。"""
    normalized = work_type.strip().upper()
    if normalized in {"REQ", "REQUIREMENT", "DEMAND", "STORY"}:
        return "REQ"
    if normalized in {"BUG", "DEFECT"}:
        return "BUG"
    if normalized == "TASK":
        return "TASK"
    raise BizError(ErrorCode.WORK_TYPE_INVALID)


async def import_workitem(
    session: AsyncSession,
    request: ExternalWorkitemImportRequest | None,
    tenant_id: int,
    user_id: int,
) -> ExternalWorkitemImportResult:
    """导入或更新。失败时先写 FAILED 记录再把原异常抛出。"""
    try:
        _apply_mappings(request)
        _validate(request)
        assert request is not None
        source = _normalize_source(request.source_system or "")
        work_type = normalize_work_type(request.type or "")
        raw = request.model_dump(by_alias=True)
        raw_json = json.dumps(raw, ensure_ascii=False, default=str)
        link = await session.scalar(
            select(ExternalWorkitemLink)
            .where(
                ExternalWorkitemLink.tenant_id == tenant_id,
                ExternalWorkitemLink.binding_id == 0,
                ExternalWorkitemLink.external_workitem_id == request.external_workitem_id,
            )
            .limit(1)
        )
        if link is None:
            return await _create(session, request, tenant_id, user_id, source, work_type, raw_json)
        return await _existing(session, request, tenant_id, user_id, source, raw_json, link)
    except BizError as error:
        await _record_failure(session, request, tenant_id, str(error))
        raise
    except RuntimeError as error:
        await _record_failure(session, request, tenant_id, str(error))
        raise


async def list_records(
    session: AsyncSession,
    source_system: str | None,
    external_workitem_id: str | None,
    status: str | None,
    tenant_id: int,
    page: int,
    size: int,
) -> list[ExternalWorkitemImportRecordView]:
    """按来源、外部 id 和状态分页查导入记录。"""
    current_page = max(page, 1)
    current_size = min(max(size, 1), 100)
    statement = select(ExternalWorkitemImportRecord).where(
        ExternalWorkitemImportRecord.tenant_id == tenant_id
    )
    if source_system is not None and source_system.strip() != "":
        statement = statement.where(
            ExternalWorkitemImportRecord.source_system == _normalize_source(source_system)
        )
    external_id = _normalize_nullable(external_workitem_id)
    if external_id is not None:
        statement = statement.where(
            ExternalWorkitemImportRecord.external_workitem_id == external_id
        )
    record_status = _normalize_nullable(status)
    if record_status is not None:
        statement = statement.where(ExternalWorkitemImportRecord.status == record_status)
    rows = await session.scalars(
        statement.order_by(ExternalWorkitemImportRecord.id.desc())
        .offset((current_page - 1) * current_size)
        .limit(current_size)
    )
    return [_record_view(row) for row in rows.all()]


async def _create(
    session: AsyncSession,
    request: ExternalWorkitemImportRequest,
    tenant_id: int,
    user_id: int,
    source: str,
    work_type: str,
    raw_json: str,
) -> ExternalWorkitemImportResult:
    node = await _init_node(session, work_type)
    workitem = Workitem(
        tenant_id=tenant_id,
        work_type=work_type,
        title=(request.title or "").strip(),
        content_md=_content(request),
        template_id=node.template_id,
        status_node_id=node.id,
        assignee_type="EXTERNAL",
        assignee_ref=0,
        priority=2 if request.priority is None else request.priority,
        creator_id=user_id,
        version=0,
    )
    session.add(workitem)
    await session.flush()
    await _event(
        session,
        tenant_id,
        workitem.id,
        "EXTERNAL_IMPORT",
        request.external_workitem_id,
        user_id,
    )
    session.add(
        ExternalWorkitemLink(
            tenant_id=tenant_id,
            provider=source,
            binding_id=0,
            external_project_id=_default(request.external_project_id),
            external_workitem_id=request.external_workitem_id or "",
            external_work_type=work_type,
            workitem_id=workitem.id,
            remote_version_hash=_hash(raw_json),
            last_sync_direction="INBOUND",
        )
    )
    record = _record(request, tenant_id, source, workitem.id, _CREATED, None, raw_json)
    session.add(record)
    await session.flush()
    await _audit(session, tenant_id, user_id, "IMPORT_CREATED", workitem.id, source, request)
    return _result(request, source, workitem.id, record.id, True, False, False)


async def _existing(
    session: AsyncSession,
    request: ExternalWorkitemImportRequest,
    tenant_id: int,
    user_id: int,
    source: str,
    raw_json: str,
    link: ExternalWorkitemLink,
) -> ExternalWorkitemImportResult:
    if request.update_existing is False:
        record = _record(
            request,
            tenant_id,
            source,
            link.workitem_id,
            _DUPLICATE,
            "external workitem already imported",
            raw_json,
        )
        session.add(record)
        await session.flush()
        await _audit(
            session,
            tenant_id,
            user_id,
            "IMPORT_DUPLICATE",
            link.workitem_id,
            source,
            request,
        )
        return _result(request, source, link.workitem_id, record.id, False, False, True)
    existing = await session.get(Workitem, link.workitem_id)
    if existing is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND, "已存在外部工单映射,但本地工单不存在")
    title = (request.title or "").strip()
    content = _content(request)
    changed = title != existing.title or content != existing.content_md
    if changed:
        existing.title = title
        existing.content_md = content
        existing.version = existing.version + 1
        existing.modifier_id = user_id
        await _event(
            session,
            tenant_id,
            existing.id,
            "EXTERNAL_UPDATE",
            request.external_workitem_id,
            user_id,
        )
    link.remote_version_hash = _hash(raw_json)
    link.last_sync_direction = "INBOUND"
    record = _record(request, tenant_id, source, existing.id, _UPDATED, None, raw_json)
    session.add(record)
    await session.flush()
    await _audit(session, tenant_id, user_id, "IMPORT_UPDATED", existing.id, source, request)
    return _result(request, source, existing.id, record.id, False, changed, True)


def _apply_mappings(request: ExternalWorkitemImportRequest | None) -> None:
    if (
        request is None
        or request.field_mappings is None
        or len(request.field_mappings) == 0
        or request.extensions is None
        or len(request.extensions) == 0
    ):
        return
    for key, target in request.field_mappings.items():
        if key is None or target is None:
            continue
        value = request.extensions.get(key)
        if value is None:
            continue
        _apply_value(request, target, value)


def _apply_value(request: ExternalWorkitemImportRequest, target_field: str, value: object) -> None:
    target = target_field.strip().lower()
    text = str(value)
    if target == "sourcesystem" and _blank(request.source_system):
        request.source_system = text
    elif target == "externalworkitemid" and _blank(request.external_workitem_id):
        request.external_workitem_id = text
    elif target == "externalprojectid" and _blank(request.external_project_id):
        request.external_project_id = text
    elif target == "title" and _blank(request.title):
        request.title = text
    elif target in {"description", "content", "contentmd"} and _blank(request.description):
        request.description = text
    elif target in {"type", "worktype"} and _blank(request.type):
        request.type = text
    elif target == "priority" and request.priority is None:
        request.priority = _priority(value)
    elif target == "assignee" and _blank(request.assignee):
        request.assignee = text
    elif target == "creator" and _blank(request.creator):
        request.creator = text
    elif target == "status" and _blank(request.status):
        request.status = text
    elif target in {"sourceurl", "rawlink"} and _blank(request.source_url):
        request.source_url = text


def _priority(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        try:
            return int(str(value))
        except ValueError as error:
            raise BizError(ErrorCode.PARAM_INVALID, "priority必须是数字") from error
    return int(value)


def _validate(request: ExternalWorkitemImportRequest | None) -> None:
    if request is None:
        raise BizError(ErrorCode.PARAM_INVALID, "请求体不能为空")
    _required(request.source_system, "sourceSystem")
    _required(request.external_workitem_id, "externalWorkitemId")
    _required(request.title, "title")
    _required(request.type, "type")
    normalize_work_type(request.type or "")


def _required(value: str | None, field: str) -> None:
    if _blank(value):
        raise BizError(ErrorCode.PARAM_INVALID, field + "不能为空")


def _content(request: ExternalWorkitemImportRequest) -> str:
    body = _default(request.description)
    body = _line(body, "来源系统", request.source_system)
    body = _line(body, "外部工单ID", request.external_workitem_id)
    body = _line(body, "外部状态", request.status)
    body = _line(body, "原始链接", request.source_url)
    body = _line(body, "创建人", request.creator)
    body = _line(body, "负责人", request.assignee)
    if request.attachments is not None and len(request.attachments) > 0:
        body = body + "\n\n### 附件"
        for attachment in request.attachments:
            body = body + "\n- " + _default(attachment.name)
            if attachment.url is not None and attachment.url.strip() != "":
                body = body + ": " + attachment.url.strip()
    return body


def _line(body: str, label: str, value: str | None) -> str:
    if value is None or value.strip() == "":
        return body
    prefix = "" if body == "" else "\n"
    return body + prefix + "> " + label + ": " + value.strip()


def _record(
    request: ExternalWorkitemImportRequest,
    tenant_id: int,
    source: str,
    workitem_id: int | None,
    status: str,
    failure: str | None,
    raw_json: str,
) -> ExternalWorkitemImportRecord:
    return ExternalWorkitemImportRecord(
        tenant_id=tenant_id,
        source_system=source,
        external_workitem_id=request.external_workitem_id or "",
        workitem_id=workitem_id,
        request_id=request.request_id,
        status=status,
        failure_reason=failure,
        source_url=request.source_url,
        raw_payload_json=json.loads(raw_json),
        extensions_json=request.extensions,
        field_mappings_json=request.field_mappings,
    )


async def _record_failure(
    session: AsyncSession,
    request: ExternalWorkitemImportRequest | None,
    tenant_id: int,
    failure: str,
) -> None:
    try:
        session.add(
            ExternalWorkitemImportRecord(
                tenant_id=tenant_id,
                source_system=_safe_source(request),
                external_workitem_id=_safe_external_id(request),
                request_id=None if request is None else request.request_id,
                status=_FAILED,
                failure_reason=failure,
                source_url=None if request is None else request.source_url,
                raw_payload_json=None if request is None else request.model_dump(by_alias=True),
                extensions_json=None if request is None else request.extensions,
                field_mappings_json=None if request is None else request.field_mappings,
            )
        )
        await session.flush()
    except RuntimeError:
        return


def _safe_source(request: ExternalWorkitemImportRequest | None) -> str:
    if request is None or _blank(request.source_system):
        return _UNKNOWN
    return _normalize_source(request.source_system or "")


def _safe_external_id(request: ExternalWorkitemImportRequest | None) -> str:
    if request is None or _blank(request.external_workitem_id):
        return _UNKNOWN
    return (request.external_workitem_id or "").strip()


def _result(
    request: ExternalWorkitemImportRequest,
    source: str,
    workitem_id: int,
    record_id: int,
    created: bool,
    updated: bool,
    duplicate: bool,
) -> ExternalWorkitemImportResult:
    return ExternalWorkitemImportResult(
        source_system=source,
        external_workitem_id=request.external_workitem_id,
        workitem_id=workitem_id,
        import_record_id=record_id,
        created=created,
        updated=updated,
        duplicate=duplicate,
    )


def _record_view(row: ExternalWorkitemImportRecord) -> ExternalWorkitemImportRecordView:
    return ExternalWorkitemImportRecordView(
        id=row.id,
        source_system=row.source_system,
        external_workitem_id=row.external_workitem_id,
        workitem_id=row.workitem_id,
        request_id=row.request_id,
        status=row.status,
        failure_reason=row.failure_reason,
        source_url=row.source_url,
        gmt_create=row.gmt_create,
        gmt_modified=row.gmt_modified,
    )


async def _init_node(session: AsyncSession, work_type: str) -> StatusNode:
    template = await session.scalar(
        select(StatusTemplate)
        .where(
            StatusTemplate.work_type == work_type,
            StatusTemplate.is_default == 1,
            StatusTemplate.is_deleted == 0,
        )
        .limit(1)
    )
    if template is None:
        raise BizError(ErrorCode.STATUS_TEMPLATE_NOT_FOUND)
    node = await session.scalar(
        select(StatusNode)
        .where(StatusNode.template_id == template.id, StatusNode.category == "INIT")
        .limit(1)
    )
    if node is None:
        raise BizError(ErrorCode.STATUS_TEMPLATE_NOT_FOUND)
    return node


async def _event(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    event_type: str,
    external_id: str | None,
    user_id: int,
) -> None:
    session.add(
        WorkitemEvent(
            tenant_id=tenant_id,
            workitem_id=workitem_id,
            event_type=event_type,
            to_val=external_id,
            actor_type="SYSTEM",
            actor_ref=user_id,
        )
    )
    await session.flush()


async def _audit(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    action: str,
    workitem_id: int,
    source: str,
    request: ExternalWorkitemImportRequest,
) -> None:
    await record_required(
        session,
        AuditRecord(
            tenant_id=tenant_id,
            actor_id=user_id,
            actor_type="HUMAN",
            module="integration",
            action=action,
            target_type="workitem",
            target_id=workitem_id,
            trigger_type="API",
            trigger_source="external-workitem-import",
            event_type=action,
        )
        .add("sourceSystem", source)
        .add("externalWorkitemId", request.external_workitem_id),
    )


def _normalize_source(source: str) -> str:
    return source.strip().upper()


def _normalize_nullable(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _default(value: str | None) -> str:
    return "" if value is None else value.strip()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
