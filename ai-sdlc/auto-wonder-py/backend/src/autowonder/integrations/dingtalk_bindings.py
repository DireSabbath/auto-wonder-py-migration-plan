"""钉钉机器人绑定。密钥只加密保存，接口回固定掩码。"""

import base64
import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.config import get_settings
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError
from autowonder.core.redis import redis_client
from autowonder.core.result import ok
from autowonder.core.schema import ApiModel
from autowonder.db.session import get_session
from autowonder.im.providers import require_selected
from autowonder.integrations.models import DingtalkRobotBinding
from autowonder.platform.branding import _current, effective_public_base_url
from autowonder.security.crypto import AesGcmSecretCrypto

logger = logging.getLogger(__name__)

_DEFAULT_TRANSPORT = "STREAM"
_DEFAULT_STREAM_ENV = "ONLINE"
_DEFAULT_STATUS = "ENABLED"
_STATUSES = frozenset({"ENABLED", "DISABLED"})
_STREAM_TTL = 7 * 24 * 3600

router = APIRouter(
    prefix="/api/integrations/dingtalk/bindings",
    tags=["dingtalk-bindings"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "管理钉钉绑定"))],
)


class BindingUpsertRequest(ApiModel):
    """创建或更新钉钉绑定。编辑时密钥留空表示不改。"""

    app_key: str | None = None
    app_secret: str | None = None
    robot_code: str | None = None
    agent_id: int | None = None
    transport_mode: str | None = None
    stream_env: str | None = None
    callback_token: str | None = None
    base_url: str | None = None
    region_id: str | None = None
    status: str | None = None


class BindingView(ApiModel):
    """绑定展示。``appSecretMasked`` 固定为掩码。"""

    id: int | None = None
    app_key: str | None = None
    app_secret_masked: str | None = None
    robot_code: str | None = None
    agent_id: int | None = None
    transport_mode: str | None = None
    stream_env: str | None = None
    stream_status: str | None = None
    stream_error: str | None = None
    stream_status_updated_at: int | None = None
    base_url: str | None = None
    region_id: str | None = None
    status: str | None = None
    last_success_at: Any = None
    last_error: str | None = None
    callback_url: str | None = None


def _crypto() -> AesGcmSecretCrypto:
    return AesGcmSecretCrypto(get_settings().secret_master_key)


async def create_binding(
    session: AsyncSession,
    tenant_id: int,
    operator_id: int,
    request: BindingUpsertRequest,
) -> DingtalkRobotBinding:
    """新建绑定。robotCode 全局唯一。"""
    existing = await _by_robot(session, request.robot_code or "")
    if existing is not None:
        raise IllegalArgumentError("robotCode already bound: " + (request.robot_code or ""))
    row = DingtalkRobotBinding(
        tenant_id=tenant_id,
        app_key=request.app_key or "",
        credential_ref=_crypto().encrypt(request.app_secret or ""),
        robot_code=request.robot_code or "",
        agent_id=request.agent_id or 0,
        transport_mode=_default_transport(request.transport_mode),
        stream_env=_stream_env_or_default(request.stream_env),
        callback_token=_default_token(request.callback_token),
        base_url=request.base_url,
        region_id=request.region_id,
        status=_status_or_default(request.status),
        creator_id=operator_id,
        modifier_id=operator_id,
    )
    session.add(row)
    await session.flush()
    return row


async def update_binding(
    session: AsyncSession,
    tenant_id: int,
    operator_id: int,
    binding_id: int,
    request: BindingUpsertRequest,
) -> DingtalkRobotBinding:
    """更新绑定。空白密钥和回调令牌保留原值。"""
    existing = await get_binding(session, tenant_id, binding_id)
    if existing is None:
        raise IllegalArgumentError("binding not found: " + str(binding_id))
    conflict = await _by_robot(session, request.robot_code or "")
    if conflict is not None and conflict.id != binding_id:
        raise IllegalArgumentError("robotCode already bound: " + (request.robot_code or ""))
    existing.app_key = request.app_key or ""
    if request.app_secret is not None and request.app_secret != "":
        existing.credential_ref = _crypto().encrypt(request.app_secret)
    existing.robot_code = request.robot_code or ""
    existing.agent_id = request.agent_id or 0
    if request.transport_mode is not None and request.transport_mode.strip() != "":
        existing.transport_mode = request.transport_mode
    existing.stream_env = _stream_env_for_update(request.stream_env)
    if request.callback_token is not None and request.callback_token.strip() != "":
        existing.callback_token = request.callback_token
    existing.base_url = request.base_url
    existing.region_id = request.region_id
    existing.status = existing.status if request.status is None else request.status
    existing.modifier_id = operator_id
    await session.flush()
    return existing


async def list_bindings(session: AsyncSession, tenant_id: int) -> list[DingtalkRobotBinding]:
    """列出当前工作空间未删除的绑定。"""
    rows = await session.scalars(
        select(DingtalkRobotBinding)
        .where(
            DingtalkRobotBinding.tenant_id == tenant_id,
            DingtalkRobotBinding.is_deleted == 0,
        )
        .order_by(DingtalkRobotBinding.id.asc())
    )
    return list(rows.all())


async def get_binding(
    session: AsyncSession,
    tenant_id: int,
    binding_id: int,
) -> DingtalkRobotBinding | None:
    """按工作空间读取绑定。"""
    return await session.scalar(
        select(DingtalkRobotBinding)
        .where(
            DingtalkRobotBinding.tenant_id == tenant_id,
            DingtalkRobotBinding.id == binding_id,
            DingtalkRobotBinding.is_deleted == 0,
        )
        .limit(1)
    )


async def delete_binding(session: AsyncSession, tenant_id: int, binding_id: int) -> None:
    """硬删除，避免 robotCode 被软删除行永久占用。"""
    await session.execute(
        delete(DingtalkRobotBinding).where(
            DingtalkRobotBinding.tenant_id == tenant_id,
            DingtalkRobotBinding.id == binding_id,
        )
    )


async def apply_stream_status(binding_id: int | None, view: BindingView) -> None:
    """把 Redis 里的 Stream 状态填进视图。读失败按未连接。"""
    status = await _stream_status(binding_id)
    view.stream_status = status["status"]
    view.stream_error = status["error"]
    view.stream_status_updated_at = status["updatedAt"]


async def start_stream_if_eligible(row: DingtalkRobotBinding) -> None:
    """启用中的 STREAM 绑定尝试拉起长连接。失败只记日志。"""
    if not _enabled_stream(row):
        return
    try:
        await _start_stream(row)
    except Exception:
        logger.warning(
            "failed to start DingTalk Stream bindingId=%s appKey=%s",
            row.id,
            row.app_key,
            exc_info=True,
        )


async def stop_stream(row: DingtalkRobotBinding | None) -> None:
    """停掉旧连接。没有旧行或停失败都不影响接口成功。"""
    if row is None:
        return
    try:
        await _stop_stream(row)
    except Exception:
        logger.warning(
            "failed to stop DingTalk Stream bindingId=%s appKey=%s",
            row.id,
            row.app_key,
            exc_info=True,
        )


def _enabled_stream(row: DingtalkRobotBinding | None) -> bool:
    return row is not None and row.status == "ENABLED" and row.transport_mode == "STREAM"


def _default_transport(transport_mode: str | None) -> str:
    if transport_mode is None or transport_mode.strip() == "":
        return _DEFAULT_TRANSPORT
    return transport_mode


def _stream_env_or_default(stream_env: str | None) -> str:
    if stream_env is None or stream_env.strip() == "":
        return _DEFAULT_STREAM_ENV
    normalized = stream_env.strip().upper()
    if normalized != _DEFAULT_STREAM_ENV:
        raise IllegalArgumentError("unsupported DingTalk Stream env: " + stream_env)
    return _DEFAULT_STREAM_ENV


def _stream_env_for_update(requested: str | None) -> str:
    if requested is None or requested.strip() == "":
        return _DEFAULT_STREAM_ENV
    return _stream_env_or_default(requested)


def _status_or_default(status: str | None) -> str:
    if status is None or status.strip() == "":
        return _DEFAULT_STATUS
    normalized = status.strip().upper()
    if normalized not in _STATUSES:
        raise IllegalArgumentError("unsupported DingTalk binding status: " + status)
    return normalized


def _default_token(callback_token: str | None) -> str:
    if callback_token is not None and callback_token.strip() != "":
        return callback_token
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


async def _by_robot(session: AsyncSession, robot_code: str) -> DingtalkRobotBinding | None:
    return await session.scalar(
        select(DingtalkRobotBinding)
        .where(
            DingtalkRobotBinding.robot_code == robot_code,
            DingtalkRobotBinding.is_deleted == 0,
        )
        .limit(1)
    )


async def _stream_status(binding_id: int | None) -> dict[str, Any]:
    if binding_id is None:
        return {"status": "NOT_CONNECTED", "error": None, "updatedAt": None}
    try:
        raw = await redis_client().get(_stream_key(binding_id))
    except Exception:
        logger.warning("failed to read DingTalk Stream status bindingId=%s", binding_id)
        return {"status": "NOT_CONNECTED", "error": None, "updatedAt": None}
    if raw is None or str(raw).strip() == "":
        return {"status": "NOT_CONNECTED", "error": None, "updatedAt": None}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "NOT_CONNECTED", "error": None, "updatedAt": None}
    status = parsed.get("status") if isinstance(parsed, dict) else None
    if not isinstance(status, str) or status.strip() == "":
        return {"status": "NOT_CONNECTED", "error": None, "updatedAt": None}
    return {
        "status": status,
        "error": parsed.get("error"),
        "updatedAt": parsed.get("updatedAt"),
    }


async def _write_stream(binding_id: int, status: str, error: str | None) -> None:
    payload = json.dumps(
        {"status": status, "error": error, "updatedAt": int(time.time() * 1000)},
        separators=(",", ":"),
    )
    await redis_client().set(_stream_key(binding_id), payload, ex=_STREAM_TTL)


def _stream_key(binding_id: int) -> str:
    return "dingtalk:stream:status:" + str(binding_id)


async def _start_stream(row: DingtalkRobotBinding) -> None:
    from autowonder.integrations.dingtalk.stream import ensure_started

    await ensure_started(row, _write_stream)


async def _stop_stream(row: DingtalkRobotBinding) -> None:
    from autowonder.integrations.dingtalk.stream import ensure_stopped

    await ensure_stopped(row, _write_stream)


async def _view(session: AsyncSession, row: DingtalkRobotBinding) -> BindingView:
    branding = await _current(session)
    base = effective_public_base_url(branding.domain, get_settings().public_base_url)
    token = "" if row.callback_token is None else row.callback_token
    view = BindingView(
        id=row.id,
        app_key=row.app_key,
        app_secret_masked="****",
        robot_code=row.robot_code,
        agent_id=row.agent_id,
        transport_mode=row.transport_mode,
        stream_env=row.stream_env,
        base_url=row.base_url,
        region_id=row.region_id,
        status=row.status,
        last_success_at=row.last_success_at,
        last_error=row.last_error,
        callback_url=base + "/api/integrations/dingtalk/callback?token=" + token,
    )
    await apply_stream_status(row.id, view)
    return view


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


@router.get("")
async def list_route(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出钉钉绑定。"""
    rows = await list_bindings(session, _workspace_id())
    return ok([await _view(session, row) for row in rows])


@router.get("/{id}")
async def get_route(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取一条钉钉绑定。"""
    row = await get_binding(session, _workspace_id(), id)
    if row is None:
        raise IllegalArgumentError("binding not found: " + str(id))
    return ok(await _view(session, row))


@router.post("")
async def create_route(
    body: BindingUpsertRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建钉钉绑定，并在 STREAM 模式下尝试拉起连接。"""
    await require_selected(session, "DINGTALK")
    row = await create_binding(session, _workspace_id(), _user_id(), body)
    await session.commit()
    await start_stream_if_eligible(row)
    return ok(await _view(session, row))


@router.put("/{id}")
async def update_route(
    id: int,
    body: BindingUpsertRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新绑定。先停旧连接，再按新配置决定是否拉起。"""
    await require_selected(session, "DINGTALK")
    tenant_id = _workspace_id()
    old = await get_binding(session, tenant_id, id)
    row = await update_binding(session, tenant_id, _user_id(), id, body)
    await session.commit()
    await stop_stream(old)
    await start_stream_if_eligible(row)
    return ok(await _view(session, row))


@router.delete("/{id}")
async def delete_route(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除绑定并停掉 Stream。"""
    tenant_id = _workspace_id()
    old = await get_binding(session, tenant_id, id)
    await delete_binding(session, tenant_id, id)
    await session.commit()
    await stop_stream(old)
    return ok(None)
