"""平台管理员。首次注册且尚无管理员时，提升最早的活跃用户。"""

from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.users.models import User


async def count_system_admins(session: AsyncSession) -> int:
    """统计未删除的平台管理员人数。"""
    result = await session.execute(
        select(func.count()).select_from(User).where(User.is_deleted == 0, User.is_admin == 1)
    )
    return int(result.scalar_one())


async def ensure_system_admin(session: AsyncSession) -> bool:
    """没有管理员时，把 id 最小的活跃用户设为管理员。"""
    if await count_system_admins(session) > 0:
        return False
    first_active_user_id = await session.scalar(
        select(User.id)
        .where(User.is_deleted == 0, User.status == 0)
        .order_by(User.id.asc())
        .limit(1)
    )
    if first_active_user_id is None:
        return False
    result = await session.execute(
        update(User)
        .where(User.id == first_active_user_id, User.is_deleted == 0, User.is_admin == 0)
        .values(is_admin=1)
    )
    return cast(CursorResult[Any], result).rowcount == 1
