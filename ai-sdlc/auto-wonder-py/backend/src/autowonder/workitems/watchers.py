"""真人关注。关注不授予写权限，只增加通知收件人。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.users.models import User
from autowonder.workitems.models import Workitem, WorkitemWatcher
from autowonder.workitems.participants import human_participant
from autowonder.workitems.schemas import ParticipantView, WatchStateView, WorkitemView
from autowonder.workspaces.models import OrgMember

_WATCHER_ROLE = "关注人"


async def follow(
    session: AsyncSession, workitem_id: int, tenant_id: int, user_id: int
) -> WatchStateView:
    """重复关注不新增行。"""
    await _require(session, workitem_id, tenant_id)
    existing = await _find(session, tenant_id, workitem_id, user_id)
    if existing is None:
        session.add(
            WorkitemWatcher(tenant_id=tenant_id, workitem_id=workitem_id, user_id=user_id)
        )
        await session.flush()
    await session.commit()
    return await watch_state(session, workitem_id, tenant_id, user_id)


async def unfollow(
    session: AsyncSession, workitem_id: int, tenant_id: int, user_id: int
) -> WatchStateView:
    """取消不存在的关注仍返回未关注。"""
    await _require(session, workitem_id, tenant_id)
    existing = await _find(session, tenant_id, workitem_id, user_id)
    if existing is not None:
        await session.delete(existing)
        await session.flush()
    await session.commit()
    return await watch_state(session, workitem_id, tenant_id, user_id)


async def watch_state(
    session: AsyncSession, workitem_id: int, tenant_id: int, user_id: int
) -> WatchStateView:
    """当前用户是否关注，以及仍有效的关注人数。"""
    watched = await _find(session, tenant_id, workitem_id, user_id) is not None
    people = await _active_users(session, tenant_id, workitem_id)
    return WatchStateView(workitem_id=workitem_id, watched=watched, watcher_count=len(people))


async def list_watchers(
    session: AsyncSession, workitem_id: int, tenant_id: int
) -> list[ParticipantView]:
    """失去空间访问权的用户不出现在关注人列表。"""
    await _require(session, workitem_id, tenant_id)
    views: list[ParticipantView] = []
    for user in await _active_users(session, tenant_id, workitem_id):
        view = human_participant(user)
        view.role_name = _WATCHER_ROLE
        views.append(view)
    return views


async def watched_ids(session: AsyncSession, tenant_id: int, user_id: int) -> set[int]:
    """当前用户在本空间关注的工单。"""
    result = await session.scalars(
        select(WorkitemWatcher).where(
            WorkitemWatcher.tenant_id == tenant_id,
            WorkitemWatcher.user_id == user_id,
        )
    )
    ids: set[int] = set()
    for row in result.all():
        if row.workitem_id is not None:
            ids.add(row.workitem_id)
    return ids


async def mark_watched(
    session: AsyncSession, view: WorkitemView, tenant_id: int, user_id: int
) -> None:
    """详情只查当前用户这一条关注关系。"""
    if view.id is None:
        return
    view.watched = await _find(session, tenant_id, view.id, user_id) is not None


async def apply_watch(
    session: AsyncSession, views: list[WorkitemView], tenant_id: int, user_id: int
) -> None:
    """一页工单只查一次关注关系。"""
    if len(views) == 0:
        return
    watched = await watched_ids(session, tenant_id, user_id)
    for view in views:
        if view.id is not None:
            view.watched = view.id in watched


async def _active_users(session: AsyncSession, tenant_id: int, workitem_id: int) -> list[User]:
    result = await session.scalars(
        select(WorkitemWatcher).where(
            WorkitemWatcher.tenant_id == tenant_id,
            WorkitemWatcher.workitem_id == workitem_id,
        )
    )
    rows = list(result.all())
    rows.sort(key=lambda row: (row.gmt_create, row.id))
    users: list[User] = []
    for row in rows:
        if row.user_id is None:
            continue
        if not await _active_member(session, tenant_id, row.user_id):
            continue
        user = await session.scalar(
            select(User).where(User.id == row.user_id, User.is_deleted == 0).limit(1)
        )
        if user is not None:
            users.append(user)
    return users


async def _active_member(session: AsyncSession, tenant_id: int, user_id: int) -> bool:
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == tenant_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    return member is not None and member.status is not None and member.status == 0


async def _require(session: AsyncSession, workitem_id: int, tenant_id: int) -> Workitem:
    workitem = await session.scalar(
        select(Workitem).where(Workitem.id == workitem_id, Workitem.is_deleted == 0).limit(1)
    )
    if workitem is None or workitem.tenant_id is None or workitem.tenant_id != tenant_id:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return workitem


async def _find(
    session: AsyncSession, tenant_id: int, workitem_id: int, user_id: int
) -> WorkitemWatcher | None:
    return await session.scalar(
        select(WorkitemWatcher)
        .where(
            WorkitemWatcher.tenant_id == tenant_id,
            WorkitemWatcher.workitem_id == workitem_id,
            WorkitemWatcher.user_id == user_id,
        )
        .limit(1)
    )
