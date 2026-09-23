"""系统设置。密文只加密进 credential_ref，列表里用掩码。"""

import json
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.im.providers import selected_provider
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.settings.models import SystemSetting
from autowonder.settings.schemas import SettingItem, SettingView, UpdateSettingsRequest

_GROUPS = {"AI", "STORAGE", "NOTIFY", "DEFAULTS", "SYSTEM"}
_NOTIFY = "NOTIFY"


def require_group(group: str) -> str:
    """只接受页面上的五个分组。"""
    if group not in _GROUPS:
        raise BizError(ErrorCode.SETTING_GROUP_INVALID)
    return group


def allowed_notify_key(key: str | None, selected: str) -> bool:
    """项目通知只露出当前 IM 渠道的键，其他分组键保持可见。"""
    if key is None:
        return False
    provider_key = key.startswith("dingtalk_") or key.startswith("feishu_")
    if not provider_key:
        return True
    return key.startswith(selected.lower() + "_")


def json_text(value: object | None) -> str | None:
    """把 JSON 列读回 Java 接口使用的字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encode_item(
    item: SettingItem,
    crypto: AesGcmSecretCrypto,
) -> tuple[object | None, int, str | None]:
    """密文写入 credential_ref，明文按 JSON 文本解析后落库。"""
    if item.secret:
        return None, 1, crypto.encrypt(cast(str, item.value_json))
    return _json_value(item.value_json), 0, None


def display_value(
    is_secret: int,
    value_json: object | None,
    credential_ref: str | None,
    crypto: AesGcmSecretCrypto,
) -> str | None:
    """列表展示：密文掩码，明文返回 JSON 文本。"""
    if is_secret == 1:
        return crypto.mask(credential_ref)
    return json_text(value_json)


async def list_by_group(session: AsyncSession, group: str, tenant_id: int) -> list[SettingView]:
    """按键名列出一个分组。通知分组会藏起未选中的 IM 渠道。"""
    require_group(group)
    rows = list(
        await session.scalars(
            select(SystemSetting)
            .where(
                SystemSetting.tenant_id == tenant_id,
                SystemSetting.setting_group == group,
                SystemSetting.is_deleted == 0,
            )
            .order_by(SystemSetting.setting_key.asc())
        )
    )
    crypto = _crypto()
    if group == _NOTIFY:
        selected = await selected_provider(session)
        return [
            _to_view(row, crypto) for row in rows if allowed_notify_key(row.setting_key, selected)
        ]
    return [_to_view(row, crypto) for row in rows]


async def update_group(
    session: AsyncSession,
    group: str,
    request: UpdateSettingsRequest,
    tenant_id: int,
    user_id: int,
) -> None:
    """没有条目时直接返回。通知分组先拒绝未选中渠道，再写入。"""
    require_group(group)
    items = request.items
    if items is None or len(items) == 0:
        return
    if group == _NOTIFY:
        selected = await selected_provider(session)
        for item in items:
            if not allowed_notify_key(item.key, selected):
                raise BizError(ErrorCode.PARAM_INVALID, "项目通知必须使用平台选择的 IM 渠道")
    crypto = _crypto()
    for item in items:
        await _upsert(session, group, item, tenant_id, user_id, crypto)
    await session.commit()


async def get_decrypted_value(
    session: AsyncSession,
    group: str,
    key: str,
    tenant_id: int,
) -> str | None:
    """给其他域读取明文。没有这条设置时返回 null。"""
    require_group(group)
    row = await _find(session, tenant_id, group, key)
    if row is None:
        return None
    if row.is_secret == 1:
        return _crypto().decrypt(cast(str, row.credential_ref))
    return json_text(row.value_json)


async def _upsert(
    session: AsyncSession,
    group: str,
    item: SettingItem,
    tenant_id: int,
    user_id: int,
    crypto: AesGcmSecretCrypto,
) -> None:
    value_json, is_secret, credential_ref = encode_item(item, crypto)
    existing = await _find(session, tenant_id, group, item.key)
    if existing is None:
        session.add(
            SystemSetting(
                tenant_id=tenant_id,
                setting_group=group,
                setting_key=item.key,
                value_json=value_json,
                is_secret=is_secret,
                credential_ref=credential_ref,
                creator_id=user_id,
                modifier_id=user_id,
                is_deleted=0,
            )
        )
        await session.flush()
        return
    await session.execute(
        update(SystemSetting)
        .where(
            SystemSetting.id == existing.id,
            SystemSetting.tenant_id == tenant_id,
            SystemSetting.is_deleted == 0,
        )
        .values(
            value_json=value_json,
            is_secret=is_secret,
            credential_ref=credential_ref,
            modifier_id=user_id,
        )
    )


def _to_view(row: SystemSetting, crypto: AesGcmSecretCrypto) -> SettingView:
    return SettingView(
        id=row.id,
        group=row.setting_group,
        key=row.setting_key,
        value_json=display_value(row.is_secret, row.value_json, row.credential_ref, crypto),
        secret=row.is_secret == 1,
    )


async def _find(
    session: AsyncSession,
    tenant_id: int,
    group: str,
    key: str | None,
) -> SystemSetting | None:
    return await session.scalar(
        select(SystemSetting)
        .where(
            SystemSetting.tenant_id == tenant_id,
            SystemSetting.setting_group == group,
            SystemSetting.setting_key == key,
            SystemSetting.is_deleted == 0,
        )
        .limit(1)
    )


def _json_value(raw: str | None) -> object | None:
    if raw is None:
        return None
    return json.loads(raw)


def _crypto() -> AesGcmSecretCrypto:
    return AesGcmSecretCrypto(get_settings().secret_master_key)
