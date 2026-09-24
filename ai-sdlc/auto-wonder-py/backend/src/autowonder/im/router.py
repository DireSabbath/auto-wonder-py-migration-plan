"""个人 IM 身份不要求工作空间。通道写入要求平台管理员。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.context import current_user_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.core.schema import ApiModel
from autowonder.db.session import get_session
from autowonder.im.channels import (
    UpdateChannelRequest,
    list_channels,
    update_channel,
)
from autowonder.im.identities import list_identities, send_test, update_identity
from autowonder.im.providers import DINGTALK, FEISHU, ImProviderRegistry
from autowonder.platform.service import require_system_admin
from autowonder.security.crypto import AesGcmSecretCrypto

identity_router = APIRouter(prefix="/api/users/me/im-identities", tags=["im-identities"])
channel_router = APIRouter(prefix="/api/platform/im-channels", tags=["im-channels"])


class UpdateUserImIdentityRequest(ApiModel):
    """保存当前渠道的外部工号。"""

    external_user_id: str | None = None


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


def _cipher() -> AesGcmSecretCrypto:
    return AesGcmSecretCrypto(get_settings().secret_master_key)


def _registry() -> ImProviderRegistry:
    return ImProviderRegistry([])


@identity_router.get("")
async def list_my_identities(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """当前选中渠道上的个人身份。"""
    return ok(await list_identities(session, _user_id()))


@identity_router.put("/feishu")
async def update_feishu_identity(
    body: UpdateUserImIdentityRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """保存飞书工号。"""
    return ok(await update_identity(session, _user_id(), FEISHU, body.external_user_id))


@identity_router.post("/feishu/test")
async def test_feishu_identity(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """发送飞书测试通知。"""
    await send_test(session, _user_id(), FEISHU, _registry())
    return ok(None)


@identity_router.put("/dingtalk")
async def update_dingtalk_identity(
    body: UpdateUserImIdentityRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """保存钉钉工号。"""
    return ok(await update_identity(session, _user_id(), DINGTALK, body.external_user_id))


@identity_router.post("/dingtalk/test")
async def test_dingtalk_identity(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """发送钉钉测试通知。"""
    await send_test(session, _user_id(), DINGTALK, _registry())
    return ok(None)


@channel_router.get("")
async def list_im_channels(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """已登录用户都能查看通道状态。"""
    return ok(await list_channels(session, _user_id()))


async def save_channel(
    session: AsyncSession,
    user_id: int,
    provider: str,
    body: UpdateChannelRequest,
) -> dict[str, Any]:
    """平台管理员才能改通道。"""
    await require_system_admin(session, user_id, "配置协作通知")
    return ok(await update_channel(session, user_id, provider, body, _cipher()))


@channel_router.put("/feishu")
async def update_feishu_channel(
    body: UpdateChannelRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """平台管理员更新飞书通道。"""
    return await save_channel(session, _user_id(), FEISHU, body)


@channel_router.put("/dingtalk")
async def update_dingtalk_channel(
    body: UpdateChannelRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """平台管理员更新钉钉通道。"""
    return await save_channel(session, _user_id(), DINGTALK, body)
