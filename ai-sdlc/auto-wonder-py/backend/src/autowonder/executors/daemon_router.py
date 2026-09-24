"""执行器读取自己数字员工当前在线版本的环境变量。路径在鉴权白名单里。"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.db.session import get_session
from autowonder.environments.snapshot import resolve_snapshot
from autowonder.executors.ws_auth import authenticate_executor

router = APIRouter(prefix="/api/daemon/executors", tags=["daemon-executors"])

_BEARER = "Bearer "
Resolve = Callable[[AsyncSession, int, int], Awaitable[Mapping[str, str]]]


def bearer_token(authorization: str | None) -> str | None:
    """只接受 ``Bearer `` 前缀。空令牌视为未提供。"""
    if authorization is None or not authorization.startswith(_BEARER):
        return None
    token = authorization[len(_BEARER) :].strip()
    if token == "":
        return None
    return token


async def executor_environment(
    session: AsyncSession,
    executor_id: int,
    authorization: str | None,
    resolve: Resolve = resolve_snapshot,
) -> tuple[int, dict[str, Any] | None]:
    """令牌失败返回空 401。数字员工缺失或空间不一致返回空 409。"""
    token = bearer_token(authorization)
    if token is None:
        return 401, None
    auth = await authenticate_executor(session, executor_id, token)
    if not auth.success:
        return 401, None
    agent = await session.scalar(
        select(Agent).where(Agent.id == auth.agent_id, Agent.is_deleted == 0).limit(1)
    )
    if agent is None or agent.tenant_id != auth.tenant_id:
        return 409, None
    variables: Mapping[str, str] = {}
    if agent.online_version_id is not None:
        variables = await resolve(session, auth.tenant_id, agent.online_version_id)
    return 200, {"environmentVariables": dict(variables)}


def environment_http_response(status: int, body: dict[str, Any] | None) -> Response:
    """成功响应禁止缓存。失败正文为空。"""
    if body is None:
        return Response(status_code=status)
    return JSONResponse(content=body, headers={"Cache-Control": "no-store"})


@router.get("/{executorId}/environment-variables")
async def environment_variables(
    executorId: int,
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """返回当前在线版本的环境变量快照。"""
    status, body = await executor_environment(session, executorId, authorization)
    return environment_http_response(status, body)
