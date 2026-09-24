"""执行器启动项的可选值和默认值，与页面表单使用同一套取值。"""

from dataclasses import dataclass

from autowonder.core.errors import BizError, ErrorCode

CLIENT_KIND_QODER_CLI = "QODER_CLI"
CLIENT_KIND_QODER_CN_CLI = "QODER_CN_CLI"
PROVIDER_QODER = "qoder"
PROVIDER_QODER_CN = "qodercn"
PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_CURSOR = "cursor"

MEMORY_MODE_PLATFORM = "platform"
MEMORY_MODE_PROVIDER_LOCAL = "provider-local"
MEMORY_MODE_NONE = "none"
DEFAULT_MEMORY_MODE = MEMORY_MODE_PLATFORM

AUTO_MODEL = "auto"
DEFAULT_MODEL = "qmodel_latest"
DEFAULT_CONTEXT_WINDOW = "260000"
DEFAULT_REASONING_EFFORT = "medium"
ULTIMATE_MODEL = "ultimate"
ULTIMATE_REASONING_EFFORT = "high"
DEFAULT_MAX_CONCURRENT = 5

PROVIDER_BY_CLIENT_KIND = {
    CLIENT_KIND_QODER_CN_CLI: PROVIDER_QODER_CN,
    CLIENT_KIND_QODER_CLI: PROVIDER_QODER,
    "CLAUDE_CODE": PROVIDER_CLAUDE,
    "CODEX_CLI": PROVIDER_CODEX,
    "CURSOR_CLI": PROVIDER_CURSOR,
}

_CREATABLE = (
    (CLIENT_KIND_QODER_CLI, "Qoder CLI"),
    (CLIENT_KIND_QODER_CN_CLI, "Qoder CLI CN"),
)
_MEMORY_MODES = (
    MEMORY_MODE_PLATFORM,
    MEMORY_MODE_PROVIDER_LOCAL,
    MEMORY_MODE_NONE,
)
_CONTEXT_WINDOWS = ("1000000", "400000", "260000")
_REASONING_EFFORTS = ("max", "xhigh", "high", "medium", "low", "none")
_FALLBACK_MODELS = (
    (AUTO_MODEL, "Auto (default)"),
    (ULTIMATE_MODEL, "Ultimate"),
    ("performance", "Performance"),
    ("efficient", "Efficient"),
    ("lite", "Lite"),
    ("qmodel_38max", "Qwen3.8-Max"),
    ("qfmodel", "Qwen3.8-Flash"),
    (DEFAULT_MODEL, "Qwen3.7-Max"),
    ("qmodel", "Qwen3.7-Plus"),
    ("kmodel_latest", "Kimi-K3"),
    ("kmodel", "Kimi-K2.7-Code"),
    ("gmodel", "GLM-5.3"),
    ("gfmodel", "GLM-5.3-Flash"),
    ("dmodel", "DeepSeek-V4-Pro"),
    ("dfmodel", "DeepSeek-V4-Flash"),
    ("mmodel", "MiniMax-M3"),
)


@dataclass(frozen=True)
class ModelOption:
    """目录或内置列表里的一个模型。"""

    value: str
    label: str


FALLBACK_MODELS = tuple(ModelOption(value, label) for value, label in _FALLBACK_MODELS)


def provider_for_client_kind(client_kind: str | None) -> str | None:
    """Qoder 国际版和国内版才有模型目录。其他类型没有目录。"""
    if client_kind == CLIENT_KIND_QODER_CLI:
        return PROVIDER_QODER
    if client_kind == CLIENT_KIND_QODER_CN_CLI:
        return PROVIDER_QODER_CN
    return None


def resolve_provider(client_kind: str | None) -> str:
    """启动命令里的 provider。未知类型沿用页面的 claude。"""
    if client_kind is None:
        return PROVIDER_CLAUDE
    provider = PROVIDER_BY_CLIENT_KIND.get(client_kind.strip())
    if provider is None:
        return PROVIDER_CLAUDE
    return provider


def is_qoder_family(provider: str | None) -> bool:
    """国际版和国内版 Qoder 才携带模型、推理和上下文参数。"""
    return provider == PROVIDER_QODER or provider == PROVIDER_QODER_CN


def canonical_client_kind(client_kind: str | None) -> str | None:
    """收成目录里的大写类型。页面不提供的类型返回空。"""
    if client_kind is None:
        return None
    upper = client_kind.strip().upper()
    if upper in PROVIDER_BY_CLIENT_KIND:
        return upper
    return None


def require_creatable_client_kind(client_kind: str | None, invalid: ErrorCode) -> str:
    """创建入口只接受两种 Qoder CLI。空值和历史类型都用调用方的错误码拒绝。"""
    canonical = canonical_client_kind(client_kind)
    if canonical != CLIENT_KIND_QODER_CLI and canonical != CLIENT_KIND_QODER_CN_CLI:
        allowed = "/".join(kind for kind, _label in _CREATABLE)
        raise BizError(invalid, "clientKind 仅支持 " + allowed)
    return canonical


def resolve_memory_mode(requested: str | None) -> str:
    """空白记忆模式用平台记忆。其他值必须在可选列表里。"""
    if requested is None or requested.strip() == "":
        return DEFAULT_MEMORY_MODE
    value = requested.strip()
    if value in _MEMORY_MODES:
        return value
    raise _invalid("memoryMode", _MEMORY_MODES)


def resolve_context_window(requested: str | None) -> str:
    """空白上下文窗口用 260K。其他值必须在可选列表里。"""
    if requested is None or requested.strip() == "":
        return DEFAULT_CONTEXT_WINDOW
    value = requested.strip()
    if value in _CONTEXT_WINDOWS:
        return value
    raise _invalid("contextWindow", _CONTEXT_WINDOWS)


def resolve_reasoning_effort(model: str | None, requested: str | None) -> str:
    """空白推理强度按模型给默认值。ultimate 用 high，其余用 medium。"""
    if requested is None or requested.strip() == "":
        return default_reasoning_effort(model)
    value = requested.strip()
    if value in _REASONING_EFFORTS:
        return value
    raise _invalid("reasoningEffort", _REASONING_EFFORTS)


def default_reasoning_effort(model: str | None) -> str:
    """ultimate 模型默认 high，其余模型默认 medium。"""
    if model == ULTIMATE_MODEL:
        return ULTIMATE_REASONING_EFFORT
    return DEFAULT_REASONING_EFFORT


def choose_model(
    models: list[ModelOption] | tuple[ModelOption, ...],
    preferred: str | None,
) -> str | None:
    """优先保留仍在目录中的模型，否则退到 auto，再否则取目录第一项。"""
    values = [item.value for item in models]
    if preferred is not None and preferred in values:
        return preferred
    if AUTO_MODEL in values:
        return AUTO_MODEL
    if not values:
        return preferred
    return values[0]


def resolve_max_concurrent(value: int | None) -> int:
    """省略并发数时用 5。显式值必须是 1 到 10 的整数。"""
    if value is None:
        return DEFAULT_MAX_CONCURRENT
    if value < 1 or value > 10:
        raise BizError(ErrorCode.PARAM_INVALID, "最大并发任务数必须为 1 到 10 的整数")
    return value


def _invalid(field: str, allowed: tuple[str, ...]) -> BizError:
    return BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID, field + " 仅支持 " + "/".join(allowed))
