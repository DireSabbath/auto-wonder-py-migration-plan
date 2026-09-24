"""ASGI 鉴权中间件，白名单与成员校验对齐 ``AuthFilter``。"""

import uuid
from collections.abc import Awaitable, Callable

from jwt import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autowonder.api.access import WorkspaceAccessLevel
from autowonder.api.whitelist import (
    is_deactivation_revoke_request,
    is_login_only_request,
    is_whitelisted,
)
from autowonder.auth.session import SessionService
from autowonder.core.context import RequestContext, reset_context, set_context
from autowonder.core.errors import ErrorCode
from autowonder.core.redis import redis_client
from autowonder.core.result import fail_omitting_nulls
from autowonder.db.session import SessionLocal
from autowonder.security.jwt import parse_access
from autowonder.users.service import find_user_by_id
from autowonder.workspaces.service import count_usable, find_member

SendWrapper = Callable[[Message], Awaitable[None]]


class AuthMiddleware:
    """只拦截 ``/api/``。静态页与健康检查不经过这里的令牌校验。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        request_id = request.headers.get("x-acs-request-id")
        if request_id is None or request_id.strip() == "":
            request_id = str(uuid.uuid4())
        context = RequestContext(
            request_id=request_id,
            operation=f"{request.method.upper()} {path}",
        )
        token = set_context(context)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-acs-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            if is_whitelisted(request.method, path):
                await self.app(scope, receive, send_with_request_id)
                return
            rejection = await _authenticate(request, context)
            if rejection is not None:
                await rejection(scope, receive, send_with_request_id)
                return
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_context(token)


async def _authenticate(request: Request, context: RequestContext) -> JSONResponse | None:
    header = request.headers.get("authorization")
    if header is None or not header.startswith("Bearer "):
        return _failure(401, ErrorCode.UNAUTHORIZED)
    token_text = header[len("Bearer ") :].strip()
    try:
        payload = parse_access(token_text)
    except (InvalidTokenError, KeyError, ValueError):
        return _failure(401, ErrorCode.UNAUTHORIZED)
    sessions = SessionService(redis_client())
    if payload.jti is not None and await sessions.is_blacklisted(payload.jti):
        return _failure(401, ErrorCode.UNAUTHORIZED)
    async with SessionLocal() as session:
        if not is_deactivation_revoke_request(request.method, request.url.path):
            user = await find_user_by_id(session, payload.user_id)
            if user is not None and user.status == 1 and user.password_hash == "DEACTIVATED":
                return _failure(401, ErrorCode.DEACTIVATION_ACCOUNT_DISABLED)
        context.user_id = payload.user_id
        context.workspace_id = payload.workspace_id
        context.trace_id = str(uuid.uuid4())
        if payload.workspace_id is not None and not is_login_only_request(
            request.method, request.url.path
        ):
            if await count_usable(session, payload.workspace_id) == 0:
                return _failure(403, ErrorCode.ORG_DELETED_OR_DISABLED)
            member = await find_member(session, payload.workspace_id, payload.user_id)
            active_member = member is not None and member.is_deleted == 0 and member.status == 0
            if active_member and member is not None:
                try:
                    level = WorkspaceAccessLevel[member.access_level]
                except KeyError:
                    return _failure(500, ErrorCode.WORKSPACE_ACCESS_LEVEL_INVALID)
                context.access_level = level.name
            elif await _is_platform_admin(session, payload.user_id):
                context.access_level = WorkspaceAccessLevel.ADMIN.name
            else:
                return _failure(403, ErrorCode.WORKSPACE_NOT_MEMBER)
    return None


async def _is_platform_admin(session: AsyncSession, user_id: int) -> bool:
    user = await find_user_by_id(session, user_id)
    return user is not None and user.is_admin == 1


def _failure(status: int, error_code: ErrorCode) -> JSONResponse:
    return JSONResponse(status_code=status, content=fail_omitting_nulls(error_code))
