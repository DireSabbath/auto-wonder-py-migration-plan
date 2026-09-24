"""执行器升级的只读面板状态。开关来自部署配置，接口不提供写入。"""

from autowonder.config import get_settings
from autowonder.core.schema import ApiModel
from autowonder.platform.branding import normalize_runtime_version


class RuntimeAutoUpdateView(ApiModel):
    """全局自动升级开关和统一目标版本。"""

    executor_auto_update_enabled: bool
    target_version: str


def runtime_auto_update_view() -> RuntimeAutoUpdateView:
    """读取部署配置中的自动升级开关和推荐运行时版本。"""
    settings = get_settings()
    return RuntimeAutoUpdateView(
        executor_auto_update_enabled=settings.executor_auto_update_enabled,
        target_version=normalize_runtime_version(settings.recommended_runtime_version),
    )
