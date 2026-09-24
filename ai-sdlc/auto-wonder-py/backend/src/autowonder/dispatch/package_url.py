"""执行器刷新仍在进行的任务包下载地址。"""

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.artifacts.daemon_auth import authenticate
from autowonder.dispatch.models import Dispatch
from autowonder.storage.objects import get_object_storage

logger = logging.getLogger(__name__)

DOWNLOAD_TTL_SECONDS = 600
REFRESHABLE = frozenset({"DISPATCHED", "ACKED", "RUNNING"})
Presign = Callable[[str, int], str]


def presign_package(oss_ref: str, ttl_seconds: int) -> str:
    """用当前对象存储签发下载地址。"""
    return get_object_storage().presign_get(oss_ref, ttl_seconds)


async def refresh_package_url(
    session: AsyncSession,
    dispatch_id: int,
    token: str | None,
    presign: Presign = presign_package,
) -> tuple[int, dict[str, object] | None]:
    """令牌失败返回空 401。终态或没有包引用时返回 409，不签发地址。"""
    auth = await authenticate(session, dispatch_id, token)
    if not auth.success:
        return 401, None
    dispatch = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    if dispatch is None:
        return 404, None
    oss_ref = dispatch.package_oss_ref
    if dispatch.status not in REFRESHABLE or oss_ref is None or oss_ref.strip() == "":
        return 409, {"error": "package URL is not refreshable"}
    download_url = presign(oss_ref, DOWNLOAD_TTL_SECONDS)
    logger.info(
        "taskpackage download url refreshed dispatchId=%s ossRef=%s ttlSeconds=%s",
        dispatch_id,
        oss_ref,
        DOWNLOAD_TTL_SECONDS,
    )
    return 200, {"downloadUrl": download_url, "expiresInSeconds": DOWNLOAD_TTL_SECONDS}
