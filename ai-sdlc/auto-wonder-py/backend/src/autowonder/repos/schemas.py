"""代码仓库接口的请求和响应。更新请求要区分省略字段和显式 null。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateRepoRequest(ApiModel):
    """创建仓库。名称和地址必填。"""

    name: str | None = None
    url: str | None = None
    default_branch: str | None = None
    description: str | None = None


class ConnectionTestRequest(ApiModel):
    """测试读取权限。只使用地址和可选默认分支。"""

    name: str | None = None
    url: str | None = None
    default_branch: str | None = None


class ConnectionTestView(ApiModel):
    """连接测试结果。失败也走成功信封，由 success 字段表示。"""

    success: bool = False
    message: str | None = None


class UpdateRepoFields:
    """手工解析后的更新字段。出现过的键才覆盖，null 清空可空列。"""

    def __init__(self) -> None:
        self.name: str | None = None
        self.url: str | None = None
        self.default_branch: str | None = None
        self.description: str | None = None
        self.name_present = False
        self.url_present = False
        self.default_branch_present = False
        self.description_present = False


class UpdateConclusionRequest(ApiModel):
    """仓库结论。JSON 列以字符串传入，和 Java String 字段一致。"""

    purpose: str | None = None
    key_business: str | None = None
    upstreams: str | None = None
    downstreams: str | None = None
    summary_md: str | None = None


class CreateRelationRequest(ApiModel):
    """两条仓库之间的关系。"""

    from_repo_id: int | None = None
    to_repo_id: int | None = None
    relation_type: str | None = None
    description: str | None = None
    ai_session_id: int | None = None


class RepoView(ApiModel):
    """仓库卡片。"""

    id: int | None = None
    name: str | None = None
    url: str | None = None
    default_branch: str | None = None
    description: str | None = None
    scan_status: str | None = None
    version: int | None = None
    gmt_create: datetime | None = None


class RepoConclusionView(ApiModel):
    """仓库结论。JSON 列按字符串返回。"""

    id: int | None = None
    repo_id: int | None = None
    purpose: str | None = None
    key_business: str | None = None
    upstreams: str | None = None
    downstreams: str | None = None
    summary_md: str | None = None
    ai_session_id: int | None = None
    version: int | None = None
    gmt_create: datetime | None = None


class RepoRelationView(ApiModel):
    """仓库关系。"""

    id: int | None = None
    from_repo_id: int | None = None
    to_repo_id: int | None = None
    relation_type: str | None = None
    description: str | None = None
    ai_session_id: int | None = None
    gmt_create: datetime | None = None


def repo_update_from_json(body: object) -> UpdateRepoFields:
    """省略的键保持原值；显式 null 写入 null。"""
    fields = UpdateRepoFields()
    if not isinstance(body, dict):
        return fields
    if "name" in body:
        fields.name_present = True
        fields.name = _text_or_null(body["name"])
    if "url" in body:
        fields.url_present = True
        fields.url = _text_or_null(body["url"])
    if "defaultBranch" in body:
        fields.default_branch_present = True
        fields.default_branch = _text_or_null(body["defaultBranch"])
    if "description" in body:
        fields.description_present = True
        fields.description = _text_or_null(body["description"])
    return fields


def _text_or_null(node: object) -> str | None:
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, bool):
        if node:
            return "true"
        return "false"
    if isinstance(node, int | float):
        return str(node)
    return ""
