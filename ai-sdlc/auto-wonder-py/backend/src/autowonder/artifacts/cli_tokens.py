"""工单需求文档的短时上传、下载令牌，以及对应的命令示例。"""

from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.api.access import WorkspaceAccessLevel
from autowonder.artifacts.documents import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    SUPPORTED_EXTENSIONS,
    find_workitem_statement,
)
from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.schema import ApiModel
from autowonder.platform.branding import (
    effective_public_base_url,
    normalize_public_base_url,
    normalize_runtime_version,
)
from autowonder.platform.models import PlatformBrandingConfig
from autowonder.scheduledtasks.models import ScheduledTask
from autowonder.security.jwt import parse_user_purpose, sign_user_purpose
from autowonder.workitems.models import Workitem
from autowonder.workspaces.models import OrgMember

UPLOAD_PREFIX = "awupload_"
UPLOAD_PURPOSE = "workitem-requirement-upload"
UPLOAD_ENV = "AUTOWONDER_UPLOAD_TOKEN"
DOWNLOAD_PREFIX = "awdownload_"
DOWNLOAD_PURPOSE = "workitem-requirement-download"
DOWNLOAD_ENV = "AUTOWONDER_DOWNLOAD_TOKEN"
TOKEN_TTL_SECONDS = 1800
MAX_FILES = 10
_WRITE_LEVELS = {WorkspaceAccessLevel.READ_WRITE, WorkspaceAccessLevel.ADMIN}


class CredentialType(Enum):
    """可以签发 CLI 令牌的 MCP 凭证种类。"""

    LONG_LIVED = "LONG_LIVED"
    DISPATCH = "DISPATCH"
    CONVERSATION = "CONVERSATION"


_MINT_CREDENTIALS = {
    CredentialType.LONG_LIVED,
    CredentialType.DISPATCH,
    CredentialType.CONVERSATION,
}


class UploadTokenView(ApiModel):
    """上传令牌和给操作者复制的命令。"""

    token: str
    token_type: str
    expires_in_seconds: int
    expires_at: str
    server_url: str
    runtime_version: str
    token_env_name: str
    command: str
    powershell_command: str
    supported_extensions: list[str]
    max_files: int
    max_file_size_bytes: int
    max_total_size_bytes: int


class DownloadTokenView(ApiModel):
    """下载令牌和给操作者复制的命令。"""

    token: str
    token_type: str
    expires_in_seconds: int
    expires_at: str
    server_url: str
    runtime_version: str
    token_env_name: str
    command: str
    powershell_command: str
    supported_extensions: list[str]


def find_any_scheduled_task_statement(task_id: int) -> Select[tuple[ScheduledTask]]:
    """按主键读取未删除任务，不先按工作空间过滤。"""
    return (
        select(ScheduledTask)
        .where(ScheduledTask.id == task_id, ScheduledTask.is_deleted == 0)
        .limit(1)
    )


def posix_quote(value: str) -> str:
    """用单引号包裹，内部单引号按 POSIX 写成 ``'\\''``。"""
    return "'" + value.replace("'", "'\\''") + "'"


def powershell_quote(value: str) -> str:
    """用单引号包裹，内部单引号写成两个单引号。"""
    return "'" + value.replace("'", "''") + "'"


def upload_command_template(server_url: str, runtime_version: str) -> str:
    """上传命令模板，不含真实令牌。"""
    return (
        "npx -y autowonder@"
        + runtime_version
        + " workitem upload --server-url "
        + server_url
        + " --workitem-id <workitem-id>"
        + " --file <filepath-1> --file <filepath-2> --file <images-1> --json"
    )


def scheduled_task_command_template(server_url: str, runtime_version: str) -> str:
    """定时任务上传命令模板，不含真实令牌。"""
    return (
        "npx -y autowonder@"
        + runtime_version
        + " scheduled-task upload --server-url "
        + server_url
        + " --scheduled-task-id <scheduled-task-id>"
        + " --file <filepath-1> --file <filepath-2> --file <images-1> --json"
    )


def download_command_template(server_url: str, runtime_version: str) -> str:
    """下载命令模板，不含真实令牌。"""
    return (
        "npx -y autowonder@"
        + runtime_version
        + " workitem download --server-url "
        + server_url
        + " --workitem-id <workitem-id>"
        + " --file <name-or-id> --output-dir <dir> --json"
    )


def upload_token_env_hint() -> str:
    """告诉调用方把上传令牌放进哪个环境变量。"""
    return "export " + UPLOAD_ENV + "='<token returned by autowonder.workitem_cli_upload_token>'"


def download_token_env_hint() -> str:
    """告诉调用方把下载令牌放进哪个环境变量。"""
    return (
        "export " + DOWNLOAD_ENV + "='<token returned by autowonder.workitem_cli_download_token>'"
    )


def presented_token(authorization: str | None) -> str:
    """取出 ``Bearer`` 后面的令牌。方案名不区分大小写，空白按 Java ``trim``。"""
    scheme = "Bearer "
    if authorization is None or len(authorization) < len(scheme):
        raise BizError(ErrorCode.UNAUTHORIZED)
    if authorization[: len(scheme)].lower() != scheme.lower():
        raise BizError(ErrorCode.UNAUTHORIZED)
    return _java_trim(authorization[len(scheme) :])


def authenticate_upload(token: str | None) -> int:
    """校验上传令牌并返回用户 id。任何失败都是未登录，响应里不回显令牌。"""
    return _authenticate(token, UPLOAD_PREFIX, UPLOAD_PURPOSE)


def authenticate_download(token: str | None) -> int:
    """校验下载令牌并返回用户 id。任何失败都是未登录，响应里不回显令牌。"""
    return _authenticate(token, DOWNLOAD_PREFIX, DOWNLOAD_PURPOSE)


async def load_workitem(session: AsyncSession, workitem_id: int) -> Workitem:
    """读取未删除工单。不存在时按工单不存在拒绝。"""
    row = await session.scalar(find_workitem_statement(workitem_id))
    if row is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return row


async def load_scheduled_task(session: AsyncSession, task_id: int) -> ScheduledTask:
    """读取未删除定时任务。不存在时按任务不存在拒绝。"""
    row = await session.scalar(find_any_scheduled_task_statement(task_id))
    if row is None:
        raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
    return row


async def require_write_membership(session: AsyncSession, tenant_id: int, user_id: int) -> None:
    """当前用户必须是该工作空间里未停用的写成员或管理员。"""
    level = await _active_level(session, tenant_id, user_id)
    if level not in _WRITE_LEVELS:
        raise BizError(ErrorCode.NO_PERMISSION)


async def require_read_membership(session: AsyncSession, tenant_id: int, user_id: int) -> None:
    """当前用户必须是该工作空间里未停用的成员，只读也可以下载。"""
    await _active_level(session, tenant_id, user_id)


async def mint_upload_token(
    session: AsyncSession,
    credential_type: CredentialType,
    user_id: int,
    workitem_id: int,
) -> UploadTokenView:
    """给有写权限的用户签发 30 分钟上传令牌，并带上本部署的命令。"""
    if credential_type not in _MINT_CREDENTIALS:
        raise BizError(ErrorCode.NO_PERMISSION)
    workitem = await load_workitem(session, workitem_id)
    await require_write_membership(session, workitem.tenant_id, user_id)
    server_url, runtime_version = await deployment_endpoint(session)
    token = UPLOAD_PREFIX + sign_user_purpose(user_id, UPLOAD_PURPOSE, TOKEN_TTL_SECONDS)
    return UploadTokenView(
        token=token,
        token_type="Bearer",
        expires_in_seconds=TOKEN_TTL_SECONDS,
        expires_at=_expires_at(),
        server_url=server_url,
        runtime_version=runtime_version,
        token_env_name=UPLOAD_ENV,
        command=_posix_upload(token, workitem_id, server_url, runtime_version),
        powershell_command=_powershell_upload(token, workitem_id, server_url, runtime_version),
        supported_extensions=list(SUPPORTED_EXTENSIONS),
        max_files=MAX_FILES,
        max_file_size_bytes=MAX_FILE_BYTES,
        max_total_size_bytes=MAX_TOTAL_BYTES,
    )


async def mint_download_token(
    session: AsyncSession,
    credential_type: CredentialType,
    user_id: int,
    workitem_id: int,
) -> DownloadTokenView:
    """给有读权限的用户签发 30 分钟下载令牌，并带上本部署的命令。"""
    if credential_type not in _MINT_CREDENTIALS:
        raise BizError(ErrorCode.NO_PERMISSION)
    workitem = await load_workitem(session, workitem_id)
    await require_read_membership(session, workitem.tenant_id, user_id)
    server_url, runtime_version = await deployment_endpoint(session)
    token = DOWNLOAD_PREFIX + sign_user_purpose(user_id, DOWNLOAD_PURPOSE, TOKEN_TTL_SECONDS)
    return DownloadTokenView(
        token=token,
        token_type="Bearer",
        expires_in_seconds=TOKEN_TTL_SECONDS,
        expires_at=_expires_at(),
        server_url=server_url,
        runtime_version=runtime_version,
        token_env_name=DOWNLOAD_ENV,
        command=_posix_download(token, workitem_id, server_url, runtime_version),
        powershell_command=_powershell_download(token, workitem_id, server_url, runtime_version),
        supported_extensions=list(SUPPORTED_EXTENSIONS),
    )


async def upload_command_template_for(session: AsyncSession) -> str:
    """按当前部署地址生成上传命令模板。"""
    server_url, runtime_version = await deployment_endpoint(session)
    return upload_command_template(server_url, runtime_version)


async def scheduled_task_command_template_for(session: AsyncSession) -> str:
    """按当前部署地址生成定时任务上传命令模板。"""
    server_url, runtime_version = await deployment_endpoint(session)
    return scheduled_task_command_template(server_url, runtime_version)


async def download_command_template_for(session: AsyncSession) -> str:
    """按当前部署地址生成下载命令模板。"""
    server_url, runtime_version = await deployment_endpoint(session)
    return download_command_template(server_url, runtime_version)


async def deployment_endpoint(session: AsyncSession) -> tuple[str, str]:
    """品牌里保存的域名优先，否则用部署根地址。运行时版本来自启动配置。"""
    row = await session.scalar(
        select(PlatformBrandingConfig)
        .where(PlatformBrandingConfig.id == 1, PlatformBrandingConfig.is_deleted == 0)
        .limit(1)
    )
    domain = None
    if row is not None:
        domain = row.domain
    settings = get_settings()
    server_url = effective_public_base_url(
        domain,
        normalize_public_base_url(settings.public_base_url),
    )
    return server_url, normalize_runtime_version(settings.recommended_runtime_version)


def _authenticate(token: str | None, prefix: str, purpose: str) -> int:
    try:
        if token is None or not token.startswith(prefix):
            raise ValueError("invalid prefix")
        claims = parse_user_purpose(token[len(prefix) :])
        if claims.get("purpose") != purpose:
            raise ValueError("invalid purpose")
        uid = claims.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int):
            raise ValueError("invalid uid")
        return uid
    except Exception as error:
        raise BizError(ErrorCode.UNAUTHORIZED) from error


async def _active_level(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
) -> WorkspaceAccessLevel:
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == tenant_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    if member is None:
        raise BizError(ErrorCode.NO_PERMISSION)
    if member.status != 0:
        raise BizError(ErrorCode.NO_PERMISSION)
    if member.is_deleted != 0:
        raise BizError(ErrorCode.NO_PERMISSION)
    try:
        return WorkspaceAccessLevel[member.access_level]
    except KeyError as error:
        raise BizError(ErrorCode.NO_PERMISSION) from error


def _expires_at() -> str:
    epoch = int(datetime.now(UTC).timestamp()) + TOKEN_TTL_SECONDS
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _posix_upload(token: str, workitem_id: int, server_url: str, runtime_version: str) -> str:
    return (
        "export "
        + UPLOAD_ENV
        + "="
        + posix_quote(token)
        + "\n\n"
        + "npx -y autowonder@"
        + runtime_version
        + " workitem upload \\\n"
        + "  --server-url "
        + posix_quote(server_url)
        + " \\\n"
        + "  --workitem-id "
        + str(workitem_id)
        + " \\\n"
        + "  --file <filepath-1> \\\n"
        + "  --file <filepath-2> \\\n"
        + "  --file <images-1> \\\n"
        + "  --json"
    )


def _powershell_upload(token: str, workitem_id: int, server_url: str, runtime_version: str) -> str:
    return (
        "$env:"
        + UPLOAD_ENV
        + "="
        + powershell_quote(token)
        + "\n\n"
        + "npx -y autowonder@"
        + runtime_version
        + " workitem upload `\n"
        + "  --server-url "
        + powershell_quote(server_url)
        + " `\n"
        + "  --workitem-id "
        + str(workitem_id)
        + " `\n"
        + "  --file <filepath-1> `\n"
        + "  --file <filepath-2> `\n"
        + "  --file <images-1> `\n"
        + "  --json"
    )


def _posix_download(token: str, workitem_id: int, server_url: str, runtime_version: str) -> str:
    return (
        "export "
        + DOWNLOAD_ENV
        + "="
        + posix_quote(token)
        + "\n\n"
        + "npx -y autowonder@"
        + runtime_version
        + " workitem download \\\n"
        + "  --server-url "
        + posix_quote(server_url)
        + " \\\n"
        + "  --workitem-id "
        + str(workitem_id)
        + " \\\n"
        + "  --file <name-or-id> \\\n"
        + "  --output-dir <dir> \\\n"
        + "  --json"
    )


def _powershell_download(
    token: str,
    workitem_id: int,
    server_url: str,
    runtime_version: str,
) -> str:
    return (
        "$env:"
        + DOWNLOAD_ENV
        + "="
        + powershell_quote(token)
        + "\n\n"
        + "npx -y autowonder@"
        + runtime_version
        + " workitem download `\n"
        + "  --server-url "
        + powershell_quote(server_url)
        + " `\n"
        + "  --workitem-id "
        + str(workitem_id)
        + " `\n"
        + "  --file <name-or-id> `\n"
        + "  --output-dir <dir> `\n"
        + "  --json"
    )


def _java_trim(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start += 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end -= 1
    return value[start:end]
