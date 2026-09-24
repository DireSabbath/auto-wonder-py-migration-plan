"""状态模板、节点和流转。查询与删除占用说明对齐 StatusTemplateService。"""

from typing import cast

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.statemachines.models import StatusNode, StatusTemplate, StatusTransition
from autowonder.statemachines.schemas import (
    CreateNodeRequest,
    CreateTemplateRequest,
    CreateTransitionRequest,
    NodeView,
    TemplateDetailView,
    TemplateView,
    TransitionView,
    UpdateNodeRequest,
    UpdateTemplateRequest,
    UpdateTransitionRequest,
)
from autowonder.workitems.models import Workitem


def require_present(value: str | None, missing: ErrorCode) -> str:
    """创建时的文本字段不能是空白。"""
    if value is None or value.strip() == "":
        raise BizError(missing)
    return value.strip()


def kept_required_text(requested: str | None, current: str) -> str:
    """更新时省略文本则保留原值，出现过的文本去掉首尾空白。"""
    if requested is not None:
        return requested.strip()
    return current


def kept_optional_text(requested: str | None, current: str | None) -> str | None:
    """更新可空文本。省略保留原值，出现过则去掉首尾空白。"""
    if requested is not None:
        return requested.strip()
    return current


def kept_int(requested: int | None, current: int) -> int:
    """省略整数时保留原值。"""
    if requested is not None:
        return requested
    return current


def node_sort(requested: int | None) -> int:
    """新建节点未给序号时用 0。"""
    if requested is None:
        return 0
    return requested


def apply_default_flag(requested: bool | None, current: int) -> tuple[bool, int]:
    """只有显式 true 才清掉同类型默认并写入 1。"""
    if requested is True:
        return True, 1
    return False, current


async def list_templates(
    session: AsyncSession,
    tenant_id: int,
    work_type: str,
) -> list[TemplateView]:
    """默认模板排在前面，其余按创建时间。"""
    rows = await session.scalars(
        select(StatusTemplate)
        .where(
            StatusTemplate.tenant_id == tenant_id,
            StatusTemplate.work_type == work_type,
            StatusTemplate.is_deleted == 0,
        )
        .order_by(StatusTemplate.is_default.desc(), StatusTemplate.gmt_create.asc())
    )
    return [_to_template(item) for item in rows]


async def get_template(session: AsyncSession, template_id: int) -> TemplateDetailView:
    """模板详情包含节点和流转。"""
    template = await _require_template(session, template_id)
    detail = TemplateDetailView(
        id=template.id,
        work_type=template.work_type,
        name=template.name,
        is_default=_is_default(template.is_default),
        gmt_create=template.gmt_create,
        gmt_modified=template.gmt_modified,
        nodes=await list_nodes(session, template_id),
        transitions=await list_transitions(session, template_id),
    )
    return detail


async def create_template(
    session: AsyncSession,
    request: CreateTemplateRequest,
    tenant_id: int,
    user_id: int,
) -> TemplateView:
    """插入非默认模板。插入语句不回读创建时间。"""
    template = StatusTemplate(
        tenant_id=tenant_id,
        work_type=require_present(request.work_type, ErrorCode.STATUS_TEMPLATE_WORK_TYPE_REQUIRED),
        name=require_present(request.name, ErrorCode.STATUS_TEMPLATE_NAME_REQUIRED),
        is_default=0,
        creator_id=user_id,
        is_deleted=0,
        version=0,
    )
    session.add(template)
    await session.flush()
    view = _to_template(template)
    view.gmt_create = None
    view.gmt_modified = None
    await session.commit()
    return view


async def update_template(
    session: AsyncSession,
    template_id: int,
    request: UpdateTemplateRequest,
    tenant_id: int,
    user_id: int,
) -> TemplateView:
    """名称出现时覆盖。设为默认时先清掉同工单类型的其他默认。"""
    template = await _require_template(session, template_id)
    name = kept_required_text(request.name, template.name)
    clear_default, is_default = apply_default_flag(request.is_default, template.is_default)
    if clear_default:
        await session.execute(
            update(StatusTemplate)
            .where(
                StatusTemplate.tenant_id == tenant_id,
                StatusTemplate.work_type == template.work_type,
                StatusTemplate.is_default == 1,
                StatusTemplate.is_deleted == 0,
            )
            .values(is_default=0, gmt_modified=func.now())
        )
    updated = rowcount(
        await session.execute(
            update(StatusTemplate)
            .where(
                StatusTemplate.id == template_id,
                StatusTemplate.tenant_id == tenant_id,
                StatusTemplate.version == template.version,
                StatusTemplate.is_deleted == 0,
            )
            .values(
                name=name,
                is_default=is_default,
                modifier_id=user_id,
                version=StatusTemplate.version + 1,
                gmt_modified=func.now(),
            )
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.STATUS_TEMPLATE_VERSION_CONFLICT)
    await session.commit()
    session.expire_all()
    stored = await _find_template(session, template_id)
    if stored is None:
        raise BizError(ErrorCode.STATUS_TEMPLATE_NOT_FOUND)
    return _to_template(stored)


async def delete_template(session: AsyncSession, template_id: int, tenant_id: int) -> None:
    """仍有工单停在节点上时不能删。删除时先清流转和节点。"""
    template = await _require_template(session, template_id)
    nodes = await _nodes(session, template_id)
    for node in nodes:
        if await _workitems_using_node(session, node.id) > 0:
            raise BizError(ErrorCode.STATUS_TEMPLATE_DELETE_IN_USE)
    await session.execute(
        sql_delete(StatusTransition).where(StatusTransition.template_id == template_id)
    )
    for node in nodes:
        await session.execute(sql_delete(StatusNode).where(StatusNode.id == node.id))
    await session.execute(
        update(StatusTemplate)
        .where(
            StatusTemplate.id == template_id,
            StatusTemplate.tenant_id == tenant_id,
            StatusTemplate.version == template.version,
            StatusTemplate.is_deleted == 0,
        )
        .values(is_deleted=1, version=StatusTemplate.version + 1)
    )
    await session.commit()


async def list_nodes(session: AsyncSession, template_id: int) -> list[NodeView]:
    """按序号列出节点。"""
    return [_to_node(node) for node in await _nodes(session, template_id)]


async def create_node(
    session: AsyncSession,
    template_id: int,
    request: CreateNodeRequest,
    tenant_id: int,
) -> NodeView:
    """同一模板下编码不能重复。"""
    await _require_template(session, template_id)
    code = require_present(request.code, ErrorCode.STATUS_NODE_CODE_REQUIRED)
    name = require_present(request.name, ErrorCode.STATUS_NODE_NAME_REQUIRED)
    category = require_present(request.category, ErrorCode.STATUS_NODE_CATEGORY_REQUIRED)
    if await _find_node_by_code(session, template_id, code) is not None:
        raise BizError(ErrorCode.STATUS_NODE_CODE_DUPLICATE)
    node = StatusNode(
        tenant_id=tenant_id,
        template_id=template_id,
        code=code,
        name=name,
        category=category,
        sort=node_sort(request.sort),
    )
    session.add(node)
    await session.flush()
    view = _to_node(node)
    view.gmt_create = None
    await session.commit()
    return view


async def update_node(
    session: AsyncSession,
    node_id: int,
    request: UpdateNodeRequest,
) -> NodeView:
    """编码变化时检查同模板是否已有该编码。"""
    node = await _require_node(session, node_id)
    code = kept_required_text(request.code, node.code)
    name = kept_required_text(request.name, node.name)
    category = kept_required_text(request.category, node.category)
    sort = kept_int(request.sort, node.sort)
    if code != node.code:
        existing = await _find_node_by_code(session, node.template_id, code)
        if existing is not None:
            raise BizError(ErrorCode.STATUS_NODE_CODE_DUPLICATE)
    await session.execute(
        update(StatusNode)
        .where(StatusNode.id == node_id)
        .values(code=code, name=name, category=category, sort=sort)
    )
    await session.commit()
    session.expire_all()
    stored = await _find_node(session, node_id)
    if stored is None:
        raise BizError(ErrorCode.STATUS_NODE_NOT_FOUND)
    return _to_node(stored)


async def delete_node(session: AsyncSession, node_id: int) -> None:
    """有工单停在该节点上时不能删。"""
    await _require_node(session, node_id)
    if await _workitems_using_node(session, node_id) > 0:
        raise BizError(ErrorCode.STATUS_NODE_DELETE_IN_USE)
    await session.execute(sql_delete(StatusNode).where(StatusNode.id == node_id))
    await session.commit()


async def list_transitions(session: AsyncSession, template_id: int) -> list[TransitionView]:
    """按创建时间列出流转。"""
    rows = await session.scalars(
        select(StatusTransition)
        .where(StatusTransition.template_id == template_id)
        .order_by(StatusTransition.gmt_create.asc())
    )
    return [_to_transition(item) for item in rows]


async def create_transition(
    session: AsyncSession,
    template_id: int,
    request: CreateTransitionRequest,
    tenant_id: int,
) -> TransitionView:
    """同一对起止节点只能有一条流转。"""
    await _require_template(session, template_id)
    name = require_present(request.name, ErrorCode.STATUS_TRANSITION_NAME_REQUIRED)
    existing = await _find_transition_edge(
        session,
        template_id,
        request.from_node_id,
        request.to_node_id,
    )
    if existing is not None:
        raise BizError(ErrorCode.STATUS_TRANSITION_DUPLICATE)
    transition = StatusTransition(
        tenant_id=tenant_id,
        template_id=template_id,
        from_node_id=request.from_node_id,
        to_node_id=request.to_node_id,
        name=name,
    )
    session.add(transition)
    await session.flush()
    view = _to_transition(transition)
    view.gmt_create = None
    await session.commit()
    return view


async def update_transition(
    session: AsyncSession,
    transition_id: int,
    request: UpdateTransitionRequest,
) -> TransitionView:
    """按出现的字段覆盖起止节点和名称。"""
    transition = await _require_transition(session, transition_id)
    from_node_id = kept_int(request.from_node_id, transition.from_node_id)
    to_node_id = kept_int(request.to_node_id, transition.to_node_id)
    name = kept_optional_text(request.name, transition.name)
    await session.execute(
        update(StatusTransition)
        .where(StatusTransition.id == transition_id)
        .values(from_node_id=from_node_id, to_node_id=to_node_id, name=name)
    )
    await session.commit()
    session.expire_all()
    stored = await _find_transition(session, transition_id)
    if stored is None:
        raise BizError(ErrorCode.STATUS_TRANSITION_NOT_FOUND)
    return _to_transition(stored)


async def delete_transition(session: AsyncSession, transition_id: int) -> None:
    """物理删除一条流转。"""
    await _require_transition(session, transition_id)
    await session.execute(sql_delete(StatusTransition).where(StatusTransition.id == transition_id))
    await session.commit()


def _is_default(value: int | None) -> bool:
    return value == 1


def _to_template(template: StatusTemplate) -> TemplateView:
    return TemplateView(
        id=template.id,
        work_type=template.work_type,
        name=template.name,
        is_default=_is_default(template.is_default),
        gmt_create=template.gmt_create,
        gmt_modified=template.gmt_modified,
    )


def _to_node(node: StatusNode) -> NodeView:
    return NodeView(
        id=node.id,
        template_id=node.template_id,
        code=node.code,
        name=node.name,
        category=node.category,
        sort=node.sort,
        gmt_create=node.gmt_create,
    )


def _to_transition(transition: StatusTransition) -> TransitionView:
    return TransitionView(
        id=transition.id,
        template_id=transition.template_id,
        from_node_id=transition.from_node_id,
        to_node_id=transition.to_node_id,
        name=transition.name,
        gmt_create=transition.gmt_create,
    )


async def _nodes(session: AsyncSession, template_id: int) -> list[StatusNode]:
    rows = await session.scalars(
        select(StatusNode)
        .where(StatusNode.template_id == template_id)
        .order_by(StatusNode.sort.asc())
    )
    return list(rows)


async def _require_template(session: AsyncSession, template_id: int) -> StatusTemplate:
    template = await _find_template(session, template_id)
    if template is None:
        raise BizError(ErrorCode.STATUS_TEMPLATE_NOT_FOUND)
    return template


async def _find_template(session: AsyncSession, template_id: int) -> StatusTemplate | None:
    return await session.scalar(
        select(StatusTemplate)
        .where(StatusTemplate.id == template_id, StatusTemplate.is_deleted == 0)
        .limit(1)
    )


async def _require_node(session: AsyncSession, node_id: int) -> StatusNode:
    node = await _find_node(session, node_id)
    if node is None:
        raise BizError(ErrorCode.STATUS_NODE_NOT_FOUND)
    return node


async def _find_node(session: AsyncSession, node_id: int) -> StatusNode | None:
    return await session.scalar(select(StatusNode).where(StatusNode.id == node_id).limit(1))


async def _find_node_by_code(
    session: AsyncSession,
    template_id: int,
    code: str,
) -> StatusNode | None:
    return await session.scalar(
        select(StatusNode)
        .where(StatusNode.template_id == template_id, StatusNode.code == code)
        .limit(1)
    )


async def _require_transition(session: AsyncSession, transition_id: int) -> StatusTransition:
    transition = await _find_transition(session, transition_id)
    if transition is None:
        raise BizError(ErrorCode.STATUS_TRANSITION_NOT_FOUND)
    return transition


async def _find_transition(session: AsyncSession, transition_id: int) -> StatusTransition | None:
    return await session.scalar(
        select(StatusTransition).where(StatusTransition.id == transition_id).limit(1)
    )


async def _find_transition_edge(
    session: AsyncSession,
    template_id: int,
    from_node_id: int | None,
    to_node_id: int | None,
) -> StatusTransition | None:
    return await session.scalar(
        select(StatusTransition)
        .where(
            StatusTransition.template_id == template_id,
            StatusTransition.from_node_id == from_node_id,
            StatusTransition.to_node_id == to_node_id,
        )
        .limit(1)
    )


async def _workitems_using_node(session: AsyncSession, node_id: int) -> int:
    counted = await session.scalar(
        select(func.count())
        .select_from(Workitem)
        .where(Workitem.status_node_id == node_id, Workitem.is_deleted == 0)
    )
    return cast(int, counted)
