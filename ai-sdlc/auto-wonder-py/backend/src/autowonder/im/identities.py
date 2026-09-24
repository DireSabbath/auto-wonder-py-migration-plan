"""用户在当前 IM 渠道上的身份。空白工号会软删已有记录。"""

import hashlib
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.evolution.jsontext import java_trim
from autowonder.im.channels import is_ready
from autowonder.im.models import UserImIdentity
from autowonder.im.providers import (
    ImProviderRegistry,
    ImSendCommand,
    normalize_provider,
    require_selected,
    selected_provider,
)
from autowonder.platform.branding import DEFAULT_PLATFORM_NAME, public_branding

logger = logging.getLogger(__name__)


class UserImIdentityView(ApiModel):
    """当前渠道下的个人 IM 身份。"""

    provider: str
    external_user_id: str | None = None
    configured: bool
    platform_ready: bool
    test_available: bool


async def list_identities(session: AsyncSession, user_id: int) -> list[UserImIdentityView]:
    """只返回平台当前选中的那一个渠道。"""
    provider = await selected_provider(session)
    return [await capability(session, user_id, provider)]


async def capability(
    session: AsyncSession,
    user_id: int,
    provider: str,
) -> UserImIdentityView:
    """身份和通道都齐了，测试按钮才可用。"""
    normalized = normalize_provider(provider)
    row = await _find(session, user_id, normalized)
    ready = await is_ready(session, normalized)
    if row is None:
        return _empty(normalized, ready)
    return _view(row, ready)


async def update_identity(
    session: AsyncSession,
    user_id: int,
    provider: str,
    external_user_id: str | None,
) -> UserImIdentityView:
    """保存或清空当前渠道的工号。"""
    normalized = normalize_provider(provider)
    await require_selected(session, normalized)
    external_id = _trim_to_null(external_user_id)
    if external_id is not None and len(external_id) > 256:
        raise BizError(ErrorCode.PARAM_INVALID, "externalUserId 长度不能超过 256")
    if external_id is None:
        await _soft_delete(session, user_id, normalized)
        await session.commit()
        logger.info(
            "IM notification user identity cleared provider=%s userId=%s",
            normalized,
            user_id,
        )
        return _empty(normalized, await is_ready(session, normalized))
    row = await _upsert(session, user_id, normalized, external_id)
    await session.commit()
    logger.info(
        "IM notification user identity updated provider=%s userId=%s fingerprint=%s",
        normalized,
        user_id,
        _fingerprint(external_id),
    )
    return _view(row, await is_ready(session, normalized))


async def send_test(
    session: AsyncSession,
    user_id: int,
    provider: str,
    registry: ImProviderRegistry,
) -> None:
    """用已保存的工号发一条带平台名称的测试通知。"""
    normalized = normalize_provider(provider)
    await require_selected(session, normalized)
    row = await _find(session, user_id, normalized)
    if row is None or not _has_text(row.external_user_id):
        raise BizError(ErrorCode.IM_IDENTITY_NOT_CONFIGURED)
    if not await is_ready(session, normalized):
        raise BizError(ErrorCode.IM_CHANNEL_NOT_READY)
    brand = (await public_branding(session)).platform_name
    if not _has_text(brand):
        brand = DEFAULT_PLATFORM_NAME
    title = brand + " 协作通知测试成功"
    command = ImSendCommand(
        normalized,
        row.external_user_id,
        title,
        "## " + title + "\n\n你的 IM 协作通知配置可正常使用。",
    )
    try:
        await registry.require(normalized).send(command)
    except BizError:
        raise
    except Exception:
        # 供应商异常原文可能含密钥或工号，接口只返回固定文案。
        raise BizError(ErrorCode.IM_TEST_SEND_FAILED) from None
    logger.info(
        "IM notification test delivered provider=%s userId=%s recipientFingerprint=%s",
        normalized,
        user_id,
        _fingerprint(row.external_user_id),
    )


async def _find(session: AsyncSession, user_id: int, provider: str) -> UserImIdentity | None:
    return await session.scalar(
        select(UserImIdentity)
        .where(
            UserImIdentity.user_id == user_id,
            UserImIdentity.provider == provider,
            UserImIdentity.is_deleted == 0,
        )
        .limit(1)
    )


async def _upsert(
    session: AsyncSession,
    user_id: int,
    provider: str,
    external_user_id: str,
) -> UserImIdentity:
    existing = await session.scalar(
        select(UserImIdentity)
        .where(UserImIdentity.user_id == user_id, UserImIdentity.provider == provider)
        .limit(1)
    )
    if existing is None:
        row = UserImIdentity(
            user_id=user_id,
            provider=provider,
            external_user_id=external_user_id,
            creator_id=user_id,
            modifier_id=user_id,
            is_deleted=0,
            version=0,
        )
        session.add(row)
        await session.flush()
        return row
    existing.external_user_id = external_user_id
    existing.modifier_id = user_id
    existing.is_deleted = 0
    existing.version = existing.version + 1
    existing.gmt_modified = now_local()
    await session.flush()
    return existing


async def _soft_delete(session: AsyncSession, user_id: int, provider: str) -> None:
    await session.execute(
        update(UserImIdentity)
        .where(
            UserImIdentity.user_id == user_id,
            UserImIdentity.provider == provider,
            UserImIdentity.is_deleted == 0,
        )
        .values(is_deleted=1, modifier_id=user_id, version=UserImIdentity.version + 1)
    )


def _view(row: UserImIdentity, platform_ready: bool) -> UserImIdentityView:
    configured = _has_text(row.external_user_id)
    return UserImIdentityView(
        provider=row.provider,
        external_user_id=row.external_user_id,
        configured=configured,
        platform_ready=platform_ready,
        test_available=configured and platform_ready,
    )


def _empty(provider: str, platform_ready: bool) -> UserImIdentityView:
    return UserImIdentityView(
        provider=provider,
        external_user_id=None,
        configured=False,
        platform_ready=platform_ready,
        test_available=False,
    )


def _fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:12]


def _trim_to_null(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = java_trim(value)
    if trimmed == "":
        return None
    return trimmed


def _has_text(value: str | None) -> bool:
    return value is not None and not java_is_blank(value)
