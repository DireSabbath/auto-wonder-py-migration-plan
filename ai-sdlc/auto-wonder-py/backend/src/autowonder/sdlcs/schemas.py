"""SDLC 接口的请求和响应。JSON 字段名与 Java VO 一致。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateSdlcRequest(ApiModel):
    """创建流程。"""

    name: str | None = None
    description: str | None = None
    work_type: str | None = None


class UpdateSdlcRequest(ApiModel):
    """更新流程名称、描述和工单类型。未传的字段保留原值。"""

    name: str | None = None
    description: str | None = None
    work_type: str | None = None


class CreateStepRequest(ApiModel):
    """新增步骤。"""

    step_order: int | None = None
    name: str | None = None
    kind: str | None = None
    instruction_md: str | None = None
    checklist_json: str | None = None
    gate_policy_json: str | None = None
    required: bool | None = None
    timeout_seconds: int | None = None
    retry_budget: int | None = None
    code: str | None = None
    handler_type: str | None = None
    handler_role_ref: str | None = None
    status_on_enter_code: str | None = None
    on_success: str | None = None
    on_fail: str | None = None


class UpdateStepRequest(ApiModel):
    """更新步骤。timeoutSeconds 与 retryBudget 要区分省略和显式 null。"""

    name: str | None = None
    kind: str | None = None
    instruction_md: str | None = None
    checklist_json: str | None = None
    gate_policy_json: str | None = None
    required: bool | None = None
    timeout_seconds: int | None = None
    retry_budget: int | None = None
    code: str | None = None
    handler_type: str | None = None
    handler_role_ref: str | None = None
    status_on_enter_code: str | None = None
    on_success: str | None = None
    on_fail: str | None = None


class ReorderStepsRequest(ApiModel):
    """按给出的步骤 id 重排。"""

    step_ids: list[int] | None = None


class StepView(ApiModel):
    """步骤。checklistJson 等 JSON 列按字符串返回。"""

    id: int | None = None
    sdlc_id: int | None = None
    step_order: int | None = None
    name: str | None = None
    kind: str | None = None
    instruction_md: str | None = None
    checklist_json: str | None = None
    gate_policy_json: str | None = None
    required: bool | None = None
    timeout_seconds: int | None = None
    retry_budget: int | None = None
    code: str | None = None
    handler_type: str | None = None
    handler_role_ref: str | None = None
    status_on_enter_code: str | None = None
    on_success: str | None = None
    on_fail: str | None = None


class SdlcView(ApiModel):
    """流程。列表不返回 steps，用 stepCount；详情才带步骤。"""

    id: int | None = None
    name: str | None = None
    description: str | None = None
    work_type: str | None = None
    status: str | None = None
    is_default: int | None = None
    entry_step_id: int | None = None
    version: int | None = None
    gmt_create: datetime | None = None
    steps: list[StepView] | None = None
    step_count: int | None = None
    squad_ids: list[int] | None = None
    squad_names: list[str] | None = None
