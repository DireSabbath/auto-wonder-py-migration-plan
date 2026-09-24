"""小队接口的请求和响应。JSON 字段名与 Java VO 一致。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateSquadRequest(ApiModel):
    """创建小队。"""

    name: str | None = None
    description: str | None = None
    owner_id: int | None = None


class UpdateSquadRequest(ApiModel):
    """更新名称、描述、负责人和 debug 开关。"""

    name: str | None = None
    description: str | None = None
    owner_id: int | None = None
    debug_log_enabled: bool | None = None


class AddMembersRequest(ApiModel):
    """向小队追加数字员工。"""

    agent_ids: list[int] | None = None


class SdlcSummary(ApiModel):
    """小队详情里的流程摘要。"""

    id: int | None = None
    name: str | None = None
    work_type: str | None = None
    status: str | None = None


class ExecutorSummary(ApiModel):
    """小队详情里的执行器摘要。"""

    id: int | None = None
    agent_id: int | None = None
    agent_name: str | None = None
    name: str | None = None
    status: str | None = None
    client_kind: str | None = None
    last_heartbeat: datetime | None = None


class SquadView(ApiModel):
    """小队卡片。列表不填 memberAgentIds、sdlcs、executors。"""

    id: int | None = None
    name: str | None = None
    description: str | None = None
    owner_id: int | None = None
    version: int | None = None
    debug_log_enabled: bool = False
    gmt_create: datetime | None = None
    member_agent_ids: list[int] | None = None
    member_count: int = 0
    role_count: int = 0
    executor_online_count: int = 0
    executor_total_count: int = 0
    sdlc_count: int = 0
    sdlcs: list[SdlcSummary] | None = None
    executors: list[ExecutorSummary] | None = None


class SdlcStepSummary(ApiModel):
    """成员身上的流程步骤摘要。"""

    id: int | None = None
    step_order: int | None = None
    name: str | None = None
    handler_type: str | None = None
    handler_role_ref: str | None = None


class SquadMemberView(ApiModel):
    """小队成员。缺数字员工或在线版本时，对应字段保持 null。"""

    agent_id: int | None = None
    agent_name: str | None = None
    agent_kind: str | None = None
    role_code: str | None = None
    role_name: str | None = None
    responsibilities: str | None = None
    sdlc_id: int | None = None
    sdlc_name: str | None = None
    sdlc_steps: list[SdlcStepSummary] | None = None
