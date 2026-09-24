"""定时任务能力闸门。完整 schema 下仍服从部署开关。"""

from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel

FEATURE_DISABLED = "FEATURE_DISABLED"
CLUSTER_NOT_READY = "CLUSTER_NOT_READY"
SCHEMA_MODE_READY = "V037_READY"


class ScheduledTaskCapabilityView(ApiModel):
    """定时任务能力的公开快照。本服务按完整 schema 运行，不返回库升级原因。"""

    available: bool
    mode: str
    cluster_ready: bool
    reason: str | None


def require_scheduled_capability() -> None:
    """模块或集群就绪关闭时拒绝。schema 探测在本服务里视为已经满足。"""
    settings = get_settings()
    if settings.scheduled_task_enabled and settings.scheduled_task_cluster_ready:
        return
    raise BizError(ErrorCode.SCHEDULED_TASK_SCHEMA_NOT_READY)


def capability_snapshot() -> ScheduledTaskCapabilityView:
    """按部署开关给出能力快照。模块关闭优先于集群未就绪。"""
    settings = get_settings()
    module_enabled = settings.scheduled_task_enabled
    cluster_ready = settings.scheduled_task_cluster_ready
    return ScheduledTaskCapabilityView(
        available=module_enabled and cluster_ready,
        mode=SCHEMA_MODE_READY,
        cluster_ready=cluster_ready,
        reason=_unavailable_reason(module_enabled, cluster_ready),
    )


def _unavailable_reason(module_enabled: bool, cluster_ready: bool) -> str | None:
    if not module_enabled:
        return FEATURE_DISABLED
    if not cluster_ready:
        return CLUSTER_NOT_READY
    return None
