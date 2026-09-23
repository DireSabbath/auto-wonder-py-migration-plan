"""SDLC 流程与步骤。查询和错误文案对齐 SdlcDao / SdlcService。"""

import json
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.sdlcs.checklist import validate_checklist
from autowonder.sdlcs.models import Sdlc, SdlcStep
from autowonder.sdlcs.schemas import (
    CreateSdlcRequest,
    CreateStepRequest,
    ReorderStepsRequest,
    SdlcView,
    StepView,
    UpdateSdlcRequest,
    UpdateStepRequest,
)
from autowonder.squads.attribution import refs_by_sdlc_ids
from autowonder.squads.models import Squad, SquadMember
from autowonder.workitems.models import Workitem

_ORDER_RETRY_LIMIT = 3
_TEMP_ORDER_BASE = -2_147_483_648
_IN_USE_SAMPLE = 5


def require_sdlc_name(name: str | None) -> str:
    """创建时名称必填，并去掉两端空白。"""
    if name is None or name.strip() == "":
        raise BizError(ErrorCode.SDLC_NAME_REQUIRED)
    return name.strip()


def page_window(page: int, size: int) -> tuple[int, int]:
    """页码小于 1 时从第 1 页起；每页小于 1 时用 20，并且不超过 100。"""
    normalized_page = page
    if page < 1:
        normalized_page = 1
    normalized_size = size
    if size < 1:
        normalized_size = 20
    if normalized_size > 100:
        normalized_size = 100
    return (normalized_page - 1) * normalized_size, normalized_size


def normalize_json(value: str | None) -> str | None:
    """JSON 列不接受空串，空白输入写成 null。"""
    if value is None or value.strip() == "":
        return None
    return value


def merge_nullable_text(incoming: str | None, current: str | None) -> str | None:
    """未携带时保留原值，显式空白视为清空。"""
    if incoming is None:
        return current
    if incoming.strip() == "":
        return None
    return incoming


def require_valid_json(field: str, value: str | None) -> None:
    """非法 JSON 以参数错误返回，避免写库时变成数据冲突。"""
    if value is None or value.strip() == "":
        return
    try:
        json.loads(value)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID, field + " 不是合法的 JSON") from error


def chosen_step_order(requested: int | None, fallback: int) -> int:
    """未给或非正数序号时用下一个可用序号。"""
    if requested is not None and requested > 0:
        return requested
    return fallback


def required_flag(value: bool | None) -> int:
    """省略 required 时按必需步骤写入。"""
    if value is False:
        return 0
    return 1


def build_in_use_message(
    workitem_count: int,
    workitem_ids: list[int],
    agent_labels: list[str],
) -> str:
    """删除被引用流程时的说明，样例条数由调用方截断。"""
    refs: list[str] = []
    if workitem_count > 0:
        part = f"工单 {workitem_count} 个"
        if len(workitem_ids) > 0:
            shown = ", ".join(f"#{item_id}" for item_id in workitem_ids)
            if workitem_count > len(workitem_ids):
                shown = shown + " 等"
            part = part + "(" + shown + ")"
        refs.append(part)
    if len(agent_labels) > 0:
        refs.append(f"数字员工 {len(agent_labels)} 个(" + ", ".join(agent_labels) + ")")
    return (
        ErrorCode.SDLC_DELETE_IN_USE.message
        + ": 引用源: "
        + "; ".join(refs)
        + "。请先解除上述引用后再删除。"
    )


async def create_sdlc(
    session: AsyncSession,
    request: CreateSdlcRequest,
    tenant_id: int,
    user_id: int,
) -> SdlcView:
    """插入草稿流程。插入语句不回读创建时间。"""
    sdlc = Sdlc(
        tenant_id=tenant_id,
        name=require_sdlc_name(request.name),
        description=request.description,
        work_type=request.work_type,
        status="DRAFT",
        is_default=0,
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(sdlc)
    await session.flush()
    await session.commit()
    view = _to_view(sdlc, [])
    view.gmt_create = None
    return view


async def get_sdlc(session: AsyncSession, sdlc_id: int) -> SdlcView:
    """流程详情，带步骤正文。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    steps = await _steps(session, sdlc_id)
    return _to_view(sdlc, steps)


async def list_sdlcs(
    session: AsyncSession,
    tenant_id: int,
    work_type: str | None,
    status: str | None,
    squad_ids: list[int] | None,
    page: int,
    size: int,
) -> list[SdlcView]:
    """流程列表。不返回步骤正文，补步骤数和在线版本上的小队。"""
    offset, limit = page_window(page, size)
    rows = await session.scalars(
        _list_statement(tenant_id, work_type, status, squad_ids).offset(offset).limit(limit)
    )
    views = [_to_view(sdlc, None) for sdlc in rows]
    await _fill_step_counts(session, views)
    await _fill_squads(session, tenant_id, views)
    return views


async def update_sdlc(
    session: AsyncSession,
    sdlc_id: int,
    request: UpdateSdlcRequest,
    tenant_id: int,
    user_id: int,
) -> SdlcView:
    """按 version 更新。未传的字段保留原值。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    _require_editable(sdlc.status)
    name = sdlc.name
    if request.name is not None:
        name = request.name.strip()
    description = sdlc.description
    if request.description is not None:
        description = request.description
    work_type = sdlc.work_type
    if request.work_type is not None:
        work_type = request.work_type
    updated = rowcount(
        await session.execute(
            update(Sdlc)
            .where(
                Sdlc.id == sdlc_id,
                Sdlc.tenant_id == tenant_id,
                Sdlc.version == sdlc.version,
                Sdlc.is_deleted == 0,
            )
            .values(
                name=name,
                description=description,
                work_type=work_type,
                version=Sdlc.version + 1,
                modifier_id=user_id,
            )
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.SDLC_VERSION_CONFLICT)
    session.expire_all()
    view = await get_sdlc(session, sdlc_id)
    await session.commit()
    return view


async def delete_sdlc(
    session: AsyncSession,
    sdlc_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """没有工单和在线数字员工引用时逻辑删除流程及其步骤。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    workitem_count = await _workitem_count(session, sdlc_id)
    agent_ids = await _agent_ids_using_sdlc(session, sdlc_id)
    if workitem_count > 0 or len(agent_ids) > 0:
        workitem_ids: list[int] = []
        if workitem_count > 0:
            workitem_ids = await _workitem_ids(session, sdlc_id, _IN_USE_SAMPLE)
        labels = await _agent_labels(session, agent_ids)
        raise BizError(
            ErrorCode.SDLC_DELETE_IN_USE,
            build_in_use_message(workitem_count, workitem_ids, labels),
        )
    deleted = rowcount(
        await session.execute(
            update(Sdlc)
            .where(
                Sdlc.id == sdlc_id,
                Sdlc.tenant_id == tenant_id,
                Sdlc.version == sdlc.version,
                Sdlc.is_deleted == 0,
            )
            .values(is_deleted=1, version=Sdlc.version + 1, modifier_id=user_id)
        )
    )
    if deleted == 0:
        raise BizError(ErrorCode.SDLC_VERSION_CONFLICT)
    await session.execute(
        update(SdlcStep)
        .where(
            SdlcStep.sdlc_id == sdlc_id,
            SdlcStep.tenant_id == tenant_id,
            SdlcStep.is_deleted == 0,
        )
        .values(is_deleted=1, step_order=-SdlcStep.id)
    )
    await session.commit()


async def add_step(
    session: AsyncSession,
    sdlc_id: int,
    request: CreateStepRequest,
    tenant_id: int,
    user_id: int,
) -> StepView:
    """追加步骤。序号冲突时换下一个序号重试。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    _require_editable(sdlc.status)
    checklist = normalize_json(request.checklist_json)
    gate = normalize_json(request.gate_policy_json)
    require_valid_json("checklistJson", checklist)
    validate_checklist(checklist)
    require_valid_json("gatePolicyJson", gate)
    order = chosen_step_order(request.step_order, await _next_step_order(session, sdlc_id))
    step = await _insert_step(
        session,
        SdlcStep(
            tenant_id=tenant_id,
            sdlc_id=sdlc_id,
            step_order=order,
            name=request.name,
            kind=request.kind,
            instruction_md=request.instruction_md,
            checklist_json=_json_document(checklist),
            gate_policy_json=_json_document(gate),
            required=required_flag(request.required),
            timeout_seconds=request.timeout_seconds,
            retry_budget=request.retry_budget,
            code=request.code,
            handler_type=request.handler_type,
            handler_role_ref=request.handler_role_ref,
            status_on_enter_code=request.status_on_enter_code,
            on_success=_json_document(request.on_success),
            on_fail=_json_document(request.on_fail),
            creator_id=user_id,
            is_deleted=0,
        ),
        sdlc_id,
    )
    await session.commit()
    return _to_step(step)


async def update_step(
    session: AsyncSession,
    sdlc_id: int,
    step_id: int,
    request: UpdateStepRequest,
    tenant_id: int,
    user_id: int,
) -> StepView:
    """更新步骤内容。未携带的字段保留原值。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    _require_content_editable(sdlc.status)
    step = await _find_step(session, step_id)
    if step is None or step.sdlc_id != sdlc_id:
        raise BizError(ErrorCode.SDLC_STEP_NOT_FOUND)
    checklist = normalize_json(
        merge_nullable_text(request.checklist_json, _json_text(step.checklist_json))
    )
    gate = normalize_json(
        merge_nullable_text(request.gate_policy_json, _json_text(step.gate_policy_json))
    )
    require_valid_json("checklistJson", checklist)
    validate_checklist(checklist)
    require_valid_json("gatePolicyJson", gate)
    name = step.name
    if request.name is not None:
        name = request.name
    kind = step.kind
    if request.kind is not None:
        kind = request.kind
    code = step.code
    if request.code is not None:
        code = request.code
    handler_type = step.handler_type
    if request.handler_type is not None:
        handler_type = request.handler_type
    required = step.required
    if request.required is not None:
        required = required_flag(request.required)
    timeout_seconds = step.timeout_seconds
    if "timeout_seconds" in request.model_fields_set:
        timeout_seconds = request.timeout_seconds
    retry_budget = step.retry_budget
    if "retry_budget" in request.model_fields_set:
        retry_budget = request.retry_budget
    await session.execute(
        update(SdlcStep)
        .where(SdlcStep.id == step_id, SdlcStep.tenant_id == tenant_id, SdlcStep.is_deleted == 0)
        .values(
            name=name,
            kind=kind,
            instruction_md=merge_nullable_text(request.instruction_md, step.instruction_md),
            checklist_json=_json_document(checklist),
            gate_policy_json=_json_document(gate),
            required=required,
            timeout_seconds=timeout_seconds,
            retry_budget=retry_budget,
            code=code,
            handler_type=handler_type,
            handler_role_ref=merge_nullable_text(request.handler_role_ref, step.handler_role_ref),
            status_on_enter_code=merge_nullable_text(
                request.status_on_enter_code, step.status_on_enter_code
            ),
            on_success=_json_document(
                merge_nullable_text(request.on_success, _json_text(step.on_success))
            ),
            on_fail=_json_document(merge_nullable_text(request.on_fail, _json_text(step.on_fail))),
            modifier_id=user_id,
        )
    )
    session.expire_all()
    updated = await _find_step(session, step_id)
    await session.commit()
    if updated is None:
        return _to_step(step)
    return _to_step(updated)


async def delete_step(
    session: AsyncSession,
    sdlc_id: int,
    step_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """软删除步骤并重排剩余序号。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    _require_editable(sdlc.status)
    step = await _find_step(session, step_id)
    if step is None or step.sdlc_id != sdlc_id:
        raise BizError(ErrorCode.SDLC_STEP_NOT_FOUND)
    await session.execute(
        update(SdlcStep)
        .where(SdlcStep.id == step_id, SdlcStep.tenant_id == tenant_id, SdlcStep.is_deleted == 0)
        .values(is_deleted=1, step_order=-SdlcStep.id, modifier_id=user_id)
    )
    await _renumber_steps(session, sdlc_id, tenant_id, user_id)
    await session.commit()


async def reorder_steps(
    session: AsyncSession,
    sdlc_id: int,
    request: ReorderStepsRequest,
    tenant_id: int,
    user_id: int,
) -> None:
    """两段式重排，避免序号唯一键在中间态冲突。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    _require_editable(sdlc.status)
    step_ids = request.step_ids
    if step_ids is None or len(step_ids) == 0:
        return
    current = {step.id for step in await _steps(session, sdlc_id)}
    if current != set(step_ids):
        raise BizError(ErrorCode.SDLC_STEP_NOT_FOUND)
    for index, step_id in enumerate(step_ids):
        await _set_order(session, step_id, tenant_id, _TEMP_ORDER_BASE + index, user_id)
    for index, step_id in enumerate(step_ids):
        await _set_order(session, step_id, tenant_id, index + 1, user_id)
    await session.commit()


async def enable_sdlc(
    session: AsyncSession,
    sdlc_id: int,
    tenant_id: int,
    user_id: int,
    status_template_id: int | None,
) -> SdlcView:
    """启用流程，入口步骤取序号最小的一步。statusTemplateId 与 Java 一样被接收但不参与启用。"""
    del status_template_id
    sdlc = await _require_sdlc(session, sdlc_id)
    if sdlc.status == "ENABLED":
        raise BizError(ErrorCode.SDLC_ALREADY_ENABLED)
    steps = await _steps(session, sdlc_id)
    if len(steps) == 0:
        raise BizError(ErrorCode.SDLC_ENABLE_NO_STEPS)
    updated = rowcount(
        await session.execute(
            update(Sdlc)
            .where(
                Sdlc.id == sdlc_id,
                Sdlc.tenant_id == tenant_id,
                Sdlc.version == sdlc.version,
                Sdlc.is_deleted == 0,
            )
            .values(
                status="ENABLED",
                entry_step_id=steps[0].id,
                version=Sdlc.version + 1,
                modifier_id=user_id,
            )
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.SDLC_VERSION_CONFLICT)
    session.expire_all()
    view = await get_sdlc(session, sdlc_id)
    await session.commit()
    return view


async def disable_sdlc(
    session: AsyncSession,
    sdlc_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """停用已启用的流程，并清空入口步骤。"""
    sdlc = await _require_sdlc(session, sdlc_id)
    if sdlc.status != "ENABLED":
        raise BizError(ErrorCode.SDLC_NOT_ENABLED)
    updated = rowcount(
        await session.execute(
            update(Sdlc)
            .where(
                Sdlc.id == sdlc_id,
                Sdlc.tenant_id == tenant_id,
                Sdlc.version == sdlc.version,
                Sdlc.is_deleted == 0,
            )
            .values(
                status="DISABLED",
                entry_step_id=None,
                version=Sdlc.version + 1,
                modifier_id=user_id,
            )
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.SDLC_VERSION_CONFLICT)
    await session.commit()


def _require_editable(status: str) -> None:
    if status != "DRAFT" and status != "DISABLED" and status != "ENABLED":
        raise BizError(ErrorCode.SDLC_NOT_DRAFT)


def _require_content_editable(status: str) -> None:
    if status != "DRAFT" and status != "DISABLED" and status != "ENABLED" and status != "ACTIVE":
        raise BizError(ErrorCode.SDLC_NOT_DRAFT)


def _to_view(sdlc: Sdlc, steps: list[SdlcStep] | None) -> SdlcView:
    view = SdlcView(
        id=sdlc.id,
        name=sdlc.name,
        description=sdlc.description,
        work_type=sdlc.work_type,
        status=sdlc.status,
        is_default=sdlc.is_default,
        entry_step_id=sdlc.entry_step_id,
        version=sdlc.version,
        gmt_create=sdlc.gmt_create,
    )
    if steps is not None:
        view.steps = [_to_step(step) for step in steps]
        view.step_count = len(steps)
    return view


def _to_step(step: SdlcStep) -> StepView:
    required = True
    if step.required is not None and step.required != 1:
        required = False
    return StepView(
        id=step.id,
        sdlc_id=step.sdlc_id,
        step_order=step.step_order,
        name=step.name,
        kind=step.kind,
        instruction_md=step.instruction_md,
        checklist_json=_json_text(step.checklist_json),
        gate_policy_json=_json_text(step.gate_policy_json),
        required=required,
        timeout_seconds=step.timeout_seconds,
        retry_budget=step.retry_budget,
        code=step.code,
        handler_type=step.handler_type,
        handler_role_ref=step.handler_role_ref,
        status_on_enter_code=step.status_on_enter_code,
        on_success=_json_text(step.on_success),
        on_fail=_json_text(step.on_fail),
    )


def _json_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_document(value: str | None) -> object | None:
    if value is None:
        return None
    return json.loads(value)


def _list_statement(
    tenant_id: int,
    work_type: str | None,
    status: str | None,
    squad_ids: list[int] | None,
) -> Any:
    statement = select(Sdlc).where(Sdlc.is_deleted == 0)
    if work_type is not None:
        statement = statement.where(Sdlc.work_type == work_type)
    if status is not None:
        statement = statement.where(Sdlc.status == status)
    if squad_ids is not None and len(squad_ids) > 0:
        linked = (
            select(AgentVersion.sdlc_id)
            .join(
                Agent,
                and_(Agent.online_version_id == AgentVersion.id, Agent.is_deleted == 0),
            )
            .join(SquadMember, SquadMember.agent_id == Agent.id)
            .join(Squad, and_(Squad.id == SquadMember.squad_id, Squad.is_deleted == 0))
            .where(
                AgentVersion.is_deleted == 0,
                AgentVersion.sdlc_id.is_not(None),
                AgentVersion.tenant_id == tenant_id,
                Agent.tenant_id == tenant_id,
                SquadMember.tenant_id == tenant_id,
                SquadMember.squad_id.in_(squad_ids),
            )
        )
        statement = statement.where(Sdlc.id.in_(linked))
    return statement.order_by(Sdlc.id.desc())


async def _fill_step_counts(session: AsyncSession, views: list[SdlcView]) -> None:
    if len(views) == 0:
        return
    ids = [view.id for view in views if view.id is not None]
    counted = await session.execute(
        select(SdlcStep.sdlc_id, func.count())
        .where(SdlcStep.is_deleted == 0, SdlcStep.sdlc_id.in_(ids))
        .group_by(SdlcStep.sdlc_id)
    )
    count_by_id = {sdlc_id: int(count) for sdlc_id, count in counted.all()}
    for view in views:
        count = count_by_id.get(view.id)
        if count is None:
            view.step_count = 0
        else:
            view.step_count = count


async def _fill_squads(session: AsyncSession, tenant_id: int, views: list[SdlcView]) -> None:
    refs = await refs_by_sdlc_ids(
        session,
        tenant_id,
        [view.id for view in views],
    )
    for view in views:
        ref = None
        if view.id is not None:
            ref = refs.get(view.id)
        if ref is None:
            view.squad_ids = []
            view.squad_names = []
        else:
            view.squad_ids = ref.ids
            view.squad_names = ref.names


async def _insert_step(session: AsyncSession, step: SdlcStep, sdlc_id: int) -> SdlcStep:
    fields = _step_fields(step)
    attempt = 1
    current = step
    while True:
        session.add(current)
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            if not _duplicate_key(error) or attempt >= _ORDER_RETRY_LIMIT:
                if _duplicate_key(error):
                    raise BizError(ErrorCode.SDLC_STEP_ORDER_DUPLICATE) from error
                raise
            attempt += 1
            fields["step_order"] = await _next_step_order(session, sdlc_id)
            current = SdlcStep(**fields)
        else:
            return current


def _step_fields(step: SdlcStep) -> dict[str, Any]:
    return {
        "tenant_id": step.tenant_id,
        "sdlc_id": step.sdlc_id,
        "step_order": step.step_order,
        "name": step.name,
        "kind": step.kind,
        "instruction_md": step.instruction_md,
        "checklist_json": step.checklist_json,
        "gate_policy_json": step.gate_policy_json,
        "required": step.required,
        "timeout_seconds": step.timeout_seconds,
        "retry_budget": step.retry_budget,
        "code": step.code,
        "handler_type": step.handler_type,
        "handler_role_ref": step.handler_role_ref,
        "status_on_enter_code": step.status_on_enter_code,
        "on_success": step.on_success,
        "on_fail": step.on_fail,
        "creator_id": step.creator_id,
        "is_deleted": 0,
    }


async def _renumber_steps(
    session: AsyncSession,
    sdlc_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    remaining = await _steps(session, sdlc_id)
    for index, step in enumerate(remaining):
        target = index + 1
        if step.step_order != target:
            await _set_order(session, step.id, tenant_id, target, user_id)


async def _set_order(
    session: AsyncSession,
    step_id: int,
    tenant_id: int,
    step_order: int,
    user_id: int,
) -> None:
    await session.execute(
        update(SdlcStep)
        .where(SdlcStep.id == step_id, SdlcStep.tenant_id == tenant_id, SdlcStep.is_deleted == 0)
        .values(step_order=step_order, modifier_id=user_id)
    )


async def _next_step_order(session: AsyncSession, sdlc_id: int) -> int:
    steps = await _steps(session, sdlc_id)
    maximum = 0
    for step in steps:
        if step.step_order > maximum:
            maximum = step.step_order
    return maximum + 1


async def _require_sdlc(session: AsyncSession, sdlc_id: int) -> Sdlc:
    sdlc = await session.scalar(
        select(Sdlc).where(Sdlc.id == sdlc_id, Sdlc.is_deleted == 0).limit(1)
    )
    if sdlc is None:
        raise BizError(ErrorCode.SDLC_NOT_FOUND)
    return sdlc


async def _find_step(session: AsyncSession, step_id: int) -> SdlcStep | None:
    return await session.scalar(
        select(SdlcStep).where(SdlcStep.id == step_id, SdlcStep.is_deleted == 0).limit(1)
    )


async def _steps(session: AsyncSession, sdlc_id: int) -> list[SdlcStep]:
    rows = await session.scalars(
        select(SdlcStep)
        .where(SdlcStep.sdlc_id == sdlc_id, SdlcStep.is_deleted == 0)
        .order_by(SdlcStep.step_order.asc())
    )
    return list(rows)


async def _workitem_count(session: AsyncSession, sdlc_id: int) -> int:
    counted = await session.scalar(
        select(func.count())
        .select_from(Workitem)
        .where(Workitem.sdlc_id == sdlc_id, Workitem.is_deleted == 0)
    )
    if counted is None:
        return 0
    return int(counted)


async def _workitem_ids(session: AsyncSession, sdlc_id: int, limit: int) -> list[int]:
    rows = await session.scalars(
        select(Workitem.id)
        .where(Workitem.sdlc_id == sdlc_id, Workitem.is_deleted == 0)
        .order_by(Workitem.id.asc())
        .limit(limit)
    )
    return list(rows)


async def _agent_ids_using_sdlc(session: AsyncSession, sdlc_id: int) -> list[int]:
    rows = await session.scalars(
        select(Agent.id)
        .join(
            AgentVersion,
            and_(
                AgentVersion.id == Agent.online_version_id,
                AgentVersion.agent_id == Agent.id,
            ),
        )
        .where(
            AgentVersion.sdlc_id == sdlc_id,
            AgentVersion.is_deleted == 0,
            Agent.is_deleted == 0,
        )
        .distinct()
        .order_by(Agent.id.asc())
        .limit(_IN_USE_SAMPLE)
    )
    return list(rows)


async def _agent_labels(session: AsyncSession, agent_ids: list[int]) -> list[str]:
    labels: list[str] = []
    for agent_id in agent_ids:
        agent = await session.scalar(
            select(Agent).where(Agent.id == agent_id, Agent.is_deleted == 0).limit(1)
        )
        if agent is not None and agent.name.strip() != "":
            labels.append(f"{agent.name}(ID:{agent_id})")
        else:
            labels.append(f"ID:{agent_id}")
    return labels


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None or not origin.args:
        return False
    return origin.args[0] == 1062
