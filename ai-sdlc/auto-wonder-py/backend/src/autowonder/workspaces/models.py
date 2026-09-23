"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Computed, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class Org(Base):
    """工作空间（历史物理表 org）"""

    __tablename__ = "org"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        nullable=False,
        comment="工作空间 ID；历史复用表以 tenant_id 关联",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="工作空间名称")
    active_name_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="在用名称键；软删除后置 NULL 以释放名称占位"
    )
    slug: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="唯一短标识（邀请链接用）"
    )
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="描述")
    background: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工作空间背景")
    owner_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="创建者/负责人 user_id"
    )
    status: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="0 正常 / 1 停用"
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
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="逻辑删除时间"
    )
    deleted_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="执行逻辑删除的 user_id"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class OrgMember(Base):
    """工作空间成员（历史物理表 org_member）"""

    __tablename__ = "org_member"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="工作空间 ID（历史物理字段 tenant_id）"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="成员")
    status: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="0 正常 / 1 待审批 / 2 已移除",
    )
    access_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="READ_ONLY",
        server_default=text("'READ_ONLY'"),
        comment="READ_ONLY/READ_WRITE/ADMIN",
    )
    identity_tags: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="成员业务身份标签；仅用于协作上下文，不参与鉴权"
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class OrgInvite(Base):
    """工作空间邀请（历史物理表 org_invite）"""

    __tablename__ = "org_invite"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="邀请码，唯一")
    inviter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_email: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="定向邀请邮箱（可空）"
    )
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="0 有效 / 1 已用 / 2 失效",
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


class WorkspaceAccessRequest(Base):
    """工作空间权限申请"""

    __tablename__ = "workspace_access_request"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="目标工作空间 ID")
    requester_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="申请人 user ID")
    requested_level: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="READ_ONLY / READ_WRITE / ADMIN"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
        comment="PENDING / APPROVED / REJECTED",
    )
    pending_marker: Mapped[int | None] = mapped_column(
        Integer,
        Computed("CASE WHEN status = 'PENDING' THEN 1 ELSE NULL END", persisted=True),
        nullable=True,
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="审批人 user ID"
    )
    reject_reason: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="拒绝原因"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
