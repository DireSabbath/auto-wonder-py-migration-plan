"""个人 MCP 令牌。不走工作空间访问级别，只要求已登录。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.artifacts.cli_tokens import deployment_endpoint
from autowonder.core.context import current_user_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.core.schema import ApiModel
from autowonder.db.session import get_session
from autowonder.mcp.catalog import bind_tool_catalog
from autowonder.mcp.skills import list_platform_skills
from autowonder.mcp.tokens import issue_token, list_tokens, revoke_token

router = APIRouter(prefix="/api/mcp/tokens", tags=["mcp-tokens"])


class CreateMcpTokenRequest(ApiModel):
    """签发请求只带名称。"""

    name: str | None = None


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


@router.post("")
async def issue(
    body: CreateMcpTokenRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """为当前用户签发个人令牌。"""
    name = None if body is None else body.name
    return ok(await issue_token(session, name, _user_id()))


@router.get("")
async def listed(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出当前用户的个人令牌。"""
    return ok(await list_tokens(session, _user_id()))


@router.get("/tools")
async def tools(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """工具目录。说明里的命令使用当前部署根地址。"""
    _user_id()
    server_url, runtime_version = await deployment_endpoint(session)
    return ok(bind_tool_catalog(server_url, runtime_version))


@router.get("/platform-skills")
async def platform_skills() -> dict[str, Any]:
    """平台技能目录。调用前仍要求已登录。"""
    _user_id()
    return ok(list_platform_skills())


@router.delete("/{token_id}")
async def revoke(
    token_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """撤销当前用户的一条个人令牌。"""
    await revoke_token(session, token_id, _user_id())
    return ok(None)
