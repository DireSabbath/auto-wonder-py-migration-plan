"""读取 Java ``listTools()`` 的工具目录。

命令示例里的根地址和运行时版本随部署变化，目录里用占位符保存。
"""

import json
from pathlib import Path
from typing import Any

from autowonder.config import get_settings
from autowonder.platform.branding import normalize_public_base_url, normalize_runtime_version

_TEMPLATE = Path(__file__).with_name("tool_catalog.json").read_text(encoding="utf-8")
_PUBLIC_BASE_URL = "__PUBLIC_BASE_URL__"
_RUNTIME_VERSION = "__RUNTIME_VERSION__"


def bind_tool_catalog(server_url: str, runtime_version: str) -> list[dict[str, Any]]:
    """把部署根地址和运行时版本填进工具说明。"""
    text = _TEMPLATE.replace(_PUBLIC_BASE_URL, server_url).replace(
        _RUNTIME_VERSION, runtime_version
    )
    loaded = json.loads(text)
    return loaded


def list_tools() -> list[dict[str, Any]]:
    """未按调用者权限裁剪的工具目录，根地址来自启动配置。"""
    settings = get_settings()
    return bind_tool_catalog(
        normalize_public_base_url(settings.public_base_url),
        normalize_runtime_version(settings.recommended_runtime_version),
    )
