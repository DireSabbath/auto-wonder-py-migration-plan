"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Workitem(Base):
    """工单"""

    __tablename__ = "workitem"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    origin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    work_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="REQ/TASK/BUG")
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True, comment="正文（Markdown）")
    template_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="引用状态模版"
    )
    status_node_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前状态"
    )
    sdlc_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="绑定 SDLC（指派数字员工时必需）"
    )
    current_step_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前 SDLC 步骤"
    )
    assignee_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="HUMAN/AGENT"
    )
    assignee_ref: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="user_id 或 agent_id"
    )
    assign_operator_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="指派操作人（触发指派动作的真人 user_id；用于交接无下一跳时兜底路由，可空）",
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
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
    scheduled_start_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="计划执行时间，NULL表示立即执行"
    )
    scheduled_start_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="定时执行实际触发时间，NULL表示尚未触发"
    )
    tags: Mapped[object | None] = mapped_column(JSON, nullable=True, comment="工单标签数组")


class WorkitemComment(Base):
    """工单评论"""

    __tablename__ = "workitem_comment"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WORKITEM", server_default=text("'WORKITEM'")
    )
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="HUMAN/AGENT")
    author_ref: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class WorkitemCommentMention(Base):
    """工单评论mention明细"""

    __tablename__ = "workitem_comment_mention"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WORKITEM", server_default=text("'WORKITEM'")
    )
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="AGENT/HUMAN")
    target_ref: Mapped[int] = mapped_column(BigInteger, nullable=False)
    display_name_snapshot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class WorkitemEvent(Base):
    """工单事件时间线"""

    __tablename__ = "workitem_event"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="CREATE/EDIT/STATUS_CHANGE/ASSIGN/DISPATCH/RESULT/COMMENT",
    )
    from_val: Mapped[str | None] = mapped_column(String(256), nullable=True)
    to_val: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actor_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="HUMAN/AGENT/SYSTEM"
    )
    actor_ref: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    detail_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class WorkitemWatcher(Base):
    """工单真人关注关系"""

    __tablename__ = "workitem_watcher"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="工作空间 ID")
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="工单 ID")
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="关注人 user ID")
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class WorkitemExecutionControl(Base):
    """workitem_execution_control"""

    __tablename__ = "workitem_execution_control"

    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    workitem_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    closed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    modifier_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(Workitem)
register_tenant_model(WorkitemComment)
register_tenant_model(WorkitemEvent)
