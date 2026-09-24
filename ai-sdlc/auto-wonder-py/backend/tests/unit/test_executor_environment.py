"""执行器令牌、在线环境变量和任务包地址。这些检查不连接 MySQL。"""

import base64
from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.package_url import refresh_package_url
from autowonder.executors.daemon_router import environment_http_response, executor_environment
from autowonder.executors.models import Executor
from autowonder.executors.ws_auth import authenticate_executor
from autowonder.main import create_app
from tests.unit.test_workitems import MemorySession


def test_executor_environment_and_package_routes_are_registered() -> None:
    """两条 daemon 路径已经挂上。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/daemon/executors/{executorId}/environment-variables"]
    assert "post" in paths["/api/daemon/dispatches/{dispatchId}/package-url"]


@pytest.mark.asyncio
async def test_valid_executor_token_returns_identity() -> None:
    """令牌匹配时带回执行器、数字员工和空间。"""
    session = MemorySession()
    session.add(_executor(1, 10, 100, "exec_1_secret", 0))
    await session.flush()
    result = await authenticate_executor(session, 1, "exec_1_secret")
    assert result.success is True
    assert result.executor_id == 1
    assert result.agent_id == 10
    assert result.tenant_id == 100


@pytest.mark.asyncio
async def test_invalid_missing_and_deleted_executor_tokens_fail() -> None:
    """令牌不对、执行器不存在或已删除时身份失败。"""
    session = MemorySession()
    session.add(_executor(1, 10, 100, "exec_1_secret", 0))
    session.add(_executor(2, 10, 100, "exec_1_secret", 1))
    await session.flush()
    assert (await authenticate_executor(session, 1, "wrong")).success is False
    assert (await authenticate_executor(session, 99, "exec_1_secret")).success is False
    assert (await authenticate_executor(session, 2, "exec_1_secret")).success is False


@pytest.mark.asyncio
async def test_online_environment_snapshot_is_not_cached() -> None:
    """在线版本返回当前快照，并禁止缓存。"""
    session = await _bound(101)
    seen: list[tuple[int, int]] = []

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        seen.append((tenant_id, agent_version_id))
        return {"TOKEN": "secret"}

    status, body = await executor_environment(session, 17, "Bearer executor-token", resolve)
    assert status == 200
    assert body == {"environmentVariables": {"TOKEN": "secret"}}
    assert seen == [(8, 101)]
    response = environment_http_response(status, body)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_missing_online_version_skips_resolve() -> None:
    """没有在线版本时返回空快照，且不解析。"""
    session = await _bound(None)

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        raise AssertionError(agent_version_id)

    status, body = await executor_environment(session, 17, "Bearer executor-token", resolve)
    assert status == 200
    assert body == {"environmentVariables": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [None, "Basic executor-token", "Bearer", "Bearer "],
)
async def test_malformed_authorization_is_unauthorized(authorization: str | None) -> None:
    """缺少或格式不对的 Authorization 在校验令牌之前拒绝。"""
    session = await _bound(101)

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        raise AssertionError(agent_version_id)

    status, body = await executor_environment(session, 17, authorization, resolve)
    assert status == 401
    assert body is None


@pytest.mark.asyncio
async def test_failed_bearer_does_not_resolve() -> None:
    """令牌不对时不读取环境变量。"""
    session = await _bound(101)

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        raise AssertionError(agent_version_id)

    status, body = await executor_environment(session, 17, "Bearer bad-token", resolve)
    assert status == 401
    assert body is None
    assert environment_http_response(status, body).body == b""


@pytest.mark.asyncio
async def test_missing_and_cross_tenant_agent_conflict() -> None:
    """数字员工不存在或空间不一致时返回空 409。"""
    missing = MemorySession()
    missing.add(_executor(17, 51, 8, "executor-token", 0))
    await missing.flush()
    status, body = await executor_environment(
        missing, 17, "Bearer executor-token", _unused_resolve
    )
    assert status == 409
    assert body is None

    crossed = await _bound(101)
    _agent(crossed).tenant_id = 9
    status, body = await executor_environment(
        crossed, 17, "Bearer executor-token", _unused_resolve
    )
    assert status == 409
    assert body is None


@pytest.mark.asyncio
async def test_snapshot_resolution_failure_propagates() -> None:
    """快照解析失败原样抛出。"""
    session = await _bound(101)
    failure = RuntimeError("snapshot unavailable")

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await executor_environment(session, 17, "Bearer executor-token", resolve)
    assert caught.value is failure


@pytest.mark.asyncio
async def test_refresh_package_url_for_running_dispatch() -> None:
    """进行中的派发按已保存的对象键重新签发 600 秒地址。"""
    session = await _packaged("RUNNING", "bucket/task-packages/99.zip")

    def presign(oss_ref: str, ttl_seconds: int) -> str:
        assert oss_ref == "bucket/task-packages/99.zip"
        assert ttl_seconds == 600
        return "https://oss/new-url"

    status, body = await refresh_package_url(session, 99, "tok", presign)
    assert status == 200
    assert body == {"downloadUrl": "https://oss/new-url", "expiresInSeconds": 600}


@pytest.mark.asyncio
async def test_terminal_or_blank_package_is_not_presigned() -> None:
    """终态、空白包引用不签发地址。"""
    terminal = await _packaged("SUCCEEDED", "bucket/task-packages/99.zip")
    status, body = await refresh_package_url(terminal, 99, "tok", _forbid_presign)
    assert status == 409
    assert body == {"error": "package URL is not refreshable"}

    blank = await _packaged("ACKED", "  ")
    status, body = await refresh_package_url(blank, 99, "tok", _forbid_presign)
    assert status == 409
    assert body == {"error": "package URL is not refreshable"}


@pytest.mark.asyncio
async def test_package_url_rejects_bad_token() -> None:
    """令牌不对时任务包地址正文为空。"""
    session = await _packaged("RUNNING", "bucket/task-packages/99.zip")
    status, body = await refresh_package_url(session, 99, "other", _forbid_presign)
    assert status == 401
    assert body is None


def _token(plain: str) -> str:
    return "b64:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")


def _executor(
    executor_id: int, agent_id: int, tenant_id: int, plain: str, deleted: int
) -> Executor:
    return Executor(
        id=executor_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        name="executor",
        token_ref=_token(plain),
        is_deleted=deleted,
    )


async def _bound(online_version_id: int | None) -> MemorySession:
    session = MemorySession()
    session.add(_executor(17, 51, 8, "executor-token", 0))
    session.add(
        Agent(
            id=51,
            tenant_id=8,
            name="worker",
            online_version_id=online_version_id,
            is_deleted=0,
        )
    )
    await session.flush()
    return session


def _agent(session: MemorySession) -> Agent:
    return next(row for row in session.rows if isinstance(row, Agent))


async def _unused_resolve(
    current: AsyncSession, tenant_id: int, agent_version_id: int
) -> Mapping[str, str]:
    raise AssertionError(agent_version_id)


async def _packaged(status: str, oss_ref: str) -> MemorySession:
    session = MemorySession()
    session.add(_executor(77, 30, 10, "tok", 0))
    session.add(
        Dispatch(
            id=99,
            tenant_id=10,
            workitem_id=20,
            agent_id=30,
            executor_id=77,
            status=status,
            package_oss_ref=oss_ref,
            idempotency_key="pkg",
            is_deleted=0,
        )
    )
    await session.flush()
    return session


def _forbid_presign(oss_ref: str, ttl_seconds: int) -> str:
    raise AssertionError(oss_ref)
