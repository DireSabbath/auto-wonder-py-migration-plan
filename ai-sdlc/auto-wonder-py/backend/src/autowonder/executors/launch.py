"""启动配置的保存、校验，以及从已保存配置生成启动命令。"""

import json
import logging
import re
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.executors.catalog import model_options
from autowonder.executors.models import Executor
from autowonder.executors.options import (
    ModelOption,
    choose_model,
    is_qoder_family,
    require_creatable_client_kind,
    resolve_context_window,
    resolve_max_concurrent,
    resolve_memory_mode,
    resolve_provider,
    resolve_reasoning_effort,
)
from autowonder.executors.schemas import (
    CreateExecutorRequest,
    ExecutorLaunchCommandView,
    ExecutorLaunchConfigView,
    UpdateExecutorLaunchConfigRequest,
)
from autowonder.executors.store import require_executor
from autowonder.executors.updates import runtime_auto_update_view
from autowonder.platform.branding import effective_public_base_url, normalize_public_base_url
from autowonder.platform.models import PlatformBrandingConfig

logger = logging.getLogger(__name__)

_WS_PATH = "/ws/executor"
_SAFE_SHELL_ARG = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_POWERSHELL_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$OutputEncoding = [System.Text.Encoding]::UTF8; "
)
_OS_POSIX = "posix"
_OS_WINDOWS = "windows"
_SHELL_BASH = "bash"
_SHELL_POWERSHELL = "powershell"


@dataclass
class LaunchConfig:
    """executor.launch_config 的字段。空字段不写入 JSON。"""

    model: str | None = None
    reasoning_effort: str | None = None
    context_window: str | None = None
    memory_mode: str | None = None
    max_concurrent_dispatches: int | None = None

    def as_json(self) -> dict[str, object]:
        """收成与 Java 相同的 camelCase JSON 对象，省略空字段。"""
        payload: dict[str, object] = {}
        if self.model is not None:
            payload["model"] = self.model
        if self.reasoning_effort is not None:
            payload["reasoningEffort"] = self.reasoning_effort
        if self.context_window is not None:
            payload["contextWindow"] = self.context_window
        if self.memory_mode is not None:
            payload["memoryMode"] = self.memory_mode
        if self.max_concurrent_dispatches is not None:
            payload["maxConcurrentDispatches"] = self.max_concurrent_dispatches
        return payload


def parse_launch_config(raw: object) -> LaunchConfig:
    """读出已保存的启动配置。空或坏 JSON 当成还没配置。"""
    if raw is None:
        return LaunchConfig()
    payload: object = raw
    if isinstance(raw, str):
        if raw.strip() == "":
            return LaunchConfig()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("executor launch config is not valid JSON, treating it as empty")
            return LaunchConfig()
    if not isinstance(payload, dict):
        return LaunchConfig()
    config = LaunchConfig(
        model=_text(payload.get("model")),
        reasoning_effort=_text(payload.get("reasoningEffort")),
        context_window=_text(payload.get("contextWindow")),
        memory_mode=_text(payload.get("memoryMode")),
        max_concurrent_dispatches=_integer(payload.get("maxConcurrentDispatches")),
    )
    if config.max_concurrent_dispatches is None and _not_blank(config.memory_mode):
        config.max_concurrent_dispatches = resolve_max_concurrent(None)
    return config


def resolve_launch_config(
    client_kind: str | None,
    memory_mode: str | None,
    model: str | None,
    reasoning_effort: str | None,
    context_window: str | None,
    require_known_model: bool,
    models: list[ModelOption] | tuple[ModelOption, ...],
) -> LaunchConfig:
    """按创建和更新共用的规则收齐启动配置。非 Qoder 只保留记忆模式。"""
    provider = resolve_provider(client_kind)
    config = LaunchConfig(memory_mode=resolve_memory_mode(memory_mode))
    if not is_qoder_family(provider):
        return config
    requested = None if model is None else model.strip()
    known = [item.value for item in models]
    if _not_blank(requested) and requested in known:
        config.model = requested
    elif require_known_model or _not_blank(requested):
        raise BizError(ErrorCode.EXECUTOR_LAUNCH_CONFIG_MODEL_INVALID)
    else:
        config.model = choose_model(models, requested)
    config.reasoning_effort = resolve_reasoning_effort(config.model, reasoning_effort)
    config.context_window = resolve_context_window(context_window)
    return config


async def resolve_for_create(request: CreateExecutorRequest) -> tuple[str, LaunchConfig]:
    """创建前先算出完整配置。显式传入的模型必须仍在目录里。"""
    client_kind = require_creatable_client_kind(
        request.client_kind,
        ErrorCode.EXECUTOR_CLIENT_KIND_INVALID,
    )
    explicit_model = request.model is not None and request.model.strip() != ""
    models = await model_options(resolve_provider(client_kind))
    config = resolve_launch_config(
        client_kind,
        request.memory_mode,
        request.model if explicit_model else None,
        request.reasoning_effort,
        request.context_window,
        explicit_model,
        models,
    )
    config.max_concurrent_dispatches = resolve_max_concurrent(request.max_concurrent_dispatches)
    return client_kind, config


async def get_launch_config(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
    user_id: int,
) -> ExecutorLaunchConfigView:
    """读取启动配置。目录里已经没有的模型会被清掉，并尽量写回。"""
    executor = await require_executor(session, executor_id, tenant_id)
    version = executor.config_version
    stored = parse_launch_config(executor.launch_config)
    provider = resolve_provider(executor.client_kind)
    if not is_qoder_family(provider):
        return _config_view(None, None, None, stored, version)
    model = stored.model
    known = [item.value for item in await model_options(provider)]
    if _not_blank(model) and model not in known:
        stored.model = None
        bumped = await _write_back_cleared(session, executor, stored, version, tenant_id, user_id)
        if bumped is not None:
            version = bumped
        model = None
    return _config_view(model, stored.reasoning_effort, stored.context_window, stored, version)


async def update_launch_config(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
    request: UpdateExecutorLaunchConfigRequest,
    user_id: int,
) -> ExecutorLaunchConfigView:
    """按乐观锁保存启动配置。版本不一致时要求刷新后重试。"""
    executor = await require_executor(session, executor_id, tenant_id)
    if request.version is None:
        raise BizError(ErrorCode.PARAM_INVALID, "缺少启动配置版本号")
    expected = request.version
    provider = resolve_provider(executor.client_kind)
    models = await model_options(provider)
    stored = resolve_launch_config(
        executor.client_kind,
        request.memory_mode,
        request.model,
        request.reasoning_effort,
        request.context_window,
        True,
        models,
    )
    existing_max = parse_launch_config(executor.launch_config).max_concurrent_dispatches
    chosen_max = request.max_concurrent_dispatches
    if chosen_max is None:
        chosen_max = existing_max
    stored.max_concurrent_dispatches = resolve_max_concurrent(chosen_max)
    result = await session.execute(
        update(Executor)
        .where(
            Executor.id == executor_id,
            Executor.tenant_id == tenant_id,
            Executor.config_version == expected,
            Executor.is_deleted == 0,
        )
        .values(
            launch_config=stored.as_json(),
            config_version=expected + 1,
            modifier_id=user_id,
        )
    )
    if rowcount(result) == 0:
        raise BizError(ErrorCode.EXECUTOR_LAUNCH_CONFIG_VERSION_CONFLICT)
    await session.commit()
    return _config_view(
        stored.model,
        stored.reasoning_effort,
        stored.context_window,
        stored,
        expected + 1,
    )


async def require_complete_config(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
) -> LaunchConfig:
    """生成命令前要求配置完整，且已保存的模型仍在目录中。"""
    executor = await require_executor(session, executor_id, tenant_id)
    stored = parse_launch_config(executor.launch_config)
    provider = resolve_provider(executor.client_kind)
    if not _not_blank(stored.memory_mode):
        raise _incomplete()
    if not is_qoder_family(provider):
        stored.model = None
        stored.reasoning_effort = None
        stored.context_window = None
        return stored
    if (
        not _not_blank(stored.model)
        or not _not_blank(stored.reasoning_effort)
        or not _not_blank(stored.context_window)
    ):
        raise _incomplete()
    known = [item.value for item in await model_options(provider)]
    if stored.model not in known:
        raise BizError(
            ErrorCode.EXECUTOR_LAUNCH_CONFIG_MODEL_INVALID,
            f"已保存的模型 {stored.model} 已不可用，请重新选择并保存启动配置",
        )
    return stored


def build_ws_url(mcp_base_url: str) -> str:
    """https 变成 wss，其余 http 变成 ws，并保留非默认端口。"""
    try:
        parsed = urlsplit(mcp_base_url.strip())
    except ValueError as error:
        raise BizError(ErrorCode.PARAM_INVALID, "MCP 地址格式不合法") from error
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if scheme not in {"http", "https"} or host is None:
        raise BizError(ErrorCode.PARAM_INVALID, "MCP 地址格式不合法")
    host = host.lower()
    secure = scheme == "https"
    default_port = 443 if secure else 80
    authority = host
    if parsed.port is not None and parsed.port > 0 and parsed.port != default_port:
        authority = f"{host}:{parsed.port}"
    prefix = "wss://" if secure else "ws://"
    return prefix + authority + _WS_PATH


def debug_log_file_name(client_kind: str, executor_id: int, now: datetime) -> str:
    """调试日志文件名，时间按传入时刻的本地钟面格式化。"""
    provider = resolve_provider(client_kind)
    clock = now.astimezone().replace(tzinfo=None) if now.tzinfo is not None else now
    stamp_date = clock.strftime("%y%m%d")
    stamp_time = clock.strftime("%H-%M-%S")
    return f"aw-{provider}-{executor_id}-{stamp_date}-{stamp_time}.log"


def build_launch_command(
    token: str | None,
    executor_id: int,
    client_kind: str | None,
    memory_mode: str | None,
    model: str | None,
    reasoning_effort: str | None,
    context_window: str | None,
    os_name: str | None,
    debug: bool,
    shell: str | None,
    now: datetime,
    max_concurrent_dispatches: int,
    public_base_url: str | None,
    runtime_version: str,
) -> ExecutorLaunchCommandView:
    """按已确定的启动参数拼出一条可粘贴命令。输出格式不影响已保存配置。"""
    if client_kind is None or client_kind.strip() == "":
        raise BizError(ErrorCode.EXECUTOR_CLIENT_KIND_MISSING)
    resolve_max_concurrent(max_concurrent_dispatches)
    if token is None or token.strip() == "":
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID, "token 不能为空")
    resolved_os = _resolve_os(os_name)
    resolved_shell = _resolve_shell(shell, resolved_os) if debug else None
    if public_base_url is None or public_base_url.strip() == "":
        raise BizError(ErrorCode.SYSTEM_ERROR, "平台 MCP 地址未配置，无法生成执行器启动命令")
    ws_url = build_ws_url(public_base_url)
    provider = resolve_provider(client_kind)
    qoder_family = is_qoder_family(provider)
    argv = [
        "npx",
        "-y",
        f"autowonder@{runtime_version}",
        "connect",
        "--ws-url",
        ws_url,
        "--token",
        token,
        "--executor-id",
        str(executor_id),
        "--provider",
        provider,
        "--memory-mode",
        cast(str, memory_mode),
        "--max-tasks",
        str(max_concurrent_dispatches),
    ]
    if qoder_family and model is not None:
        argv.extend(
            [
                "--model",
                model,
                "--reasoning-effort",
                cast(str, reasoning_effort),
                "--context-window",
                cast(str, context_window),
            ]
        )
    if qoder_family:
        argv.append("--token-aware-enable")
    log_file_name = None
    if debug:
        log_file_name = debug_log_file_name(client_kind, executor_id, now)
        argv.append("--debug")
        if resolved_shell == _SHELL_POWERSHELL:
            pipeline = (
                _join_powershell(argv) + f' 2>&1 | Tee-Object -FilePath "$HOME/{log_file_name}"'
            )
            command = _encoded_powershell(pipeline)
        else:
            command = _join_posix(argv) + f" 2>&1 | tee ~/{log_file_name}"
    elif resolved_os == _OS_WINDOWS:
        command = _encoded_powershell(_join_powershell(argv))
    else:
        command = _join_posix(argv)
    return ExecutorLaunchCommandView(
        executor_id=executor_id,
        client_kind=client_kind,
        provider=provider,
        memory_mode=memory_mode,
        max_concurrent_dispatches=max_concurrent_dispatches,
        model=model if qoder_family else None,
        reasoning_effort=reasoning_effort if qoder_family else None,
        context_window=context_window if qoder_family else None,
        ws_url=ws_url,
        runtime_version=runtime_version,
        os=resolved_os,
        debug=debug,
        shell=resolved_shell,
        log_file_name=log_file_name,
        command=command,
    )


async def build_for_executor(
    session: AsyncSession,
    executor_id: int,
    tenant_id: int,
    os_name: str | None,
    debug: bool,
    shell: str | None,
) -> ExecutorLaunchCommandView:
    """用库里的令牌和启动配置生成命令。配置不完整或模型已下线时拒绝。"""
    from autowonder.executors.service import executor_detail, executor_token

    detail = await executor_detail(session, executor_id, tenant_id)
    token = await executor_token(session, executor_id, tenant_id)
    config = await require_complete_config(session, executor_id, tenant_id)
    return build_launch_command(
        token,
        executor_id,
        detail.client_kind,
        config.memory_mode,
        config.model,
        config.reasoning_effort,
        config.context_window,
        os_name,
        debug,
        shell,
        datetime.now().astimezone(),
        resolve_max_concurrent(config.max_concurrent_dispatches),
        await _platform_public_base_url(session),
        runtime_auto_update_view().target_version,
    )


async def _platform_public_base_url(session: AsyncSession) -> str:
    """已保存的品牌域名优先，否则用部署根地址。"""
    settings = get_settings()
    configured = normalize_public_base_url(settings.public_base_url)
    row = await session.scalar(
        select(PlatformBrandingConfig)
        .where(PlatformBrandingConfig.id == 1, PlatformBrandingConfig.is_deleted == 0)
        .limit(1)
    )
    domain = None if row is None else row.domain
    return effective_public_base_url(domain, configured)


async def _write_back_cleared(
    session: AsyncSession,
    executor: Executor,
    cleared: LaunchConfig,
    expected_version: int,
    tenant_id: int,
    user_id: int,
) -> int | None:
    try:
        result = await session.execute(
            update(Executor)
            .where(
                Executor.id == executor.id,
                Executor.tenant_id == tenant_id,
                Executor.config_version == expected_version,
                Executor.is_deleted == 0,
            )
            .values(
                launch_config=cleared.as_json(),
                config_version=expected_version + 1,
                modifier_id=user_id,
            )
        )
    except Exception:
        logger.warning(
            "executor launch config cleanup write-back failed executorId=%s",
            executor.id,
            exc_info=True,
        )
        await session.rollback()
        return None
    if rowcount(result) != 1:
        return None
    await session.commit()
    return expected_version + 1


def _config_view(
    model: str | None,
    reasoning_effort: str | None,
    context_window: str | None,
    stored: LaunchConfig,
    version: int,
) -> ExecutorLaunchConfigView:
    return ExecutorLaunchConfigView(
        model=model,
        reasoning_effort=reasoning_effort,
        context_window=context_window,
        memory_mode=stored.memory_mode,
        max_concurrent_dispatches=stored.max_concurrent_dispatches,
        version=version,
    )


def _incomplete() -> BizError:
    return BizError(ErrorCode.EXECUTOR_LAUNCH_CONFIG_INCOMPLETE)


def _resolve_os(os_name: str | None) -> str:
    if os_name is None or os_name.strip() == "":
        return _OS_POSIX
    value = os_name.strip().lower()
    if value == _OS_POSIX or value == _OS_WINDOWS:
        return value
    raise BizError(
        ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
        "os 仅支持 " + _OS_POSIX + "/" + _OS_WINDOWS,
    )


def _resolve_shell(shell: str | None, os_name: str) -> str:
    if shell is None or shell.strip() == "":
        if os_name == _OS_WINDOWS:
            return _SHELL_POWERSHELL
        return _SHELL_BASH
    value = shell.strip().lower()
    if value == _SHELL_BASH or value == _SHELL_POWERSHELL:
        return value
    raise BizError(
        ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
        "shell 仅支持 " + _SHELL_BASH + "/" + _SHELL_POWERSHELL,
    )


def _join_posix(argv: list[str]) -> str:
    return " ".join(_quote_posix(item) for item in argv)


def _join_powershell(argv: list[str]) -> str:
    return " ".join(_quote_powershell(item) for item in argv)


def _quote_posix(value: str) -> str:
    if _SAFE_SHELL_ARG.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _quote_powershell(value: str) -> str:
    if _SAFE_SHELL_ARG.fullmatch(value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _encoded_powershell(command: str) -> str:
    script = _POWERSHELL_PREAMBLE + command
    encoded = b64encode(script.encode("utf-16-le")).decode("ascii")
    return "powershell -NoProfile -EncodedCommand " + encoded


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _not_blank(value: str | None) -> bool:
    return value is not None and value.strip() != ""
