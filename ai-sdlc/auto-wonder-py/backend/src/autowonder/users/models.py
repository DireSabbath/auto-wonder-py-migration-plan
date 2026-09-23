"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class User(Base):
    """用户/员工（全局）"""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, comment="登录名，全局唯一")
    email: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="邮箱，全局唯一（可空）"
    )
    password_hash: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="BCrypt 哈希（含盐）"
    )
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="昵称")
    avatar_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="头像（OSS 引用）"
    )
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="联系方式")
    status: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="0 正常 / 1 禁用"
    )
    is_admin: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="0 普通用户 / 1 平台管理员",
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="注销申请时间"
    )
    cooling_off_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="冷静期截止时间（7天后）"
    )
    deactivation_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="撤销注销时间"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近登录"
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


class UserSetting(Base):
    """用户级偏好配置（全局）"""

    __tablename__ = "user_setting"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="归属用户（user.id）")
    setting_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="配置键，例如 clarification_send_mode"
    )
    value_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="配置值（JSON，支持简单值与复杂结构）"
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
