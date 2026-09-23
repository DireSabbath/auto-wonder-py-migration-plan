"""演进模式与身份快照。缺省和无法识别的存量值都是 ASSISTED。"""

import json

from autowonder.core.errors import BizError, ErrorCode

_MODES = {"MANUAL", "ASSISTED", "AUTO_PROPOSAL"}


def evolution_mode_from(value: object | None) -> str:
    """空白或未知存量值回落到 ASSISTED。"""
    if not isinstance(value, str) or value.strip() == "":
        return "ASSISTED"
    name = value.strip().upper()
    if name in _MODES:
        return name
    return "ASSISTED"


def parse_requested_evolution_mode(requested: str) -> str:
    """调用方显式写入的模式必须是枚举名。"""
    name = requested.strip().upper()
    if name not in _MODES:
        raise BizError(ErrorCode.PARAM_INVALID)
    return name


def parse_identity(value: object | None) -> dict[str, object]:
    """身份列是对象。空值和损坏文本都当成没有快照。"""
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or value.strip() == "":
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def evolution_mode_of_identity(value: object | None) -> str:
    """从身份快照读取演进模式。"""
    return evolution_mode_from(parse_identity(value).get("evolutionMode"))


def compact_identity(payload: dict[str, object]) -> dict[str, object]:
    """Fastjson 默认不写出 null 字段。"""
    result: dict[str, object] = {}
    for key, item in payload.items():
        if item is not None:
            result[key] = item
    return result


def identity_text(payload: dict[str, object]) -> str:
    """紧凑 JSON。键序按 MySQL JSON 回读时的字典序。"""
    return json.dumps(
        compact_identity(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def identity_column_text(value: object | None) -> str | None:
    """接口上的 identityJson 是字符串。空列保持 null。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return identity_text(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def identity_with_evolution_mode(
    existing: object | None,
    requested: str | None,
) -> dict[str, object] | None:
    """空白请求不改身份列。合法模式写入 evolutionMode。"""
    if requested is None or requested.strip() == "":
        return None
    mode = parse_requested_evolution_mode(requested)
    identity = parse_identity(existing)
    identity["evolutionMode"] = mode
    return compact_identity(identity)


def build_identity_map(
    *,
    name: str | None,
    avatar_url: str | None,
    role_name: str | None,
    role_code: str | None,
    business_background: str | None,
    responsibilities: str | None,
    identity: object | None,
) -> dict[str, object]:
    """审核通过时冻结的身份。字段顺序与 Java LinkedHashMap 一致。"""
    raw: dict[str, object] = {
        "name": name,
        "avatarUrl": avatar_url,
        "roleName": role_name,
        "roleCode": role_code,
        "businessBackground": business_background,
        "responsibilities": responsibilities,
        "evolutionMode": evolution_mode_of_identity(identity),
    }
    return compact_identity(raw)
