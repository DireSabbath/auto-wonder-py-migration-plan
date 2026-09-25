"""执行器用户接口的请求和响应。内部 snake_case，JSON 为 camelCase。"""

from datetime import datetime
from typing import Any

from autowonder.core.schema import ApiModel


class CreateExecutorRequest(ApiModel):
    """创建执行器。省略的启动项按创建表单的默认值补齐。"""

    name: str | None = None
    client_kind: str | None = None
    memory_mode: str | None = None
    max_concurrent_dispatches: int | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    context_window: str | None = None


class UpdateExecutorLaunchConfigRequest(ApiModel):
    """全量保存启动配置。version 是上次读到的乐观锁版本。"""

    model: str | None = None
    reasoning_effort: str | None = None
    context_window: str | None = None
    memory_mode: str | None = None
    max_concurrent_dispatches: int | None = None
    version: int | None = None


class ExecutorLaunchCommandRequest(ApiModel):
    """只决定命令的输出格式。启动参数来自已保存的配置。"""

    os: str | None = None
    debug: bool | None = None
    shell: str | None = None


class RestartRequest(ApiModel):
    """update 为真时先拉发布版再重启。省略时只重启。"""

    update: bool = False


class IssuedExecutorView(ApiModel):
    """创建成功后的执行器、一次性明文令牌和刚落库的启动配置。"""

    id: int
    agent_id: int
    name: str
    token: str
    client_kind: str
    memory_mode: str | None = None
    max_concurrent_dispatches: int | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    context_window: str | None = None
    config_version: int


class ExecutorUpdateView(ApiModel):
    """一个执行器最近的升级任务。"""

    task_id: int
    request_id: str
    status: str
    current_version: str | None = None
    target_version: str
    attempt_count: int | None = None
    max_attempts: int | None = None
    last_error: str | None = None
    source: str
    requested_at: datetime | None = None
    next_attempt_at: datetime | None = None
    completed_at: datetime | None = None


class ExecutorView(ApiModel):
    """执行器列表项。在线、版本和升级标记来自接入现场，不读状态列。"""

    id: int
    agent_id: int
    agent_name: str | None = None
    name: str
    status: str
    client_kind: str | None = None
    last_connect_ip: str | None = None
    last_heartbeat: datetime | None = None
    last_started_at: datetime | None = None
    version: str | None = None
    model: str | None = None
    model_name: str | None = None
    gmt_create: datetime | None = None
    restart_supported: bool = False
    update_restart_supported: bool = False
    restart: dict[str, Any] | None = None
    upgrade_supported: bool = False
    upgrade_available: bool = False
    version_comparable: bool = False
    target_version: str | None = None
    update: ExecutorUpdateView | None = None
    squad_ids: list[int] = []
    squad_names: list[str] = []


class ExecutorLaunchConfigView(ApiModel):
    """已保存的启动配置。从未配置时各字段为空、版本为 1。"""

    model: str | None = None
    reasoning_effort: str | None = None
    context_window: str | None = None
    memory_mode: str | None = None
    max_concurrent_dispatches: int | None = None
    version: int


class ExecutorLaunchCommandView(ApiModel):
    """可直接粘贴的启动命令，以及生成它的各项取值。"""

    executor_id: int
    client_kind: str
    provider: str
    memory_mode: str | None = None
    max_concurrent_dispatches: int
    model: str | None = None
    reasoning_effort: str | None = None
    context_window: str | None = None
    ws_url: str
    runtime_version: str
    os: str
    debug: bool
    shell: str | None = None
    log_file_name: str | None = None
    command: str


class ExecutorUpdateSkipView(ApiModel):
    """批量升级时主动跳过的一台执行器。"""

    executor_id: int
    executor_name: str
    reason: str


class ExecutorUpdateAllResultView(ApiModel):
    """一键全量升级的计数和跳过原因。"""

    target_version: str
    total: int = 0
    scheduled: int = 0
    already_up_to_date: int = 0
    skipped: list[ExecutorUpdateSkipView] = []


class ProviderModelCatalogItemView(ApiModel):
    """目录中的一个模型。"""

    id: str
    name: str


class ProviderModelCatalogView(ApiModel):
    """一个 provider 的模型目录和最近一次成功刷新时间。"""

    provider: str
    models: list[ProviderModelCatalogItemView]
    last_successful_at: datetime | None = None
