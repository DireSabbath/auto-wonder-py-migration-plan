"""把 Aone 工单同步进本地工单和外部链接。"""

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import now_local
from autowonder.db.rows import rowcount
from autowonder.integrations.aone_api import (
    AoneClient,
    AoneConfig,
    ExternalWorkitemDetail,
    get_workitem,
    search_by_ids,
    search_project,
)
from autowonder.integrations.aone_codec import AoneDisabledError, AoneOpenApiError, require_enabled
from autowonder.integrations.aone_schemas import AoneSyncResult
from autowonder.integrations.aone_status import ensure_status
from autowonder.integrations.models import ExternalProjectBinding, ExternalWorkitemLink
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.workitems.models import Workitem, WorkitemEvent

logger = logging.getLogger(__name__)

_TITLE_MAX = 256
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
    return await _sync_details(session, binding, details, user_id, False)


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
    details: list[ExternalWorkitemDetail] = []
    seen: set[str] = set()
    for item in page.items:
        if not isinstance(item, ExternalWorkitemDetail) or not item.external_id:
            continue
        if item.external_id in seen:
            continue
        seen.add(item.external_id)
        details.append(item)
    return await _sync_details(session, binding, details, user_id, False)


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
    return await _sync_details(session, binding, details, user_id, True)


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


async def _sync_details(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    details: list[ExternalWorkitemDetail],
    user_id: int,
    _include_comments: bool,
) -> AoneSyncResult:
    result = AoneSyncResult()
    for detail in details:
        upsert = await _upsert(session, binding, detail, user_id)
        if upsert.created:
            result.imported += 1
        if upsert.updated:
            result.updated += 1
        result.workitem_ids.append(upsert.workitem_id)
    binding.last_success_at = now_local()
    binding.last_error = None
    await session.flush()
    return result


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
