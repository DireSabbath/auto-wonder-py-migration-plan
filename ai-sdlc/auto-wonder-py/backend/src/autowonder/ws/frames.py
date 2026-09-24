"""执行器 WebSocket 帧。

字段与 ``websocket.frame`` 里的 Java Bean 一一对应，JSON 使用 camelCase。
反序列化到这些模型时忽略未知字段，与 Fastjson 写入 Bean 的缺省一致。
入站路由仍要读原始 JSON：心跳的 ``protocolFeatures``、进度的 ``resultSummary``
等不在对应 Bean 上，Java 用 ``JSONObject`` 保留它们。
"""

import json
from typing import Any

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from autowonder.core.schema import ApiModel

BROADCAST_CHANNEL = "node:dispatch:broadcast"
TASK_PACKAGE_SIGNATURE_V1 = "TASK_PACKAGE_SIGNATURE_V1"
TASK_PACKAGE_HOOKS_V1 = "TASK_PACKAGE_HOOKS_V1"
TASK_PACKAGE_TOOL_HOOKS_V1 = "TASK_PACKAGE_TOOL_HOOKS_V1"
DEBUG_LOG_V1 = "DEBUG_LOG_V1"
AGENT_ENVIRONMENT_VARIABLES_V1 = "AGENT_ENVIRONMENT_VARIABLES_V1"
DEBUG_LOG_MAX_BYTES = 209_715_200
EXECUTOR_REPLACED_CLOSE_CODE = 4001
EXECUTOR_REPLACED_REASON = "executor connection replaced"
VIOLATED_POLICY_CLOSE_CODE = 1008


class FrameModel(ApiModel):
    """帧模型。未知字段忽略，序列化时省略 null。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class InboundFrame(FrameModel):
    """执行器上行帧的公共判别字段。"""

    type: str | None = None


class OutboundFrame(FrameModel):
    """服务端下行帧的公共判别字段。"""

    type: str | None = None


class DebugLogDirective(FrameModel):
    """``TASK_DISPATCH`` 的 debugLog 段。``maxBytes`` 是单轮日志硬上限。"""

    enabled: bool | None = None
    max_bytes: int | None = None


class ResumeCheckpointCandidate(FrameModel):
    """恢复时可选的一条检查点。字段名与 ``ResumeCheckpointCandidate`` 一致。"""

    download_url: str | None = None
    sha256: str | None = None
    checkpoint_seq: int | None = None


class TaskAckFrame(InboundFrame):
    """执行器确认收到派发。"""

    type: str | None = "TASK_ACK"
    dispatch_id: int | None = None


class TaskDispatchFrame(OutboundFrame):
    """下发给执行器的任务包。构造时带上 ``TASK_DISPATCH``。"""

    type: str | None = "TASK_DISPATCH"
    dispatch_id: int | None = None
    executor_id: int | None = None
    tenant_id: int | None = None
    workitem_id: int | None = None
    idempotency_key: str | None = None
    agent_id: int | None = None
    agent_version_id: int | None = None
    sdlc_step_id: int | None = None
    attempt: int | None = None
    download_url: str | None = None
    md5: str | None = None
    size: int | None = None
    package_id: str | None = None
    checksum: str | None = None
    checksum_algorithm: str | None = None
    checksum_scope: str | None = None
    issuer: str | None = None
    signature_ref: str | None = None
    signature: str | None = None
    signature_algorithm: str | None = None
    signature_public_key: str | None = None
    expires_at: str | None = None
    allow_commit: bool | None = None
    allow_push: bool | None = None
    allow_network: bool | None = None
    package_refresh_path: str | None = None
    artifact_upload_path: str | None = None
    checkpoint_upload_path: str | None = None
    debug_log: DebugLogDirective | None = None
    resume_mode: str | None = None
    resume_session_behavior: str | None = None
    resume_from_dispatch_id: int | None = None
    resume_provider: str | None = None
    resume_session_id: str | None = None
    resume_checkpoint_url: str | None = None
    resume_checkpoint_sha256: str | None = None
    resume_checkpoint_seq: int | None = None
    resume_checkpoint_candidates: list[ResumeCheckpointCandidate] | None = None
    dispatch_mcp_token: str | None = None
    mcp_secrets: dict[str, str] | None = None
    environment_variables: dict[str, str] | None = None


class TaskHandoffFrame(InboundFrame):
    """执行器上报的下一跳。``from`` 是 Java 字段名。"""

    type: str | None = "TASK_HANDOFF"
    dispatch_id: int | None = None
    workitem_id: int | None = None
    from_: str | None = Field(default=None, alias="from", serialization_alias="from")
    to: str | None = None
    to_type: str | None = None
    next_role: str | None = None
    reason: str | None = None


class TaskProgressFrame(InboundFrame):
    """进度帧。Bean 只有 ``dispatchId`` 与 ``log``，其余键留在原始 JSON。"""

    type: str | None = "TASK_PROGRESS"
    dispatch_id: int | None = None
    log: str | None = None


class TaskResultFrame(InboundFrame):
    """执行结果。``debugLog`` 不在 Bean 上，路由从原始 JSON 读取。"""

    type: str | None = "TASK_RESULT"
    dispatch_id: int | None = None
    success: bool | None = None
    result_summary: str | None = None
    error: str | None = None


class HeartbeatFrame(InboundFrame):
    """心跳 Bean 只有 ``type``。能力位在原始 JSON 的 ``protocolFeatures``。"""

    type: str | None = "HEARTBEAT"


class ArtifactUploadedFrame(InboundFrame):
    """产物已上传。客户端主环当前不发这帧，服务端仍接收。"""

    type: str | None = "ARTIFACT_UPLOADED"
    dispatch_id: int | None = None
    workitem_id: int | None = None
    name: str | None = None
    artifact_type: str | None = None
    oss_ref: str | None = None
    size: int | None = None
    meta_json: str | None = None


def dump_frame(frame: FrameModel) -> str:
    """序列化为紧凑 JSON。null 字段不出现，与 Fastjson 缺省写出一致。"""
    return frame.model_dump_json(by_alias=True, exclude_none=True)


def parse_inbound_object(message: str) -> Any:
    """按 ``JSONObject`` 解析文本。非法 JSON 抛出 ``json.JSONDecodeError``。"""
    return json.loads(message)


def enabled_debug_log() -> DebugLogDirective:
    """协商成功时下发的 debugLog：开启，单轮上限 200MB。"""
    return DebugLogDirective(enabled=True, max_bytes=DEBUG_LOG_MAX_BYTES)


def task_dispatch_paths(dispatch_id: int) -> tuple[str, str, str]:
    """包刷新、产物上传、检查点上传三条 daemon 路径。"""
    base = "/api/daemon/dispatches/" + str(dispatch_id)
    return (base + "/package-url", base + "/artifacts", base + "/checkpoint")


def build_task_dispatch_frame(
    *,
    dispatch_id: int,
    executor_id: int,
    tenant_id: int,
    workitem_id: int,
    idempotency_key: str,
    agent_id: int,
    agent_version_id: int,
    sdlc_step_id: int,
    attempt: int,
    download_url: str,
    md5: str,
    size: int,
    sha256: str,
    allow_commit: bool,
    allow_push: bool,
    allow_network: bool,
    issuer: str | None = None,
    signature_ref: str | None = None,
    signature: str | None = None,
    signature_algorithm: str | None = None,
    signature_public_key: str | None = None,
    expires_at: str | None = None,
    dispatch_mcp_token: str | None = None,
    debug_log: DebugLogDirective | None = None,
    environment_variables: dict[str, str] | None = None,
    mcp_secrets: dict[str, str] | None = None,
) -> TaskDispatchFrame:
    """组装 ``TASK_DISPATCH``。幂等键原样取自调度行，不在这里重算。"""
    package_refresh, artifact_upload, checkpoint_upload = task_dispatch_paths(dispatch_id)
    environment = {}
    if environment_variables is not None:
        environment = environment_variables
    return TaskDispatchFrame(
        dispatch_id=dispatch_id,
        executor_id=executor_id,
        tenant_id=tenant_id,
        workitem_id=workitem_id,
        idempotency_key=idempotency_key,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        sdlc_step_id=sdlc_step_id,
        attempt=attempt,
        download_url=download_url,
        md5=md5,
        size=size,
        package_id="pkg_" + str(dispatch_id),
        checksum="sha256:" + sha256,
        checksum_algorithm="sha256",
        checksum_scope="zip_archive",
        issuer=issuer,
        signature_ref=signature_ref,
        signature=signature,
        signature_algorithm=signature_algorithm,
        signature_public_key=signature_public_key,
        expires_at=expires_at,
        allow_commit=allow_commit,
        allow_push=allow_push,
        allow_network=allow_network,
        package_refresh_path=package_refresh,
        artifact_upload_path=artifact_upload,
        checkpoint_upload_path=checkpoint_upload,
        debug_log=debug_log,
        dispatch_mcp_token=dispatch_mcp_token,
        mcp_secrets=mcp_secrets,
        environment_variables=environment,
    )


def task_result_ack(dispatch_id: int, accepted: bool) -> str:
    """结果确认帧。``accepted`` 为假时执行器可以丢掉无法重试成功的出站记录。"""
    return json.dumps(
        {"type": "TASK_RESULT_ACK", "dispatchId": dispatch_id, "accepted": accepted},
        separators=(",", ":"),
    )


def task_pause_frame(dispatch_id: int, executor_id: int) -> str:
    """暂停帧。Java 用有序 JSONObject 写出这三个字段。"""
    return json.dumps(
        {"type": "TASK_PAUSE", "dispatchId": dispatch_id, "executorId": executor_id},
        separators=(",", ":"),
    )


def task_handoff_result(
    dispatch_id: int,
    workitem_id: int,
    status: str,
    downstream_dispatch_id: int | None,
    target_type: str | None,
    target_ref: int | None,
    reason_code: str | None,
    message: str | None,
) -> str:
    """交接回执。``targetType`` 只在已派给数字人或真人时出现。"""
    body: dict[str, Any] = {
        "type": "TASK_HANDOFF_RESULT",
        "dispatchId": dispatch_id,
        "workitemId": workitem_id,
        "status": status,
        "downstreamDispatchId": downstream_dispatch_id,
        "targetType": target_type,
        "targetRef": target_ref,
        "reasonCode": reason_code,
        "message": message,
    }
    return json.dumps(body, separators=(",", ":"))
