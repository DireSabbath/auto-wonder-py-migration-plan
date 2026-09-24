"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Repo(Base):
    """代码仓库"""

    __tablename__ = "repo"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False, comment="仓库地址（https/ssh）")
    default_branch: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="默认分支"
    )
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    scan_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="UNSCANNED",
        server_default=text("'UNSCANNED'"),
        comment="UNSCANNED/SCANNING/CONCLUDED",
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


class RepoConclusion(Base):
    """仓库结论"""

    __tablename__ = "repo_conclusion"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True, comment="仓库作用")
    key_business: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="关键业务信息（数组）"
    )
    upstreams: Mapped[object | None] = mapped_column(JSON, nullable=True, comment="业务上游")
    downstreams: Mapped[object | None] = mapped_column(JSON, nullable=True, comment="业务下游")
    summary_md: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="结论正文（Markdown）"
    )
    ai_session_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="来源 AI 会话（可空：手工）"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="结论版本"
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


class RepoRelation(Base):
    """仓库关系（repo-map）"""

    __tablename__ = "repo_relation"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    relation_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="FRONTEND_OF/BACKEND_OF/GATEWAY_OF/DEPENDS_ON/RELATED"
    )
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ai_session_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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


register_tenant_model(Repo)
register_tenant_model(RepoConclusion)
register_tenant_model(RepoRelation)
