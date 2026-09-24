"""``/api/users/me``。偏好和注销只认登录用户，不要求工作空间。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.context import current_user_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.users.deactivation import (
    get_deactivation_status,
    initiate_deactivation,
    revoke_deactivation,
)
from autowonder.users.preferences import (
    delete_user_setting,
    get_user_setting,
    list_user_settings,
    upsert_user_setting,
)
from autowonder.users.schemas import (
    ChangePasswordRequest,
    DeactivationRequest,
    UpsertUserSettingRequest,
)
from autowonder.users.service import change_password

router = APIRouter(prefix="/api/users/me", tags=["users"])


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


@router.put("/password")
async def put_password(
    body: ChangePasswordRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """校验旧口令后更新当前用户的口令。"""
    await change_password(session, _user_id(), body.old_password, body.new_password)
    return ok(None)


@router.post("/deactivation")
async def post_deactivation(
    body: DeactivationRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """申请注销并进入冷静期。"""
    await initiate_deactivation(session, _user_id(), body.confirm_username)
    return ok(None)


@router.post("/deactivation/revoke")
async def post_deactivation_revoke(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """撤销仍在冷静期内的注销申请。"""
    await revoke_deactivation(session, _user_id())
    return ok(None)


@router.get("/deactivation")
async def read_deactivation(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """查看当前用户的注销状态。"""
    return ok(await get_deactivation_status(session, _user_id()))


@router.get("/settings")
async def list_settings(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出当前用户的偏好。"""
    return ok(await list_user_settings(session, _user_id()))


@router.get("/settings/{key}")
async def read_setting(key: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取一条偏好。没有记录时 valueJson 为 null。"""
    return ok(await get_user_setting(session, _user_id(), key))


@router.put("/settings/{key}")
async def put_setting(
    key: str,
    body: UpsertUserSettingRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """写入一条偏好。缺少正文时与 ``@RequestBody`` 一样是参数不合法。"""
    return ok(await upsert_user_setting(session, _user_id(), key, body.value_json))


@router.delete("/settings/{key}")
async def remove_setting(
    key: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除一条偏好。不存在时也成功。"""
    await delete_user_setting(session, _user_id(), key)
    return ok(None)
