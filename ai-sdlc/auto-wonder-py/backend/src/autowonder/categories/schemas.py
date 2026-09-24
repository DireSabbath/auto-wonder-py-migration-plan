"""分类请求和响应。更新请求要区分省略字段和显式 null。"""

from datetime import datetime

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel

_PARENT_MESSAGE = "parentId 必须是正整数或 null"


class CategoryView(ApiModel):
    """分类节点。path 只用于展示，关联仍用 id。"""

    id: int | None = None
    parent_id: int | None = None
    name: str | None = None
    description: str | None = None
    path: str | None = None
    version: int | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None


class SkillCategoryResult(ApiModel):
    """批量打标的一项。失败不中断其余项。"""

    skill_id: int | None = None
    success: bool = False
    message: str | None = None


class CreateCategoryFields:
    """创建分类时已经校验过的字段。"""

    def __init__(self, name: str | None, parent_id: int | None, description: str | None) -> None:
        self.name = name
        self.parent_id = parent_id
        self.description = description


class UpdateCategoryFields:
    """出现过的键才覆盖。parentId 的 null 表示移到顶级。"""

    def __init__(self) -> None:
        self.name: str | None = None
        self.parent_id: int | None = None
        self.description: str | None = None
        self.name_present = False
        self.parent_id_present = False
        self.description_present = False


def create_fields_from_json(body: object) -> CreateCategoryFields:
    """创建时 parentId 只能是正整数或 null，其他类型按参数不合法拒绝。"""
    if not isinstance(body, dict):
        return CreateCategoryFields(None, None, None)
    parent_id = None
    if "parentId" in body:
        parent_id = _create_parent(body["parentId"])
    return CreateCategoryFields(
        _bound_text(body.get("name")) if "name" in body else None,
        parent_id,
        _bound_text(body.get("description")) if "description" in body else None,
    )


def update_fields_from_json(body: object) -> UpdateCategoryFields:
    """省略的键保持原值；显式 null 清空可空列。"""
    fields = UpdateCategoryFields()
    if not isinstance(body, dict):
        return fields
    if "name" in body:
        fields.name_present = True
        fields.name = _as_text(body["name"])
    if "parentId" in body:
        fields.parent_id_present = True
        fields.parent_id = _update_parent(body["parentId"])
    if "description" in body:
        fields.description_present = True
        fields.description = _as_text(body["description"])
    return fields


def _create_parent(node: object) -> int | None:
    if node is None:
        return None
    if isinstance(node, bool) or not isinstance(node, int) or node <= 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    return node


def _update_parent(node: object) -> int | None:
    if node is None:
        return None
    if isinstance(node, bool) or not isinstance(node, int) or node <= 0:
        raise BizError(ErrorCode.PARAM_INVALID, _PARENT_MESSAGE)
    return node


def _bound_text(node: object) -> str | None:
    """创建接口按 Jackson 把标量收成字符串，对象和数组不是合法文本。"""
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
    raise BizError(ErrorCode.PARAM_INVALID)


def _as_text(node: object) -> str | None:
    """更新接口用 Jackson asText：对象和数组变成空串。"""
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
