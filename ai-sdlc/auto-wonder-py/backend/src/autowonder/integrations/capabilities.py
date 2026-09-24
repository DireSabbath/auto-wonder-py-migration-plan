"""集成能力开关。Aone 默认关闭，由部署配置打开。"""

from autowonder.config import get_settings


def integration_capabilities() -> dict[str, bool]:
    """公开的集成能力。键名与 Java ``Map`` 一致。"""
    return {"aoneEnabled": get_settings().aone_enabled}
