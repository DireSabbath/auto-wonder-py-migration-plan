"""平台数字人状态、能力快照和执行器容量探测。这些检查不访问数据库。"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.agents.models import Agent, AgentVersion
from autowonder.agents.platform_intelligence import (
    AVAILABLE,
    NOT_CONFIGURED,
    RUNTIME_UNAVAILABLE,
    get_capability_status,
    require_positive_workspace,
    version_has_identity,
    version_statement,
)
from autowonder.agents.platform_status import (
    STATE_NOT_CONFIGURED,
    STATE_OFFLINE,
    STATE_OK,
    executors_for_agent_statement,
    platform_agent_statement,
    platform_agent_status,
)
from autowonder.core.errors import IllegalArgumentError
from autowonder.core.result import dump_data
from autowonder.dispatch.selector import (
    execs_key,
    has_available_executor,
    has_remaining_capacity,
    probe_members,
)
from autowonder.executors.registry import DispatchSnapshot, drop_session, register_session
from autowonder.main import create_app


def test_missing_platform_agent_is_not_configured() -> None:
    """没有平台数字人时计数为 0，也不查看执行器是否在线。"""
    status = platform_agent_status(None, [])
    assert status.state == STATE_NOT_CONFIGURED
    assert status.agent_id is None
    assert status.executor_count == 0
    assert status.online_executor_count == 0


def test_agent_without_executor_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """有数字人但没有执行器时仍是未配置，并且不探测在线。"""

    def probed(executor_id: int) -> bool:
        raise AssertionError(executor_id)

    monkeypatch.setattr("autowonder.agents.platform_status.is_online", probed)
    status = platform_agent_status(11, [])
    assert status.state == STATE_NOT_CONFIGURED
    assert status.agent_id == 11
    assert status.executor_count == 0
    assert status.online_executor_count == 0


def test_offline_when_no_executor_session_is_registered() -> None:
    """执行器都没有接入会话时为离线。"""
    drop_session(21)
    drop_session(22)
    status = platform_agent_status(11, [21, 22])
    assert status.state == STATE_OFFLINE
    assert status.executor_count == 2
    assert status.online_executor_count == 0


def test_ok_when_one_executor_is_online() -> None:
    """至少一个执行器在线即为 OK，在线数只计已登记的会话。"""
    drop_session(21)
    register_session(22)
    drop_session(23)
    status = platform_agent_status(11, [21, 22, 23])
    drop_session(22)
    assert status.state == STATE_OK
    assert status.executor_count == 3
    assert status.online_executor_count == 1
    body = dump_data(status)
    assert body["agentId"] == 11
    assert body["executorCount"] == 3
    assert body["onlineExecutorCount"] == 1


def test_platform_agent_queries_match_java() -> None:
    """平台数字人取最早一条，执行器按 id 倒序，版本按主键读取。"""
    agent_sql = _sql(platform_agent_statement(100))
    assert "FROM agent" in agent_sql
    assert "agent.tenant_id = 100" in agent_sql
    assert "agent.kind = 'PLATFORM'" in agent_sql
    assert "agent.is_deleted = 0" in agent_sql
    assert "ORDER BY agent.id ASC" in agent_sql
    assert agent_sql.endswith("LIMIT 1")
    executor_sql = _sql(executors_for_agent_statement(100, 11))
    assert "executor.tenant_id = 100" in executor_sql
    assert "executor.agent_id = 11" in executor_sql
    assert "executor.is_deleted = 0" in executor_sql
    assert "ORDER BY executor.id DESC" in executor_sql
    version_sql = _sql(version_statement(5))
    assert "agent_version.id = 5" in version_sql
    assert "agent_version.is_deleted = 0" in version_sql


def test_capability_follows_configuration_then_runtime() -> None:
    """先区分未配置、离线、版本不可用，最后才看执行器容量。"""
    agent = _agent()
    version = _version()
    session = _Rows(agent, version)
    seen: list[int] = []

    async def unavailable(agent_id: int) -> bool:
        seen.append(agent_id)
        return False

    status = _run(get_capability_status(session, 1), unavailable)
    assert status.status == RUNTIME_UNAVAILABLE
    assert status.available is False
    assert seen == [3]

    async def ready(agent_id: int) -> bool:
        seen.append(agent_id)
        return True

    seen.clear()
    status = _run(get_capability_status(session, 1), ready)
    assert status.status == AVAILABLE
    assert status.available is True
    body = dump_data(status)
    assert body == {"agentId": 3, "status": "AVAILABLE", "available": True}

    agent.status = "OFFLINE"
    seen.clear()
    reads_before_offline = session.version_reads
    status = _run(get_capability_status(session, 1), ready)
    assert status.status == "AGENT_OFFLINE"
    assert seen == []
    assert session.version_reads == reads_before_offline

    agent.status = "ONLINE"
    agent.online_version_id = None
    status = _run(get_capability_status(session, 1), ready)
    assert status.status == "VERSION_UNAVAILABLE"
    assert session.version_reads == reads_before_offline

    session.agent = None
    status = _run(get_capability_status(session, 1), ready)
    assert status.status == NOT_CONFIGURED
    assert status.agent_id is None


def test_wrong_workspace_or_version_skips_runtime() -> None:
    """数字人不属于该工作空间时未配置；版本租户不符时不探测执行器。"""
    agent = _agent()
    agent.tenant_id = 2
    version = _version()
    session = _Rows(agent, version)
    seen: list[int] = []

    async def ready(agent_id: int) -> bool:
        seen.append(agent_id)
        return True

    status = _run(get_capability_status(session, 1), ready)
    assert status.status == NOT_CONFIGURED
    assert seen == []

    agent.tenant_id = 1
    version.tenant_id = 2
    status = _run(get_capability_status(session, 1), ready)
    assert status.status == "VERSION_UNAVAILABLE"
    assert seen == []


def test_identity_accepts_whitespace_and_json_object() -> None:
    """空白字符算有内容；身份列只要不是空串就算有快照。"""
    version = _version()
    version.role_name = " "
    version.role_code = ""
    version.business_background = None
    version.responsibilities = None
    version.identity_json = None
    assert version_has_identity(version) is True
    version.role_name = ""
    assert version_has_identity(version) is False
    version.identity_json = {}
    assert version_has_identity(version) is True
    version.identity_json = ""
    assert version_has_identity(version) is False


def test_workspace_must_be_positive() -> None:
    """空工作空间和非正数都拒绝，文案与 Java 一致。"""
    with pytest.raises(IllegalArgumentError, match="workspaceId must be positive"):
        require_positive_workspace(None)
    with pytest.raises(IllegalArgumentError, match="workspaceId must be positive"):
        require_positive_workspace(0)


def test_capacity_probe_matches_java_vectors() -> None:
    """畸形成员跳过；容量 2 的普通派发只留 1 个槽，占用 1 条后不可用。"""
    snapshot = DispatchSnapshot(
        capacity=2,
        authoritative_inventory=True,
        inventory_ready=True,
        inventory_error=None,
        running_dispatch_ids=frozenset(),
        running_conversation_turn_ids=frozenset(),
    )
    assert (
        probe_members(
            {"bad", "10"},
            lambda executor_id: executor_id == 10,
            lambda executor_id: snapshot,
            lambda executor_id: {1},
        )
        is False
    )
    assert (
        probe_members(
            {"bad", "10"},
            lambda executor_id: executor_id == 10,
            lambda executor_id: snapshot,
            lambda executor_id: set(),
        )
        is True
    )
    assert (
        probe_members(
            None,
            lambda executor_id: True,
            lambda executor_id: snapshot,
            lambda executor_id: set(),
        )
        is False
    )
    assert has_remaining_capacity(None, set(), False) is False
    blocked = DispatchSnapshot(
        capacity=2,
        authoritative_inventory=True,
        inventory_ready=False,
        inventory_error=None,
        running_dispatch_ids=frozenset(),
        running_conversation_turn_ids=frozenset(),
    )
    assert has_remaining_capacity(blocked, set(), False) is False
    assert execs_key(3) == "agent:execs:3"


async def test_live_probe_reads_agent_execs(monkeypatch: pytest.MonkeyPatch) -> None:
    """在线集合来自 Redis。没有可调度登记时，即使键里有成员也不可用。"""

    class _Redis:
        async def smembers(self, key: str) -> set[str]:
            assert key == "agent:execs:3"
            return {"10", "bad"}

    monkeypatch.setattr(
        "autowonder.dispatch.selector.redis_client",
        lambda: _Redis(),
    )
    assert await has_available_executor(3) is False


def test_platform_status_routes_require_login() -> None:
    """两条状态路径都要登录。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/platform-agent/status"]
    assert "get" in paths["/api/platform-intelligence/status"]
    agent = client.get("/api/platform-agent/status")
    intelligence = client.get("/api/platform-intelligence/status")
    assert agent.status_code == 401
    assert agent.json()["code"] == "10401"
    assert intelligence.status_code == 401
    assert intelligence.json()["code"] == "10401"


def _run(awaitable: object, runtime: Callable[[int], object]) -> object:
    import asyncio

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "autowonder.agents.platform_intelligence.has_available_executor",
        runtime,
    )
    try:
        return asyncio.run(awaitable)  # type: ignore[arg-type]
    finally:
        monkey.undo()


class _Rows:
    """按语句里的表名交回预先放好的数字人或版本。"""

    def __init__(self, agent: Agent | None, version: AgentVersion | None) -> None:
        self.agent = agent
        self.version = version
        self.version_reads = 0

    async def scalar(self, statement: object) -> Agent | AgentVersion | None:
        sql = str(statement)
        if "agent_version" in sql:
            self.version_reads = self.version_reads + 1
            return self.version
        return self.agent


def _agent() -> Agent:
    return Agent(
        id=3,
        tenant_id=1,
        name="Chief of Staff",
        kind="PLATFORM",
        status="ONLINE",
        online_version_id=5,
        is_deleted=0,
    )


def _version() -> AgentVersion:
    return AgentVersion(
        id=5,
        tenant_id=1,
        agent_id=3,
        version_no=1,
        role_name="Chief of Staff",
        is_deleted=0,
    )


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)
