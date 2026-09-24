"""小队模板接口的响应。JSON 字段名与 Java VO 一致。"""

from autowonder.core.schema import ApiModel


class SquadTemplateView(ApiModel):
    """模板列表项。``system`` 表示租户为空的内置模板。"""

    id: int | None = None
    name: str | None = None
    description: str | None = None
    squad_size: int | None = None
    icon: str | None = None
    tags: list[str] = []
    system: bool = False


class TemplateSquadInfo(ApiModel):
    """模板内容里的小队名称。"""

    name: str | None = None
    description: str | None = None


class TemplateStepSummary(ApiModel):
    """模板流程步骤。"""

    order: int | None = None
    name: str | None = None
    kind: str | None = None


class TemplateSdlcDetail(ApiModel):
    """模板里的 SDLC。"""

    name: str | None = None
    description: str | None = None
    steps: list[TemplateStepSummary] = []


class TemplateAgentDetail(ApiModel):
    """模板里的数字员工。"""

    name: str | None = None
    role_code: str | None = None
    role_name: str | None = None
    responsibilities: str | None = None
    sdlc: TemplateSdlcDetail | None = None


class SquadTemplateDetailView(SquadTemplateView):
    """模板详情，带小队和数字员工配置。"""

    squad: TemplateSquadInfo | None = None
    agents: list[TemplateAgentDetail] = []


class AppliedAgent(ApiModel):
    """应用模板后创建出的数字员工。"""

    agent_id: int | None = None
    role_name: str | None = None
    role_code: str | None = None


class ApplyTemplateResult(ApiModel):
    """应用模板的结果。"""

    squad_id: int | None = None
    agents: list[AppliedAgent] = []
