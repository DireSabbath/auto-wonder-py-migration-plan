"""平台可安装技能目录。安装规格是紧凑 JSON。"""

import json

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel


class PlatformSkillView(ApiModel):
    """一条可安装的平台技能。"""

    id: str
    type: str
    name: str
    description: str
    install_spec: str


def _skill(
    skill_id: str,
    skill_type: str,
    name: str,
    description: str,
    tools: list[str],
) -> PlatformSkillView:
    # Fastjson 写出 Map.of 时的键序，与 Java ``PlatformSkillCatalog`` 的 installSpec 一致。
    spec = json.dumps(
        {
            "tools": tools,
            "id": skill_id,
            "instructions": description,
            "mcpServer": "autowonder",
            "kind": "codex-skill",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return PlatformSkillView(
        id=skill_id,
        type=skill_type,
        name=name,
        description=description,
        install_spec=spec,
    )


_SKILLS = (
    _skill(
        "autowonder-workitem-operator",
        "CODEX_SKILL",
        "AutoWonder Workitem Operator",
        "Create, inspect, update, assign, comment, pause, resume and transition "
        "AutoWonder workitems through MCP tools.",
        [
            "autowonder.create_workitem",
            "autowonder.list_workitems",
            "autowonder.get_workitem",
            "autowonder.add_workitem_comment",
            "autowonder.list_workitem_comments",
            "autowonder.update_workitem",
            "autowonder.delete_workitem",
            "autowonder.assign_workitem",
            "autowonder.transition_workitem",
            "autowonder.pause_workitem",
            "autowonder.resume_workitem",
            "autowonder.workitem_cli_upload_token",
            "autowonder.workitem_cli_download_token",
        ],
    ),
    _skill(
        "autowonder-sdlc-manager",
        "CODEX_SKILL",
        "AutoWonder SDLC Manager",
        "Manage AutoWonder SDLC flows, steps, enablement and workitem status templates.",
        [
            "autowonder.create_sdlc",
            "autowonder.list_sdlcs",
            "autowonder.get_sdlc",
            "autowonder.update_sdlc",
            "autowonder.delete_sdlc",
            "autowonder.add_sdlc_step",
            "autowonder.update_sdlc_step",
            "autowonder.delete_sdlc_step",
            "autowonder.reorder_sdlc_steps",
            "autowonder.enable_sdlc",
            "autowonder.disable_sdlc",
            "autowonder.list_status_templates",
            "autowonder.get_status_template",
        ],
    ),
    _skill(
        "autowonder-agent-manager",
        "CODEX_SKILL",
        "AutoWonder Digital Worker Manager",
        "Create, update, list and inspect AutoWonder digital workers from MCP clients.",
        [
            "autowonder.create_agent",
            "autowonder.update_agent",
            "autowonder.list_agents",
            "autowonder.get_agent",
        ],
    ),
    _skill(
        "autowonder-project-navigator",
        "CODEX_SKILL",
        "AutoWonder Project Navigator",
        "List AutoWonder projects available to the token owner before selecting an MCP workspace.",
        ["autowonder.list_projects"],
    ),
    _skill(
        "autowonder-skill-manager",
        "CODEX_SKILL",
        "AutoWonder Skill Manager",
        "Manage skills, MCP server records and plugin records, upload Skill packages, install "
        "reusable AutoWonder platform skills, and organize skills with project-level categories.",
        [
            "autowonder.list_platform_skills",
            "autowonder.install_platform_skill",
            "autowonder.create_skill",
            "autowonder.list_skills",
            "autowonder.get_skill",
            "autowonder.update_skill",
            "autowonder.delete_skill",
            "autowonder.inspect_skill_package",
            "autowonder.upload_skill_package",
            "autowonder.create_skill_from_package",
            "autowonder.update_skill_package",
            "autowonder.list_categories",
            "autowonder.get_category",
            "autowonder.create_category",
            "autowonder.update_category",
            "autowonder.delete_category",
            "autowonder.set_skill_category",
            "autowonder.batch_set_skill_category",
        ],
    ),
    _skill(
        "autowonder-scheduled-task-operator",
        "CODEX_SKILL",
        "AutoWonder Scheduled Task Operator",
        "Create, inspect, update, delete and transition 7x24 scheduled tasks and their runs "
        "through MCP tools.",
        [
            "autowonder.create_scheduled_task",
            "autowonder.list_scheduled_tasks",
            "autowonder.get_scheduled_task",
            "autowonder.update_scheduled_task",
            "autowonder.transition_scheduled_task",
            "autowonder.delete_scheduled_task",
            "autowonder.get_scheduled_task_run",
            "autowonder.list_scheduled_task_runs",
            "autowonder.add_scheduled_task_run_comment",
        ],
    ),
)


def list_platform_skills() -> list[PlatformSkillView]:
    """全部平台技能，顺序固定。"""
    return list(_SKILLS)


def get_platform_skill(skill_id: str) -> PlatformSkillView:
    """按 id 取一条平台技能。"""
    for skill in _SKILLS:
        if skill.id == skill_id:
            return skill
    raise BizError(ErrorCode.SKILL_NOT_FOUND)
