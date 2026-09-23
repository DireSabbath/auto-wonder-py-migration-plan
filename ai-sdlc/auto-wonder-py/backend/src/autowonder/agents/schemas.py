"""数字员工接口的请求和响应。JSON 字段名与 Java VO 一致。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateAgentRequest(ApiModel):
    """创建数字员工。"""

    name: str | None = None
    avatar_url: str | None = None
    role_name: str | None = None
    role_code: str | None = None
    business_background: str | None = None
    responsibilities: str | None = None


class UpdateAgentRequest(ApiModel):
    """更新名称、头像和角色文案。

    REST 不携带 providedFields，省略和 null 都按旧语义处理。
    MCP 之后会传入字段集合，显式 null 才清空。
    """

    name: str | None = None
    avatar_url: str | None = None
    role_code: str | None = None
    role_name: str | None = None
    business_background: str | None = None
    responsibilities: str | None = None


class UpdateConfigRequest(ApiModel):
    """编辑草稿配置。sdlcId 与 evolutionMode 在这里修改。"""

    role_name: str | None = None
    role_code: str | None = None
    business_background: str | None = None
    responsibilities: str | None = None
    sdlc_id: int | None = None
    evolution_mode: str | None = None


class RepoPermRequest(ApiModel):
    """给草稿版本增加或合并仓库权限。"""

    repo_id: int | None = None
    perm_level: str | None = None
    allowed_branch_patterns: list[str | None] | None = None


class SkillRequest(ApiModel):
    """挂载技能。"""

    skill_id: int | None = None


class MemoryRefRequest(ApiModel):
    """挂载记忆。source 省略时写成 DIRECT。"""

    memory_id: int | None = None
    source: str | None = None


class ReviewRequest(ApiModel):
    """审核意见。"""

    comment: str | None = None


class RollbackRequest(ApiModel):
    """回退到某个已通过版本号。"""

    version_no: int | None = None


class EnvironmentVariableRefView(ApiModel):
    """挂载的环境变量。值固定脱敏。"""

    id: int | None = None
    name: str | None = None
    description: str | None = None
    value: str | None = None


class RepoPermItem(ApiModel):
    """版本上的一条仓库权限。"""

    repo_id: int | None = None
    perm_level: str | None = None
    allowed_branch_patterns: list[str] | None = None


class SkillItem(ApiModel):
    """版本上的一条技能。"""

    skill_id: int | None = None


class MemoryRefItem(ApiModel):
    """版本上的一条记忆引用。"""

    memory_id: int | None = None
    source: str | None = None


class AgentVersionSummaryView(ApiModel):
    """版本列表中的摘要。"""

    id: int | None = None
    version_no: int | None = None
    status: str | None = None
    role_name: str | None = None
    gmt_create: datetime | None = None


class AgentVersionView(ApiModel):
    """版本详情。identityJson 按字符串返回。"""

    id: int | None = None
    agent_id: int | None = None
    version_no: int | None = None
    status: str | None = None
    role_name: str | None = None
    role_code: str | None = None
    business_background: str | None = None
    responsibilities: str | None = None
    sdlc_id: int | None = None
    identity_json: str | None = None
    evolution_mode: str | None = None
    reviewer_id: int | None = None
    review_comment: str | None = None
    reviewed_at: datetime | None = None
    version: int | None = None
    gmt_create: datetime | None = None
    repo_perms: list[RepoPermItem] | None = None
    skills: list[SkillItem] | None = None
    memory_refs: list[MemoryRefItem] | None = None
    environment_variables: list[EnvironmentVariableRefView] | None = None


class AgentView(ApiModel):
    """数字员工卡片。创建响应用原始字段，列表和详情再填展示版本。"""

    id: int | None = None
    name: str | None = None
    avatar_url: str | None = None
    status: str | None = None
    kind: str | None = None
    online_version_id: int | None = None
    editing_version_id: int | None = None
    latest_version_no: int | None = None
    version: int | None = None
    gmt_create: datetime | None = None
    role_name: str | None = None
    role_code: str | None = None
    business_background: str | None = None
    responsibilities: str | None = None
    sdlc_id: int | None = None
    evolution_mode: str | None = None
    has_draft: bool = False
    draft_version_no: int | None = None
    executor_online_count: int = 0
    executor_total_count: int = 0
    skill_count: int = 0
    memory_count: int = 0
    repo_perm_count: int = 0
    environment_variables: list[EnvironmentVariableRefView] | None = None
    squad_ids: list[int] | None = None
    squad_names: list[str] | None = None
