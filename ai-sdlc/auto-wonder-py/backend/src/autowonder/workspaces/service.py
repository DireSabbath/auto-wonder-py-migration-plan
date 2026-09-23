"""鉴权过滤器需要的工作空间与成员查询。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.workspaces.models import Org, OrgMember


async def count_usable(session: AsyncSession, workspace_id: int) -> int:
    """未删除且未停用的工作空间数量。"""
    result = await session.execute(
        select(func.count())
        .select_from(Org)
        .where(Org.id == workspace_id, Org.is_deleted == 0, Org.status == 0)
    )
    return int(result.scalar_one())


async def find_member(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
) -> OrgMember | None:
    """查找未删除的成员记录，不在这里判断 status。"""
    return await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
