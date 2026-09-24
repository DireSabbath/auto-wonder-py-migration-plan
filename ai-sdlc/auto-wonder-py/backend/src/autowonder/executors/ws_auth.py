"""执行器 WebSocket 与 daemon 拉取共用的令牌校验。"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.executors.models import Executor
from autowonder.executors.tokens import validate

logger = logging.getLogger(__name__)


@dataclass
class ExecutorAuth:
    """一次执行器身份。失败时编号保持 0。"""

    success: bool
    executor_id: int
    agent_id: int
    tenant_id: int


async def authenticate_executor(
    session: AsyncSession,
    executor_id: int,
    plain_token: str | None,
) -> ExecutorAuth:
    """按执行器主键核对令牌。已删除或不存在时不再比对令牌。"""
    executor = await session.scalar(select(Executor).where(Executor.id == executor_id).limit(1))
    if executor is None or executor.is_deleted != 0:
        logger.info("ws auth failed executorId=%s reason=not_found_or_deleted", executor_id)
        return ExecutorAuth(False, 0, 0, 0)
    if not validate(executor.token_ref, plain_token):
        logger.info("ws auth failed executorId=%s reason=token_mismatch", executor_id)
        return ExecutorAuth(False, 0, 0, 0)
    logger.info(
        "ws auth ok executorId=%s agentId=%s tenantId=%s",
        executor.id,
        executor.agent_id,
        executor.tenant_id,
    )
    return ExecutorAuth(True, executor.id, executor.agent_id, executor.tenant_id)
