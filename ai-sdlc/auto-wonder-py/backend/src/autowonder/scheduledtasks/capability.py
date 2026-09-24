"""定时任务能力闸门。完整 schema 下仍服从部署开关。"""

from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode


def require_scheduled_capability() -> None:
    """模块或集群就绪关闭时拒绝。schema 探测在本服务里视为已经满足。"""
    settings = get_settings()
    if settings.scheduled_task_enabled and settings.scheduled_task_cluster_ready:
        return
    raise BizError(ErrorCode.SCHEDULED_TASK_SCHEMA_NOT_READY)
