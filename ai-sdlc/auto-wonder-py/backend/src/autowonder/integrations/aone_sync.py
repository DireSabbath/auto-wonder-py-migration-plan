"""把 Aone 工单同步进本地工单和外部链接。"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import SHANGHAI, now_local
from autowonder.db.rows import rowcount
from autowonder.integrations.aone_api import (
    AoneClient,
    AoneConfig,
    ExternalComment,
    ExternalWorkitemDetail,
    get_workitem,
    list_comments,
    search_by_ids,
    search_project,
)
from autowonder.integrations.aone_codec import AoneDisabledError, AoneOpenApiError, require_enabled
from autowonder.integrations.aone_schemas import AoneSyncResult
from autowonder.integrations.aone_status import ensure_status
from autowonder.integrations.models import (
    ExternalCommentLink,
    ExternalPrincipal,
    ExternalProjectBinding,
    ExternalWorkitemLink,
)
from autowonder.notifications.models import Notification
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.workitems.models import Workitem, WorkitemComment, WorkitemEvent

logger = logging.getLogger(__name__)

_TITLE_MAX = 256
_COMMENT_BATCH = 20
_RECONCILE_MAX = 200
_OVERLAP = timedelta(hours=1)
_DELETED_COMMENT = "（该外部评论已在来源平台删除）"
PROVIDER = "AONE"


@dataclass
class _Upsert:
    workitem_id: int
    created: bool
    updated: bool


async def sync_issue_ids(
    session: AsyncSession,
    client: AoneClient,
    crypto: AesGcmSecretCrypto,
    binding: ExternalProjectBinding,
    issue_ids: list[str],
    user_id: int,
) -> AoneSyncResult:
    """按指定 id 同步。搜索没有的再单独拉详情。"""
    require_enabled()
    config = _config(crypto, binding)
    page = search_by_ids(client, config, binding.external_project_id, issue_ids)
    by_id = {
        item.external_id: item
        for item in page.items
        if isinstance(item, ExternalWorkitemDetail) and item.external_id
    }
    details: list[ExternalWorkitemDetail] = []
    seen: set[str] = set()
    for issue_id in issue_ids:
        if issue_id in seen:
            continue
        seen.add(issue_id)
        item = by_id.get(issue_id)
        detail = fetch_detail_or_none(client, config, issue_id) if item is None else item
        if detail is not None:
            details.append(detail)
    return await _sync_details(session, binding, details, user_id, False, client, config, [])


async def sync_workitems(
    session: AsyncSession,
    client: AoneClient,
    crypto: AesGcmSecretCrypto,
    binding: ExternalProjectBinding,
    user_id: int,
) -> AoneSyncResult:
    """扫描项目后按搜索结果导入，同一外部 id 只处理一次。"""
    require_enabled()
    config = _config(crypto, binding)
    page = search_project(client, config, binding.external_project_id)
    return await _sync_catalog(session, binding, page.items, user_id, client, config)


async def refresh_issue_ids(
    session: AsyncSession,
    client: AoneClient,
    crypto: AesGcmSecretCrypto,
    binding: ExternalProjectBinding,
    issue_ids: list[str],
    user_id: int,
) -> AoneSyncResult:
    """单工单刷新走详情接口，并带回评论计数入口。"""
    require_enabled()
    config = _config(crypto, binding)
    details: list[ExternalWorkitemDetail] = []
    for issue_id in issue_ids:
        detail = fetch_detail_or_none(client, config, issue_id)
        if detail is not None:
            details.append(detail)
    return await _sync_details(
        session,
        binding,
        details,
        user_id,
        True,
        client,
        config,
        issue_ids,
    )


async def sync_binding_increment(
    session: AsyncSession,
    client: AoneClient,
    crypto: AesGcmSecretCrypto,
    binding: ExternalProjectBinding,
    user_id: int,
) -> int:
    """按上次成功时间增量扫描。没有工单时仍记下成功，评论留给对账。"""
    require_enabled()
    config = _config(crypto, binding)
    logger.info(
        "Aone inbound poll start bindingId=%s tenantId=%s projectId=%s projectName=%s",
        binding.id,
        binding.tenant_id,
        binding.external_project_id,
        binding.external_project_name,
    )
    page = search_project(
        client,
        config,
        binding.external_project_id,
        incremental_from(binding.last_success_at),
    )
    issue_ids = _distinct_external_ids(page.items)
    if len(issue_ids) == 0:
        binding.last_success_at = now_local()
        binding.last_error = None
        await session.flush()
        logger.info(
            "Aone inbound poll success (no workitems) bindingId=%s projectId=%s",
            binding.id,
            binding.external_project_id,
        )
        return 0
    await _sync_catalog(session, binding, page.items, user_id, client, config)
    logger.info(
        "Aone inbound poll success bindingId=%s projectId=%s syncedIssueCount=%s",
        binding.id,
        binding.external_project_id,
        len(issue_ids),
    )
    return len(issue_ids)


async def reconcile_linked_workitems(
    session: AsyncSession,
    client: AoneClient,
    crypto: AesGcmSecretCrypto,
    binding: ExternalProjectBinding,
    user_id: int,
    batch_size: int,
) -> int:
    """按链接 id 分批重拉已关联工单，并导入评论。游标走到末尾后回到 0。"""
    require_enabled()
    after_id = reconcile_cursor(binding.reconcile_cursor)
    limit = batch_size
    if limit < 1:
        limit = 1
    if limit > _RECONCILE_MAX:
        limit = _RECONCILE_MAX
    links = list(
        await session.scalars(
            select(ExternalWorkitemLink)
            .where(
                ExternalWorkitemLink.binding_id == binding.id,
                ExternalWorkitemLink.id > after_id,
            )
            .order_by(ExternalWorkitemLink.id.asc())
            .limit(limit)
        )
    )
    if len(links) == 0:
        if after_id > 0:
            binding.reconcile_cursor = "0"
            await session.flush()
        return 0
    external_ids = _distinct_external_ids(links)
    if len(external_ids) > 0:
        config = _config(crypto, binding)
        page = search_by_ids(client, config, binding.external_project_id, external_ids)
        details = [
            item
            for item in page.items
            if isinstance(item, ExternalWorkitemDetail) and item.external_id
        ]
        await _sync_details(
            session,
            binding,
            details,
            user_id,
            True,
            client,
            config,
            external_ids,
        )
    binding.reconcile_cursor = str(links[-1].id)
    await session.flush()
    return len(links)


def incremental_from(last_success_at: datetime | None) -> datetime | None:
    """首次轮询扫全量；之后从上次成功往前重叠 1 小时。"""
    if last_success_at is None:
        return None
    return last_success_at - _OVERLAP


def reconcile_cursor(cursor: str | None) -> int:
    """空白或非数字游标从 0 开始，负数夹到 0。"""
    if cursor is None or cursor.strip() == "":
        return 0
    try:
        parsed = int(cursor)
    except ValueError:
        return 0
    if parsed < 0:
        return 0
    return parsed


def fetch_detail_or_none(
    client: AoneClient,
    config: AoneConfig,
    external_id: str,
) -> ExternalWorkitemDetail | None:
    """详情失败就跳过这一条，不让整批中断。"""
    try:
        return get_workitem(client, config, external_id)
    except (AoneOpenApiError, AoneDisabledError, ValueError) as error:
        logger.warning(
            "Aone detail lookup failed, skip workitem externalWorkitemId=%s error=%s",
            external_id,
            error,
        )
        return None


async def _sync_catalog(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    items: list[ExternalWorkitemDetail],
    user_id: int,
    client: AoneClient,
    config: AoneConfig,
) -> AoneSyncResult:
    details: list[ExternalWorkitemDetail] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, ExternalWorkitemDetail) or not item.external_id:
            continue
        if item.external_id in seen:
            continue
        seen.add(item.external_id)
        details.append(item)
    return await _sync_details(session, binding, details, user_id, False, client, config, [])


async def _sync_details(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    details: list[ExternalWorkitemDetail],
    user_id: int,
    include_comments: bool,
    client: AoneClient,
    config: AoneConfig,
    comment_ids: list[str],
) -> AoneSyncResult:
    result = AoneSyncResult()
    for detail in details:
        upsert = await _upsert(session, binding, detail, user_id)
        if upsert.created:
            result.imported += 1
        if upsert.updated:
            result.updated += 1
        result.workitem_ids.append(upsert.workitem_id)
    if include_comments:
        if len(comment_ids) > 0:
            await _import_comments(session, client, config, binding, comment_ids, result, user_id)
    binding.last_success_at = now_local()
    binding.last_error = None
    await session.flush()
    return result


def _distinct_external_ids(items: Sequence[object]) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for item in items:
        external_id = getattr(item, "external_id", None)
        if external_id is None:
            external_id = getattr(item, "external_workitem_id", None)
        if not isinstance(external_id, str) or external_id.strip() == "":
            continue
        if external_id in seen:
            continue
        seen.add(external_id)
        ids.append(external_id)
    return ids


async def _import_comments(
    session: AsyncSession,
    client: AoneClient,
    config: AoneConfig,
    binding: ExternalProjectBinding,
    ids: list[str],
    result: AoneSyncResult,
    user_id: int,
) -> None:
    start = 0
    while start < len(ids):
        batch = ids[start : start + _COMMENT_BATCH]
        await _import_comment_batch(session, client, config, binding, batch, result, user_id)
        start += _COMMENT_BATCH


async def _import_comment_batch(
    session: AsyncSession,
    client: AoneClient,
    config: AoneConfig,
    binding: ExternalProjectBinding,
    ids: list[str],
    result: AoneSyncResult,
    user_id: int,
) -> None:
    comments = _load_comments(client, config, binding, ids)
    for comment in comments:
        imported = await _import_comment(session, binding, comment, user_id)
        if imported:
            result.comments_imported += 1


def _load_comments(
    client: AoneClient,
    config: AoneConfig,
    binding: ExternalProjectBinding,
    ids: list[str],
) -> list[ExternalComment]:
    """整批失败时逐条再拉；单条失败则跳过，工单同步继续。"""
    try:
        return list_comments(client, config, ids)
    except AoneOpenApiError as error:
        if len(ids) <= 1:
            issue_id = None
            if len(ids) == 1:
                issue_id = ids[0]
            logger.warning(
                "Aone comment lookup failed, skip comment import bindingId=%s issueId=%s error=%s",
                binding.id,
                issue_id,
                error,
            )
            return []
        logger.warning(
            "Aone comment batch failed, retry each issue bindingId=%s issueCount=%s error=%s",
            binding.id,
            len(ids),
            error,
        )
    comments: list[ExternalComment] = []
    for issue_id in ids:
        try:
            comments.extend(list_comments(client, config, [issue_id]))
        except AoneOpenApiError as error:
            logger.warning(
                "Aone comment lookup failed, skip comment import bindingId=%s issueId=%s error=%s",
                binding.id,
                issue_id,
                error,
            )
    return comments


async def _import_comment(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    comment: ExternalComment,
    user_id: int,
) -> bool:
    if comment.external_id is None:
        return False
    existing = await _find_comment_link(
        session,
        binding,
        comment.external_workitem_id,
        comment.external_id,
    )
    if existing is not None:
        if existing.direction == "OUTBOUND":
            return False
        return await _update_external_comment(session, binding, existing, comment, user_id)
    link = await _find_link(session, binding, _comment_workitem_id(comment))
    if link is None:
        return False
    principal_id = await _author_principal_id(session, comment)
    author_ref = 0
    if principal_id is not None:
        author_ref = principal_id
    local = WorkitemComment(
        tenant_id=binding.tenant_id,
        workitem_id=link.workitem_id,
        author_type="EXTERNAL",
        author_ref=author_ref,
        content_md=comment.content_md,
    )
    if comment.created_at is not None:
        local.gmt_create = comment.created_at
    session.add(local)
    await session.flush()
    source_status = "ACTIVE"
    if comment.source_status is not None:
        source_status = comment.source_status
    session.add(
        ExternalCommentLink(
            tenant_id=binding.tenant_id,
            provider=PROVIDER,
            binding_id=binding.id,
            external_workitem_id=_comment_workitem_id(comment),
            external_comment_id=comment.external_id,
            workitem_comment_id=local.id,
            direction="INBOUND",
            source_updated_at=comment.updated_at,
            source_status=source_status,
        )
    )
    await session.flush()
    await _notify_external_reply(session, binding, link.workitem_id, comment)
    return True


async def _update_external_comment(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    existing: ExternalCommentLink,
    comment: ExternalComment,
    user_id: int,
) -> bool:
    incoming_status = "ACTIVE"
    if comment.source_status is not None:
        incoming_status = comment.source_status
    status_changed = existing.source_status != incoming_status
    newer = _comment_is_newer(existing.source_updated_at, comment.updated_at)
    local = await session.scalar(
        select(WorkitemComment).where(
            WorkitemComment.tenant_id == binding.tenant_id,
            WorkitemComment.id == existing.workitem_comment_id,
        )
    )
    resolved = await _author_principal_id(session, comment)
    author_changed = resolved is not None and local is not None and local.author_ref != resolved
    if not status_changed and not newer and not author_changed:
        return False
    author_principal_id = resolved
    if author_principal_id is None and local is not None:
        author_principal_id = local.author_ref
    content = comment.content_md
    if incoming_status == "DELETED":
        content = _DELETED_COMMENT
    await _write_external_comment(
        session,
        binding,
        existing.workitem_comment_id,
        author_principal_id,
        content,
    )
    existing.source_updated_at = comment.updated_at
    existing.source_status = incoming_status
    await session.flush()
    workitem_link = await _find_link(session, binding, _comment_workitem_id(comment))
    if workitem_link is not None:
        event_type = "EXTERNAL_COMMENT_EDIT"
        if incoming_status == "DELETED":
            event_type = "EXTERNAL_COMMENT_DELETE"
        elif author_changed and not newer:
            event_type = "EXTERNAL_COMMENT_AUTHOR_CHANGE"
        to_val = None
        if comment.updated_at is not None:
            to_val = _epoch_text(comment.updated_at)
        await _event(
            session,
            binding.tenant_id,
            workitem_link.workitem_id,
            event_type,
            comment.external_id,
            to_val,
            user_id,
        )
    return True


async def _write_external_comment(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    comment_id: int,
    author_principal_id: int | None,
    content: str | None,
) -> None:
    if author_principal_id is not None and content is not None:
        await session.execute(
            update(WorkitemComment)
            .where(
                WorkitemComment.tenant_id == binding.tenant_id,
                WorkitemComment.id == comment_id,
                WorkitemComment.author_type == "EXTERNAL",
            )
            .values(author_ref=author_principal_id, content_md=content)
        )
        return
    if author_principal_id is not None:
        await session.execute(
            update(WorkitemComment)
            .where(
                WorkitemComment.tenant_id == binding.tenant_id,
                WorkitemComment.id == comment_id,
                WorkitemComment.author_type == "EXTERNAL",
            )
            .values(author_ref=author_principal_id)
        )
        return
    if content is not None:
        await session.execute(
            update(WorkitemComment)
            .where(
                WorkitemComment.tenant_id == binding.tenant_id,
                WorkitemComment.id == comment_id,
                WorkitemComment.author_type == "EXTERNAL",
            )
            .values(content_md=content)
        )


async def _author_principal_id(session: AsyncSession, comment: ExternalComment) -> int | None:
    subject_id = comment.author_staff_id
    if subject_id is None or subject_id.strip() == "":
        return None
    return await _upsert_principal(session, PROVIDER, subject_id, comment.author_name)


async def _upsert_principal(
    session: AsyncSession,
    provider: str,
    subject_id: str,
    display_name: str | None,
) -> int:
    existing = await _find_principal(session, provider, subject_id)
    if existing is not None:
        if display_name is not None:
            existing.display_name = display_name
        await session.flush()
        return existing.id
    row = ExternalPrincipal(provider=provider, subject_id=subject_id, display_name=display_name)
    session.add(row)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raced = await _find_principal(session, provider, subject_id)
        if raced is None:
            raise
        if display_name is not None:
            raced.display_name = display_name
        return raced.id
    return row.id


async def _find_principal(
    session: AsyncSession,
    provider: str,
    subject_id: str,
) -> ExternalPrincipal | None:
    return await session.scalar(
        select(ExternalPrincipal)
        .where(
            ExternalPrincipal.provider == provider,
            ExternalPrincipal.subject_id == subject_id,
        )
        .limit(1)
    )


async def _notify_external_reply(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    workitem_id: int,
    comment: ExternalComment,
) -> None:
    workitem = await session.get(Workitem, workitem_id)
    if workitem is None:
        return
    recipient_id = workitem.creator_id
    if workitem.assignee_type == "HUMAN":
        recipient_id = workitem.assignee_ref
    elif workitem.assign_operator_id is not None:
        recipient_id = workitem.assign_operator_id
    if recipient_id is None or recipient_id <= 0:
        return
    author = "外部用户"
    if comment.author_name is not None and comment.author_name.strip() != "":
        author = comment.author_name
    session.add(
        Notification(
            tenant_id=binding.tenant_id,
            recipient_id=recipient_id,
            type="EXTERNAL_COMMENT",
            title="外部工单有新回复",
            content=author + "：" + _comment_preview(comment.content_md),
            link="/workitems/" + str(workitem_id),
            ref_type="WORKITEM",
            ref_id=workitem_id,
            status="UNREAD",
        )
    )
    await session.flush()


def _comment_workitem_id(comment: ExternalComment) -> str:
    return _text_or_empty(comment.external_workitem_id)


def _text_or_empty(value: str | None) -> str:
    if value is None:
        return ""
    return value


def _comment_preview(content: str | None) -> str:
    if content is None or content.strip() == "":
        return "新增了一条回复"
    if len(content) <= 120:
        return content
    return content[:120] + "…"


def _comment_is_newer(existing: datetime | None, incoming: datetime | None) -> bool:
    if existing is None:
        return incoming is not None
    if incoming is None:
        return False
    return incoming > existing


def _epoch_text(value: datetime) -> str:
    aware = value
    if value.tzinfo is None:
        aware = value.replace(tzinfo=SHANGHAI)
    return str(int(aware.timestamp() * 1000))


async def _find_comment_link(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    external_workitem_id: str | None,
    external_comment_id: str,
) -> ExternalCommentLink | None:
    return await session.scalar(
        select(ExternalCommentLink)
        .where(
            ExternalCommentLink.tenant_id == binding.tenant_id,
            ExternalCommentLink.binding_id == binding.id,
            ExternalCommentLink.external_workitem_id == _text_or_empty(external_workitem_id),
            ExternalCommentLink.external_comment_id == external_comment_id,
        )
        .limit(1)
    )


async def _upsert(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    detail: ExternalWorkitemDetail,
    user_id: int,
) -> _Upsert:
    digest = hashlib.sha256((detail.raw_json or "").encode()).hexdigest()
    link = await _find_link(session, binding, detail.external_id or "")
    if link is None:
        return await _create(session, binding, detail, digest, user_id)
    return await _update_existing(session, binding, detail, link, digest, user_id)


async def _create(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    detail: ExternalWorkitemDetail,
    digest: str,
    user_id: int,
) -> _Upsert:
    node = await ensure_status(session, binding, detail, [], user_id)
    workitem = Workitem(
        tenant_id=binding.tenant_id,
        work_type=detail.work_type or "TASK",
        title=_truncate(detail.title) or "",
        content_md=detail.content_md,
        template_id=None if node is None else node.template_id,
        status_node_id=None if node is None else node.id,
        assignee_type="EXTERNAL",
        assignee_ref=0,
        priority=2 if detail.priority is None else detail.priority,
        creator_id=user_id,
        version=0,
        gmt_create=detail.created_at or now_local(),
    )
    session.add(workitem)
    await session.flush()
    await _event(
        session,
        binding.tenant_id,
        workitem.id,
        "AONE_IMPORT",
        None,
        detail.external_id,
        user_id,
    )
    link = ExternalWorkitemLink(
        tenant_id=binding.tenant_id,
        provider=PROVIDER,
        binding_id=binding.id,
        external_project_id=binding.external_project_id,
        external_workitem_id=detail.external_id or "",
        external_work_type=detail.work_type,
        workitem_id=workitem.id,
        external_url=detail.external_url,
        source_status_id=detail.status_id,
        source_status_name=detail.status_name,
        source_lifecycle=detail.source_lifecycle,
        remote_updated_at=detail.updated_at,
        remote_version_hash=digest,
        last_sync_direction="INBOUND",
        last_sync_at=now_local(),
        sync_status="HEALTHY",
    )
    session.add(link)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raced = await _find_link(session, binding, detail.external_id or "")
        if raced is None:
            raise
        logger.info(
            "Aone inbound link insert raced bindingId=%s externalWorkitemId=%s",
            binding.id,
            detail.external_id,
        )
        return await _update_existing(session, binding, detail, raced, digest, user_id)
    return _Upsert(workitem.id, True, False)


async def _update_existing(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    detail: ExternalWorkitemDetail,
    link: ExternalWorkitemLink,
    digest: str,
    user_id: int,
) -> _Upsert:
    if link.last_sync_direction == "OUTBOUND" and (
        link.remote_version_hash is None or link.remote_version_hash == digest
    ):
        return _Upsert(link.workitem_id, False, False)
    if (
        link.remote_updated_at is not None
        and detail.updated_at is not None
        and detail.updated_at < link.remote_updated_at
    ):
        return _Upsert(link.workitem_id, False, False)
    existing = await session.get(Workitem, link.workitem_id)
    updated = False
    if existing is not None and existing.assignee_type == "EXTERNAL":
        incoming = detail.status_name
        if (
            incoming is not None
            and incoming.strip() != ""
            and incoming != link.source_status_name
        ):
            node = await ensure_status(session, binding, detail, [], user_id)
            if node is not None and node.id != existing.status_node_id:
                changed = await session.execute(
                    update(Workitem)
                    .where(
                        Workitem.id == existing.id,
                        Workitem.tenant_id == binding.tenant_id,
                        Workitem.version == existing.version,
                    )
                    .values(
                        status_node_id=node.id,
                        version=Workitem.version + 1,
                        modifier_id=user_id,
                        gmt_modified=now_local(),
                    )
                )
                if rowcount(changed) == 0:
                    raise RuntimeError("external workitem status version conflict")
                await _event(
                    session,
                    binding.tenant_id,
                    existing.id,
                    "STATUS_CHANGE",
                    link.source_status_name,
                    node.name,
                    user_id,
                )
                existing.version = existing.version + 1
                existing.status_node_id = node.id
                updated = True
    if existing is not None:
        title = existing.title if detail.title is None else (_truncate(detail.title) or "")
        content = existing.content_md if detail.content_md is None else detail.content_md
        priority = existing.priority if detail.priority is None else detail.priority
        content_changed = (
            title != existing.title
            or content != existing.content_md
            or priority != existing.priority
        )
        if content_changed:
            changed = await session.execute(
                update(Workitem)
                .where(
                    Workitem.id == existing.id,
                    Workitem.tenant_id == binding.tenant_id,
                    Workitem.version == existing.version,
                )
                .values(
                    title=title,
                    content_md=content,
                    priority=priority,
                    version=Workitem.version + 1,
                    modifier_id=user_id,
                    gmt_modified=now_local(),
                )
            )
            if rowcount(changed) == 0:
                raise RuntimeError("external workitem content version conflict")
            await _event(
                session,
                binding.tenant_id,
                existing.id,
                "AONE_UPDATE",
                None,
                detail.external_id,
                user_id,
            )
            updated = True
    link.source_status_id = detail.status_id
    link.source_status_name = detail.status_name
    link.source_lifecycle = detail.source_lifecycle
    link.external_url = detail.external_url
    link.remote_updated_at = detail.updated_at
    link.remote_version_hash = digest
    link.last_sync_direction = "INBOUND"
    link.last_sync_at = now_local()
    link.sync_status = "HEALTHY"
    await session.flush()
    return _Upsert(link.workitem_id, False, updated)


async def _find_link(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    external_id: str,
) -> ExternalWorkitemLink | None:
    return await session.scalar(
        select(ExternalWorkitemLink)
        .where(
            ExternalWorkitemLink.tenant_id == binding.tenant_id,
            ExternalWorkitemLink.binding_id == binding.id,
            ExternalWorkitemLink.external_workitem_id == external_id,
        )
        .limit(1)
    )


async def _event(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    event_type: str,
    from_val: str | None,
    to_val: str | None,
    user_id: int,
) -> None:
    session.add(
        WorkitemEvent(
            tenant_id=tenant_id,
            workitem_id=workitem_id,
            event_type=event_type,
            from_val=from_val,
            to_val=to_val,
            actor_type="SYSTEM",
            actor_ref=user_id,
        )
    )
    await session.flush()


def _config(crypto: AesGcmSecretCrypto, binding: ExternalProjectBinding) -> AoneConfig:
    return AoneConfig(
        binding.base_url,
        binding.client_key,
        crypto.decrypt(binding.credential_ref),
        binding.region_id,
    )


def _truncate(title: str | None) -> str | None:
    if title is None or len(title) <= _TITLE_MAX:
        return title
    return title[:_TITLE_MAX]
