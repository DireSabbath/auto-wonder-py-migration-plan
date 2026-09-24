"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class PlatformAdminInit(Base):
    """平台管理员一次性初始化迁移完成标记"""

    __tablename__ = "platform_admin_init"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    initialized: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="0 未完成 / 1 初始化迁移已完成",
    )


class PlatformBrandingConfig(Base):
    """平台品牌与私有化部署一致性配置"""

    __tablename__ = "platform_branding_config"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    platform_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="AutoWonder",
        server_default=text("'AutoWonder'"),
        comment="平台展示名称",
    )
    logo_oss_ref: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="Logo 对象存储引用"
    )
    logo_content_type: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Logo MIME 类型"
    )
    theme_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="aliyun-orange",
        server_default=text("'aliyun-orange'"),
        comment="主题配色 key",
    )
    primary_color: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="#f97316",
        server_default=text("'#f97316'"),
        comment="主品牌色",
    )
    domain: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="私有化部署访问域名"
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
