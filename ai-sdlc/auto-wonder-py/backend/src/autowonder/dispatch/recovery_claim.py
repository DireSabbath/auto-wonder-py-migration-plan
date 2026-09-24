"""执行器续认领仍由自己持有的活动派发，并取回该版本的环境变量快照。"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.artifacts.daemon_auth import authenticate, load_mutation_fence
from autowonder.core.clock import now_local
from autowonder.db.rows import rowcount
from autowonder.dispatch.models import Dispatch
from autowonder.environments.snapshot import resolve_snapshot

RECOVERABLE = frozenset({"DISPATCHED", "ACKED", "RUNNING"})
_LOST = "dispatch is no longer recoverable"

Fence = Callable[[AsyncSession, int], Awaitable[bool]]
Claim = Callable[[AsyncSession, int, int, int], Awaitable[int]]
Resolve = Callable[[AsyncSession, int, int], Awaitable[Mapping[str, str]]]


async def claim_owned_active(
    session: AsyncSession,
    dispatch_id: int,
    tenant_id: int,
    executor_id: int,
) -> int:
    """只刷新仍属于该执行器、且状态可恢复的行。影响行数不是 1 表示认领已丢失。"""
    result = await session.execute(
        update(Dispatch)
        .where(
            Dispatch.id == dispatch_id,
            Dispatch.tenant_id == tenant_id,
            Dispatch.executor_id == executor_id,
            Dispatch.status.in_(tuple(RECOVERABLE)),
            Dispatch.is_deleted == 0,
        )
        .values(gmt_modified=now_local())
    )
    return rowcount(result)


async def claim_recovery(
    session: AsyncSession,
    dispatch_id: int,
    token: str | None,
    fence: Fence = load_mutation_fence,
    claim: Claim = claim_owned_active,
    resolve: Resolve = resolve_snapshot,
) -> tuple[int, dict[str, Any] | None]:
    """令牌或写入栅栏失败返回 401。状态已不可恢复，或并发认领失败，返回 409。"""
    auth = await authenticate(session, dispatch_id, token)
    if not auth.success or await fence(session, dispatch_id):
        return 401, None
    dispatch = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    if dispatch is None:
        return 404, None
    executor_id = dispatch.executor_id
    if dispatch.status not in RECOVERABLE or executor_id is None:
        return 409, {"allowed": False, "error": _LOST}
    changed = await claim(session, dispatch.id, dispatch.tenant_id, executor_id)
    if changed != 1:
        return 409, {"allowed": False, "error": _LOST}
    variables: Mapping[str, str] = {}
    if dispatch.agent_version_id is not None:
        variables = await resolve(session, dispatch.tenant_id, dispatch.agent_version_id)
    return 200, {
        "allowed": True,
        "status": dispatch.status,
        "environmentVariables": dict(variables),
    }


def claim_http_response(status: int, body: dict[str, Any] | None) -> Response:
    """成功响应禁止缓存。401 和 404 正文为空。"""
    if body is None:
        return Response(status_code=status)
    headers = None
    if status == 200:
        headers = {"Cache-Control": "no-store"}
    return JSONResponse(status_code=status, content=body, headers=headers)
