"""执行器帧的字段、幂等键和调度状态允许集合。这些检查不访问 Redis。"""

import json

import pytest
from starlette.websockets import WebSocketState

from autowonder.dispatch.enqueue import workitem_idempotency_key
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.transport import deliver_pause
from autowonder.ws.frames import (
    DEBUG_LOG_MAX_BYTES,
    ArtifactUploadedFrame,
    DebugLogDirective,
    HeartbeatFrame,
    TaskAckFrame,
    TaskDispatchFrame,
    TaskHandoffFrame,
    TaskProgressFrame,
    TaskResultFrame,
    build_task_dispatch_frame,
    dump_frame,
    enabled_debug_log,
    parse_inbound_object,
    task_pause_frame,
    task_result_ack,
)
from autowonder.ws.inbound import (
    ACK_ALLOWED_STATUSES,
    DISPATCH_FRAME_TYPES,
    FAILOVER_SOURCE_STATUSES,
    PROGRESS_ALLOWED_STATUSES,
    RESULT_BLOCKED_STATUSES,
    RESULT_MUTABLE_STATUSES,
    TERMINAL_STATUSES,
    ack_target,
    progress_target,
    protocol_features,
    result_accepted,
)
from autowonder.ws.presence import normalize_capacity
from autowonder.ws.session import ExecutorSession, session_registry

_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_protocol_samples_round_trip() -> None:
    """§5 的样例按 camelCase 来回，null 不写出。"""
    ack = TaskAckFrame.model_validate({"type": "TASK_ACK", "dispatchId": 300001})
    assert json.loads(dump_frame(ack)) == {"type": "TASK_ACK", "dispatchId": 300001}

    progress = TaskProgressFrame.model_validate(
        {"type": "TASK_PROGRESS", "dispatchId": 300001, "log": "step.started"}
    )
    assert json.loads(dump_frame(progress)) == {
        "type": "TASK_PROGRESS",
        "dispatchId": 300001,
        "log": "step.started",
    }

    result = TaskResultFrame.model_validate(
        {
            "type": "TASK_RESULT",
            "dispatchId": 300001,
            "success": True,
            "resultSummary": "已完成并产出交付物",
            "error": "",
        }
    )
    assert json.loads(dump_frame(result))["error"] == ""
    assert json.loads(dump_frame(result))["success"] is True

    handoff = TaskHandoffFrame.model_validate(
        {
            "type": "TASK_HANDOFF",
            "dispatchId": 300001,
            "workitemId": 500001,
            "to": "reviewer",
            "toType": "AGENT",
            "nextRole": "reviewer",
            "reason": "编码与自测完成，交接评审",
        }
    )
    assert json.loads(dump_frame(handoff))["toType"] == "AGENT"
    assert "from" not in json.loads(dump_frame(handoff))

    uploaded = ArtifactUploadedFrame.model_validate(
        {
            "type": "ARTIFACT_UPLOADED",
            "dispatchId": 300001,
            "workitemId": 500001,
            "name": "deliverables/report.md",
            "artifactType": "DELIVERABLE",
            "ossRef": "oss://autowonder-artifact/report.md",
            "size": 1024,
            "metaJson": "{}",
        }
    )
    assert json.loads(dump_frame(uploaded))["artifactType"] == "DELIVERABLE"

    directive = enabled_debug_log()
    assert json.loads(dump_frame(directive)) == {
        "enabled": True,
        "maxBytes": DEBUG_LOG_MAX_BYTES,
    }
    dispatch = TaskDispatchFrame.model_validate(
        {
            "type": "TASK_DISPATCH",
            "dispatchId": 300001,
            "executorId": 900001,
            "tenantId": 100001,
            "workitemId": 500001,
            "attempt": 1,
            "downloadUrl": "https://oss.example/presigned/package.zip",
            "md5": "3bb338675acb7bd6fb050e6415a3d3a6",
            "size": 20481,
            "debugLog": {"enabled": True, "maxBytes": 209715200},
        }
    )
    body = json.loads(dump_frame(dispatch))
    assert body["debugLog"] == {"enabled": True, "maxBytes": 209715200}
    assert body["size"] == 20481


def test_unknown_fields_follow_fastjson_bean_rules() -> None:
    """写到 Bean 上的未知字段丢掉；原始 JSON 仍保留路由要读的键。"""
    ack = TaskAckFrame.model_validate({"type": "TASK_ACK", "dispatchId": 300001, "unexpected": 1})
    assert "unexpected" not in json.loads(dump_frame(ack))

    progress = TaskProgressFrame.model_validate(
        {
            "type": "TASK_PROGRESS",
            "dispatchId": 55,
            "resultSummary": "step.started",
            "stepOrder": 3,
        }
    )
    assert json.loads(dump_frame(progress)) == {"type": "TASK_PROGRESS", "dispatchId": 55}

    result = TaskResultFrame.model_validate(
        {
            "type": "TASK_RESULT",
            "dispatchId": 300001,
            "success": True,
            "debugLog": {"status": "UPLOADED", "channel": "DIRECT"},
        }
    )
    assert "debugLog" not in json.loads(dump_frame(result))
    raw = (
        '{"type":"TASK_RESULT","dispatchId":300001,"success":true,'
        '"debugLog":{"status":"UPLOADED","channel":"DIRECT"}}'
    )
    assert parse_inbound_object(raw)["debugLog"]["channel"] == "DIRECT"

    heartbeat = HeartbeatFrame.model_validate(
        {
            "type": "HEARTBEAT",
            "protocolFeatures": ["TASK_PACKAGE_SIGNATURE_V1", "DEBUG_LOG_V1"],
            "version": "1.2.3",
            "model": "qwen",
        }
    )
    assert json.loads(dump_frame(heartbeat)) == {"type": "HEARTBEAT"}
    assert protocol_features(
        {
            "protocolFeatures": ["TASK_PACKAGE_SIGNATURE_V1", "", "DEBUG_LOG_V1"],
        }
    ) == ["TASK_PACKAGE_SIGNATURE_V1", "DEBUG_LOG_V1"]


def test_handoff_from_keeps_java_field_name() -> None:
    """交接帧的 ``from`` 不改成 Python 关键字。"""
    frame = TaskHandoffFrame.model_validate(
        {"type": "TASK_HANDOFF", "from": "coder", "to": "reviewer", "toType": "HUMAN"}
    )
    assert frame.from_ == "coder"
    assert json.loads(dump_frame(frame))["from"] == "coder"


def test_dispatch_frame_keeps_idempotency_key() -> None:
    """幂等键原样来自调度行。Java 用例是 dispatch-identity，工单键是 工单:步骤:尝试。"""
    package_sha = _SHA
    frame = build_task_dispatch_frame(
        dispatch_id=99,
        executor_id=5,
        tenant_id=100,
        workitem_id=500,
        idempotency_key="dispatch-identity",
        agent_id=7,
        agent_version_id=8,
        sdlc_step_id=400164,
        attempt=1,
        download_url="https://oss/dl",
        md5="abc123",
        size=2048,
        sha256=package_sha,
        allow_commit=True,
        allow_push=False,
        allow_network=False,
        issuer="autowonder-server",
        signature_ref="sha256:key",
        signature="signed-envelope",
        signature_algorithm="ed25519",
        signature_public_key="public-key",
        expires_at="2026-08-07T04:00:00Z",
        dispatch_mcp_token="awdispatch_signed",
    )
    body = json.loads(dump_frame(frame))
    assert body["idempotencyKey"] == "dispatch-identity"
    assert body["checksum"] == "sha256:" + package_sha
    assert body["checksumScope"] == "zip_archive"
    assert body["packageId"] == "pkg_99"
    assert body["packageRefreshPath"] == "/api/daemon/dispatches/99/package-url"
    assert body["artifactUploadPath"] == "/api/daemon/dispatches/99/artifacts"
    assert body["checkpointUploadPath"] == "/api/daemon/dispatches/99/checkpoint"
    assert body["dispatchMcpToken"] == "awdispatch_signed"
    assert body["allowCommit"] is True
    assert body["allowPush"] is False
    assert body["environmentVariables"] == {}
    assert body["sdlcStepId"] == 400164

    workitem_key = workitem_idempotency_key(500001, 700001, 1)
    assert workitem_key == "500001:700001:1"
    copied = TaskDispatchFrame(idempotency_key=workitem_key)
    assert json.loads(dump_frame(copied))["idempotencyKey"] == workitem_key


def test_unsigned_package_omits_signature_fields() -> None:
    """没有签名时帧里不出现 signature 键，和 Fastjson 省略 null 一样。"""
    frame = build_task_dispatch_frame(
        dispatch_id=99,
        executor_id=5,
        tenant_id=100,
        workitem_id=500,
        idempotency_key="dispatch-identity",
        agent_id=7,
        agent_version_id=8,
        sdlc_step_id=400164,
        attempt=1,
        download_url="https://oss/dl",
        md5="abc123",
        size=2048,
        sha256=_SHA,
        allow_commit=True,
        allow_push=False,
        allow_network=False,
    )
    body = json.loads(dump_frame(frame))
    assert "signatureRef" not in body
    assert "signatureAlgorithm" not in body
    assert body["checksumScope"] == "zip_archive"


def test_debug_log_directive_fields() -> None:
    """debugLog 只有 enabled 与 maxBytes。"""
    directive = DebugLogDirective.model_validate({"enabled": True, "maxBytes": 10, "extra": 1})
    assert json.loads(dump_frame(directive)) == {"enabled": True, "maxBytes": 10}


def test_result_ack_shape() -> None:
    """结果确认三个字段与 Java 插入顺序一致。"""
    assert json.loads(task_result_ack(55, False)) == {
        "type": "TASK_RESULT_ACK",
        "dispatchId": 55,
        "accepted": False,
    }


def test_status_allow_sets_match_dispatch_service() -> None:
    """确认、进度、结果和故障转移只从 Java 允许的状态出发。"""
    assert TERMINAL_STATUSES == frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
    assert ACK_ALLOWED_STATUSES == frozenset({"DISPATCHED"})
    assert PROGRESS_ALLOWED_STATUSES == frozenset({"DISPATCHED", "ACKED"})
    assert RESULT_MUTABLE_STATUSES == frozenset(
        {
            "PENDING",
            "PACKAGING",
            "DISPATCHED",
            "ACKED",
            "RUNNING",
            "WAITING_FOR_PAUSE",
        }
    )
    assert RESULT_BLOCKED_STATUSES == frozenset({"PAUSING", "PAUSED", "PAUSE_FAILED", "CANCELED"})
    assert FAILOVER_SOURCE_STATUSES == frozenset({"DISPATCHED", "ACKED", "RUNNING"})
    assert DISPATCH_FRAME_TYPES == frozenset(
        {
            "TASK_ACK",
            "TASK_PROGRESS",
            "TASK_RESULT",
            "TASK_BUSY",
            "TASK_PAUSED",
            "TASK_PAUSE_FAILED",
            "TASK_GUIDANCE_ACK",
            "ARTIFACT_UPLOADED",
            "TASK_HANDOFF",
        }
    )
    assert ack_target("DISPATCHED") == "ACKED"
    assert ack_target("ACKED") is None
    assert ack_target("SUCCEEDED") is None
    assert progress_target("DISPATCHED") == "RUNNING"
    assert progress_target("ACKED") == "RUNNING"
    assert progress_target("RUNNING") is None
    assert result_accepted("RUNNING", True) is True
    assert result_accepted("PAUSING", True) is False
    assert result_accepted("CANCELED", False) is False
    assert result_accepted("SUCCEEDED", True) is True
    assert result_accepted("SUCCEEDED", False) is False
    assert result_accepted("FAILED", False) is True
    assert result_accepted("TIMEOUT", True) is False
    assert result_accepted("PENDING", False) is True


class _OpenSocket:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[str] = []

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


async def test_deliver_pause_requires_assigned_executor() -> None:
    """没有执行器时不发暂停帧。"""
    with pytest.raises(RuntimeError, match="pause requires an assigned executor"):
        await deliver_pause(Dispatch(id=1, executor_id=None))


async def test_deliver_pause_sends_task_pause_on_local_session() -> None:
    """本机连接直接收到有序的 TASK_PAUSE。"""
    socket = _OpenSocket()
    session = ExecutorSession(99001, 1, 1, 3, "pause-session", socket)  # type: ignore[arg-type]
    await session_registry.register(session)
    try:
        await deliver_pause(Dispatch(id=44, executor_id=99001))
    finally:
        await session_registry.remove_by_session_id(session.session_id)
    assert socket.sent == [task_pause_frame(44, 99001)]


def test_capacity_normalization() -> None:
    """心跳并发上限：缺省 3，非法为 1，最大 50。"""
    assert normalize_capacity(None) == 3
    assert normalize_capacity("0") == 1
    assert normalize_capacity("80") == 50
    assert normalize_capacity("nope") == 1
    assert normalize_capacity("4") == 4
