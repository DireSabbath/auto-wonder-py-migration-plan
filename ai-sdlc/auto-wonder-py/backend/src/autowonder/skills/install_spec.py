"""技能安装规格。MCP 配置会规范化，私密值只保留密文引用。"""

import json
import re
from collections.abc import Mapping

from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.security.crypto import AesGcmSecretCrypto

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MCP_URL = re.compile(r"^https?://\S+$")
_RESERVED_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}
_MCP_TRANSPORTS = {"http", "sse", "stdio"}
_MAX_ENTRIES = 32
_MAX_VALUE_LENGTH = 4096


def reject_packaged_capability(skill_type: str | None) -> None:
    """插件和 Runtime Hook 只能从安装包写入。"""
    normalized = ""
    if skill_type is not None:
        normalized = skill_type.strip()
    if normalized.lower() == "plugin" or normalized.lower() == "hook":
        raise BizError(ErrorCode.PARAM_INVALID, "插件和 Runtime Hook 必须通过安装包入口配置")


def is_mcp_type(skill_type: str | None) -> bool:
    """类型比较与 Java ``equalsIgnoreCase`` 一致，不额外去掉空白。"""
    if skill_type is None:
        return False
    return skill_type.lower() == "mcp"


def normalize_install_spec(
    skill_type: str | None,
    install_spec: str | None,
    previous: object | None,
) -> object | None:
    """准备写入 JSON 列的值。非法 JSON 文本按原文包成字符串。"""
    if install_spec is None:
        return None
    trimmed = install_spec.strip()
    if trimmed == "":
        return ""
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        return install_spec
    if is_mcp_type(skill_type):
        if not isinstance(parsed, dict):
            raise BizError(ErrorCode.PARAM_INVALID, "MCP 配置必须是 JSON 对象")
        return normalize_mcp_config(parsed, parse_mcp_config(previous))
    return parsed


def display_install_spec(stored: object | None) -> str | None:
    """把库存值还原成接口上的文本，并掩掉私密引用。"""
    if stored is None:
        return None
    if isinstance(stored, str):
        return stored
    if isinstance(stored, dict):
        return _dump(mask_mcp_secrets(stored))
    return _dump(stored)


def normalize_mcp_config(
    source: Mapping[str, object],
    previous: dict[str, object] | None,
) -> dict[str, object]:
    """补齐传输方式，并按 HTTP 或 stdio 校验地址、命令和键值。"""
    config: dict[str, object] = dict(source)
    transport = _text(config.get("transport"))
    if transport is None or transport.strip() == "":
        transport = "http"
    transport = transport.strip().lower()
    if transport not in _MCP_TRANSPORTS:
        raise BizError(ErrorCode.PARAM_INVALID, "MCP 连接方式仅支持 HTTP、SSE 或 stdio")
    config["transport"] = transport
    previous_env = None
    previous_headers = None
    if previous is not None:
        previous_env = previous.get("env")
        previous_headers = previous.get("headers")
    if transport == "stdio":
        command = _text(config.get("command"))
        if command is None or command.strip() == "":
            raise BizError(ErrorCode.PARAM_INVALID, "MCP 本地命令不能为空")
        if config.get("headers") is not None or config.get("timeoutSeconds") is not None:
            raise BizError(ErrorCode.PARAM_INVALID, "stdio MCP 不支持请求头或超时配置")
        config["env"] = normalize_values(
            config.get("env"),
            previous_env,
            "MCP Env",
            _ENV_NAME,
            False,
        )
        return config
    url = _text(config.get("url"))
    if url is None or _MCP_URL.fullmatch(url) is None:
        raise BizError(ErrorCode.PARAM_INVALID, "MCP 地址必须是 HTTP/HTTPS URL")
    config["url"] = url.strip()
    config["headers"] = normalize_values(
        config.get("headers"),
        previous_headers,
        "MCP Headers",
        _HEADER_NAME,
        True,
    )
    timeout = _timeout(config.get("timeoutSeconds"))
    if timeout < 1 or timeout > 600:
        raise BizError(ErrorCode.PARAM_INVALID, "MCP 超时时间必须在 1 到 600 秒之间")
    config["timeoutSeconds"] = timeout
    return config


def normalize_values(
    source: object,
    previous: object,
    label: str,
    name_pattern: re.Pattern[str],
    header: bool,
) -> dict[str, object]:
    """校验一组请求头或环境变量。同名不区分大小写。"""
    values: dict[str, object] = {}
    if source is None:
        return values
    if not isinstance(source, Mapping):
        raise BizError(ErrorCode.PARAM_INVALID, label + " 必须是键值对象")
    if len(source) > _MAX_ENTRIES:
        raise BizError(ErrorCode.PARAM_INVALID, label + " 最多支持 32 项")
    seen: set[str] = set()
    for key, raw_value in source.items():
        name = ""
        if key is not None:
            name = str(key).strip()
        normalized_name = name.lower()
        invalid_name = name_pattern.fullmatch(name) is None
        reserved = header and normalized_name in _RESERVED_HEADERS
        if invalid_name or reserved:
            raise BizError(ErrorCode.PARAM_INVALID, label + " 名称不合法: " + name)
        if normalized_name in seen:
            raise BizError(ErrorCode.PARAM_INVALID, label + " 名称不能重复: " + name)
        seen.add(normalized_name)
        value = normalize_secret_value(raw_value, previous_value(previous, name))
        if isinstance(value, dict):
            values[name] = value
            continue
        literal = _java_literal(value)
        if "\r" in literal or "\n" in literal or _utf16_length(literal) > _MAX_VALUE_LENGTH:
            raise BizError(ErrorCode.PARAM_INVALID, label + " 值不合法: " + name)
        values[name] = literal
    return values


def normalize_secret_value(raw: object, previous: object) -> object:
    """私密项加密进 secretRef。空白值沿用上一次已经保存的引用。"""
    if not isinstance(raw, Mapping):
        return _java_literal(raw)
    secret = _marked_secret(raw)
    if not secret:
        return _java_literal(raw)
    plain = ""
    if raw.get("value") is not None:
        plain = str(raw.get("value"))
    if plain.strip() == "":
        if isinstance(previous, Mapping) and str(previous.get("kind")) == "secretRef":
            return dict(previous)
        raise BizError(ErrorCode.PARAM_INVALID, "私密配置首次保存时必须填写值")
    crypto = _crypto()
    if crypto is None:
        raise RuntimeError("密文存储未配置，无法保存私密 MCP 配置")
    ref = crypto.encrypt(plain)
    if ref.strip() == "":
        raise RuntimeError("密文存储未返回私密配置引用")
    return {"kind": "secretRef", "ref": ref}


def mask_mcp_secrets(source: Mapping[str, object]) -> dict[str, object]:
    """响应里不回传密文引用，只标出该项是私密的。"""
    copy: dict[str, object] = dict(source)
    for key in ("headers", "env"):
        raw = copy.get(key)
        if not isinstance(raw, Mapping):
            continue
        values: dict[str, object] = {}
        for name, value in raw.items():
            if isinstance(value, Mapping) and str(value.get("kind")) == "secretRef":
                values[str(name)] = {"kind": "secretRef", "secret": True}
            else:
                values[str(name)] = value
        copy[key] = values
    return copy


def parse_mcp_config(stored: object | None) -> dict[str, object] | None:
    """读取上一次 MCP 配置。无法解析时当作没有历史值。"""
    if isinstance(stored, dict):
        return stored
    if not isinstance(stored, str) or stored.strip() == "":
        return None
    try:
        parsed = json.loads(stored)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def previous_value(previous: object, name: str) -> object:
    """按不区分大小写的名字找回上一次的值。"""
    if not isinstance(previous, Mapping):
        return None
    for key, value in previous.items():
        if name.lower() == str(key).lower():
            return value
    return None


def _timeout(value: object) -> int:
    parsed = _as_int(value)
    if parsed is None:
        return 60
    return parsed


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _marked_secret(value: Mapping[str, object]) -> bool:
    flag = value.get("secret")
    if flag is True:
        return True
    if isinstance(flag, str) and flag.lower() == "true":
        return True
    return str(value.get("kind")) == "secretRef"


def _java_literal(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        if value:
            return "true"
        return "false"
    if isinstance(value, dict) or isinstance(value, list):
        return _dump(value)
    return str(value)


def _crypto() -> AesGcmSecretCrypto | None:
    master_key = get_settings().secret_master_key
    if master_key.strip() == "":
        return None
    return AesGcmSecretCrypto(master_key)


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
