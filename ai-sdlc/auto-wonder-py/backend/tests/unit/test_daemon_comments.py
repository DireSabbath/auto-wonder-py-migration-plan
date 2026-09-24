"""执行器评论和工单状态。这些检查不连接 MySQL。"""

import base64
import json
from datetime import datetime

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from autowonder.agents.models import Agent
from autowonder.audits.models import AuditLog
from autowonder.core.errors import BizError
from autowonder.dispatch.models import Dispatch
from autowonder.executors.models import Executor
from autowonder.main import create_app
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.statemachines.models import StatusNode, StatusTransition
from autowonder.users.models import User
from autowonder.workitems.daemon_router import (
    parse_target_human_ids,
    submit_daemon_comment,
    submit_daemon_workitem_status,
)
from autowonder.workitems.models import (
    Workitem,
    WorkitemComment,
    WorkitemCommentMention,
    WorkitemEvent,
)
from autowonder.workspaces.models import OrgMember
from tests.unit.test_workitems import MemorySession


def test_daemon_comment_routes_are_registered() -> None:
    """评论和状态路径挂在 daemon 白名单下。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "post" in paths["/api/daemon/dispatches/{dispatchId}/comments"]
    assert "post" in paths["/api/daemon/dispatches/{dispatchId}/workitem-status"]
    response = client.post("/api/daemon/dispatches/500/comments")
    assert response.status_code == 422


def test_target_human_ids_follow_java_number_rules() -> None:
    """布尔值跳过，浮点截断，非集合当成没有目标。"""
    assert parse_target_human_ids([10000, 10000.9, True, "10000", None]) == [10000, 10000]
    assert parse_target_human_ids({"user": 1}) == []
    assert parse_target_human_ids("10000") == []


@pytest.mark.asyncio
async def test_unavailable_scheduled_status_fails_before_workitem_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """能力未就绪时状态接口在改工单之前抛出 30006。"""
    session = await _scene("SCHEDULED_TASK_RUN", None)
    _disable_scheduled(monkeypatch)
    with pytest.raises(BizError) as caught:
        await submit_daemon_workitem_status(session, 500, "tok", {"status": "verifying"})
    assert caught.value.code == "30006"
    assert _workitem(session).version == 0
    assert _audits(session) == []


@pytest.mark.asyncio
async def test_available_scheduled_status_is_rejected_without_workitem_mutation() -> None:
    """能力就绪时，定时任务运行仍然不能改工单状态。"""
    session = await _scene("SCHEDULED_TASK_RUN", None)
    response = await submit_daemon_workitem_status(session, 500, "tok", {"status": "verifying"})
    assert response.status_code == 409
    assert _json(response) == {
        "error": "scheduled task runs do not support workitem status mutation"
    }
    assert _workitem(session).version == 0
    assert _audits(session) == []
    assert _comments(session) == []


@pytest.mark.asyncio
async def test_comment_records_agent_audit_log() -> None:
    """工单评论写审计，并按原文返回评论。"""
    session = await _scene("WORKITEM", None)
    response = await submit_daemon_comment(session, 500, "tok", {"contentMd": "done"})
    assert response.status_code == 200
    body = _json(response)
    assert body["authorType"] == "AGENT"
    assert body["authorRef"] == 300
    assert body["workitemId"] == 200
    assert body["contentMd"] == "done"
    assert isinstance(body["gmtCreate"], int)
    assert "success" not in body
    comment = _comments(session)[0]
    assert comment.source_type == "WORKITEM"
    assert comment.author_type == "AGENT"
    audit = _audits(session)[0]
    detail = audit.detail_json
    assert audit.tenant_id == 100
    assert audit.actor_id == 300
    assert audit.module == "WORKITEM"
    assert audit.action == "CREATE_WORKITEM_COMMENT"
    assert audit.target_type == "workitem"
    assert audit.target_id == 200
    assert detail["actorType"] == "AGENT"
    assert detail["triggerType"] == "EVENT"
    assert detail["triggerSource"] == "DAEMON_CALLBACK"
    assert detail["eventType"] == "daemon.comment"
    assert detail["dispatchId"] == 500
    assert detail["sourceType"] == "WORKITEM"
    assert detail["contentLength"] == 4
    events = [row for row in session.rows if isinstance(row, WorkitemEvent)]
    assert events[0].event_type == "COMMENT"
    assert events[0].actor_type == "AGENT"


@pytest.mark.asyncio
async def test_workitem_status_records_agent_audit_log() -> None:
    """状态编码流转后，审计记下目标和事件。"""
    session = await _scene("WORKITEM", None)
    response = await submit_daemon_workitem_status(session, 500, "tok", {"status": "verifying"})
    assert response.status_code == 200
    body = _json(response)
    assert body["id"] == 200
    assert body["statusNodeId"] == 12
    assert body["version"] == 1
    assert "success" not in body
    stored = _workitem(session)
    assert stored.status_node_id == 12
    assert stored.modifier_id == 300
    audit = _audits(session)[0]
    assert audit.action == "UPDATE_WORKITEM_STATUS"
    assert audit.target_type == "workitem"
    assert audit.target_id == 200
    assert audit.detail_json["eventType"] == "daemon.workitem-status"
    assert audit.detail_json["status"] == "verifying"
    assert audit.detail_json["actorType"] == "AGENT"
    events = [row for row in session.rows if isinstance(row, WorkitemEvent)]
    assert events[0].event_type == "STATUS_CHANGE"
    assert events[0].actor_type == "AGENT"
    assert events[0].from_val == "new"
    assert events[0].to_val == "verifying"


@pytest.mark.asyncio
async def test_interaction_dispatch_rejects_direct_comment_and_status() -> None:
    """三种交互调度都不能直接写评论或状态。"""
    for mode in ("COMMENT_INTERACTION", "SIDE_INTERACTION", "CANONICAL_INTERACTION"):
        session = await _scene("WORKITEM", mode)
        comment_response = await submit_daemon_comment(
            session, 500, "tok", {"contentMd": "duplicate"}
        )
        status_response = await submit_daemon_workitem_status(
            session, 500, "tok", {"status": "verifying"}
        )
        assert comment_response.status_code == 409
        assert status_response.status_code == 409
        assert _json(comment_response)["error"] == (
            "interaction dispatch replies are delivered through TASK_GUIDANCE_ACK"
        )
        assert _comments(session) == []
        assert _audits(session) == []
        assert _workitem(session).version == 0


@pytest.mark.asyncio
async def test_scheduled_run_comment_forwards_target_human_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定时任务评论把显式真人写成运行提及，不写工单指引。"""
    _silence_redis(monkeypatch)
    session = await _scene("SCHEDULED_TASK_RUN", None)
    session.add(
        ScheduledTaskRun(
            id=200,
            workspace_id=100,
            scheduled_task_id=9,
            trigger_key="once",
            trigger_type="MANUAL",
            scheduled_at=datetime(2026, 9, 24, 8, 0, 0),
            status="RUNNING",
            squad_id=1,
            initial_agent_id=300,
            session_mode="ISOLATED",
            execution_snapshot_json={},
            owner_id=10000,
            creator_id=10000,
        )
    )
    session.add(
        User(
            id=10000,
            username="cai",
            nickname="蔡何",
            password_hash="hash",
            status=0,
            is_deleted=0,
        )
    )
    session.add(OrgMember(tenant_id=100, user_id=10000, status=0, is_deleted=0))
    await session.flush()
    response = await submit_daemon_comment(
        session,
        500,
        "tok",
        {"contentMd": "分析完成 @蔡何", "targetHumanIds": [10000]},
    )
    assert response.status_code == 200
    comment = _comments(session)[0]
    assert comment.source_type == "SCHEDULED_TASK_RUN"
    assert comment.workitem_id == 200
    assert comment.content_md == "分析完成 @蔡何"
    mentions = [row for row in session.rows if isinstance(row, WorkitemCommentMention)]
    assert len(mentions) == 1
    assert mentions[0].source_type == "SCHEDULED_TASK_RUN"
    assert mentions[0].target_type == "HUMAN"
    assert mentions[0].target_ref == 10000
    assert mentions[0].display_name_snapshot == "蔡何"
    audit = _audits(session)[0]
    assert audit.module == "SCHEDULED_TASK"
    assert audit.target_type == "scheduled_task_run"
    assert audit.detail_json["runId"] == 200
    assert audit.detail_json["taskId"] == 9
    assert audit.detail_json["sourceType"] == "SCHEDULED_TASK_RUN"
    assert [row for row in session.rows if isinstance(row, WorkitemEvent)] == []


@pytest.mark.asyncio
async def test_blank_content_and_status_are_rejected() -> None:
    """空白正文和空白状态在写入之前返回 400。"""
    session = await _scene("WORKITEM", None)
    comment_response = await submit_daemon_comment(session, 500, "tok", {"contentMd": "  "})
    status_response = await submit_daemon_workitem_status(session, 500, "tok", {"status": " "})
    assert comment_response.status_code == 400
    assert _json(comment_response) == {"error": "contentMd required"}
    assert status_response.status_code == 400
    assert _json(status_response) == {"error": "status required"}
    assert _comments(session) == []
    assert _workitem(session).status_node_id == 11


@pytest.mark.asyncio
async def test_non_run_scheduled_source_conflicts() -> None:
    """定时任务定义本身不能通过这条路径写评论。"""
    session = await _scene("SCHEDULED_TASK", None)
    response = await submit_daemon_comment(session, 500, "tok", {"contentMd": "done"})
    assert response.status_code == 409
    assert response.body == b""
    assert _comments(session) == []


@pytest.mark.asyncio
async def test_bad_token_skips_fence_and_canceled_dispatch_is_empty_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """令牌失败时不查栅栏。已取消的调度返回空 401。"""

    async def boom(session: object, dispatch_id: int) -> bool:
        raise AssertionError(dispatch_id)

    monkeypatch.setattr("autowonder.workitems.daemon_router.load_mutation_fence", boom)
    session = await _scene("WORKITEM", None)
    denied = await submit_daemon_comment(session, 500, "other", {"contentMd": "done"})
    assert denied.status_code == 401
    assert denied.body == b""
    monkeypatch.undo()
    canceled = await _scene("WORKITEM", None, "CANCELED")
    fenced = await submit_daemon_comment(canceled, 500, "tok", {"contentMd": "done"})
    assert fenced.status_code == 401
    assert fenced.body == b""
    assert _comments(canceled) == []


def _disable_scheduled(monkeypatch: pytest.MonkeyPatch) -> None:
    class Flags:
        scheduled_task_enabled = False
        scheduled_task_cluster_ready = True

    monkeypatch.setattr("autowonder.scheduledtasks.capability.get_settings", lambda: Flags())


def _silence_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    class Redis:
        async def publish(self, channel: str, message: str) -> int:
            return 1

        async def lpush(self, key: str, value: str) -> int:
            return 1

    client = Redis()
    monkeypatch.setattr("autowonder.scheduledtasks.notify.redis_client", lambda: client)
    monkeypatch.setattr("autowonder.notifications.service.redis_client", lambda: client)


def _json(response: Response) -> dict[str, object]:
    return json.loads(response.body)


def _comments(session: MemorySession) -> list[WorkitemComment]:
    return [row for row in session.rows if isinstance(row, WorkitemComment)]


def _audits(session: MemorySession) -> list[AuditLog]:
    return [row for row in session.rows if isinstance(row, AuditLog)]


def _workitem(session: MemorySession) -> Workitem:
    return next(row for row in session.rows if isinstance(row, Workitem))


def _token(plain: str) -> str:
    return "b64:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")


async def _scene(
    source_type: str, resume_mode: str | None, status: str = "RUNNING"
) -> MemorySession:
    session = MemorySession()
    now = datetime(2026, 9, 24, 8, 0, 0)
    session.add(
        Executor(
            id=8,
            tenant_id=100,
            agent_id=300,
            name="executor",
            token_ref=_token("tok"),
            is_deleted=0,
        )
    )
    session.add(
        Agent(id=300, tenant_id=100, name="分析员", is_deleted=0, gmt_create=now, gmt_modified=now)
    )
    session.add(
        Dispatch(
            id=500,
            tenant_id=100,
            source_type=source_type,
            workitem_id=200,
            agent_id=300,
            executor_id=8,
            status=status,
            idempotency_key="comment",
            resume_mode=resume_mode,
            is_deleted=0,
        )
    )
    session.add(
        Workitem(
            id=200,
            tenant_id=100,
            work_type="TASK",
            title="工单",
            template_id=10,
            status_node_id=11,
            version=0,
            is_deleted=0,
            gmt_create=now,
            gmt_modified=now,
        )
    )
    session.add(
        StatusNode(
            id=11,
            tenant_id=100,
            template_id=10,
            code="new",
            name="新建",
            category="INIT",
            sort=0,
            gmt_create=now,
        )
    )
    session.add(
        StatusNode(
            id=12,
            tenant_id=100,
            template_id=10,
            code="verifying",
            name="验收",
            category="IN_PROGRESS",
            sort=1,
            gmt_create=now,
        )
    )
    session.add(
        StatusTransition(
            id=13,
            tenant_id=100,
            template_id=10,
            from_node_id=11,
            to_node_id=12,
            name="验收",
            gmt_create=now,
        )
    )
    await session.flush()
    return session
