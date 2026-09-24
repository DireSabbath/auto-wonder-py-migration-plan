"""环境变量名称。保留名与 Java ``EnvironmentVariableNamePolicy`` 一致。"""

import re

from autowonder.core.errors import BizError, ErrorCode

_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RUNTIME_OWNED = {
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "QODER_CONFIG_DIR",
    "QODERCN_CONFIG_DIR",
    "QODER_INTEGRATION_ID",
    "QODER_HOST_SERVICE_NAME",
}


def is_reserved(name: str) -> bool:
    """平台前缀和运行时占用的名称不能进环境变量库。"""
    canonical = name.upper()
    if canonical.startswith("AUTOWONDER_"):
        return True
    return canonical in _RUNTIME_OWNED


def validate_name(raw_name: str | None) -> str:
    """去掉两端空白后必须是合法标识，且不是保留名。"""
    name = ""
    if raw_name is not None:
        name = raw_name.strip()
    if _java_length(name) > 128 or _NAME_PATTERN.fullmatch(name) is None:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NAME_INVALID)
    if is_reserved(name):
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NAME_RESERVED)
    return name


def normalize_description(description: str | None) -> str | None:
    """空白说明写成 null。超过 512 个 Java 字符则拒绝。"""
    if description is None:
        return None
    trimmed = description.strip()
    if _java_length(trimmed) > 512:
        raise BizError(ErrorCode.PARAM_INVALID, "环境变量说明不能超过512个字符")
    if trimmed == "":
        return None
    return trimmed


def _java_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
