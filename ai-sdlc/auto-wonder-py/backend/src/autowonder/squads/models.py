"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Squad(Base):
    """小队"""

    __tablename__ = "squad"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="负责人（可空）"
    )
    status: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="0 正常 / 1 解散"
    )
    debug_log_enabled: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="小队级 debug 日志收集开关",
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


class SquadMember(Base):
    """小队成员"""

    __tablename__ = "squad_member"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    squad_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class SquadTemplate(Base):
    """小队模版间"""

    __tablename__ = "squad_template"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="null=系统内置，非null=租户自建"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="模版名称")
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="模版描述")
    squad_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"), comment="小队人数"
    )
    icon: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="图标标识(solo/pair/team)"
    )
    tags: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="标签，逗号分隔")
    content_json: Mapped[str] = mapped_column(
        Text, nullable=False, comment="完整小队配置JSON(squad+agents+sdlc)"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ACTIVE",
        server_default=text("'ACTIVE'"),
        comment="ACTIVE/DISABLED",
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


register_tenant_model(Squad)
register_tenant_model(SquadMember)
