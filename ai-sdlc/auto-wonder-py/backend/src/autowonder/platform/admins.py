"""平台管理员名册。升降级只认 ``user.is_admin``，不把首位用户当作永久管理员。"""

import logging
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.platform.models import PlatformAdminInit
from autowonder.platform.schemas import (
    PlatformAdminCandidateView,
    PlatformAdminListView,
    PlatformAdminView,
)
from autowonder.platform.service import (
    count_system_admins,
    ensure_system_admin,
    require_system_admin,
)
from autowonder.users.models import User

logger = logging.getLogger(__name__)

ADMIN_CANDIDATE_LIMIT = 20
SELF_REMOVAL_DENIED = "平台管理员不可移除自己"
LAST_ADMIN_DENIED = "平台管理员至少保留一名，无法移除最后一名"
_ADD_ACTION = "添加平台管理员"
_REMOVE_ACTION = "移除平台管理员"


def list_admins_statement() -> Select[tuple[User]]:
    """未删除的平台管理员，按 id 升序。人数与“至少保留一名”用的是同一条件。"""
    return select(User).where(User.is_deleted == 0, User.is_admin == 1).order_by(User.id.asc())


def user_by_id_statement(user_id: int) -> Select[tuple[User]]:
    """按主键读取未删除用户。"""
    return select(User).where(User.id == user_id, User.is_deleted == 0).limit(1)


def candidate_statement(keyword: str, limit: int) -> Select[tuple[User]]:
    """活跃且还不是管理员的用户。关键字同时匹配登录名、邮箱和昵称。"""
    filters = [User.is_deleted == 0, User.status == 0, User.is_admin == 0]
    if keyword != "":
        pattern = "%" + keyword + "%"
        filters.append(
            or_(
                User.username.like(pattern),
                User.email.like(pattern),
                User.nickname.like(pattern),
            )
        )
    return (
        select(User).where(*filters).order_by(User.gmt_create.desc(), User.id.desc()).limit(limit)
    )


def mark_admin_statement(user_id: int) -> Any:
    """仅当当前不是管理员时提升。并发第二次写入影响 0 行。"""
    return (
        update(User)
        .where(User.id == user_id, User.is_deleted == 0, User.is_admin == 0)
        .values(is_admin=1, gmt_modified=now_local())
    )


def revoke_admin_statement(user_id: int) -> Any:
    """仅当当前是管理员时撤销。"""
    return (
        update(User)
        .where(User.id == user_id, User.is_deleted == 0, User.is_admin == 1)
        .values(is_admin=0, gmt_modified=now_local())
    )


def init_done_statement() -> Select[tuple[int]]:
    """初始化标记行存在且已完成。"""
    return (
        select(func.count())
        .select_from(PlatformAdminInit)
        .where(PlatformAdminInit.id == 1, PlatformAdminInit.initialized == 1)
    )


def mark_init_done_statement() -> Any:
    """写入一次性初始化标记。重复执行仍保持已完成。"""
    statement = insert(PlatformAdminInit).values(id=1, initialized=1)
    return statement.on_duplicate_key_update(initialized=1)


def normalize_admin_keyword(keyword: str | None) -> str:
    """缺省关键字当成空串。Java ``String.trim`` 只去掉码点不大于 U+0020 的字符。"""
    if keyword is None:
        return ""
    return _java_trim(keyword)


def admin_view(
    user: User,
    operator_id: int | None,
    more_than_one: bool,
) -> PlatformAdminView:
    """自己不可移除。只剩一名时，其余行也不可移除，并写明原因。"""
    subject = user.id == operator_id
    removable = False
    if subject:
        reason: str | None = SELF_REMOVAL_DENIED
    elif more_than_one:
        removable = True
        reason = None
    else:
        reason = LAST_ADMIN_DENIED
    return PlatformAdminView(
        user_id=user.id,
        username=user.username,
        nickname=user.nickname,
        email=user.email,
        active=user.status == 0,
        subject=subject,
        removable=removable,
        remove_disabled_reason=reason,
    )


def roster_view(
    operator_is_admin: bool,
    operator_id: int | None,
    admins: list[User],
) -> PlatformAdminListView:
    """按查询顺序组装名册。多于一名管理员时，非本人行可以移除。"""
    more_than_one = len(admins) > 1
    views = [admin_view(admin, operator_id, more_than_one) for admin in admins]
    return PlatformAdminListView(admins=views, can_manage=operator_is_admin)


async def list_platform_admins(
    session: AsyncSession,
    operator_id: int | None,
) -> PlatformAdminListView:
    """任何已登录用户都能看名册。能否管理取决于调用者自己的标记。"""
    admins = list(await session.scalars(list_admins_statement()))
    can_manage = False
    if operator_id is not None:
        operator = await session.scalar(user_by_id_statement(operator_id))
        can_manage = operator is not None and operator.is_admin == 1
    return roster_view(can_manage, operator_id, admins)


async def search_platform_admin_candidates(
    session: AsyncSession,
    keyword: str | None,
) -> list[PlatformAdminCandidateView]:
    """最多返回 20 名候选人。空关键字不加模糊条件。"""
    normalized = normalize_admin_keyword(keyword)
    rows = await session.scalars(candidate_statement(normalized, ADMIN_CANDIDATE_LIMIT))
    return [
        PlatformAdminCandidateView(
            user_id=row.id,
            username=row.username,
            nickname=row.nickname,
            email=row.email,
        )
        for row in rows
    ]


async def add_platform_admin(
    session: AsyncSession,
    operator_id: int | None,
    target_user_id: int | None,
) -> None:
    """提升活跃用户。对方已经是管理员时写入条件不成立，不报错。"""
    await require_system_admin(session, operator_id, _ADD_ACTION)
    if target_user_id is None:
        raise BizError(ErrorCode.SYSTEM_ADMIN_USER_REQUIRED)
    target = await session.scalar(user_by_id_statement(target_user_id))
    if target is None or target.status != 0:
        raise BizError(ErrorCode.SYSTEM_ADMIN_TARGET_NOT_FOUND)
    await session.execute(mark_admin_statement(target_user_id))


async def remove_platform_admin(
    session: AsyncSession,
    operator_id: int | None,
    target_user_id: int | None,
) -> None:
    """不能移除自己，也不能把名册减到零。"""
    await require_system_admin(session, operator_id, _REMOVE_ACTION)
    if target_user_id is None:
        raise BizError(ErrorCode.SYSTEM_ADMIN_USER_REQUIRED)
    if operator_id == target_user_id:
        raise BizError(ErrorCode.SYSTEM_ADMIN_SELF_REMOVAL_FORBIDDEN, SELF_REMOVAL_DENIED)
    target = await session.scalar(user_by_id_statement(target_user_id))
    if target is None or target.is_admin != 1:
        raise BizError(ErrorCode.SYSTEM_ADMIN_TARGET_NOT_ADMIN)
    if await count_system_admins(session) <= 1:
        raise BizError(ErrorCode.SYSTEM_ADMIN_LAST_ONE_FORBIDDEN, LAST_ADMIN_DENIED)
    await session.execute(revoke_admin_statement(target_user_id))


async def ensure_platform_admin_initialized(session: AsyncSession) -> bool:
    """标记已存在则不再提升任何人。没有管理员时仍只提升一次最早的活跃用户。"""
    done = await session.scalar(init_done_statement())
    if done is not None and int(done) > 0:
        return False
    promoted = False
    if await count_system_admins(session) == 0:
        promoted = await ensure_system_admin(session)
    await session.execute(mark_init_done_statement())
    return promoted


async def bootstrap_platform_admins(session: AsyncSession) -> None:
    """启动时跑一次性迁移。失败或事后没有管理员时只记录，不自动再授予。"""
    try:
        promoted = await ensure_platform_admin_initialized(session)
        await session.commit()
        if promoted:
            logger.info("Platform admin init migration promoted the first active user")
    except Exception:
        await session.rollback()
        logger.exception(
            "Platform admin init migration failed; no platform admin was granted. "
            "An administrator must review recovery; Community V067 only records initialization"
        )
        return
    try:
        if await count_system_admins(session) == 0:
            logger.error(
                "No platform admin is configured and the one-shot init migration has "
                "already completed. Grant one manually via the user.is_admin flag; "
                "first-user privileges are no longer granted implicitly"
            )
    except Exception:
        await session.rollback()
        logger.warning("Platform admin roster check skipped", exc_info=True)


def _java_trim(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start = start + 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end = end - 1
    return value[start:end]
