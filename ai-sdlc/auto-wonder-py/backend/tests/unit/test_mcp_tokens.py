"""个人 MCP 令牌、调度凭证、会话凭证和平台技能目录。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from autowonder.api.access import WorkspaceAccessLevel
from autowonder.config import get_settings
from autowonder.conversations.models import AgentConversation
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch
from autowonder.main import create_app
from autowonder.mcp.catalog import list_tools
from autowonder.mcp.conversation_tokens import issue_conversation_token
from autowonder.mcp.dispatch_tokens import issue_dispatch_token
from autowonder.mcp.principal import CredentialType
from autowonder.mcp.router import CreateMcpTokenRequest
from autowonder.mcp.skills import get_platform_skill, list_platform_skills
from autowonder.mcp.tokens import (
    TOKEN_PATTERN,
    authenticate,
    authenticate_bearer,
    hash_token,
    issue_token,
    list_tokens,
    revoke_token,
)
from autowonder.workitems.models import Workitem
from tests.unit.test_workitems import MemorySession

_SECRET = "test-secret-test-secret-test-secret-test-secret"


def test_mcp_token_routes_require_login_and_skip_workspace_access() -> None:
    """个人令牌路径要登录，不挂工作空间访问级别。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "post" in paths["/api/mcp/tokens"]
    assert "get" in paths["/api/mcp/tokens"]
    assert "delete" in paths["/api/mcp/tokens/{token_id}"]
    assert "get" in paths["/api/mcp/tokens/tools"]
    assert "get" in paths["/api/mcp/tokens/platform-skills"]
    assert "access_level" not in CreateMcpTokenRequest.model_fields
    response = client.get("/api/mcp/tokens")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    tools = client.get("/api/mcp/tokens/tools")
    assert tools.status_code == 401


async def test_issue_list_and_revoke_personal_tokens() -> None:
    """签发只保存哈希，空白名称用默认名，撤销不碰到别人的令牌。"""
    session = MemorySession()
    issued = await issue_token(session, "  local-codex  ", 7)
    assert issued.name == "local-codex"
    assert issued.user_id == 7
    assert issued.token.startswith("awmcp_")
    assert issued.token.startswith(issued.token_prefix)
    assert issued.token != hash_token(issued.token)
    assert TOKEN_PATTERN.fullmatch(issued.token) is not None
    blank = await issue_token(session, "   ", 7)
    assert blank.name == "MCP Token"
    for row in session.rows:
        if row.name == "local-codex":
            row.gmt_create = datetime(2026, 1, 1)
        if row.name == "MCP Token":
            row.gmt_create = datetime(2026, 1, 2)
    listed = await list_tokens(session, 7)
    assert [item.name for item in listed] == ["MCP Token", "local-codex"]
    with pytest.raises(BizError) as missing:
        await revoke_token(session, 9, 8)
    assert missing.value.code == ErrorCode.MCP_TOKEN_NOT_FOUND.code
    await revoke_token(session, issued.id, 7)
    assert session.rows[0].revoked_at is not None
    with pytest.raises(BizError) as again:
        await revoke_token(session, issued.id, 7)
    assert again.value.code == "27001"


async def test_personal_token_authenticates_and_rejects_bad_tokens() -> None:
    """查询参数和 Bearer 都能用；撤销、畸形和抢先撤销都是 10401。"""
    session = MemorySession()
    issued = await issue_token(session, "legacy", 7)
    principal = await authenticate_bearer(session, "Bearer " + issued.token)
    assert principal.workspace_id is None
    assert principal.access_level is None
    assert principal.is_workspace_scoped() is False
    assert principal.user_id == 7
    assert principal.token_id == issued.id
    assert principal.credential_type is CredentialType.LONG_LIVED
    queried = await authenticate(session, None, " " + issued.token + " ")
    assert queried.user_id == 7
    assert session.rows[0].last_used_at is not None
    with pytest.raises(BizError) as bad:
        await authenticate_bearer(session, "Bearer not-mcp")
    assert bad.value.code == "10401"
    with pytest.raises(BizError):
        await authenticate_bearer(session, "Bearer awmcp_short")
    await revoke_token(session, issued.id, 7)
    with pytest.raises(BizError):
        await authenticate_bearer(session, "Bearer " + issued.token)


async def test_touch_failure_rejects_the_token() -> None:
    """查找之后没能写上最近使用时间，令牌不算通过。"""
    session = MemorySession()
    issued = await issue_token(session, "racy", 7)

    async def reject_update(statement: object) -> object:
        class Result:
            rowcount = 0

        assert statement.__class__.__name__ == "Update"
        return Result()

    session.execute = reject_update  # type: ignore[method-assign]
    with pytest.raises(BizError) as error:
        await authenticate_bearer(session, "Bearer " + issued.token)
    assert error.value.code == "10401"


async def test_dispatch_token_stays_on_its_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """活跃派发签出读写主体；结束后同一令牌失效。创建人为 0 时改用工单创建人。"""
    monkeypatch.setenv("AUTOWONDER_JWT_SECRET", _SECRET)
    get_settings.cache_clear()
    session = MemorySession()
    dispatch = Dispatch(
        tenant_id=100,
        workitem_id=1,
        agent_id=2,
        idempotency_key="dispatch-99",
        creator_id=7,
        status="RUNNING",
    )
    session.add(dispatch)
    await session.flush()
    token = await issue_dispatch_token(session, dispatch)
    principal = await authenticate_bearer(session, "Bearer " + token)
    assert principal.workspace_id == 100
    assert principal.is_workspace_scoped() is True
    assert principal.user_id == 7
    assert principal.access_level is WorkspaceAccessLevel.READ_WRITE
    assert principal.credential_type is CredentialType.DISPATCH
    assert principal.token_id == -dispatch.id
    dispatch.status = "SUCCEEDED"
    with pytest.raises(BizError) as inactive:
        await authenticate_bearer(session, "Bearer " + token)
    assert inactive.value.code == "10401"

    owned = Dispatch(
        tenant_id=200,
        workitem_id=300,
        agent_id=2,
        idempotency_key="dispatch-100",
        creator_id=0,
        status="RUNNING",
    )
    session.add(owned)
    session.add(
        Workitem(
            id=300,
            tenant_id=200,
            work_type="TASK",
            title="owned",
            creator_id=8,
        )
    )
    await session.flush()
    owner = await authenticate_bearer(
        session,
        "Bearer " + await issue_dispatch_token(session, owned),
    )
    assert owner.user_id == 8
    with pytest.raises(BizError):
        await authenticate_bearer(session, "Bearer awdispatch_x")
    get_settings.cache_clear()


async def test_conversation_token_follows_the_active_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话令牌钉在工作空间和版本上。关闭后失效。没有会话服务时前缀也拒绝。"""
    monkeypatch.setenv("AUTOWONDER_JWT_SECRET", _SECRET)
    get_settings.cache_clear()
    session = MemorySession()
    conversation = AgentConversation(
        id=22,
        tenant_id=100,
        agent_id=77,
        agent_version_id=88,
        channel="WORKITEM_CLARIFICATION",
        channel_conversation_id="conv-22",
        status="ACTIVE",
    )
    session.add(conversation)
    await session.flush()
    token = issue_conversation_token(conversation, 7)
    principal = await authenticate_bearer(session, "Bearer " + token)
    assert principal.workspace_id == 100
    assert principal.user_id == 7
    assert principal.token_id == 22
    assert principal.access_level is WorkspaceAccessLevel.READ_WRITE
    assert principal.credential_type is CredentialType.CONVERSATION
    conversation.status = "CLOSED"
    with pytest.raises(BizError) as closed:
        await authenticate_bearer(session, "Bearer " + token)
    assert closed.value.code == "10401"
    with pytest.raises(BizError):
        await authenticate_bearer(session, "Bearer awconversation_x")
    get_settings.cache_clear()


def test_platform_skills_and_tool_names() -> None:
    """平台技能按固定 id 列出，工具目录有 112 个名称。"""
    skills = list_platform_skills()
    assert [skill.id for skill in skills] == [
        "autowonder-workitem-operator",
        "autowonder-sdlc-manager",
        "autowonder-agent-manager",
        "autowonder-project-navigator",
        "autowonder-skill-manager",
        "autowonder-scheduled-task-operator",
    ]
    with pytest.raises(BizError) as missing:
        get_platform_skill("missing")
    assert missing.value.code == ErrorCode.SKILL_NOT_FOUND.code
    tools = list_tools()
    assert len(tools) == 112
    assert tools[0]["name"] == "autowonder.list_projects"
    assert tools[0]["description"].startswith("List the AutoWonder workspaces")
    assert len({tool["name"] for tool in tools}) == 112
