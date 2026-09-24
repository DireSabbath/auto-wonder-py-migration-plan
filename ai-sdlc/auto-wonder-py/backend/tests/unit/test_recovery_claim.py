"""执行器续认领和环境变量快照。这些检查不连接 MySQL。"""

import base64
from collections.abc import Mapping
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery_claim import claim_http_response, claim_recovery
from autowonder.environments.snapshot import (
    EnvironmentSnapshotResolutionException,
    SnapshotRow,
    resolution_snapshot_statement,
    resolve_rows,
)
from autowonder.executors.models import Executor
from autowonder.main import create_app
from tests.unit.test_workitems import MemorySession

TENANT_ID = 100
VERSION_ID = 200
_RESERVED = (
    "AUTOWONDER_SECRET",
    "autowonder_secret",
    "CODEX_HOME",
    "codex_home",
    "CLAUDE_CONFIG_DIR",
    "claude_config_dir",
    "QODER_CONFIG_DIR",
    "qoder_config_dir",
    "QODERCN_CONFIG_DIR",
    "qodercn_config_dir",
    "QODER_INTEGRATION_ID",
    "qoder_integration_id",
    "QODER_HOST_SERVICE_NAME",
    "qoder_host_service_name",
)


def test_sorted_snapshot_rejects_later_mutation() -> None:
    """一条快照按名称排序，解密后的映射不能再改。"""
    snapshot = resolve_rows(
        TENANT_ID,
        VERSION_ID,
        [_row(2, "ZETA", "ref-z"), _row(1, "ALPHA", "ref-a")],
        _values({"ref-a": "value-a", "ref-z": "value-z"}),
    )
    assert list(snapshot) == ["ALPHA", "ZETA"]
    assert snapshot["ALPHA"] == "value-a"
    assert snapshot["ZETA"] == "value-z"
    with pytest.raises(TypeError):
        snapshot["EXTRA"] = "value"  # type: ignore[index]


def test_empty_snapshot_does_not_decrypt() -> None:
    """没有引用时直接返回空映射。"""
    snapshot = resolve_rows(TENANT_ID, VERSION_ID, [], _forbid_decrypt)
    assert dict(snapshot) == {}


def test_decrypts_current_value_on_every_call() -> None:
    """每次解析都重新解密，不复用上一次明文。"""
    first = resolve_rows(TENANT_ID, VERSION_ID, [_row(1, "TOKEN", "ref-old")], _echo)
    second = resolve_rows(TENANT_ID, VERSION_ID, [_row(1, "TOKEN", "ref-new")], _echo)
    assert first["TOKEN"] == "ref-old"
    assert second["TOKEN"] == "ref-new"


def test_missing_join_fails_before_decrypt() -> None:
    """变量已删除或跨空间时，错误里不出现密文，也不调用解密。"""
    invalid = _row(1, None, None)
    invalid.variable_id = None
    invalid.variable_tenant_id = None
    with pytest.raises(EnvironmentSnapshotResolutionException) as caught:
        resolve_rows(TENANT_ID, VERSION_ID, [invalid], _forbid_decrypt)
    assert "credential" not in str(caught.value)


def test_duplicate_name_fails_before_decrypt() -> None:
    """忽略大小写后重名则整份快照失败。"""
    rows = [_row(1, "TOKEN", "ref-one"), _row(2, "token", "ref-two")]
    with pytest.raises(EnvironmentSnapshotResolutionException):
        resolve_rows(TENANT_ID, VERSION_ID, rows, _forbid_decrypt)


@pytest.mark.parametrize("name", _RESERVED)
def test_reserved_name_fails_before_decrypt(name: str) -> None:
    """保留名按大小写不敏感拒绝，消息里不带回密文。"""
    with pytest.raises(EnvironmentSnapshotResolutionException) as caught:
        resolve_rows(TENANT_ID, VERSION_ID, [_row(1, name, "sensitive-ref")], _forbid_decrypt)
    assert "sensitive-ref" not in str(caught.value)


def test_decryption_failure_hides_reference_and_cause() -> None:
    """解密异常换成固定消息，不再带上原来的原因。"""

    def decrypt(credential_ref: str) -> str:
        raise RuntimeError("failed sensitive-ref secret-value")

    with pytest.raises(EnvironmentSnapshotResolutionException) as caught:
        resolve_rows(TENANT_ID, VERSION_ID, [_row(1, "TOKEN", "sensitive-ref")], decrypt)
    assert caught.value.__cause__ is None
    assert "sensitive-ref" not in str(caught.value)
    assert "secret-value" not in str(caught.value)


def test_null_decryption_fails() -> None:
    """解密结果为空时同样拒绝。"""

    def decrypt(credential_ref: str) -> None:
        return None

    with pytest.raises(EnvironmentSnapshotResolutionException):
        resolve_rows(TENANT_ID, VERSION_ID, [_row(1, "TOKEN", "sensitive-ref")], decrypt)


def test_resolution_query_is_one_left_join() -> None:
    """解析只发一条左连接，并带上空间、版本和未删除条件。"""
    compiled = resolution_snapshot_statement(TENANT_ID, VERSION_ID).compile(
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "LEFT OUTER JOIN" in sql
    assert "agent_environment_variable_ref.tenant_id = 100" in sql
    assert "agent_environment_variable_ref.agent_version_id = 200" in sql
    assert "environment_variable.is_deleted = 0" in sql


def test_recovery_claim_route_is_registered() -> None:
    """续认领路径已挂到 daemon 派发路由上。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "post" in paths["/api/daemon/dispatches/{dispatchId}/recovery-claim"]


@pytest.mark.asyncio
async def test_renews_lease_for_owned_running_dispatch() -> None:
    """仍在运行且认领成功时返回状态和最新环境变量，并禁止缓存。"""
    session = await _loaded("RUNNING", 501)
    seen: list[tuple[int, int]] = []

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        seen.append((tenant_id, agent_version_id))
        return {"TOKEN": "latest"}

    status, body = await claim_recovery(session, 99, "tok", resolve=resolve)
    assert status == 200
    assert body == {
        "allowed": True,
        "status": "RUNNING",
        "environmentVariables": {"TOKEN": "latest"},
    }
    assert seen == [(10, 501)]
    response = claim_http_response(status, body)
    assert response.headers["cache-control"] == "no-store"
    assert _dispatch(session).gmt_modified != datetime(2020, 1, 1)


@pytest.mark.asyncio
async def test_terminal_status_does_not_claim() -> None:
    """栅栏未挡住时，已经取消的派发返回 409，并且不刷新修改时间。"""
    session = await _loaded("CANCELED", 501)
    status, body = await claim_recovery(session, 99, "tok", fence=_open)
    assert status == 409
    assert body == {"allowed": False, "error": "dispatch is no longer recoverable"}
    assert _dispatch(session).gmt_modified == datetime(2020, 1, 1)


@pytest.mark.asyncio
async def test_lost_claim_returns_conflict() -> None:
    """并发把活动行改走之后，认领返回 409。"""
    session = await _loaded("ACKED", 501)

    async def claim(
        current: AsyncSession, dispatch_id: int, tenant_id: int, executor_id: int
    ) -> int:
        return 0

    status, body = await claim_recovery(session, 99, "tok", claim=claim)
    assert status == 409
    assert body is not None
    assert body["allowed"] is False


@pytest.mark.asyncio
async def test_empty_environment_still_resolves() -> None:
    """认领成功且版本没有变量时，仍调用解析并返回空对象。"""
    session = await _loaded("ACKED", 501)
    calls = 0

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        nonlocal calls
        calls += 1
        return {}

    status, body = await claim_recovery(session, 99, "tok", resolve=resolve)
    assert status == 200
    assert body is not None
    assert body["environmentVariables"] == {}
    assert calls == 1


@pytest.mark.asyncio
async def test_missing_version_skips_resolve() -> None:
    """没有冻结版本时环境变量是空对象，且不解析。"""
    session = await _loaded("DISPATCHED", None)

    async def resolve(
        current: AsyncSession, tenant_id: int, agent_version_id: int
    ) -> Mapping[str, str]:
        raise AssertionError(agent_version_id)

    status, body = await claim_recovery(session, 99, "tok", resolve=resolve)
    assert status == 200
    assert body is not None
    assert body["environmentVariables"] == {}


@pytest.mark.asyncio
async def test_bad_token_is_empty_unauthorized() -> None:
    """令牌不对时正文为空。"""
    session = await _loaded("RUNNING", 501)
    status, body = await claim_recovery(session, 99, "other")
    assert status == 401
    assert body is None
    assert claim_http_response(status, body).body == b""


@pytest.mark.asyncio
async def test_canceled_dispatch_is_fenced() -> None:
    """真实栅栏把已取消的派发挡在认领之前。"""
    session = await _loaded("CANCELED", 501)
    status, body = await claim_recovery(session, 99, "tok")
    assert status == 401
    assert body is None
    assert _dispatch(session).gmt_modified == datetime(2020, 1, 1)


def _row(variable_id: int, name: str | None, credential_ref: str | None) -> SnapshotRow:
    return SnapshotRow(
        TENANT_ID,
        VERSION_ID,
        variable_id,
        variable_id,
        TENANT_ID,
        name,
        credential_ref,
    )


def _values(mapping: dict[str, str]):
    def decrypt(credential_ref: str) -> str:
        return mapping[credential_ref]

    return decrypt


def _echo(credential_ref: str) -> str:
    return credential_ref


def _forbid_decrypt(credential_ref: str) -> str:
    raise AssertionError(credential_ref)


async def _open(session: AsyncSession, dispatch_id: int) -> bool:
    return False


async def _loaded(status: str, agent_version_id: int | None) -> MemorySession:
    session = MemorySession()
    session.add(
        Dispatch(
            id=99,
            tenant_id=10,
            workitem_id=20,
            agent_id=30,
            agent_version_id=agent_version_id,
            executor_id=77,
            status=status,
            idempotency_key="claim",
            is_deleted=0,
            gmt_modified=datetime(2020, 1, 1),
        )
    )
    session.add(
        Executor(
            id=77,
            tenant_id=10,
            agent_id=30,
            name="executor",
            token_ref="b64:" + base64.b64encode(b"tok").decode("ascii"),
            is_deleted=0,
        )
    )
    await session.flush()
    return session


def _dispatch(session: MemorySession) -> Dispatch:
    return next(row for row in session.rows if isinstance(row, Dispatch))
