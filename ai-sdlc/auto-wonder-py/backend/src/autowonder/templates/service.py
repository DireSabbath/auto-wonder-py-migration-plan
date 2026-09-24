"""列出并应用小队模板。系统模板 tenant_id 为空，不走租户条件。"""

from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentRepoPerm, AgentVersion
from autowonder.core.errors import BizError, ErrorCode
from autowonder.repos.models import Repo
from autowonder.sdlcs.models import Sdlc, SdlcStep
from autowonder.squads.models import Squad, SquadMember, SquadTemplate
from autowonder.templates.content import (
    agent_details,
    is_system_template,
    parse_content,
    split_tags,
    squad_info,
    step_required,
)
from autowonder.templates.schemas import (
    AppliedAgent,
    ApplyTemplateResult,
    SquadTemplateDetailView,
    SquadTemplateView,
)

_REPO_LIMIT = 200


async def list_templates(session: AsyncSession, tenant_id: int) -> list[SquadTemplateView]:
    """当前工作空间可见的启用模板，系统模板排在同规模租户模板旁边。"""
    rows = await session.scalars(
        select(SquadTemplate)
        .where(
            SquadTemplate.is_deleted == 0,
            SquadTemplate.status == "ACTIVE",
            or_(SquadTemplate.tenant_id.is_(None), SquadTemplate.tenant_id == tenant_id),
        )
        .order_by(SquadTemplate.squad_size.asc(), SquadTemplate.id.asc())
    )
    return [_to_view(template) for template in rows]


async def get_template(session: AsyncSession, template_id: int) -> SquadTemplateDetailView:
    """模板详情，展开 content_json 里的小队和数字员工。"""
    template = await _require_template(session, template_id)
    content = parse_content(template.content_json)
    view = SquadTemplateDetailView(
        id=template.id,
        name=template.name,
        description=template.description,
        squad_size=template.squad_size,
        icon=template.icon,
        tags=split_tags(template.tags),
        system=is_system_template(template.tenant_id),
        squad=squad_info(content),
        agents=agent_details(content),
    )
    return view


async def apply_template(
    session: AsyncSession,
    template_id: int,
    tenant_id: int,
    user_id: int,
) -> ApplyTemplateResult:
    """按模板创建小队、流程、数字员工、成员，并把已有仓库授成 WRITE。"""
    template = await _require_template(session, template_id)
    content = parse_content(template.content_json)
    squad_json = content["squad"]
    squad = Squad(
        tenant_id=tenant_id,
        name=squad_json.get("name"),
        description=squad_json.get("description"),
        owner_id=user_id,
        creator_id=user_id,
        status=0,
        is_deleted=0,
        version=0,
    )
    session.add(squad)
    await session.flush()
    repos = await _repos(session, tenant_id)
    applied: list[AppliedAgent] = []
    for agent_json in content["agents"]:
        applied.append(
            await _create_agent(session, agent_json, tenant_id, user_id, squad.id, repos)
        )
    await session.commit()
    return ApplyTemplateResult(squad_id=squad.id, agents=applied)


async def _create_agent(
    session: AsyncSession,
    agent_json: dict[str, Any],
    tenant_id: int,
    user_id: int,
    squad_id: int,
    repos: list[Repo],
) -> AppliedAgent:
    sdlc_json: dict[str, Any] = agent_json["sdlc"]
    sdlc = Sdlc(
        tenant_id=tenant_id,
        name=sdlc_json.get("name"),
        description=sdlc_json.get("description"),
        status="ENABLED",
        is_default=0,
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(sdlc)
    await session.flush()
    steps: list[dict[str, Any]] = sdlc_json["steps"]
    entry_step_id: int | None = None
    for index, step_json in enumerate(steps):
        step = SdlcStep(
            tenant_id=tenant_id,
            sdlc_id=sdlc.id,
            step_order=step_json.get("order"),
            name=step_json.get("name"),
            kind=step_json.get("kind"),
            instruction_md=step_json.get("instruction"),
            required=step_required(step_json),
            creator_id=user_id,
            is_deleted=0,
        )
        session.add(step)
        await session.flush()
        if index == 0:
            entry_step_id = step.id
    if entry_step_id is not None:
        await session.execute(
            update(Sdlc)
            .where(
                Sdlc.id == sdlc.id,
                Sdlc.tenant_id == tenant_id,
                Sdlc.version == sdlc.version,
                Sdlc.is_deleted == 0,
            )
            .values(
                status="ENABLED",
                entry_step_id=entry_step_id,
                version=Sdlc.version + 1,
                modifier_id=user_id,
            )
        )
    name = agent_json.get("name")
    role_code = agent_json.get("roleCode")
    role_name = agent_json.get("roleName")
    agent = Agent(
        tenant_id=tenant_id,
        name=name,
        status="ONLINE",
        kind="STANDARD",
        latest_version_no=1,
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(agent)
    await session.flush()
    version = AgentVersion(
        tenant_id=tenant_id,
        agent_id=agent.id,
        version_no=1,
        status="APPROVED",
        role_name=role_name,
        role_code=role_code,
        business_background=agent_json.get("businessBackground"),
        responsibilities=agent_json.get("responsibilities"),
        sdlc_id=sdlc.id,
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(version)
    await session.flush()
    await session.execute(
        update(Agent)
        .where(
            Agent.id == agent.id,
            Agent.tenant_id == tenant_id,
            Agent.version == agent.version,
            Agent.is_deleted == 0,
        )
        .values(
            status="ONLINE",
            online_version_id=version.id,
            editing_version_id=None,
            latest_version_no=1,
            version=Agent.version + 1,
            modifier_id=user_id,
        )
    )
    session.add(SquadMember(tenant_id=tenant_id, squad_id=squad_id, agent_id=agent.id))
    for repo in repos:
        session.add(
            AgentRepoPerm(
                tenant_id=tenant_id,
                agent_version_id=version.id,
                repo_id=repo.id,
                perm_level="WRITE",
            )
        )
    return AppliedAgent(agent_id=agent.id, role_name=role_name, role_code=role_code)


async def _repos(session: AsyncSession, tenant_id: int) -> list[Repo]:
    rows = await session.scalars(
        select(Repo)
        .where(Repo.tenant_id == tenant_id, Repo.is_deleted == 0)
        .order_by(Repo.id.desc())
        .offset(0)
        .limit(_REPO_LIMIT)
    )
    return list(rows)


async def _require_template(session: AsyncSession, template_id: int) -> SquadTemplate:
    template = await session.scalar(
        select(SquadTemplate)
        .where(SquadTemplate.id == template_id, SquadTemplate.is_deleted == 0)
        .limit(1)
    )
    if template is None:
        raise BizError(ErrorCode.SQUAD_TEMPLATE_NOT_FOUND)
    return template


def _to_view(template: SquadTemplate) -> SquadTemplateView:
    return SquadTemplateView(
        id=template.id,
        name=template.name,
        description=template.description,
        squad_size=template.squad_size,
        icon=template.icon,
        tags=split_tags(template.tags),
        system=is_system_template(template.tenant_id),
    )
