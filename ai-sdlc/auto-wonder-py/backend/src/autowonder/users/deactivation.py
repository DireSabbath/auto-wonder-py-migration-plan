"""账号注销。冷静期 7 天，到期后匿名化；唯一管理员和未完结工单会拦住申请。"""

import logging
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.users.models import User
from autowonder.users.schemas import DeactivationStatusView
from autowonder.users.service import find_user_by_id
from autowonder.workspaces.models import OrgMember

logger = logging.getLogger(__name__)

COOLING_OFF_DAYS = 7
_EXPIRED_LIMIT = 100

# Java 的 TenantSqlRewriter 不改写 JOIN。注销拦截必须看见全部工作空间的未完结工单，
# 所以这条计数保持原文，不走会按当前工作空间收窄的 ORM 条件。
ACTIVE_ASSIGNEE_SQL = text(
    """
    SELECT COUNT(*) FROM workitem w
    INNER JOIN status_node sn ON sn.id = w.status_node_id
    WHERE w.is_deleted = 0
      AND w.assignee_type = :assignee_type
      AND w.assignee_ref = :assignee_ref
      AND sn.category NOT IN ('DONE', 'CANCELED')
    """
)


def cooling_off_deadline(now: datetime) -> datetime:
    """申请时刻加 7 个日历日。上海没有夏令时，与 ``Calendar.DAY_OF_MONTH`` 一致。"""
    return now + timedelta(days=COOLING_OFF_DAYS)


def confirm_username_matches(confirm_username: str | None, username: str) -> bool:
    """确认文本必须与登录名完全相同。"""
    if confirm_username is None:
        return False
    return confirm_username == username


def account_is_disabled(status: int | None) -> bool:
    """status 为 1 表示账号已禁用。"""
    if status is None:
        return False
    return status == 1


def in_cooling_off(
    deactivated_at: datetime | None,
    cooling_off_expires_at: datetime | None,
    deactivation_revoked_at: datetime | None,
    now: datetime,
) -> bool:
    """冷静期截止时刻晚于当前时刻，且申请尚未撤销。"""
    if deactivated_at is None:
        return False
    if deactivation_revoked_at is not None:
        return False
    if cooling_off_expires_at is None:
        return False
    return cooling_off_expires_at > now


def deactivation_expired(
    deactivated_at: datetime | None,
    cooling_off_expires_at: datetime | None,
    deactivation_revoked_at: datetime | None,
    status: int | None,
    now: datetime,
) -> bool:
    """冷静期已到且账号已被禁用，状态接口把这种情况标成已撤销。"""
    if deactivated_at is None:
        return False
    if cooling_off_expires_at is None:
        return False
    if deactivation_revoked_at is not None:
        return False
    if cooling_off_expires_at > now:
        return False
    return account_is_disabled(status)


def deactivation_view(
    deactivated_at: datetime | None,
    cooling_off_expires_at: datetime | None,
    deactivation_revoked_at: datetime | None,
    status: int | None,
    now: datetime,
) -> DeactivationStatusView:
    """组装注销状态。时间字段只在冷静期内返回。"""
    cooling = in_cooling_off(
        deactivated_at,
        cooling_off_expires_at,
        deactivation_revoked_at,
        now,
    )
    expired = deactivation_expired(
        deactivated_at,
        cooling_off_expires_at,
        deactivation_revoked_at,
        status,
        now,
    )
    shown_deactivated = None
    shown_expires = None
    if cooling:
        shown_deactivated = deactivated_at
        shown_expires = cooling_off_expires_at
    revoked = False
    if deactivation_revoked_at is not None:
        revoked = True
    if expired:
        revoked = True
    return DeactivationStatusView(
        pending=cooling,
        deactivated_at=shown_deactivated,
        cooling_off_expires_at=shown_expires,
        revoked=revoked,
    )


def active_workitem_block_message(active_workitems: int) -> str:
    """未完结工单拦截文案，数量来自实时计数。"""
    return f"存在 {active_workitems} 个未完结的工单，请先处理后再申请注销"


def sole_admin_statement(user_id: int) -> Select[Any]:
    """是否存在某个工作空间只剩这一个有效管理员。"""
    member = aliased(OrgMember)
    others = (
        select(OrgMember.id)
        .where(
            OrgMember.tenant_id == member.tenant_id,
            OrgMember.user_id != user_id,
            OrgMember.access_level == "ADMIN",
            OrgMember.is_deleted == 0,
            OrgMember.status == 0,
        )
        .exists()
    )
    return (
        select(func.count())
        .select_from(member)
        .where(
            member.user_id == user_id,
            member.access_level == "ADMIN",
            member.is_deleted == 0,
            member.status == 0,
            ~others,
        )
    )


async def initiate_deactivation(
    session: AsyncSession,
    user_id: int,
    confirm_username: str | None,
) -> None:
    """进入 7 天冷静期。已禁用、已在冷静期、用户名不符、有工单或唯一管理员时拒绝。"""
    user = await find_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    now = now_local()
    if account_is_disabled(user.status):
        raise BizError(ErrorCode.DEACTIVATION_ACCOUNT_DISABLED)
    if in_cooling_off(
        user.deactivated_at,
        user.cooling_off_expires_at,
        user.deactivation_revoked_at,
        now,
    ):
        raise BizError(ErrorCode.DEACTIVATION_ALREADY_PENDING)
    if not confirm_username_matches(confirm_username, user.username):
        raise BizError(ErrorCode.DEACTIVATION_CONFIRM_MISMATCH)
    active_workitems = await count_active_by_assignee(session, "HUMAN", user_id)
    if active_workitems > 0:
        raise BizError(
            ErrorCode.DEACTIVATION_BLOCKED_BY_WORKITEMS,
            active_workitem_block_message(active_workitems),
        )
    if await is_sole_admin_of_any_workspace(session, user_id):
        raise BizError(ErrorCode.DEACTIVATION_BLOCKED_BY_SOLE_ADMIN)
    await session.execute(
        update(User)
        .where(User.id == user_id, User.is_deleted == 0)
        .values(
            deactivated_at=now,
            cooling_off_expires_at=cooling_off_deadline(now),
            deactivation_revoked_at=None,
            gmt_modified=now,
        )
    )
    await session.commit()
    logger.info("Deactivation initiated for user %s", user_id)


async def revoke_deactivation(session: AsyncSession, user_id: int) -> None:
    """撤销仍在冷静期内的注销申请。"""
    user = await find_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    now = now_local()
    if not in_cooling_off(
        user.deactivated_at,
        user.cooling_off_expires_at,
        user.deactivation_revoked_at,
        now,
    ):
        raise BizError(ErrorCode.DEACTIVATION_NOT_PENDING)
    await session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.is_deleted == 0,
            User.deactivated_at.is_not(None),
            User.deactivation_revoked_at.is_(None),
        )
        .values(deactivation_revoked_at=now, gmt_modified=now)
    )
    await session.commit()
    logger.info("Deactivation revoked for user %s", user_id)


async def get_deactivation_status(
    session: AsyncSession,
    user_id: int,
) -> DeactivationStatusView:
    """查询注销状态。用户不存在时返回空状态，不抛错。"""
    user = await find_user_by_id(session, user_id)
    if user is None:
        return DeactivationStatusView()
    return deactivation_view(
        user.deactivated_at,
        user.cooling_off_expires_at,
        user.deactivation_revoked_at,
        user.status,
        now_local(),
    )


async def process_expired_deactivations(session: AsyncSession) -> int:
    """匿名化已经过冷静期的账号。单个用户失败不影响同批其余用户。"""
    users = list(
        await session.scalars(
            select(User)
            .where(
                User.is_deleted == 0,
                User.status == 0,
                User.deactivated_at.is_not(None),
                User.cooling_off_expires_at.is_not(None),
                User.cooling_off_expires_at <= func.now(),
                User.deactivation_revoked_at.is_(None),
            )
            .limit(_EXPIRED_LIMIT)
        )
    )
    processed = 0
    for user in users:
        user_id = user.id
        try:
            await anonymize_user(session, user_id)
            await session.commit()
            processed += 1
            logger.info("Account deactivated and anonymized: user %s", user_id)
        except Exception:
            await session.rollback()
            logger.exception("Failed to process expired deactivation for user %s", user_id)
    return processed


async def sweep_expired_deactivations(session: AsyncSession) -> int:
    """定时扫描入口。整批失败时记日志并返回 0，避免打断后续调度。"""
    try:
        processed = await process_expired_deactivations(session)
    except Exception:
        await session.rollback()
        logger.exception("Failed to process expired account deactivations")
        return 0
    if processed > 0:
        logger.info("Processed %s expired account deactivations", processed)
    return processed


async def is_account_deactivated(session: AsyncSession, user_id: int) -> bool:
    """冷静期已结束且账号已被禁用。"""
    user = await find_user_by_id(session, user_id)
    if user is None:
        return False
    return deactivation_expired(
        user.deactivated_at,
        user.cooling_off_expires_at,
        user.deactivation_revoked_at,
        user.status,
        now_local(),
    )


async def has_pending_deactivation(session: AsyncSession, user_id: int) -> bool:
    """数据库时钟下，该用户是否仍在冷静期。"""
    counted = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.id == user_id,
            User.is_deleted == 0,
            User.deactivated_at.is_not(None),
            User.cooling_off_expires_at.is_not(None),
            User.deactivation_revoked_at.is_(None),
            User.cooling_off_expires_at > func.now(),
        )
    )
    return cast(int, counted) > 0


async def count_active_by_assignee(
    session: AsyncSession,
    assignee_type: str,
    assignee_ref: int,
) -> int:
    """未进入 DONE/CANCELED 的指派工单数，跨工作空间累计。"""
    connection = await session.connection()
    result = await connection.execute(
        ACTIVE_ASSIGNEE_SQL,
        {"assignee_type": assignee_type, "assignee_ref": assignee_ref},
    )
    return int(result.scalar_one())


async def is_sole_admin_of_any_workspace(session: AsyncSession, user_id: int) -> bool:
    """任一工作空间里，有效管理员只剩这一个用户。"""
    counted = await session.scalar(sole_admin_statement(user_id))
    return cast(int, counted) > 0


async def anonymize_user(session: AsyncSession, user_id: int) -> None:
    """清空个人资料，口令改为不可登录的占位值，并禁用账号。"""
    await session.execute(
        update(User)
        .where(User.id == user_id, User.is_deleted == 0)
        .values(
            nickname=None,
            avatar_url=None,
            phone=None,
            email=None,
            password_hash="DEACTIVATED",
            status=1,
            gmt_modified=now_local(),
        )
    )
