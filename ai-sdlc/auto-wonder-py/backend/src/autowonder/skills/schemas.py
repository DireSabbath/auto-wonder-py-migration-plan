"""技能接口的请求和响应。"""

from datetime import datetime

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel

_MAX_LONG = 9223372036854775807


class CreateSkillRequest(ApiModel):
    """手工创建技能。插件和 Hook 不能走这个入口。"""

    type: str | None = None
    name: str | None = None
    install_spec: str | None = None
    description: str | None = None


class UpdateSkillRequest(ApiModel):
    """更新技能。字段为 null 时保留原值。"""

    name: str | None = None
    type: str | None = None
    install_spec: str | None = None
    description: str | None = None


class SkillView(ApiModel):
    """技能详情。installSpec 是展示用文本，私密项已掩码。"""

    id: int | None = None
    type: str | None = None
    name: str | None = None
    install_spec: str | None = None
    description: str | None = None
    source_type: str | None = None
    package_oss_ref: str | None = None
    package_file_name: str | None = None
    package_size: int | None = None
    package_md5: str | None = None
    version: int | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None
    modifier_id: int | None = None
    modifier_name: str | None = None
    category_id: int | None = None
    category_path: str | None = None


def category_id_from_json(body: object) -> int | None:
    """categoryId 必须出现。JSON null 表示取消打标。"""
    if not isinstance(body, dict) or "categoryId" not in body:
        raise BizError(ErrorCode.PARAM_INVALID, "缺少 categoryId 参数")
    node = body["categoryId"]
    if node is None:
        return None
    return _positive_long(node, "categoryId 必须是正整数或 null")


def skill_ids_from_json(body: object) -> list[int]:
    """skillIds 必须是正整数数组。"""
    if not isinstance(body, dict) or not isinstance(body.get("skillIds"), list):
        raise BizError(ErrorCode.PARAM_INVALID, "缺少 skillIds 参数")
    skill_ids: list[int] = []
    for node in body["skillIds"]:
        skill_ids.append(_positive_long(node, "skillIds 必须是数字数组"))
    return skill_ids


def _positive_long(node: object, message: str) -> int:
    if node is None:
        raise BizError(ErrorCode.PARAM_INVALID, message)
    if isinstance(node, bool) or not isinstance(node, int) or node <= 0 or node > _MAX_LONG:
        raise BizError(ErrorCode.PARAM_INVALID, message)
    return node
