"""平台当前选择的 IM 渠道，以及发送端口。未选择时按钉钉处理。"""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.evolution.jsontext import java_trim
from autowonder.im.models import PlatformImSelection

_DINGTALK = "DINGTALK"
_FEISHU = "FEISHU"
DINGTALK = _DINGTALK
FEISHU = _FEISHU
PROVIDERS = (DINGTALK, FEISHU)


def normalize_provider(provider: str | None) -> str:
    """把渠道名收成 DINGTALK 或 FEISHU。"""
    normalized = ""
    if provider is not None:
        normalized = provider.strip().upper()
    if normalized == "":
        raise BizError(ErrorCode.PARAM_INVALID, "IM provider 不能为空")
    if normalized != _DINGTALK and normalized != _FEISHU:
        raise BizError(ErrorCode.PARAM_INVALID, "不支持的 IM provider")
    return normalized


def resolve_selected_provider(stored: str | None) -> str:
    """选择行为空时用钉钉，否则按渠道名归一。"""
    if stored is None:
        return _DINGTALK
    return normalize_provider(stored)


async def selected_provider(session: AsyncSession) -> str:
    """读取平台当前 IM 渠道。"""
    row = await session.scalar(
        select(PlatformImSelection).where(PlatformImSelection.id == 1).limit(1)
    )
    if row is None:
        return _DINGTALK
    return resolve_selected_provider(row.provider)


def require_selected_provider(selected: str, provider: str) -> None:
    """偏好里打开的渠道必须是平台当前选择的那一个。"""
    if selected != normalize_provider(provider):
        raise BizError(ErrorCode.PARAM_INVALID, "请使用平台当前选择的 IM 渠道")


async def require_selected(session: AsyncSession, provider: str) -> None:
    """核对平台当前 IM 渠道。"""
    require_selected_provider(await selected_provider(session), provider)


class ImDeliveryError(Exception):
    """供应商拒绝发送。调用方改写成固定业务错误，避免带出密钥。"""

    def __init__(
        self,
        provider: str,
        retryable: bool,
        provider_code: str | None,
        provider_request_id: str | None,
    ) -> None:
        self.provider = provider
        self.retryable = retryable
        self.provider_code = provider_code
        self.provider_request_id = provider_request_id
        super().__init__("im delivery failed")


@dataclass(frozen=True)
class ImSendCommand:
    """一条协作通知。"""

    provider: str
    external_user_id: str
    title: str
    markdown: str


class ImProvider(Protocol):
    """一个 IM 供应商。"""

    def provider(self) -> str:
        """规范名称。"""

    async def send(self, command: ImSendCommand) -> None:
        """发送一条消息。"""


class ImProviderRegistry:
    """按规范名称查找供应商。重复注册直接失败。"""

    def __init__(self, providers: list[ImProvider]) -> None:
        indexed: dict[str, ImProvider] = {}
        for item in providers:
            name = _registry_name(item.provider())
            if name in indexed:
                raise RuntimeError(f"Duplicate IM provider: {name}")
            indexed[name] = item
        self._providers = indexed

    def require(self, provider: str) -> ImProvider:
        """没有对应实现时拒绝。"""
        name = _registry_name(provider)
        selected = self._providers.get(name)
        if selected is None:
            raise ValueError(f"Unsupported IM provider: {name}")
        return selected


def _registry_name(provider: str | None) -> str:
    if provider is None or java_is_blank(provider):
        raise ValueError("IM provider is required")
    return java_trim(provider).upper()
