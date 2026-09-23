"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class AssetCategory(Base):
    """项目级资产分类树节点"""

    __tablename__ = "asset_category"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="项目/工作空间归属，复用平台既有隔离边界"
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="父分类 id；NULL 表示顶级分类"
    )
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="分类名称，同级（同一父节点下）不允许重复"
    )
    description: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, comment="分类说明（选填）"
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


class AssetCategoryRef(Base):
    """资产与分类的主分类关联（一资产一分类，物理删除即取消打标）"""

    __tablename__ = "asset_category_ref"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="SKILL",
        server_default=text("'SKILL'"),
        comment="资产类型：SKILL（能力）；未来可扩展 MEMORY 等",
    )
    asset_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="资产 id，SKILL 资产对应 skill.id"
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="主分类 asset_category.id；每个资产最多一条在用关联"
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


register_tenant_model(AssetCategory)
register_tenant_model(AssetCategoryRef)
