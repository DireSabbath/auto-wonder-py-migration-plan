"""状态模板、节点和流转的请求与响应。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateTemplateRequest(ApiModel):
    """创建状态模板。名称和工单类型必填。"""

    work_type: str | None = None
    name: str | None = None


class UpdateTemplateRequest(ApiModel):
    """更新模板。省略的字段保持原值；isDefault 只有 true 才改成默认。"""

    name: str | None = None
    is_default: bool | None = None


class CreateNodeRequest(ApiModel):
    """创建状态节点。编码、名称和分类必填。"""

    code: str | None = None
    name: str | None = None
    category: str | None = None
    sort: int | None = None


class UpdateNodeRequest(ApiModel):
    """更新节点。省略的字段保持原值。"""

    code: str | None = None
    name: str | None = None
    category: str | None = None
    sort: int | None = None


class CreateTransitionRequest(ApiModel):
    """创建一条流转。名称必填。"""

    from_node_id: int | None = None
    to_node_id: int | None = None
    name: str | None = None


class UpdateTransitionRequest(ApiModel):
    """更新流转。省略的字段保持原值。"""

    from_node_id: int | None = None
    to_node_id: int | None = None
    name: str | None = None


class TemplateView(ApiModel):
    """状态模板卡片。isDefault 与 Java Boolean 字段同名。"""

    id: int | None = None
    work_type: str | None = None
    name: str | None = None
    is_default: bool | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None


class NodeView(ApiModel):
    """状态节点。"""

    id: int | None = None
    template_id: int | None = None
    code: str | None = None
    name: str | None = None
    category: str | None = None
    sort: int | None = None
    gmt_create: datetime | None = None


class TransitionView(ApiModel):
    """状态流转。"""

    id: int | None = None
    template_id: int | None = None
    from_node_id: int | None = None
    to_node_id: int | None = None
    name: str | None = None
    gmt_create: datetime | None = None


class TemplateDetailView(TemplateView):
    """模板详情，带节点和流转。"""

    nodes: list[NodeView] = []
    transitions: list[TransitionView] = []
