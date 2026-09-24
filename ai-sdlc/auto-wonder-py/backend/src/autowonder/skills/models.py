"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Skill(Base):
    """技能"""

    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, comment="MCP/SKILLS/PLUGIN")
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="真实名称（执行器据此加载）"
    )
    install_spec: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="安装与下载规格"
    )
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="INSTALL_SPEC",
        server_default=text("'INSTALL_SPEC'"),
        comment="INSTALL_SPEC/OSS_ZIP",
    )
    package_oss_ref: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="目录上传 skill zip 的 OSS 引用"
    )
    package_file_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="上传 zip 文件名"
    )
    package_size: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="上传 zip 字节数"
    )
    package_md5: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="上传 zip md5"
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


register_tenant_model(Skill)
