"""``/api/platform/branding``。公开配置和 Logo 不要求登录。"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from autowonder.core.context import current_user_id
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.platform.branding import (
    admin_branding,
    public_branding,
    read_logo,
    update_branding,
    upload_logo,
)
from autowonder.platform.schemas import UpdateBrandingRequest
from autowonder.storage.objects import ObjectStorageError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/platform/branding", tags=["branding"])


@router.get("/public")
async def read_public(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """登录前读取品牌配置。"""
    return ok(await public_branding(session))


@router.get("/logo")
async def read_logo_bytes(session: AsyncSession = Depends(get_session)) -> Response:
    """返回当前 Logo。对象存储失败时响应 502，没有 Logo 时响应 404。"""
    try:
        payload = await read_logo(session)
    except ObjectStorageError as error:
        logger.error("logo OSS read failed: %s", error)
        return Response(status_code=502)
    if payload is None:
        return Response(status_code=404)
    data, content_type = payload
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.post("/logo")
async def post_logo(
    session: AsyncSession = Depends(get_session),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """平台管理员上传 Logo。"""
    data = await file.read()
    return ok(await upload_logo(session, current_user_id(), file.content_type, data))


@router.get("")
async def read_admin(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """登录后读取品牌配置，并标出当前用户能否管理。"""
    return ok(await admin_branding(session, current_user_id()))


@router.put("")
async def put_branding(
    body: UpdateBrandingRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """平台管理员更新品牌配置。"""
    return ok(await update_branding(session, current_user_id(), body))
