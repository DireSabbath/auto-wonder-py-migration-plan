"""工作空间接口的请求和响应。JSON 字段名与 Java VO 一致。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateWorkspaceRequest(ApiModel):
    """创建工作空间。"""

    name: str | None = None
    description: str | None = None
    background: str | None = None


class WorkspaceUpdateRequest(ApiModel):
    """修改名称、描述和背景，并带上乐观锁 version。"""

    name: str | None = None
    description: str | None = None
    background: str | None = None
    version: int | None = None


class RestoreWorkspaceRequest(ApiModel):
    """恢复时可选的新名称。"""

    new_name: str | None = None


class WorkspaceView(ApiModel):
    """工作空间卡片。未计算的权限字段保持 null。"""

    id: int | None = None
    name: str | None = None
    description: str | None = None
    background: str | None = None
    version: int | None = None
    access_level: str | None = None
    is_owner: bool | None = None
    can_manage: bool | None = None


class WorkspaceListItem(ApiModel):
    """全部工作空间列表中的一行。"""

    id: int | None = None
    name: str | None = None
    description: str | None = None
    membership_status: str | None = None
    access_level: str | None = None
    pending_request_id: int | None = None
    is_owner: bool | None = None
    can_manage: bool | None = None
    version: int | None = None


class SwitchWorkspaceResponse(ApiModel):
    """切换工作空间后的访问令牌。"""

    access_token: str
    access_level: str


class MemberView(ApiModel):
    """成员。``owner`` 是布尔字段，JSON 键为 owner。"""

    user_id: int | None = None
    username: str | None = None
    email: str | None = None
    nickname: str | None = None
    joined_at: datetime | None = None
    owner: bool = False
    access_level: str | None = None
    identity_tags: list[str] | None = None


class CurrentMembershipView(ApiModel):
    """当前用户在当前工作空间的成员身份。"""

    user_id: int | None = None
    username: str | None = None
    email: str | None = None
    nickname: str | None = None
    joined_at: datetime | None = None
    owner: bool = False
    access_level: str | None = None
    identity_tags: list[str] | None = None


class MemberCandidateView(ApiModel):
    """可添加的用户。"""

    user_id: int | None = None
    username: str | None = None
    email: str | None = None
    nickname: str | None = None


class RecycleBinItem(ApiModel):
    """回收站一行。"""

    id: int | None = None
    name: str | None = None
    description: str | None = None
    owner_id: int | None = None
    owner_name: str | None = None
    deleted_at: datetime | None = None
    deleted_by: int | None = None
    deleted_by_name: str | None = None
    restorable: bool | None = None


class AddMemberRequest(ApiModel):
    """添加成员。"""

    user_id: int | None = None


class UpdateMemberAccessRequest(ApiModel):
    """修改访问级别。"""

    access_level: str | None = None


class UpdateMemberIdentityTagsRequest(ApiModel):
    """修改身份标签。"""

    identity_tags: list[str] | None = None


class TransferOwnerRequest(ApiModel):
    """转让所有者。"""

    target_user_id: int | None = None


class SubmitAccessRequestBody(ApiModel):
    """申请加入。级别可空，空值由服务拒绝。"""

    requested_level: str | None = None


class RejectAccessRequestBody(ApiModel):
    """拒绝申请。"""

    reason: str | None = None


class AccessRequestView(ApiModel):
    """一条权限申请。"""

    id: int | None = None
    tenant_id: int | None = None
    requester_id: int | None = None
    requester_name: str | None = None
    requested_level: str | None = None
    status: str | None = None
    reviewer_id: int | None = None
    reviewer_name: str | None = None
    reject_reason: str | None = None
    gmt_create: datetime | None = None
