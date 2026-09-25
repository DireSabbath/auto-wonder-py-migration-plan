"""执行器的创建、列表、令牌和删除。"""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.core.errors import BizError, ErrorCode
from autowonder.executors.catalog import catalog_names
from autowonder.executors.issue import issue_executor_token
from autowonder.executors.launch import resolve_for_create
from autowonder.executors.models import Executor
from autowonder.executors.options import provider_for_client_kind
from autowonder.executors.presence import (
    current_features,
    current_model,
    current_version,
    executor_online,
    mark_deleted,
    publish,
    unregister,
)
from autowonder.executors.registry import drop_session
from autowonder.executors.restart import restart_status
from autowonder.executors.schemas import CreateExecutorRequest, ExecutorView, IssuedExecutorView
from autowonder.executors.store import list_executors, require_executor
from autowonder.executors.tokens import resolve
from autowonder.executors.updates import runtime_auto_update_view
from autowonder.executors.upgrade import latest_updates
from autowonder.executors.version import compare_versions
from autowonder.squads.models import Squad, SquadMember
from autowonder.ws.session import session_registry

logger = logging.getLogger(__name__)

_FEATURE_RESTART = "EXECUTOR_RESTART_V1"
_FEATURE_UPDATE_RESTART = "EXECUTOR_UPDATE_RESTART_V1"
_FEATURE_UPGRADE = "EXECUTOR_UPDATE_V1"


async def create_executor(
    session: AsyncSession,
    agent_id: int,
    request: CreateExecutorRequest,
    tenant_id: int,
    user_id: int,
) -> IssuedExecutorView:
    """创建离线执行器，并在同一事务里签发可回显令牌。"""
    if request.name is None or request.name.strip() == "":
        raise BizError(ErrorCode.EXECUTOR_NAME_REQUIRED)
    client_kind, launch_config = await resolve_for_create(request)
    executor = Executor(
        tenant_id=tenant_id,
        agent_id=agent_id,
        name=request.name.strip(),
        status="OFFLINE",
        client_kind=client_kind,
        creator_id=user_id,
        launch_config=launch_config.as_json(),
        config_version=1,
        is_deleted=0,
    )
    session.add(executor)
    await session.flush()
    plaintext, token_ref = issue_executor_token(executor.id)
    executor.token_ref = token_ref
    await session.commit()
    logger.info("executor registered id=%s agentId=%s", executor.id, agent_id)
    return IssuedExecutorView(
        id=executor.id,
        agent_id=agent_id,
        name=executor.name,
        token=plaintext,
        client_kind=client_kind,
        memory_mode=launch_config.memory_mode,
        max_concurrent_dispatches=launch_config.max_concurrent_dispatches,
        model=launch_config.model,
        reasoning_effort=launch_config.reasoning_effort,
        context_window=launch_config.context_window,
        config_version=1,
    )


async def list_by_agent(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
) -> list[ExecutorView]:
    """列出一个数字员工下的执行器，并补上小队、模型名和升级任务。"""
    executors = await list_executors(session, tenant_id, agent_id, None)
    return await _views(session, tenant_id, executors)


async def list_all(
    session: AsyncSession,
    tenant_id: int,
    squad_ids: list[int] | None,
) -> list[ExecutorView]:
    """列出工作空间里的执行器。squad_ids 非空时只保留这些小队的数字员工。"""
    executors = await list_executors(session, tenant_id, None, squad_ids)
    return await _views(session, tenant_id, executors)


async def executor_detail(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
) -> ExecutorView:
    """读取一台执行器。生成启动命令时用来拿到客户端类型。"""
    executor = await require_executor(session, executor_id, tenant_id)
    view = await _to_view(executor, None)
    await _fill_model_names([view])
    await _fill_updates(session, tenant_id, [view])
    return view


async def executor_token(session: AsyncSession, executor_id: int, tenant_id: int) -> str:
    """回显 b64 令牌。哈希引用没有可还原的明文。"""
    executor = await require_executor(session, executor_id, tenant_id)
    plaintext = resolve(executor.token_ref)
    if plaintext is None or plaintext.strip() == "":
        raise BizError(ErrorCode.EXECUTOR_TOKEN_NOT_RETRIEVABLE)
    return plaintext


async def delete_executor(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """软删除执行器，写下墓碑，并通知持有连接的节点关闭会话。"""
    executor = await require_executor(session, executor_id, tenant_id)
    await session.execute(
        update(Executor)
        .where(
            Executor.id == executor_id,
            Executor.tenant_id == tenant_id,
            Executor.is_deleted == 0,
        )
        .values(is_deleted=1, modifier_id=user_id)
    )
    await mark_deleted(executor_id)
    await unregister(executor_id, executor.agent_id)
    drop_session(executor_id)
    await _close_local_session(executor_id)
    await publish({"type": "SESSION_CLOSE", "executorId": executor_id})
    await session.commit()
    logger.info("executor deleted id=%s agentId=%s", executor_id, executor.agent_id)


async def _close_local_session(executor_id: int) -> None:
    current = session_registry.find_by_executor_id(executor_id)
    if current is None or not current.is_open():
        return
    try:
        await current.websocket.close()
    except Exception:
        logger.warning(
            "failed to close local session for deleted executor %s",
            executor_id,
            exc_info=True,
        )
        return
    logger.info("closed local WS session for deleted executor %s", executor_id)


async def _views(
    session: AsyncSession,
    tenant_id: int,
    executors: list[Executor],
) -> list[ExecutorView]:
    names = await _agent_names(session, tenant_id, [item.agent_id for item in executors])
    views = [await _to_view(item, names.get(item.agent_id)) for item in executors]
    await _fill_squads(session, tenant_id, views)
    await _fill_model_names(views)
    await _fill_updates(session, tenant_id, views)
    return views


async def _to_view(executor: Executor, agent_name: str | None) -> ExecutorView:
    version = await current_version(executor.id)
    model = await current_model(executor.id)
    features = await current_features(executor.id)
    target = runtime_auto_update_view().target_version
    comparison = compare_versions(version, target)
    status = "OFFLINE"
    if await executor_online(executor.id):
        status = "ONLINE"
    return ExecutorView(
        id=executor.id,
        agent_id=executor.agent_id,
        agent_name=agent_name,
        name=executor.name,
        status=status,
        client_kind=executor.client_kind,
        last_connect_ip=executor.last_connect_ip,
        last_heartbeat=executor.last_heartbeat,
        last_started_at=executor.last_started_at,
        version=version,
        model=model,
        gmt_create=executor.gmt_create,
        restart_supported=_FEATURE_RESTART in features,
        update_restart_supported=_FEATURE_UPDATE_RESTART in features,
        restart=await restart_status(executor.id),
        upgrade_supported=_FEATURE_UPGRADE in features,
        version_comparable=comparison is not None,
        upgrade_available=comparison is not None and comparison < 0,
        target_version=target,
    )


async def _agent_names(
    session: AsyncSession,
    tenant_id: int,
    agent_ids: list[int],
) -> dict[int, str]:
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(Agent.id, Agent.name).where(
            Agent.id.in_(agent_ids),
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
        )
    )
    return {row.id: row.name for row in rows}


async def _fill_squads(
    session: AsyncSession,
    tenant_id: int,
    views: list[ExecutorView],
) -> None:
    agent_ids = list(dict.fromkeys(view.agent_id for view in views))
    squad_ids_by_agent: dict[int, list[int]] = {}
    all_squad_ids: list[int] = []
    if agent_ids:
        rows = await session.execute(
            select(SquadMember.agent_id, SquadMember.squad_id)
            .where(
                SquadMember.tenant_id == tenant_id,
                SquadMember.agent_id.in_(agent_ids),
            )
            .order_by(SquadMember.agent_id, SquadMember.squad_id)
        )
        for agent_id, squad_id in rows:
            bucket = squad_ids_by_agent.setdefault(agent_id, [])
            if squad_id in bucket:
                continue
            bucket.append(squad_id)
            if squad_id not in all_squad_ids:
                all_squad_ids.append(squad_id)
    names: dict[int, str] = {}
    if all_squad_ids:
        squad_rows = await session.execute(
            select(Squad.id, Squad.name).where(Squad.is_deleted == 0, Squad.id.in_(all_squad_ids))
        )
        names = {row.id: row.name for row in squad_rows}
    for view in views:
        ids: list[int] = []
        labels: list[str] = []
        for squad_id in squad_ids_by_agent.get(view.agent_id, []):
            name = names.get(squad_id)
            if name is None:
                continue
            ids.append(squad_id)
            labels.append(name)
        view.squad_ids = ids
        view.squad_names = labels


async def _fill_model_names(views: list[ExecutorView]) -> None:
    names_by_provider: dict[str, dict[str, str]] = {}
    for view in views:
        if view.model is None or view.model.strip() == "":
            continue
        provider = provider_for_client_kind(view.client_kind)
        if provider is None:
            continue
        if provider not in names_by_provider:
            names_by_provider[provider] = await catalog_names(provider)
        name = names_by_provider[provider].get(view.model)
        if name is not None:
            view.model_name = name


async def _fill_updates(
    session: AsyncSession,
    tenant_id: int,
    views: list[ExecutorView],
) -> None:
    if not views:
        return
    updates = await latest_updates(session, tenant_id, [view.id for view in views])
    for view in views:
        view.update = updates.get(view.id)
