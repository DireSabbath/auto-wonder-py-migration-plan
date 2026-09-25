"""Aone 项目绑定、连接测试和立即同步。"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.integrations.aone_api import (
    AoneClient,
    AoneConfig,
    ExternalIssueType,
    get_project,
    list_enabled_issue_types,
    list_members,
    list_status_rules,
    search_project_first_page,
    search_projects,
)
from autowonder.integrations.aone_schemas import (
    AoneBindingRequest,
    AoneBindingView,
    AoneSyncResult,
    AoneTestConnectionResult,
    ProjectPageView,
    member_view,
    project_view,
)
from autowonder.integrations.aone_status import ensure_statuses
from autowonder.integrations.aone_sync import (
    PROVIDER,
    refresh_issue_ids,
    sync_issue_ids,
    sync_workitems,
)
from autowonder.integrations.models import ExternalProjectBinding
from autowonder.security.crypto import AesGcmSecretCrypto

logger = logging.getLogger(__name__)

_CLIENT = AoneClient()


def crypto() -> AesGcmSecretCrypto:
    """用部署主密钥加解密绑定凭据。"""
    return AesGcmSecretCrypto(get_settings().secret_master_key)


async def create_binding(
    session: AsyncSession,
    request: AoneBindingRequest,
    tenant_id: int,
    user_id: int,
) -> AoneBindingView:
    """新建绑定；同一项目已存在时复用并补状态模版。"""
    _validate(request)
    external_project_id = (request.external_project_id or "").strip()
    existing = await _find_project(session, tenant_id, external_project_id)
    if existing is not None:
        existing.writeback_staff_id = _default_if_blank(
            existing.writeback_staff_id,
            request.writeback_staff_id,
        )
        synced = await _bootstrap(session, existing, _config_from_binding(existing), user_id)
        view = _to_view(existing)
        view.reused_existing_binding = True
        view.status_template_synced = synced
        return view
    binding = ExternalProjectBinding(
        tenant_id=tenant_id,
        provider=PROVIDER,
        external_project_id=external_project_id,
        external_project_name=request.external_project_name,
        base_url=(request.base_url or "").strip(),
        client_key=_default_if_blank(request.client_key, "auto-wonder") or "auto-wonder",
        credential_ref=crypto().encrypt((request.access_secret or "").strip()),
        region_id=_default_if_blank(request.region_id, "1") or "1",
        writeback_staff_id=(request.writeback_staff_id or "").strip(),
        poll_interval_seconds=3
        if request.poll_interval_seconds is None
        else request.poll_interval_seconds,
        enabled=0 if request.enabled is False else 1,
        creator_id=user_id,
    )
    session.add(binding)
    await session.flush()
    synced = await _bootstrap(session, binding, _config_from_request(request), user_id)
    view = _to_view(binding)
    view.reused_existing_binding = False
    view.status_template_synced = synced
    return view


async def list_bindings(
    session: AsyncSession,
    tenant_id: int,
    page: int,
    size: int,
) -> list[AoneBindingView]:
    """按页列出当前工作空间的 Aone 绑定。"""
    current_page = max(page, 1)
    current_size = min(max(size, 1), 100)
    offset = (current_page - 1) * current_size
    rows = await session.scalars(
        select(ExternalProjectBinding)
        .where(
            ExternalProjectBinding.tenant_id == tenant_id,
            ExternalProjectBinding.provider == PROVIDER,
            ExternalProjectBinding.is_deleted == 0,
        )
        .order_by(ExternalProjectBinding.id.asc())
        .offset(offset)
        .limit(current_size)
    )
    return [_to_view(row) for row in rows.all()]


def test_connection(request: AoneBindingRequest) -> AoneTestConnectionResult:
    """依次检查项目、成员和工单搜索。任一步失败都记在结果里。"""
    _validate(request)
    config = _config_from_request(request)
    result = AoneTestConnectionResult()
    try:
        project = get_project(_CLIENT, config, request.external_project_id or "")
        result.checks.append("project:" + ("" if project.name is None else project.name))
        members = list_members(_CLIENT, config, request.external_project_id or "")
        result.checks.append("members:" + str(len(members)))
        search_project_first_page(_CLIENT, config, request.external_project_id or "")
        result.checks.append("workitem-search:ok")
        if request.writeback_staff_id is not None and request.writeback_staff_id.strip() != "":
            result.checks.append("writeback-staff:" + request.writeback_staff_id)
        result.success = True
        result.message = "Aone 连接测试成功"
    except Exception as error:
        result.success = False
        result.message = str(error)
    return result


def search_project_page(
    request: AoneBindingRequest,
    query: str,
    page: int,
    size: int,
) -> ProjectPageView:
    """用请求里的凭据搜索项目，不落库。"""
    page_result = search_projects(_CLIENT, _config_from_request(request), query, page, size)
    return ProjectPageView(
        items=[project_view(item) for item in page_result.items if hasattr(item, "external_id")],
        page=page_result.page,
        page_size=page_result.page_size,
        total_count=page_result.total_count,
    )


def project_members(request: AoneBindingRequest, project_id: str) -> list[object]:
    """列出指定项目的成员。"""
    members = list_members(_CLIENT, _config_from_request(request), project_id)
    return [member_view(item) for item in members]


async def sync_now(
    session: AsyncSession,
    binding_id: int,
    issue_ids: list[str | None] | None,
    tenant_id: int,
    user_id: int,
) -> AoneSyncResult:
    """立即同步。没有工单 id 时扫描整个项目。"""
    binding = await session.get(ExternalProjectBinding, binding_id)
    if binding is None or binding.tenant_id != tenant_id or binding.is_deleted == 1:
        raise BizError(ErrorCode.NOT_FOUND)
    ids = [item.strip() for item in (issue_ids or []) if item is not None and item.strip() != ""]
    if len(ids) == 0:
        return await sync_workitems(session, _CLIENT, crypto(), binding, user_id)
    return await sync_issue_ids(session, _CLIENT, crypto(), binding, ids, user_id)


async def sync_local(
    session: AsyncSession,
    workitem_id: int,
    tenant_id: int,
    user_id: int,
) -> AoneSyncResult:
    """按本地工单找到 Aone 链接后刷新那一条。"""
    from autowonder.integrations.models import ExternalWorkitemLink

    link = await session.scalar(
        select(ExternalWorkitemLink)
        .where(
            ExternalWorkitemLink.tenant_id == tenant_id,
            ExternalWorkitemLink.provider == PROVIDER,
            ExternalWorkitemLink.workitem_id == workitem_id,
        )
        .limit(1)
    )
    if link is None:
        raise BizError(ErrorCode.NOT_FOUND, "当前工单未关联 Aone 工单")
    binding = await session.get(ExternalProjectBinding, link.binding_id)
    if binding is None or binding.tenant_id != tenant_id:
        raise BizError(ErrorCode.NOT_FOUND, "Aone 托管配置不存在")
    return await refresh_issue_ids(
        session,
        _CLIENT,
        crypto(),
        binding,
        [link.external_workitem_id],
        user_id,
    )


async def _bootstrap(
    session: AsyncSession,
    binding: ExternalProjectBinding,
    config: AoneConfig,
    user_id: int,
) -> bool:
    issue_types = _enabled_issue_types(binding, config)
    logger.info(
        "Aone status template bootstrap started bindingId=%s projectId=%s issueTypeCount=%s",
        binding.id,
        binding.external_project_id,
        len(issue_types),
    )
    synced = False
    for issue_type in issue_types:
        work_type = _work_type(issue_type.stamp)
        issue_type_id = _int_or_none(issue_type.external_id)
        if work_type is None or issue_type_id is None:
            logger.warning(
                "Aone status template bootstrap skip unsupported issueType stamp=%s",
                issue_type.stamp,
            )
            continue
        statuses = list_status_rules(_CLIENT, config, binding.external_project_id, issue_type_id)
        if len(statuses) == 0:
            logger.warning(
                "Aone status rule list is empty bindingId=%s workType=%s",
                binding.id,
                work_type,
            )
        await ensure_statuses(
            session,
            binding,
            work_type,
            str(issue_type_id),
            statuses,
            user_id,
        )
        synced = synced or len(statuses) > 0
    return synced


def _enabled_issue_types(
    binding: ExternalProjectBinding, config: AoneConfig
) -> list[ExternalIssueType]:
    result: list[ExternalIssueType] = []
    for stamp in ("Req", "Bug", "Task"):
        try:
            loaded = list_enabled_issue_types(
                _CLIENT,
                config,
                binding.external_project_id,
                binding.writeback_staff_id,
                stamp,
            )
            logger.info(
                "Aone enabled issue types loaded bindingId=%s stamp=%s count=%s",
                binding.id,
                stamp,
                len(loaded),
            )
            result.extend(loaded)
        except RuntimeError as error:
            logger.warning(
                "Aone enabled issue types lookup failed bindingId=%s stamp=%s error=%s",
                binding.id,
                stamp,
                error,
            )
    return result


def _config_from_request(request: AoneBindingRequest) -> AoneConfig:
    return AoneConfig(
        (request.base_url or "").strip(),
        _default_if_blank(request.client_key, "auto-wonder") or "auto-wonder",
        request.access_secret or "",
        _default_if_blank(request.region_id, "1"),
    )


def _config_from_binding(binding: ExternalProjectBinding) -> AoneConfig:
    return AoneConfig(
        binding.base_url,
        binding.client_key,
        crypto().decrypt(binding.credential_ref),
        binding.region_id,
    )


def _to_view(binding: ExternalProjectBinding) -> AoneBindingView:
    return AoneBindingView(
        id=binding.id,
        provider=binding.provider,
        external_project_id=binding.external_project_id,
        external_project_name=binding.external_project_name,
        base_url=binding.base_url,
        client_key=binding.client_key,
        credential_masked=crypto().mask(binding.credential_ref),
        region_id=binding.region_id,
        writeback_staff_id=binding.writeback_staff_id,
        poll_interval_seconds=binding.poll_interval_seconds,
        enabled=binding.enabled == 1,
        last_success_at=binding.last_success_at,
        last_error=binding.last_error,
    )


def _validate(request: AoneBindingRequest) -> None:
    if (
        request.base_url is None
        or request.base_url.strip() == ""
        or request.access_secret is None
        or request.access_secret.strip() == ""
        or request.external_project_id is None
        or request.external_project_id.strip() == ""
        or request.writeback_staff_id is None
        or request.writeback_staff_id.strip() == ""
    ):
        raise BizError(ErrorCode.PARAM_INVALID)


async def _find_project(
    session: AsyncSession,
    tenant_id: int,
    external_project_id: str,
) -> ExternalProjectBinding | None:
    return await session.scalar(
        select(ExternalProjectBinding)
        .where(
            ExternalProjectBinding.tenant_id == tenant_id,
            ExternalProjectBinding.provider == PROVIDER,
            ExternalProjectBinding.external_project_id == external_project_id,
            ExternalProjectBinding.is_deleted == 0,
        )
        .limit(1)
    )


def _work_type(stamp: str | None) -> str | None:
    if stamp is None:
        return None
    lowered = stamp.lower()
    if lowered == "req":
        return "REQ"
    if lowered == "bug":
        return "BUG"
    if lowered == "task":
        return "TASK"
    return None


def _int_or_none(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _default_if_blank(value: str | None, default: str | None) -> str | None:
    if value is None or value.strip() == "":
        return default
    return value.strip()
