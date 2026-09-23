"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class ExternalPrincipal(Base):
    """外部平台身份主体"""

    __tablename__ = "external_principal"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="来源平台，例如 AONE 或 JIRA"
    )
    subject_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="来源侧主体稳定 ID"
    )
    display_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="来源侧展示名称"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class ExternalProjectBinding(Base):
    """外部项目绑定"""

    __tablename__ = "external_project_binding"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_project_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    client_key: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_ref: Mapped[str] = mapped_column(
        Text, nullable=False, comment="SecretCrypto 加密密文"
    )
    region_id: Mapped[str] = mapped_column(
        String(16), nullable=False, default="1", server_default=text("'1'")
    )
    writeback_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    poll_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reconcile_cursor: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="已关联工单分批对账游标"
    )
    comment_poll_watermark: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="独立评论链路已完整扫描的修改时间上界"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class ExternalWorkitemLink(Base):
    """外部工单映射"""

    __tablename__ = "external_workitem_link"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_workitem_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_work_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="外部工单原始链接"
    )
    source_status_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="来源业务状态 ID"
    )
    source_status_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="来源业务状态名称"
    )
    source_lifecycle: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default=text("'ACTIVE'"),
        comment="来源生命周期：ACTIVE、CLOSED、DELETED 或 UNAVAILABLE",
    )
    reporter_principal_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="归一化后的需求提出者身份主体"
    )
    business_owner_principal_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="归一化后的当前业务负责人身份主体"
    )
    principal_relations_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="来源系统定义的身份参与关系组"
    )
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_version_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_sync_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="当前工单最后成功同步时间"
    )
    sync_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="HEALTHY",
        server_default=text("'HEALTHY'"),
        comment="同步状态：HEALTHY、DELAYED 或 ACTION_REQUIRED",
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="工单级稳定错误码"
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="脱敏后的工单同步错误摘要"
    )
    comment_sync_cursor: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="0",
        server_default=text("'0'"),
        comment="已同步的最大 Aone commentId",
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class ExternalWorkitemImportRecord(Base):
    """外部工单导入记录"""

    __tablename__ = "external_workitem_import_record"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    external_workitem_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workitem_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_payload_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    extensions_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    field_mappings_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class ExternalCommentLink(Base):
    """外部评论映射"""

    __tablename__ = "external_comment_link"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_workitem_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_comment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workitem_comment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="来源侧评论更新时间"
    )
    source_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default=text("'ACTIVE'"),
        comment="来源评论状态：ACTIVE 或 DELETED",
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class ExternalStatusMapping(Base):
    """外部状态映射"""

    __tablename__ = "external_status_mapping"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_issue_type_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_status_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_status_name: Mapped[str] = mapped_column(String(128), nullable=False)
    work_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status_node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class IntegrationOutbox(Base):
    """外部评论写回回执与存量写回队列"""

    __tablename__ = "integration_outbox"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[object] = mapped_column(JSON, nullable=False)
    operation_key: Mapped[str] = mapped_column(
        String(191), nullable=False, comment="评论语义幂等键；存量任务使用 legacy:<id>"
    )
    lock_version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="执行抢占与恢复接管的数字栅栏",
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class AoneRateBucket(Base):
    """Aone OpenAPI 全局限流桶"""

    __tablename__ = "aone_rate_bucket"

    client_key: Mapped[str] = mapped_column(
        String(128), primary_key=True, nullable=False, comment="限流客户端标识"
    )
    capacity: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False, comment="桶容量")
    tokens: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False, comment="当前令牌数")
    refill_per_sec: Mapped[float] = mapped_column(
        Numeric(10, 6), nullable=False, comment="每秒补充令牌数"
    )
    last_refill_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="最近补充时间，Unix 毫秒"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class DingtalkRobotBinding(Base):
    """钉钉机器人绑定(一机器人一数字人)"""

    __tablename__ = "dingtalk_robot_binding"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    app_key: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_ref: Mapped[str] = mapped_column(
        Text, nullable=False, comment="SecretCrypto 加密后的 appSecret"
    )
    robot_code: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="关联数字人")
    transport_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="HTTP_CALLBACK", server_default=text("'HTTP_CALLBACK'")
    )
    callback_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    region_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stream_env: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="DingTalk Stream environment: ONLINE/PRE/OVERSEA/OVERSEA_PRE",
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ENABLED", server_default=text("'ENABLED'")
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class FeishuRobotBinding(Base):
    """飞书企业自建应用绑定"""

    __tablename__ = "feishu_robot_binding"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_ref: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="SecretCrypto encrypted App Secret / Verification Token / Encrypt Key JSON",
    )
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ENABLED", server_default=text("'ENABLED'")
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )


class FeishuMessageInbox(Base):
    """飞书回调持久化收件箱"""

    __tablename__ = "feishu_message_inbox"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Agent at receipt time; never reroute after a binding change",
    )
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
