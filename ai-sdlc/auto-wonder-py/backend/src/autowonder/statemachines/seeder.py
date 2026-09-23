"""新工作空间的 REQ/TASK/BUG 默认状态模版。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.statemachines.models import StatusNode, StatusTemplate, StatusTransition

NodeSpec = tuple[str, str, str]

REQ_NODES: tuple[NodeSpec, ...] = (
    ("new", "新建", "INIT"),
    ("developing", "开发中", "IN_PROGRESS"),
    ("verifying", "验证中", "IN_PROGRESS"),
    ("released", "已发布", "DONE"),
    ("canceled", "已取消", "CANCELED"),
)
TASK_NODES: tuple[NodeSpec, ...] = (
    ("todo", "待办", "INIT"),
    ("doing", "进行中", "IN_PROGRESS"),
    ("done", "已完成", "DONE"),
)
BUG_NODES: tuple[NodeSpec, ...] = (
    ("open", "待修复", "INIT"),
    ("fixing", "修复中", "IN_PROGRESS"),
    ("verifying", "验证中", "IN_PROGRESS"),
    ("closed", "已关闭", "DONE"),
)


async def seed(session: AsyncSession, tenant_id: int, creator_id: int) -> None:
    """播种三套默认流程，含线性前进边；需求流程额外有取消边。"""
    await _seed_template(session, tenant_id, creator_id, "REQ", "需求默认流程", REQ_NODES, True)
    await _seed_template(session, tenant_id, creator_id, "TASK", "任务默认流程", TASK_NODES, False)
    await _seed_template(session, tenant_id, creator_id, "BUG", "缺陷默认流程", BUG_NODES, False)


async def _seed_template(
    session: AsyncSession,
    tenant_id: int,
    creator_id: int,
    work_type: str,
    name: str,
    nodes: tuple[NodeSpec, ...],
    with_cancel: bool,
) -> None:
    template = StatusTemplate(
        tenant_id=tenant_id,
        work_type=work_type,
        name=name,
        is_default=1,
        creator_id=creator_id,
        is_deleted=0,
        version=0,
    )
    session.add(template)
    await session.flush()
    node_ids: list[int] = []
    cancel_node_id: int | None = None
    for index, (code, node_name, category) in enumerate(nodes):
        node = StatusNode(
            tenant_id=tenant_id,
            template_id=template.id,
            code=code,
            name=node_name,
            category=category,
            sort=index,
        )
        session.add(node)
        await session.flush()
        node_ids.append(node.id)
        if category == "CANCELED":
            cancel_node_id = node.id
    previous: int | None = None
    for index, (_code, _node_name, category) in enumerate(nodes):
        if category == "CANCELED":
            continue
        if previous is not None:
            await _insert_transition(
                session,
                tenant_id,
                template.id,
                node_ids[previous],
                node_ids[index],
                "前进",
            )
        previous = index
    if with_cancel and cancel_node_id is not None:
        for index, (_code, _node_name, category) in enumerate(nodes):
            if category == "INIT" or category == "IN_PROGRESS":
                await _insert_transition(
                    session,
                    tenant_id,
                    template.id,
                    node_ids[index],
                    cancel_node_id,
                    "取消",
                )


async def _insert_transition(
    session: AsyncSession,
    tenant_id: int,
    template_id: int,
    from_node_id: int,
    to_node_id: int,
    name: str,
) -> None:
    session.add(
        StatusTransition(
            tenant_id=tenant_id,
            template_id=template_id,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            name=name,
        )
    )
    await session.flush()
