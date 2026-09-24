"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Agent(Base):
    """数字员工（身份）"""

    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="展示名")
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="DRAFT",
        server_default=text("'DRAFT'"),
        comment="DRAFT/PENDING_REVIEW/ONLINE/OFFLINE",
    )
    kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="STANDARD",
        server_default=text("'STANDARD'"),
        comment="STANDARD=普通数字员工 / PLATFORM=平台数字人",
    )
    online_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前在线版本（调度只读此版本）"
    )
    editing_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前草稿版本"
    )
    latest_version_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="最近版本号（发号用）",
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class AgentVersion(Base):
    """数字员工版本"""

    __tablename__ = "agent_version"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="该 agent 内单调递增")
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="DRAFT",
        server_default=text("'DRAFT'"),
        comment="DRAFT/PENDING_REVIEW/APPROVED/REJECTED",
    )
    role_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="角色名/职责标识（队友目录组织）"
    )
    role_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="角色机器码（按角色路由）"
    )
    business_background: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="SOUL.md 内容（Markdown；REST 字段 businessBackground）"
    )
    responsibilities: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="AGENT.md 内容（Markdown；REST 字段 responsibilities）"
    )
    sdlc_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="绑定 SDLC")
    identity_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="冻结身份快照（供 identity.json）"
    )
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    review_comment: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class AgentEnvironmentVariableRef(Base):
    """数字员工版本-环境变量引用"""

    __tablename__ = "agent_environment_variable_ref"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    environment_variable_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class AgentRepoPerm(Base):
    """版本-仓库权限"""

    __tablename__ = "agent_repo_perm"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    perm_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="READ",
        server_default=text("'READ'"),
        comment="READ/WRITE",
    )
    allowed_branch_patterns: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON branch allowlist; NULL means unrestricted"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class AgentSkill(Base):
    """版本-技能"""

    __tablename__ = "agent_skill"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    skill_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class AgentMemoryRef(Base):
    """版本-记忆引用"""

    __tablename__ = "agent_memory_ref"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memory_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="DIRECT",
        server_default=text("'DIRECT'"),
        comment="DIRECT/ORG_IMPORT/SQUAD_IMPORT/AGENT_IMPORT",
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(Agent)
register_tenant_model(AgentVersion)
register_tenant_model(AgentEnvironmentVariableRef)
register_tenant_model(AgentRepoPerm)
register_tenant_model(AgentSkill)
register_tenant_model(AgentMemoryRef)
