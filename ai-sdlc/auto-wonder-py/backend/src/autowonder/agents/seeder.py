"""为工作空间播种出厂平台数字人 Chief of Staff。已存在则跳过。"""

import json
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.core.clock import now_local

PLATFORM_KIND = "PLATFORM"
PLATFORM_AGENT_NAME = "Chief of Staff"
PLATFORM_ROLE_CODE = "chief_of_staff"
_RESOURCE_DIR = Path(__file__).resolve().parent / "resources"


async def seed(session: AsyncSession, tenant_id: int, creator_id: int) -> bool:
    """创建平台数字人并直接上线。返回本次是否新建。"""
    existing = await session.scalar(
        select(Agent.id)
        .where(Agent.tenant_id == tenant_id, Agent.kind == PLATFORM_KIND, Agent.is_deleted == 0)
        .order_by(Agent.id.asc())
        .limit(1)
    )
    if existing is not None:
        return False
    agent_md = _load_template("chief_of_staff_agent.md")
    soul_md = _load_template("chief_of_staff_soul.md")
    agent = Agent(
        tenant_id=tenant_id,
        name=PLATFORM_AGENT_NAME,
        kind=PLATFORM_KIND,
        status="ONLINE",
        latest_version_no=1,
        creator_id=creator_id,
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
        role_name=PLATFORM_AGENT_NAME,
        role_code=PLATFORM_ROLE_CODE,
        responsibilities=agent_md,
        business_background=soul_md,
        sdlc_id=None,
        identity_json=_identity(agent_md, soul_md),
        review_comment="platform seeded",
        reviewed_at=now_local(),
        creator_id=creator_id,
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
            latest_version_no=1,
            version=Agent.version + 1,
            modifier_id=creator_id,
        )
    )
    return True


def _identity(agent_md: str, soul_md: str) -> dict[str, object]:
    return {
        "name": PLATFORM_AGENT_NAME,
        "avatarUrl": None,
        "roleName": PLATFORM_AGENT_NAME,
        "roleCode": PLATFORM_ROLE_CODE,
        "businessBackground": soul_md,
        "responsibilities": agent_md,
        "evolutionMode": "ASSISTED",
    }


def _load_template(name: str) -> str:
    content = (_RESOURCE_DIR / name).read_text(encoding="utf-8")
    if content.strip() == "":
        raise RuntimeError(f"platform agent template is blank: {name}")
    return content


def identity_json(agent_md: str, soul_md: str) -> str:
    """与 Fastjson 紧凑输出同构，供对照。"""
    return json.dumps(_identity(agent_md, soul_md), ensure_ascii=False, separators=(",", ":"))
