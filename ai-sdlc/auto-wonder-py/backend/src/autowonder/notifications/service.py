"""站内通知、偏好和投递。删除必须命中当前用户自己的通知。"""

import logging
from collections.abc import Awaitable
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import delete

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.redis import redis_client
from autowonder.db.rows import rowcount
from autowonder.im.models import PlatformImChannelConfig, UserImIdentity
from autowonder.im.providers import require_selected, selected_provider
from autowonder.notifications.models import Notification, NotifyPref
from autowonder.notifications.schemas import (
    NotificationPage,
    NotificationView,
    NotifyPrefView,
    PrefItem,
    UpdatePrefRequest,
)
from autowonder.settings.service import get_decrypted_value

logger = logging.getLogger(__name__)

_LIST_CAP = 100
_SKIP_IM = {
    "WORKITEM_ASSIGNED",
    "COMMENT_MENTION",
    "WORKSPACE_ACCESS_REQUEST",
    "WORKSPACE_ACCESS_REVIEWED",
}
MISSING_NOTIFICATION = "通知不存在"


def page_window(page: int, size: int) -> tuple[int, int]:
    """页码小于 1 时从第 1 页起；每页小于 1 时用 1，并且不超过 100。"""
    normalized_page = page
    if page < 1:
        normalized_page = 1
    normalized_size = size
    if size < 1:
        normalized_size = 1
    if normalized_size > _LIST_CAP:
        normalized_size = _LIST_CAP
    return (normalized_page - 1) * normalized_size, normalized_size


def display_channels(
    in_app: int | None,
    dingtalk: int | None,
    feishu: int | None,
    selected: str,
) -> tuple[bool, bool, bool]:
    """站内看自己的开关。钉钉和飞书还要平台当前选了该渠道。"""
    return (
        in_app == 1,
        selected == "DINGTALK" and dingtalk == 1,
        selected == "FEISHU" and feishu == 1,
    )


def should_deliver(channel_name: str, pref: NotifyPref | None) -> bool:
    """没有偏好时全部投递。有偏好时站内默认开，IM 必须显式打开。"""
    if pref is None:
        return True
    if channel_name == "inApp":
        return pref.in_app is None or pref.in_app == 1
    if channel_name == "feishu":
        return pref.feishu is not None and pref.feishu == 1
    if channel_name == "dingtalk":
        return pref.dingtalk is not None and pref.dingtalk == 1
    return True


def java_parse_boolean(text: str | None) -> bool:
    """对齐 Boolean.parseBoolean。JSON 字符串外层引号先剥掉。"""
    if text is None:
        return False
    value = text.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.lower() == "true"


def prefs_unchanged(items: list[PrefItem] | None) -> bool:
    """空请求不改偏好。"""
    if items is None:
        return True
    return len(items) == 0


async def list_notifications(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    status: str | None,
    page: int,
    size: int,
) -> NotificationPage:
    """按创建时间倒序列出当前用户的通知。"""
    offset, limit = page_window(page, size)
    statement = select(Notification).where(
        Notification.tenant_id == tenant_id,
        Notification.recipient_id == user_id,
    )
    counted = (
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.tenant_id == tenant_id,
            Notification.recipient_id == user_id,
        )
    )
    if status is not None:
        statement = statement.where(Notification.status == status)
        counted = counted.where(Notification.status == status)
    rows = await session.scalars(
        statement.order_by(Notification.gmt_create.desc()).offset(offset).limit(limit)
    )
    total = await session.scalar(counted)
    return NotificationPage(
        items=[_view(row) for row in rows],
        total=cast(int, total),
    )


async def unread_count(session: AsyncSession, tenant_id: int, user_id: int) -> int:
    """未读数量。"""
    counted = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.tenant_id == tenant_id,
            Notification.recipient_id == user_id,
            Notification.status == "UNREAD",
        )
    )
    return cast(int, counted)


async def mark_read(
    session: AsyncSession,
    notification_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """把一条未读改成已读。已经读过或不是本人的通知保持原样。"""
    await session.execute(
        update(Notification)
        .where(
            Notification.id == notification_id,
            Notification.tenant_id == tenant_id,
            Notification.recipient_id == user_id,
            Notification.status == "UNREAD",
        )
        .values(status="READ")
    )
    await session.commit()


async def mark_all_read(session: AsyncSession, tenant_id: int, user_id: int) -> None:
    """把当前用户的未读全部标成已读。"""
    await session.execute(
        update(Notification)
        .where(
            Notification.tenant_id == tenant_id,
            Notification.recipient_id == user_id,
            Notification.status == "UNREAD",
        )
        .values(status="READ")
    )
    await session.commit()


async def delete_notification(
    session: AsyncSession,
    notification_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """物理删除。0 行表示通知不存在或不属于当前用户。"""
    deleted = rowcount(
        await session.execute(
            delete(Notification).where(
                Notification.id == notification_id,
                Notification.tenant_id == tenant_id,
                Notification.recipient_id == user_id,
            )
        )
    )
    if deleted == 0:
        raise BizError(ErrorCode.NOT_FOUND, MISSING_NOTIFICATION)
    await session.commit()


async def list_prefs(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
) -> list[NotifyPrefView]:
    """列出偏好。未选中的 IM 渠道在响应里是关。"""
    selected = await selected_provider(session)
    rows = await session.scalars(
        select(NotifyPref).where(
            NotifyPref.tenant_id == tenant_id,
            NotifyPref.user_id == user_id,
        )
    )
    views: list[NotifyPrefView] = []
    for row in rows:
        in_app, dingtalk, feishu = display_channels(
            row.in_app,
            row.dingtalk,
            row.feishu,
            selected,
        )
        views.append(
            NotifyPrefView(type=row.type, in_app=in_app, dingtalk=dingtalk, feishu=feishu)
        )
    return views


async def update_prefs(
    session: AsyncSession,
    request: UpdatePrefRequest,
    tenant_id: int,
    user_id: int,
) -> None:
    """先核对渠道，再插入或覆盖偏好。空列表直接返回。"""
    if prefs_unchanged(request.items):
        return
    items = cast(list[PrefItem], request.items)
    for item in items:
        if item.dingtalk:
            await require_selected(session, "DINGTALK")
        if item.feishu:
            await require_selected(session, "FEISHU")
    for item in items:
        existing = await _find_pref(session, tenant_id, user_id, item.type)
        in_app = _flag(item.in_app)
        dingtalk = _flag(item.dingtalk)
        feishu = _flag(item.feishu)
        if existing is None:
            session.add(
                NotifyPref(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    type=cast(str, item.type),
                    in_app=in_app,
                    dingtalk=dingtalk,
                    feishu=feishu,
                )
            )
        else:
            await session.execute(
                update(NotifyPref)
                .where(NotifyPref.id == existing.id)
                .values(in_app=in_app, dingtalk=dingtalk, feishu=feishu)
            )
    await session.commit()


async def publish(
    session: AsyncSession,
    tenant_id: int,
    event_type: str | None,
    title: str | None,
    content: str | None,
    link: str | None,
    ref_type: str | None,
    ref_id: int | None,
    recipient_ids: list[int] | None,
) -> None:
    """给每个接收人写一条未读通知，再按偏好投递渠道。"""
    if recipient_ids is None or len(recipient_ids) == 0:
        return
    for recipient_id in recipient_ids:
        row = Notification(
            tenant_id=tenant_id,
            recipient_id=recipient_id,
            type=cast(str, event_type),
            title=cast(str, title),
            content=content,
            link=link,
            ref_type=ref_type,
            ref_id=ref_id,
            status="UNREAD",
        )
        session.add(row)
        await session.flush()
        pref = await _find_pref(session, tenant_id, recipient_id, event_type)
        results: dict[str, str] = {}
        await _deliver(session, row, pref, "inApp", results)
        provider = await selected_provider(session)
        await _deliver(session, row, pref, provider.lower(), results)
        await session.execute(
            update(Notification)
            .where(Notification.id == row.id, Notification.tenant_id == tenant_id)
            .values(channels_json=results)
        )
    await session.commit()


async def _deliver(
    session: AsyncSession,
    row: Notification,
    pref: NotifyPref | None,
    channel_name: str,
    results: dict[str, str],
) -> None:
    if not should_deliver(channel_name, pref):
        return
    try:
        delivered = await _send(session, row, channel_name)
        if delivered:
            results[channel_name] = "ok"
        else:
            results[channel_name] = "failed"
    except Exception as error:
        logger.warning(
            "channel %s delivery failed for notification %s",
            channel_name,
            row.id,
            exc_info=True,
        )
        detail = "null"
        if error.args and error.args[0] is not None:
            detail = str(error.args[0])
        results[channel_name] = "error:" + detail


async def _send(session: AsyncSession, row: Notification, channel_name: str) -> bool:
    if channel_name == "inApp":
        await cast(
            Awaitable[int],
            redis_client().lpush("notify:" + str(row.recipient_id), str(row.id)),
        )
        return True
    return await deliver_platform_im(session, row)


async def deliver_platform_im(session: AsyncSession, row: Notification) -> bool:
    """已有独立 IM 队列的事件直接算成功。其余要等渠道就绪和身份。

    真正的钉钉、飞书 HTTP 发送还没接入，就绪后的发送目前记为失败。
    """
    event_type = row.type
    if event_type is None:
        event_type = ""
    if event_type in _SKIP_IM:
        return True
    provider = await selected_provider(session)
    if not await _im_ready(session, provider):
        return False
    enabled = await get_decrypted_value(
        session,
        "NOTIFY",
        provider.lower() + "_enabled",
        row.tenant_id,
    )
    if not java_parse_boolean(enabled):
        return False
    identity = await session.scalar(
        select(UserImIdentity)
        .where(
            UserImIdentity.user_id == row.recipient_id,
            UserImIdentity.provider == provider,
            UserImIdentity.is_deleted == 0,
        )
        .limit(1)
    )
    if identity is None or identity.external_user_id.strip() == "":
        return False
    logger.warning("platform IM send is not wired provider=%s notification=%s", provider, row.id)
    return False


async def _im_ready(session: AsyncSession, provider: str) -> bool:
    selected = await selected_provider(session)
    if selected != provider:
        return False
    config = await session.scalar(
        select(PlatformImChannelConfig)
        .where(
            PlatformImChannelConfig.provider == provider,
            PlatformImChannelConfig.is_deleted == 0,
        )
        .limit(1)
    )
    if config is None or config.enabled != 1:
        return False
    return _config_complete(config)


def _config_complete(config: PlatformImChannelConfig) -> bool:
    if not _has_text(config.app_key) or not _has_text(config.credential_ref):
        return False
    if config.provider == "FEISHU":
        return True
    return _has_text(config.robot_code)


def _has_text(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip() != ""


def _flag(enabled: bool) -> int:
    if enabled:
        return 1
    return 0


def _view(row: Notification) -> NotificationView:
    return NotificationView(
        id=row.id,
        type=row.type,
        title=row.title,
        content=row.content,
        link=row.link,
        ref_type=row.ref_type,
        ref_id=row.ref_id,
        status=row.status,
        gmt_create=row.gmt_create,
    )


async def _find_pref(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    event_type: str | None,
) -> NotifyPref | None:
    return await session.scalar(
        select(NotifyPref)
        .where(
            NotifyPref.tenant_id == tenant_id,
            NotifyPref.user_id == user_id,
            NotifyPref.type == event_type,
        )
        .limit(1)
    )
