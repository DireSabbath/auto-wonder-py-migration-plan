"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Notification(Base):
    """通知"""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recipient_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="接收用户")
    type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="MEMORY_REVIEW/AGENT_REVIEW/HUMAN_HANDOFF/DISPATCH_ALERT/MENTION/AI_DONE",
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="摘要文本")
    link: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="前端跳转路由")
    ref_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="UNREAD",
        server_default=text("'UNREAD'"),
        comment="UNREAD/READ",
    )
    channels_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="已投递渠道与结果"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class NotifyPref(Base):
    """通知偏好"""

    __tablename__ = "notify_pref"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False, comment="通知类型")
    in_app: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"), comment="站内（0/1）"
    )
    dingtalk: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"), comment="钉钉（0/1）"
    )
    feishu: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="飞书（0/1）"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class WorkitemCommentDelivery(Base):
    """工单评论定向 Worker 投递状态"""

    __tablename__ = "workitem_comment_delivery"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False, comment="投递记录ID"
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="租户ID")
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WORKITEM", server_default=text("'WORKITEM'")
    )
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="工单ID")
    comment_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="来源评论ID，正文以workitem_comment为准"
    )
    target_agent_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="被@的目标数字员工ID"
    )
    dispatch_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="实际承载本次交互的派发ID"
    )
    executor_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="实际接收本次交互的执行器ID"
    )
    reply_comment_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="本次旁路交互生成的Agent回复评论ID"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="QUEUED",
        server_default=text("'QUEUED'"),
        comment="投递状态：QUEUED/DELIVERED/APPLIED/FAILED",
    )
    error: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="投递或执行失败原因"
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="发送给Runtime的时间"
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Runtime确认已处理的时间"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=now_local,
        server_default=text("CURRENT_TIMESTAMP(3)"),
        comment="创建时间",
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=now_local,
        server_default=text("CURRENT_TIMESTAMP(3)"),
        comment="最后修改时间",
    )
    retry_dispatch_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )


register_tenant_model(Notification)
register_tenant_model(NotifyPref)
register_tenant_model(WorkitemCommentDelivery)
