"""解析小队模板 ``content_json``。字段取舍与 Fastjson ``getString`` / ``containsKey`` 一致。"""

import json
from typing import Any

from autowonder.templates.schemas import (
    TemplateAgentDetail,
    TemplateSdlcDetail,
    TemplateSquadInfo,
    TemplateStepSummary,
)


def split_tags(tags: str | None) -> list[str]:
    """逗号拆分标签。空列返回空列表，不裁剪每一段。"""
    if tags is None:
        return []
    return tags.split(",")


def is_system_template(tenant_id: int | None) -> bool:
    """系统模板的 tenant_id 为空。"""
    return tenant_id is None


def step_required(step: dict[str, Any]) -> int:
    """未声明 required 时按必需步骤写入 1。"""
    if "required" not in step:
        return 1
    if step["required"] is True:
        return 1
    return 0


def parse_content(raw: str) -> dict[str, Any]:
    """把模板正文解析成对象。"""
    loaded: Any = json.loads(raw)
    return loaded


def squad_info(content: dict[str, Any]) -> TemplateSquadInfo:
    """模板正文中的小队名称和描述。"""
    squad = content["squad"]
    return TemplateSquadInfo(name=squad.get("name"), description=squad.get("description"))


def agent_details(content: dict[str, Any]) -> list[TemplateAgentDetail]:
    """模板正文中的数字员工和流程步骤。"""
    agents: list[TemplateAgentDetail] = []
    for agent in content["agents"]:
        sdlc = agent["sdlc"]
        steps = [
            TemplateStepSummary(
                order=step.get("order"),
                name=step.get("name"),
                kind=step.get("kind"),
            )
            for step in sdlc["steps"]
        ]
        agents.append(
            TemplateAgentDetail(
                name=agent.get("name"),
                role_code=agent.get("roleCode"),
                role_name=agent.get("roleName"),
                responsibilities=agent.get("responsibilities"),
                sdlc=TemplateSdlcDetail(
                    name=sdlc.get("name"),
                    description=sdlc.get("description"),
                    steps=steps,
                ),
            )
        )
    return agents
