"""把 Aone 工作流状态落成工作空间状态模版、节点和映射。"""

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.integrations.aone_api import ExternalStatusOption, ExternalWorkitemDetail
from autowonder.integrations.models import ExternalProjectBinding, ExternalStatusMapping
from autowonder.statemachines.models import StatusNode, StatusTemplate, StatusTransition


async def ensure_statuses(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    work_type: str,
    issue_type_id: str,
    statuses: list[ExternalStatusOption],
    user_id: int,
) -> None:
    """为一种工作项类型补齐状态节点和映射。"""
    template = await _ensure_template(session, binding, work_type, user_id)
    sort = 0
    for status in statuses:
        if status.name is None or status.name.strip() == "":
            continue
        node = await _ensure_node(session, binding, template, status, sort)
        sort += 1
        await _ensure_mapping(session, binding, work_type, issue_type_id, status, node)
    await _ensure_transitions(session, binding.tenant_id, template.id)


async def ensure_status(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    detail: ExternalWorkitemDetail,
    operational: list[ExternalStatusOption],
    user_id: int,
) -> StatusNode | None:
    """确保当前状态有节点，并返回应落在工单上的节点。"""
    work_type = "TASK" if detail.work_type is None else detail.work_type
    template = await _ensure_template(session, binding, work_type, user_id)
    ordered = _ordered(detail, operational)
    selected: StatusNode | None = None
    sort = 0
    for status in ordered.values():
        node = await _ensure_node(session, binding, template, status, sort)
        sort += 1
        await _ensure_mapping(
            session,
            binding,
            work_type,
            detail.external_issue_type_id,
            status,
            node,
        )
        if status.name == detail.status_name:
            selected = node
    await _ensure_transitions(session, binding.tenant_id, template.id)
    if selected is not None:
        return selected
    return await session.scalar(
        select(StatusNode)
        .where(StatusNode.template_id == template.id, StatusNode.category == "INIT")
        .limit(1)
    )


async def _ensure_template(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    work_type: str,
    user_id: int,
) -> StatusTemplate:
    name = _template_name(binding, work_type)
    rows = await session.scalars(
        select(StatusTemplate).where(
            StatusTemplate.tenant_id == binding.tenant_id,
            StatusTemplate.work_type == work_type,
            StatusTemplate.is_deleted == 0,
        )
    )
    for existing in rows.all():
        if existing.name == name:
            return existing
    template = StatusTemplate(
        tenant_id=binding.tenant_id,
        work_type=work_type,
        name=name,
        is_default=0,
        creator_id=user_id,
        version=0,
    )
    session.add(template)
    await session.flush()
    return template


async def _ensure_node(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    template: StatusTemplate,
    status: ExternalStatusOption,
    sort: int,
) -> StatusNode:
    code = _status_code(status)
    existing = await session.scalar(
        select(StatusNode)
        .where(StatusNode.template_id == template.id, StatusNode.code == code)
        .limit(1)
    )
    if existing is not None:
        return existing
    node = StatusNode(
        tenant_id=binding.tenant_id,
        template_id=template.id,
        code=code,
        name=status.name or "",
        category=_category(status.name, sort),
        sort=sort,
    )
    session.add(node)
    await session.flush()
    return node


async def _ensure_mapping(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    work_type: str,
    issue_type_id: str | None,
    status: ExternalStatusOption,
    node: StatusNode,
) -> None:
    existing = await session.scalar(
        select(ExternalStatusMapping)
        .where(
            ExternalStatusMapping.tenant_id == binding.tenant_id,
            ExternalStatusMapping.provider == binding.provider,
            ExternalStatusMapping.binding_id == binding.id,
            ExternalStatusMapping.work_type == work_type,
            ExternalStatusMapping.external_status_name == status.name,
        )
        .limit(1)
    )
    if existing is not None:
        if (
            issue_type_id is not None
            and issue_type_id.strip() != ""
            and (
                existing.external_issue_type_id is None
                or existing.external_issue_type_id.strip() == ""
            )
        ):
            existing.external_issue_type_id = issue_type_id
            await session.flush()
        return
    session.add(
        ExternalStatusMapping(
            tenant_id=binding.tenant_id,
            provider=binding.provider,
            binding_id=binding.id,
            external_issue_type_id=issue_type_id,
            external_status_id=status.external_id,
            external_status_name=status.name or "",
            work_type=work_type,
            status_node_id=node.id,
            enabled=1,
        )
    )
    await session.flush()


async def _ensure_transitions(session: AsyncSession, tenant_id: int, template_id: int) -> None:
    nodes = list(
        await session.scalars(select(StatusNode).where(StatusNode.template_id == template_id))
    )
    existing_rows = await session.scalars(
        select(StatusTransition).where(StatusTransition.template_id == template_id)
    )
    existing = {str(row.from_node_id) + ":" + str(row.to_node_id) for row in existing_rows.all()}
    for source in nodes:
        for target in nodes:
            if source.id == target.id:
                continue
            key = str(source.id) + ":" + str(target.id)
            if key in existing:
                continue
            session.add(
                StatusTransition(
                    tenant_id=tenant_id,
                    template_id=template_id,
                    from_node_id=source.id,
                    to_node_id=target.id,
                    name=target.name,
                )
            )
            existing.add(key)
    await session.flush()


def _ordered(
    detail: ExternalWorkitemDetail,
    operational: list[ExternalStatusOption],
) -> dict[str, ExternalStatusOption]:
    result: dict[str, ExternalStatusOption] = {}
    if detail.status_name is not None and detail.status_name.strip() != "":
        result[detail.status_name] = ExternalStatusOption(detail.status_id, detail.status_name)
    for status in operational:
        if status.name is not None and status.name.strip() != "" and status.name not in result:
            result[status.name] = status
    return result


def _template_name(binding: ExternalProjectBinding, work_type: str) -> str:
    project = binding.external_project_id
    if binding.external_project_name is not None and binding.external_project_name.strip() != "":
        project = binding.external_project_name
    return binding.provider + " " + project + " " + work_type + " 状态"


def _status_code(status: ExternalStatusOption) -> str:
    if status.external_id is not None and status.external_id.strip() != "":
        return "aone_" + status.external_id
    digest = hashlib.sha1((status.name or "").encode()).hexdigest()
    return "aone_" + digest[:16]


def _category(name: str | None, sort: int) -> str:
    if sort == 0 or _contains(name, "待处理", "open", "new"):
        return "INIT"
    if _contains(name, "已完成", "完成", "fixed", "done", "closed"):
        return "DONE"
    if _contains(name, "取消", "cancel", "won'tfix", "invalid", "duplicate"):
        return "CANCELED"
    return "IN_PROGRESS"


def _contains(text: str | None, *needles: str) -> bool:
    lowered = "" if text is None else text.lower()
    for needle in needles:
        if needle.lower() in lowered:
            return True
    return False
