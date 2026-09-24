"""平台 IM 通道。空白密钥保留原密文，启用时配置必须完整。"""

import logging
from typing import Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.evolution.jsontext import java_trim
from autowonder.im.models import PlatformImChannelConfig, PlatformImSelection
from autowonder.im.providers import FEISHU, PROVIDERS, normalize_provider, selected_provider

logger = logging.getLogger(__name__)


class SecretCipher(Protocol):
    """加密和解密通道密钥。"""

    def encrypt(self, plaintext: str) -> str:
        """返回密文引用。"""

    def decrypt(self, ciphertext: str) -> str:
        """还原明文。"""


class ChannelConfigView(ApiModel):
    """返回给前端的通道配置，不含密钥。"""

    provider: str
    enabled: bool
    selected: bool = False
    app_key: str | None = None
    robot_code: str | None = None
    base_url: str | None = None
    secret_configured: bool
    ready: bool


class UpdateChannelRequest(ApiModel):
    """更新钉钉或飞书通道。空白密钥表示保留已有密文。"""

    enabled: bool = False
    app_key: str | None = None
    app_secret: str | None = None
    robot_code: str | None = None
    base_url: str | None = None


async def list_channels(session: AsyncSession, user_id: int) -> list[ChannelConfigView]:
    """列出两个渠道。未落库的渠道按关闭展示。"""
    selected = await selected_provider(session)
    views: list[ChannelConfigView] = []
    for provider in PROVIDERS:
        row = await _find_active(session, provider)
        view = _view(row, provider)
        view.selected = selected == provider
        views.append(view)
    logger.info("IM notification platform config read operatorId=%s", user_id)
    return views


async def update_channel(
    session: AsyncSession,
    user_id: int,
    provider: str,
    request: UpdateChannelRequest | None,
    cipher: SecretCipher,
) -> ChannelConfigView:
    """写入一个渠道，关掉其他渠道，并把选择切过来。"""
    normalized = normalize_provider(provider)
    if request is None:
        raise BizError(ErrorCode.PARAM_INVALID, "请求不能为空")
    app_key = _limited(request.app_key, 128, "appKey")
    secret = _limited(request.app_secret, 1024, "appSecret")
    robot_code = _limited(request.robot_code, 128, "robotCode")
    base_url = _https_url(request.base_url)
    credential = None if secret is None else cipher.encrypt(secret)
    if credential is not None and len(credential) > 1024:
        raise BizError(ErrorCode.PARAM_INVALID, "加密凭据引用过长")
    await session.scalar(select(PlatformImSelection).where(PlatformImSelection.id == 1).limit(1))
    existing = await _find_any(session, normalized)
    stored_credential = credential
    if not _has_text(credential) and existing is not None:
        stored_credential = existing.credential_ref
    enabled = 1 if request.enabled else 0
    if enabled == 1 and not _complete_fields(normalized, app_key, stored_credential, robot_code):
        raise BizError(ErrorCode.IM_CHANNEL_NOT_READY)
    saved = await _upsert(
        session,
        existing,
        normalized,
        enabled,
        app_key,
        stored_credential,
        robot_code,
        base_url,
        user_id,
    )
    await _disable_others(session, normalized, user_id)
    await _select(session, normalized)
    await session.commit()
    logger.info(
        "IM notification platform config updated provider=%s enabled=%s secretConfigured=%s",
        normalized,
        saved.enabled == 1,
        _has_text(saved.credential_ref),
    )
    view = _view(saved, normalized)
    view.selected = True
    return view


async def find_enabled(session: AsyncSession, provider: str) -> PlatformImChannelConfig | None:
    """只有当前选中且启用的行可以拿来发消息。"""
    normalized = normalize_provider(provider)
    if await selected_provider(session) != normalized:
        return None
    row = await _find_active(session, normalized)
    if row is None or row.enabled != 1:
        return None
    return row


def decrypt_secret(row: PlatformImChannelConfig | None, cipher: SecretCipher) -> str | None:
    """没有密文时返回空。"""
    if row is None or not _has_text(row.credential_ref) or row.credential_ref is None:
        return None
    return cipher.decrypt(row.credential_ref)


async def is_ready(session: AsyncSession, provider: str) -> bool:
    """启用且字段齐全才算可测试。飞书不要求机器人编码。"""
    row = await find_enabled(session, provider)
    return row is not None and _complete(row)


async def _find_any(session: AsyncSession, provider: str) -> PlatformImChannelConfig | None:
    return await session.scalar(
        select(PlatformImChannelConfig)
        .where(PlatformImChannelConfig.provider == provider)
        .limit(1)
    )


async def _upsert(
    session: AsyncSession,
    existing: PlatformImChannelConfig | None,
    provider: str,
    enabled: int,
    app_key: str | None,
    credential: str | None,
    robot_code: str | None,
    base_url: str | None,
    user_id: int,
) -> PlatformImChannelConfig:
    if existing is None:
        row = PlatformImChannelConfig(
            provider=provider,
            enabled=enabled,
            app_key=app_key,
            credential_ref=credential,
            robot_code=robot_code,
            base_url=base_url,
            creator_id=user_id,
            modifier_id=user_id,
            is_deleted=0,
            version=0,
        )
        session.add(row)
        await session.flush()
        return row
    existing.enabled = enabled
    existing.app_key = app_key
    existing.credential_ref = credential
    existing.robot_code = robot_code
    existing.base_url = base_url
    existing.modifier_id = user_id
    existing.is_deleted = 0
    existing.version = existing.version + 1
    existing.gmt_modified = now_local()
    await session.flush()
    return existing


async def _disable_others(session: AsyncSession, provider: str, user_id: int) -> None:
    rows = (
        await session.scalars(
            select(PlatformImChannelConfig).where(
                PlatformImChannelConfig.enabled == 1,
                PlatformImChannelConfig.is_deleted == 0,
            )
        )
    ).all()
    for row in rows:
        if row.provider == provider:
            continue
        row.enabled = 0
        row.modifier_id = user_id
        row.version = row.version + 1
        row.gmt_modified = now_local()


async def _select(session: AsyncSession, provider: str) -> None:
    row = await session.scalar(
        select(PlatformImSelection).where(PlatformImSelection.id == 1).limit(1)
    )
    if row is None:
        session.add(PlatformImSelection(id=1, provider=provider))
        await session.flush()
        return
    row.provider = provider


async def _find_active(session: AsyncSession, provider: str) -> PlatformImChannelConfig | None:
    return await session.scalar(
        select(PlatformImChannelConfig)
        .where(
            PlatformImChannelConfig.provider == provider,
            PlatformImChannelConfig.is_deleted == 0,
        )
        .limit(1)
    )


def _view(row: PlatformImChannelConfig | None, provider: str) -> ChannelConfigView:
    if row is None:
        return ChannelConfigView(
            provider=provider,
            enabled=False,
            app_key=None,
            robot_code=None,
            base_url=None,
            secret_configured=False,
            ready=False,
        )
    enabled = row.enabled == 1
    complete = _complete(row)
    return ChannelConfigView(
        provider=row.provider,
        enabled=enabled,
        app_key=row.app_key,
        robot_code=row.robot_code,
        base_url=row.base_url,
        secret_configured=_has_text(row.credential_ref),
        ready=enabled and complete,
    )


def _complete(row: PlatformImChannelConfig) -> bool:
    return _complete_fields(row.provider, row.app_key, row.credential_ref, row.robot_code)


def _complete_fields(
    provider: str,
    app_key: str | None,
    credential: str | None,
    robot_code: str | None,
) -> bool:
    if not _has_text(app_key) or not _has_text(credential):
        return False
    if provider == FEISHU:
        return True
    return _has_text(robot_code)


def _limited(value: str | None, max_length: int, field: str) -> str | None:
    normalized = _trim_to_null(value)
    if normalized is not None and len(normalized) > max_length:
        raise BizError(ErrorCode.PARAM_INVALID, f"{field} 长度不能超过 {max_length}")
    return normalized


def _https_url(value: str | None) -> str | None:
    normalized = _limited(value, 512, "baseUrl")
    if normalized is None:
        return None
    parts = urlsplit(normalized)
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.query != ""
        or parts.fragment != ""
    ):
        raise BizError(ErrorCode.PARAM_INVALID, "baseUrl 必须是绝对 HTTPS URL")
    while normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def _trim_to_null(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = java_trim(value)
    if trimmed == "":
        return None
    return trimmed


def _has_text(value: str | None) -> bool:
    return value is not None and not java_is_blank(value)
