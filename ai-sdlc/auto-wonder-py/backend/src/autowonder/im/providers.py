"""平台当前选择的 IM 渠道。未选择时按钉钉处理。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.im.models import PlatformImSelection

_DINGTALK = "DINGTALK"
_FEISHU = "FEISHU"


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
    stored = await session.scalar(
        select(PlatformImSelection.provider).where(PlatformImSelection.id == 1)
    )
    return resolve_selected_provider(stored)
