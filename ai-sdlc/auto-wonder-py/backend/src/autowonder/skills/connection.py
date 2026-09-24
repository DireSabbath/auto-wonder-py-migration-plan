"""MCP 技能连接测试。失败写进结果，不把安装规格里的密钥打进日志。"""

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel
from autowonder.skills.install_spec import _crypto
from autowonder.skills.runtime_mcp import SkillConnectionTestResult, test_runtime
from autowonder.skills.service import _require_skill

logger = logging.getLogger(__name__)


class SkillConnectionTestView(ApiModel):
    """一次连接测试的展示结果。工具列表不落库。"""

    success: bool
    message: str
    duration_ms: int | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)


def failure_view(started_at: float, message: str | None) -> SkillConnectionTestView:
    """失败结果。空白原因统一写成「连接失败」。"""
    text = "连接失败" if message is None or message.strip() == "" else message
    return SkillConnectionTestView(
        success=False,
        message=text,
        duration_ms=elapsed_millis(started_at),
    )


def elapsed_millis(started_at: float) -> int:
    """从单调时钟起点到现在的毫秒数。"""
    return int((time.monotonic() - started_at) * 1000)


def parse_config(install_spec: object) -> dict[str, Any]:
    """安装规格必须是 JSON 对象。"""
    if install_spec is None:
        raise ValueError("MCP 配置为空")
    if isinstance(install_spec, str):
        if install_spec.strip() == "":
            raise ValueError("MCP 配置为空")
        try:
            parsed = json.loads(install_spec)
        except json.JSONDecodeError as error:
            raise ValueError("MCP 配置不是有效 JSON") from error
    elif isinstance(install_spec, dict):
        parsed = install_spec
    else:
        raise ValueError("MCP 配置不是有效 JSON")
    if not isinstance(parsed, dict):
        raise ValueError("MCP 配置不是有效 JSON")
    return parsed


def config_args(config: Mapping[str, Any]) -> list[str]:
    """命令参数。没有数组时为空。"""
    raw = config.get("args")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def timeout_seconds(config: Mapping[str, Any]) -> int:
    """缺省 60 秒。"""
    raw = config.get("timeoutSeconds")
    if isinstance(raw, bool) or raw is None:
        return 60
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return 60


def transport_of(config: Mapping[str, Any]) -> str:
    """未声明传输方式时按 HTTP。"""
    raw = config.get("transport")
    if raw is None or str(raw).strip() == "":
        return "http"
    return str(raw)


def resolve_values(source: object) -> dict[str, str]:
    """字面量原样保留。``secretRef`` 用主密钥解开。"""
    if not isinstance(source, Mapping) or len(source) == 0:
        return {}
    values: dict[str, str] = {}
    for key, value in source.items():
        if isinstance(value, Mapping) and str(value.get("kind")) == "secretRef":
            crypto = _crypto()
            if crypto is None:
                raise RuntimeError("密文存储未配置，无法测试私密 MCP 配置")
            values[str(key)] = crypto.decrypt(str(value.get("ref")))
            continue
        values[str(key)] = str(value)
    return values


def normalize_error(error: Exception) -> str:
    """超时有固定文案，其余用异常消息。"""
    if isinstance(error, TimeoutError):
        return "连接超时"
    message = str(error)
    if message.strip() == "":
        return type(error).__name__
    return message


async def test_connection(
    session: AsyncSession,
    skill_id: int,
    tenant_id: int,
    executor_id: int | None,
) -> SkillConnectionTestView:
    """只测 MCP。配置或 Runtime 失败时返回失败结果，技能不存在仍抛业务错误。"""
    skill = await _require_skill(session, skill_id, tenant_id)
    if skill.type.upper() != "MCP":
        raise BizError(ErrorCode.PARAM_INVALID, "仅 MCP 类型能力支持连接测试")
    started_at = time.monotonic()
    transport = "unknown"
    try:
        config = parse_config(skill.install_spec)
        transport = transport_of(config)
        if executor_id is None:
            return failure_view(started_at, "请选择在线 Runtime 测试 MCP")
        headers = resolve_values(config.get("headers"))
        env = resolve_values(config.get("env"))
        result = await test_runtime(
            session,
            tenant_id,
            executor_id,
            transport,
            _optional_text(config.get("command")),
            config_args(config),
            _optional_text(config.get("url")),
            headers,
            env,
            timeout_seconds(config),
        )
        return _from_runtime(started_at, result)
    except Exception as error:
        if isinstance(error, ValueError):
            logger.warning(
                "MCP connection test rejected skillId=%s tenantId=%s executorId=%s "
                "transport=%s errorType=%s message=%s",
                skill_id,
                tenant_id,
                executor_id,
                transport,
                type(error).__name__,
                str(error),
            )
        else:
            logger.error(
                "MCP connection test failed skillId=%s tenantId=%s executorId=%s "
                "transport=%s errorType=%s",
                skill_id,
                tenant_id,
                executor_id,
                transport,
                type(error).__name__,
            )
        return failure_view(started_at, normalize_error(error))


def _from_runtime(started_at: float, result: SkillConnectionTestResult) -> SkillConnectionTestView:
    duration = elapsed_millis(started_at)
    if result.duration_ms is not None:
        duration = result.duration_ms
    message = result.message
    if message is None or message.strip() == "":
        message = "连接失败"
    return SkillConnectionTestView(
        success=result.success,
        message=message,
        duration_ms=duration,
        tools=result.tools,
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
