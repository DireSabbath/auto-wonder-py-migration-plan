"""执行器任务用量上报。这些检查不连接 MySQL。"""

import base64
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.aiusage.daemon_router import (
    TaskUsageEntry,
    TaskUsageReportRequest,
    chosen_dispatch_text,
    header_token,
    parse_dispatch_id,
    report_task_usage,
    usage_authorization_token,
    usage_entries,
    usage_http_response,
)
from autowonder.aiusage.dispatch_usage import ingest_usage_artifact, record_task_usage
from autowonder.dispatch.models import Dispatch
from autowonder.executors.models import Executor
from autowonder.main import create_app
from tests.unit.test_workitems import MemorySession


def test_task_usage_route_is_registered() -> None:
    """用量上报挂在 daemon 前缀下。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "post" in paths["/api/daemon/tasks/{taskId}/usage"]


def test_parse_dispatch_id_matches_java_long() -> None:
    """只接受有符号 64 位十进制，不接受空白、下划线或溢出。"""
    assert parse_dispatch_id("99") == 99
    assert parse_dispatch_id("+99") == 99
    assert parse_dispatch_id("-1") == -1
    assert parse_dispatch_id("9223372036854775807") == 9223372036854775807
    assert parse_dispatch_id("-9223372036854775808") == -9223372036854775808
    assert parse_dispatch_id("9223372036854775808") is None
    assert parse_dispatch_id("-9223372036854775809") is None
    assert parse_dispatch_id(" 99") is None
    assert parse_dispatch_id("99 ") is None
    assert parse_dispatch_id("1_000") is None
    assert parse_dispatch_id("task-x") is None
    assert parse_dispatch_id("") is None
    assert parse_dispatch_id("+") is None


def test_token_and_dispatch_id_selection() -> None:
    """查询令牌优先。空白查询参数回落到头。非空白 dispatchId 覆盖任务号。"""
    assert header_token(None) is None
    assert header_token("Bearer tok") == "tok"
    assert header_token("Bearer ") == ""
    assert header_token("tok") == "tok"
    assert header_token("bearer tok") == "bearer tok"
    assert usage_authorization_token("tok", "Bearer other") == "tok"
    assert usage_authorization_token("  ", "Bearer tok") == "tok"
    assert usage_authorization_token(None, None) is None
    assert chosen_dispatch_text("99", None) == "99"
    assert chosen_dispatch_text("task-x", "99") == "99"
    assert chosen_dispatch_text("99", "  ") == "99"
    body = TaskUsageReportRequest(
        usage=[TaskUsageEntry(provider="codex", model="gpt-5", input_tokens=1)]
    )
    assert usage_entries(None) is None
    assert usage_entries(TaskUsageReportRequest()) is None
    assert usage_entries(body) == [{"provider": "codex", "model": "gpt-5", "input_tokens": 1}]


async def test_bearer_token_records_usage() -> None:
    """没有查询令牌时，Bearer 头通过后写入用量。"""
    session = await _ready()
    status, body = await report_task_usage(
        session,
        "99",
        None,
        None,
        "Bearer tok",
        [{"provider": "codex", "model": "gpt-5", "input_tokens": 1, "output_tokens": 2}],
    )
    assert status == 200
    assert body == {"status": "accepted"}
    values = _values(session.statements[0])
    assert values["tenant_id"] == 10
    assert values["workitem_id"] == 20
    assert values["dispatch_id"] == 99
    assert values["agent_id"] == 30
    assert values["executor_id"] == 40
    assert values["artifact_id"] is None
    assert values["provider"] == "codex"
    assert values["model"] == "gpt-5"
    assert values["total_tokens"] == 3
    response = usage_http_response(status, body)
    assert response.status_code == 200


async def test_opaque_task_id_uses_dispatch_override_and_query_token() -> None:
    """路径里的不透明任务号用 dispatchId 和查询令牌。"""
    session = await _ready()
    status, body = await report_task_usage(
        session,
        "task-x",
        "99",
        "tok",
        None,
        [{"provider": "codex", "model": "gpt-5"}],
    )
    assert status == 200
    assert body == {"status": "accepted"}
    assert _values(session.statements[0])["dispatch_id"] == 99


async def test_plain_authorization_header_is_the_token() -> None:
    """没有 Bearer 前缀时，整段 Authorization 就是令牌。"""
    session = await _ready()
    status, body = await report_task_usage(
        session,
        "+99",
        None,
        None,
        "tok",
        [{"provider": "codex", "model": "gpt-5"}],
    )
    assert status == 200
    assert body == {"status": "accepted"}
    assert len(session.statements) == 1


async def test_invalid_task_id_does_not_authenticate() -> None:
    """调度号非法时不鉴权、不写库。"""
    session = UsageSession()
    status, body = await report_task_usage(
        session,
        "task-x",
        None,
        "tok",
        None,
        [],
    )
    assert status == 400
    assert body == {"error": "invalid dispatch id"}
    assert session.scalars == 0
    assert session.statements == []
    assert usage_http_response(status, body).status_code == 400


async def test_blank_or_bad_token_is_unauthorized() -> None:
    """空令牌和错误令牌都是空 401，且不写用量。"""
    blank = UsageSession()
    status, body = await report_task_usage(blank, "99", None, "   ", None, [{"provider": "codex"}])
    assert status == 401
    assert body is None
    assert blank.scalars == 0
    assert usage_http_response(status, body).body == b""

    session = await _ready()
    status, body = await report_task_usage(
        session, "99", None, "other", None, [{"provider": "codex"}]
    )
    assert status == 401
    assert body is None
    assert session.statements == []


async def test_empty_usage_is_accepted_without_write() -> None:
    """鉴权通过后，空用量仍接受，但不插入。"""
    session = await _ready()
    status, body = await report_task_usage(session, "99", None, "tok", None, None)
    assert status == 200
    assert body == {"status": "accepted"}
    assert session.statements == []

    status, body = await report_task_usage(session, "99", None, "tok", None, [])
    assert status == 200
    assert session.statements == []


async def test_missing_deleted_or_foreign_dispatch_skips_write() -> None:
    """调度不存在、已删除或空间不一致时不写库。"""
    missing = UsageSession()
    await record_task_usage(missing, 10, 99, [{"provider": "codex", "model": "gpt-5"}])
    assert missing.statements == []

    deleted = await _ready()
    _dispatch(deleted).is_deleted = 1
    await record_task_usage(deleted, 10, 99, [{"provider": "codex", "model": "gpt-5"}])
    assert deleted.statements == []

    foreign = await _ready()
    await record_task_usage(foreign, 11, 99, [{"provider": "codex", "model": "gpt-5"}])
    assert foreign.statements == []


async def test_endpoint_and_artifact_share_usage_identity() -> None:
    """接口上报的产物号为空，产物入库带上产物号，其余身份相同。"""
    session = await _ready()
    await record_task_usage(
        session,
        10,
        99,
        [{"provider": "codex", "model": "gpt-5", "input_tokens": 1, "output_tokens": 2}],
    )
    await ingest_usage_artifact(
        session,
        10,
        20,
        99,
        77,
        "observability/usage.json",
        "bucket/key",
        b'{"usage":[{"provider":"codex","model":"gpt-5","input_tokens":3,"output_tokens":4}]}',
    )
    first = _values(session.statements[0])
    second = _values(session.statements[1])
    assert first["tenant_id"] == second["tenant_id"]
    assert first["dispatch_id"] == second["dispatch_id"]
    assert first["provider"] == second["provider"]
    assert first["model"] == second["model"]
    assert first["artifact_id"] is None
    assert second["artifact_id"] == 77
    assert second["total_tokens"] == 7


async def test_step_credits_and_invalid_tokens() -> None:
    """分步编号、积分和空步骤按 Java 归一。负数和非整数 token 记 0。"""
    session = await _ready()
    content = (
        b'{"usage":['
        b'{"provider":"qoder","model":"auto","step_id":"400554","input_tokens":500,'
        b'"output_tokens":50,"reasoning_tokens":10,"credits":1.5},'
        b'{"provider":"qoder","model":"auto","step_id":"400555","input_tokens":300,'
        b'"output_tokens":30,"reasoning_tokens":5,"credits":0.8}'
        b"]}"
    )
    await ingest_usage_artifact(
        session, 10, 20, 99, 77, "observability/usage.json", "bucket/key", content
    )
    first = _values(session.statements[0])
    second = _values(session.statements[1])
    assert first["step_id"] == "400554"
    assert second["step_id"] == "400555"
    assert first["input_tokens"] == 500
    assert second["input_tokens"] == 300
    assert first["reasoning_tokens"] == 10
    assert second["reasoning_tokens"] == 5
    assert first["credits"] == Decimal("1.5")

    credited = UsageSession()
    credited.rows.append(_dispatch_row(0))
    await record_task_usage(
        credited,
        10,
        99,
        [
            {
                "provider": "qoder",
                "model": "auto",
                "step_id": "700001",
                "input_tokens": 1000,
                "output_tokens": 100,
                "reasoning_tokens": 50,
                "credits": 2.5,
            }
        ],
    )
    usage = _values(credited.statements[0])
    assert usage["step_id"] == "700001"
    assert usage["reasoning_tokens"] == 50
    assert usage["credits"] == Decimal("2.5")
    assert usage["total_tokens"] == 1150

    blank_step = UsageSession()
    blank_step.rows.append(_dispatch_row(0))
    await record_task_usage(
        blank_step,
        10,
        99,
        [{"provider": "qoder", "model": "auto", "input_tokens": 100, "output_tokens": 10}],
    )
    assert _values(blank_step.statements[0])["step_id"] == ""

    normalized = UsageSession()
    normalized.rows.append(_dispatch_row(0))
    await record_task_usage(
        normalized,
        10,
        99,
        [
            {
                "provider": "  ",
                "model": None,
                "input_tokens": -3,
                "output_tokens": True,
                "cache_read_tokens": "4",
            }
        ],
    )
    cleaned = _values(normalized.statements[0])
    assert cleaned["provider"] == "unknown"
    assert cleaned["model"] == "unknown"
    assert cleaned["input_tokens"] == 0
    assert cleaned["output_tokens"] == 0
    assert cleaned["cache_read_tokens"] == 0
    assert cleaned["total_tokens"] == 0
    assert cleaned["credits"] is None


async def test_bad_json_empty_and_other_artifacts_do_not_persist() -> None:
    """坏 JSON、空数组和非用量文件都不写库，坏 JSON 也不抛出。"""
    session = await _ready()
    await ingest_usage_artifact(
        session,
        10,
        20,
        99,
        77,
        "observability/usage.json",
        "bucket/key",
        b"bad json",
    )
    await ingest_usage_artifact(
        session,
        10,
        20,
        99,
        77,
        "observability/usage.json",
        "bucket/key",
        b'{"usage":[]}',
    )
    await ingest_usage_artifact(
        session,
        10,
        20,
        99,
        77,
        "artifacts/output/report.md",
        "bucket/key",
        b"hello",
    )
    assert session.statements == []


class UsageSession(MemorySession):
    """记下用量写入，避免内存会话执行 MySQL upsert。"""

    def __init__(self) -> None:
        super().__init__()
        self.statements: list[object] = []
        self.scalars = 0

    async def scalar(self, statement: object) -> object:
        self.scalars += 1
        return await MemorySession.scalar(self, statement)

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return _Ack()


class _Ack:
    rowcount = 1


def _values(statement: object) -> dict[str, object]:
    compiled = statement.compile(dialect=mysql.dialect())
    return dict(compiled.params)


def _token(plain: str) -> str:
    return "b64:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")


def _dispatch_row(deleted: int) -> Dispatch:
    return Dispatch(
        id=99,
        tenant_id=10,
        workitem_id=20,
        agent_id=30,
        executor_id=40,
        status="RUNNING",
        idempotency_key="usage",
        is_deleted=deleted,
    )


def _dispatch(session: UsageSession) -> Dispatch:
    return next(row for row in session.rows if isinstance(row, Dispatch))


async def _ready() -> UsageSession:
    session = UsageSession()
    session.add(
        Executor(
            id=40,
            tenant_id=10,
            agent_id=30,
            name="executor",
            token_ref=_token("tok"),
            is_deleted=0,
        )
    )
    session.add(_dispatch_row(0))
    await session.flush()
    return session
