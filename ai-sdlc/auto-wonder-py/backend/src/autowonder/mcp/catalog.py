"""读取从 Java ``listTools()`` 抽出的工具名称和说明。"""

import json
from pathlib import Path
from typing import Any

_TOOLS: list[dict[str, Any]] = json.loads(
    Path(__file__).with_name("tool_catalog.json").read_text(encoding="utf-8")
)


def list_tools() -> list[dict[str, Any]]:
    """未按调用者权限裁剪的工具目录。参数 schema 仍是占位对象。"""
    return [dict(tool) for tool in _TOOLS]
