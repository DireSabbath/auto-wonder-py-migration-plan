"""个人 MCP 长效令牌。只保存哈希，明文只在签发时返回一次。"""

import base64
import hashlib
import re
import secrets
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel
from autowonder.db.rows import rowcount
from autowonder.evolution.jsontext import java_trim
from autowonder.mcp.models import McpAccessToken
from autowonder.mcp.principal import Principal

TOKEN_PREFIX = "awmcp_"
TOKEN_PATTERN = re.compile(r"^awmcp_[A-Za-z0-9_-]{43}$")
_DEFAULT_NAME = "MCP Token"


class McpAccessTokenView(ApiModel):
    """已签发令牌的公开字段，不含明文。"""

    id: int | None = None
    name: str
    user_id: int
    token_prefix: str
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    gmt_create: datetime | None = None


class IssuedMcpTokenView(McpAccessTokenView):
    """签发响应。``token`` 只出现这一次。"""

    token: str


def hash_token(token: str) -> str:
    """SHA-256 十六进制摘要，与 Java ``HexFormat`` 小写一致。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_token(session: AsyncSession, name: str | None, user_id: int) -> IssuedMcpTokenView:
    """写入个人令牌。不绑定工作空间。"""
    token = _generate_token()
    row = McpAccessToken(
        user_id=user_id,
        name=_normalize_name(name),
        token_hash=hash_token(token),
        token_prefix=token[:16],
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    return IssuedMcpTokenView(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        token_prefix=row.token_prefix,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        gmt_create=row.gmt_create,
        token=token,
    )


async def list_tokens(session: AsyncSession, user_id: int) -> list[McpAccessTokenView]:
    """该用户全部未删除的个人令牌，新的在前。"""
    rows = list(
        (
            await session.scalars(
                select(McpAccessToken)
                .where(McpAccessToken.user_id == user_id, McpAccessToken.is_deleted == 0)
                .order_by(McpAccessToken.gmt_create.desc(), McpAccessToken.id.desc())
            )
        ).all()
    )
    rows.sort(key=lambda row: (row.gmt_create, row.id), reverse=True)
    return [_view(row) for row in rows]


async def revoke_token(session: AsyncSession, token_id: int, user_id: int) -> None:
    """只撤销调用者自己的、尚未撤销的令牌。"""
    row = await session.scalar(
        select(McpAccessToken)
        .where(
            McpAccessToken.id == token_id,
            McpAccessToken.user_id == user_id,
            McpAccessToken.is_deleted == 0,
        )
        .limit(1)
    )
    if row is None:
        raise BizError(ErrorCode.MCP_TOKEN_NOT_FOUND)
    result = await session.execute(
        update(McpAccessToken)
        .where(
            McpAccessToken.id == token_id,
            McpAccessToken.user_id == user_id,
            McpAccessToken.revoked_at.is_(None),
            McpAccessToken.is_deleted == 0,
        )
        .values(
            revoked_at=now_local(),
            modifier_id=user_id,
            version=McpAccessToken.version + 1,
        )
    )
    if rowcount(result) != 1:
        raise BizError(ErrorCode.MCP_TOKEN_NOT_FOUND)
    await session.commit()


async def authenticate(
    session: AsyncSession,
    authorization: str | None,
    query_token: str | None,
) -> Principal:
    """查询参数里的令牌优先，否则读取 Bearer。"""
    token = _query_token(query_token)
    if token is not None:
        return await _authenticate_plain(session, token)
    return await authenticate_bearer(session, authorization)


async def authenticate_bearer(session: AsyncSession, authorization: str | None) -> Principal:
    """要求 ``Bearer `` 前缀，剩余部分按 Java ``trim`` 处理。"""
    if authorization is None or not authorization.startswith("Bearer "):
        raise _unauthorized()
    return await _authenticate_plain(session, java_trim(authorization[len("Bearer ") :]))


async def _authenticate_plain(session: AsyncSession, token: str | None) -> Principal:
    if token is not None and token.startswith("awdispatch_"):
        from autowonder.mcp.dispatch_tokens import authenticate_dispatch

        return await authenticate_dispatch(session, token)
    if token is not None and token.startswith("awconversation_"):
        from autowonder.mcp.conversation_tokens import authenticate_conversation

        return await authenticate_conversation(session, token)
    if token is None or TOKEN_PATTERN.fullmatch(token) is None:
        raise _unauthorized()
    row = await session.scalar(
        select(McpAccessToken)
        .where(McpAccessToken.token_hash == hash_token(token), McpAccessToken.is_deleted == 0)
        .limit(1)
    )
    if row is None or row.revoked_at is not None:
        raise _unauthorized()
    result = await session.execute(
        update(McpAccessToken)
        .where(
            McpAccessToken.id == row.id,
            McpAccessToken.revoked_at.is_(None),
            McpAccessToken.is_deleted == 0,
        )
        .values(last_used_at=now_local())
    )
    if rowcount(result) != 1:
        raise _unauthorized()
    await session.commit()
    return Principal.personal(row.user_id, row.id)


def _generate_token() -> str:
    encoded = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    return TOKEN_PREFIX + encoded.rstrip("=")


def _normalize_name(name: str | None) -> str:
    if name is None:
        return _DEFAULT_NAME
    trimmed = java_trim(name)
    if trimmed == "":
        return _DEFAULT_NAME
    return trimmed


def _query_token(token: str | None) -> str | None:
    if token is None:
        return None
    trimmed = java_trim(token)
    if trimmed == "":
        return None
    return trimmed


def _view(row: McpAccessToken) -> McpAccessTokenView:
    return McpAccessTokenView(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        token_prefix=row.token_prefix,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        gmt_create=row.gmt_create,
    )


def _unauthorized() -> BizError:
    return BizError(ErrorCode.UNAUTHORIZED)
