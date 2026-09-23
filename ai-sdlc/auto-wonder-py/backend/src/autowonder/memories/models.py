"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Memory(Base):
    """记忆"""

    __tablename__ = "memory"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, comment="AGENT/SQUAD/ORG")
    owner_ref: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="AGENT 为 agent_id，SQUAD 为 squad_id，ORG 为空"
    )
    type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="项目知识/工程规则/经验/偏好/避坑/组织知识..."
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content_md: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="记忆正文（Markdown）"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="DRAFT",
        server_default=text("'DRAFT'"),
        comment="DRAFT/PENDING/ADOPTED/REJECTED",
    )
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="MANUAL",
        server_default=text("'MANUAL'"),
        comment="MANUAL/AI_IMPORT/EXECUTOR_LEARNED/ARTIFACT",
    )
    source_ref: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="来源引用（ai_session_id/dispatch_id/artifact_id/链接）"
    )
    source_dedupe_key: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="自动导入来源的幂等键"
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


class MemoryReview(Base):
    """记忆审核记录"""

    __tablename__ = "memory_review"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memory_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reviewer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, comment="ADOPT/REJECT")
    edited_content_md: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="审核时可编辑后再采纳"
    )
    comment: Mapped[str | None] = mapped_column(String(512), nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(Memory)
register_tenant_model(MemoryReview)
