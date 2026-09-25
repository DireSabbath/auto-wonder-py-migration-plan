"""Aone 集成请求和响应。"""

from datetime import datetime

from autowonder.core.schema import ApiModel
from autowonder.integrations.aone_api import ExternalProject, ExternalProjectMember


class AoneBindingRequest(ApiModel):
    """创建、测试或搜索项目时提交的连接参数。"""

    base_url: str | None = None
    client_key: str | None = None
    access_secret: str | None = None
    region_id: str | None = None
    external_project_id: str | None = None
    external_project_name: str | None = None
    writeback_staff_id: str | None = None
    poll_interval_seconds: int | None = None
    enabled: bool | None = None


class AoneBindingView(ApiModel):
    """绑定展示。凭据只回掩码。"""

    id: int | None = None
    provider: str | None = None
    external_project_id: str | None = None
    external_project_name: str | None = None
    base_url: str | None = None
    client_key: str | None = None
    credential_masked: str | None = None
    region_id: str | None = None
    writeback_staff_id: str | None = None
    poll_interval_seconds: int | None = None
    enabled: bool | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    reused_existing_binding: bool | None = None
    status_template_synced: bool | None = None


class AoneTestConnectionResult(ApiModel):
    """连接测试。失败时仍返回 200，``success`` 在 data 里。"""

    success: bool = False
    message: str | None = None
    checks: list[str] = []


class AoneSyncNowRequest(ApiModel):
    """指定要立即同步的外部工单。空列表表示扫整个项目。"""

    issue_ids: list[str | None] | None = None


class AoneSyncResult(ApiModel):
    """一次入站同步的计数。"""

    imported: int = 0
    updated: int = 0
    comments_imported: int = 0
    workitem_ids: list[int] = []


class ExternalProjectView(ApiModel):
    """项目搜索结果。"""

    external_id: str | None = None
    name: str | None = None
    raw_json: str | None = None


class ExternalProjectMemberView(ApiModel):
    """项目成员。"""

    external_user_id: str | None = None
    staff_id: str | None = None
    display_name: str | None = None
    role_name: str | None = None
    raw_json: str | None = None


class ProjectPageView(ApiModel):
    """项目分页。"""

    items: list[ExternalProjectView]
    page: int
    page_size: int
    total_count: int


def project_view(project: ExternalProject) -> ExternalProjectView:
    """把查询结果收成响应。"""
    return ExternalProjectView(
        external_id=project.external_id,
        name=project.name,
        raw_json=project.raw_json,
    )


def member_view(member: ExternalProjectMember) -> ExternalProjectMemberView:
    """把成员收成响应。"""
    return ExternalProjectMemberView(
        external_user_id=member.external_user_id,
        staff_id=member.staff_id,
        display_name=member.display_name,
        role_name=member.role_name,
        raw_json=member.raw_json,
    )
