"""当前用户的偏好。每条语句都带登录用户 id，软删行仍占唯一键。"""

import json

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.users.models import UserSetting
from autowonder.users.schemas import UserSettingView

MAX_KEY_LENGTH = 128
MAX_VALUE_JSON_LENGTH = 65536


def java_utf16_length(text: str) -> int:
    """Java ``String.length()`` 计的是 UTF-16 代码单元。"""
    return len(text.encode("utf-16-le")) // 2


def validate_setting_key(key: str | None) -> str:
    """空键和超过列宽的键都是参数错误。"""
    if key is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    if key.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID)
    if java_utf16_length(key) > MAX_KEY_LENGTH:
        raise BizError(ErrorCode.PARAM_INVALID)
    return key


def normalize_value_json(value_json: str | None) -> tuple[str | None, object | None]:
    """校验 JSON 文本。null 表示清空；返回值是回显文本和准备入库的对象。"""
    if value_json is None:
        return None, None
    if value_json.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID)
    if java_utf16_length(value_json) > MAX_VALUE_JSON_LENGTH:
        raise BizError(ErrorCode.PARAM_INVALID)
    try:
        parsed = json.loads(value_json)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    return value_json, parsed


def json_text(value: object | None) -> str | None:
    """把 JSON 列读回接口上的 JSON 文本。"""
    if value is None:
        return None
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def to_setting_view(key: str, stored: object | None) -> UserSettingView:
    """已入库的值按 JSON 文本返回。从未设置时 valueJson 为 null。"""
    return UserSettingView(key=key, value_json=json_text(stored))


async def get_user_setting(session: AsyncSession, user_id: int, key: str) -> UserSettingView:
    """读取一条未删除的偏好。没有记录时仍返回这个键。"""
    setting_key = validate_setting_key(key)
    row = await _find_live(session, user_id, setting_key)
    stored = None
    if row is not None:
        stored = row.value_json
    return to_setting_view(setting_key, stored)


async def list_user_settings(session: AsyncSession, user_id: int) -> list[UserSettingView]:
    """按键名列出当前用户未删除的偏好。"""
    rows = await session.scalars(
        select(UserSetting)
        .where(UserSetting.user_id == user_id, UserSetting.is_deleted == 0)
        .order_by(UserSetting.setting_key)
    )
    return [to_setting_view(row.setting_key, row.value_json) for row in rows]


async def upsert_user_setting(
    session: AsyncSession,
    user_id: int,
    key: str,
    value_json: str | None,
) -> UserSettingView:
    """插入或原地更新。软删行占着唯一键，再次写入时复活原行。"""
    setting_key = validate_setting_key(key)
    echoed, parsed = normalize_value_json(value_json)
    existing = await _find_by_uk(session, user_id, setting_key)
    if existing is None:
        session.add(
            UserSetting(
                user_id=user_id,
                setting_key=setting_key,
                value_json=parsed,
                creator_id=user_id,
                modifier_id=user_id,
                is_deleted=0,
            )
        )
    else:
        await session.execute(
            update(UserSetting)
            .where(UserSetting.id == existing.id, UserSetting.user_id == user_id)
            .values(
                value_json=parsed,
                modifier_id=user_id,
                is_deleted=0,
                gmt_modified=now_local(),
            )
        )
    await session.commit()
    return UserSettingView(key=setting_key, value_json=echoed)


async def delete_user_setting(session: AsyncSession, user_id: int, key: str) -> None:
    """删除不存在的键直接返回，方便前端用一次删除恢复默认。"""
    setting_key = validate_setting_key(key)
    existing = await _find_by_uk(session, user_id, setting_key)
    if existing is None:
        return
    await session.execute(
        update(UserSetting)
        .where(
            UserSetting.id == existing.id,
            UserSetting.user_id == user_id,
            UserSetting.is_deleted == 0,
        )
        .values(is_deleted=1, modifier_id=user_id, gmt_modified=now_local())
    )
    await session.commit()


async def _find_live(session: AsyncSession, user_id: int, key: str) -> UserSetting | None:
    return await session.scalar(
        select(UserSetting)
        .where(
            UserSetting.user_id == user_id,
            UserSetting.setting_key == key,
            UserSetting.is_deleted == 0,
        )
        .limit(1)
    )


async def _find_by_uk(session: AsyncSession, user_id: int, key: str) -> UserSetting | None:
    return await session.scalar(
        select(UserSetting)
        .where(UserSetting.user_id == user_id, UserSetting.setting_key == key)
        .limit(1)
    )
