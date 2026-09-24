"""飞书应用绑定和回调收件。"""

import json
import logging
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.config import get_settings
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.core.schema import ApiModel
from autowonder.db.session import get_session
from autowonder.im.providers import require_selected
from autowonder.integrations.feishu_security import (
    FeishuSecrets,
    SecurityError,
    verify_callback,
)
from autowonder.integrations.models import FeishuMessageInbox, FeishuRobotBinding
from autowonder.platform.branding import _current, effective_public_base_url
from autowonder.security.crypto import AesGcmSecretCrypto

logger = logging.getLogger(__name__)

_APP_ID = re.compile(r"^cli_[A-Za-z0-9]+$")
_MESSAGE_ID = re.compile(r"^om_[A-Za-z0-9]{1,120}$")
_CHAT_ID = re.compile(r"^oc_[A-Za-z0-9]{1,100}$")

router = APIRouter(
    prefix="/api/integrations/feishu/bindings",
    tags=["feishu-bindings"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "管理飞书绑定"))],
)
callback_router = APIRouter(tags=["feishu-callback"])


class FeishuBindingRequest(ApiModel):
    """保存飞书应用。密钥留空表示沿用已保存的值。"""

    app_id: str | None = None
    app_secret: str | None = None
    verification_token: str | None = None
    encrypt_key: str | None = None
    clear_encrypt_key: bool = False
    agent_id: int | None = None
    status: str | None = None
    version: int | None = None


class FeishuBindingView(ApiModel):
    """绑定展示。不回传密钥明文，只说明 Encrypt Key 是否已配置。"""

    id: int | None = None
    app_id: str | None = None
    agent_id: int | None = None
    status: str | None = None
    version: int | None = None
    encrypt_key_configured: bool = False
    callback_url: str | None = None
    last_success_at: Any = None
    last_error: str | None = None


def _crypto() -> AesGcmSecretCrypto:
    return AesGcmSecretCrypto(get_settings().secret_master_key)


async def list_bindings(session: AsyncSession, tenant_id: int) -> list[FeishuRobotBinding]:
    """列出当前工作空间的飞书绑定。"""
    rows = await session.scalars(
        select(FeishuRobotBinding)
        .where(FeishuRobotBinding.tenant_id == tenant_id)
        .order_by(FeishuRobotBinding.id.asc())
    )
    return list(rows.all())


async def get_binding(
    session: AsyncSession,
    tenant_id: int,
    binding_id: int,
) -> FeishuRobotBinding:
    """读取绑定。不存在是业务 10404。"""
    row = await session.scalar(
        select(FeishuRobotBinding)
        .where(FeishuRobotBinding.tenant_id == tenant_id, FeishuRobotBinding.id == binding_id)
        .limit(1)
    )
    if row is None:
        raise BizError(ErrorCode.NOT_FOUND, "飞书绑定不存在")
    return row


async def save_binding(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    binding_id: int | None,
    request: FeishuBindingRequest,
) -> FeishuRobotBinding:
    """新建或按版本更新。App ID 创建后不能改。"""
    if request.agent_id is None:
        raise BizError(ErrorCode.PARAM_INVALID, "请选择数字人")
    agent = await session.get(Agent, request.agent_id)
    if agent is None or agent.tenant_id != tenant_id or agent.is_deleted == 1:
        raise BizError(ErrorCode.PARAM_INVALID, "数字人不属于当前项目或已删除")
    if binding_id is None:
        row = FeishuRobotBinding()
    else:
        row = await get_binding(session, tenant_id, binding_id)
    app_id = _required(request.app_id, "App ID", 128)
    if _APP_ID.fullmatch(app_id) is None:
        raise BizError(ErrorCode.PARAM_INVALID, "App ID 格式不正确")
    if binding_id is not None and app_id != row.app_id:
        raise BizError(ErrorCode.PARAM_INVALID, "App ID 不支持修改，请新建绑定")
    if request.status is None:
        status = "ENABLED" if binding_id is None else row.status
    else:
        status = request.status
    if status not in {"ENABLED", "DISABLED"}:
        raise BizError(ErrorCode.PARAM_INVALID, "绑定状态不正确")
    old = FeishuSecrets(None, None, None) if binding_id is None else secrets_of(row)
    if request.clear_encrypt_key:
        encrypt_key = None
    else:
        encrypt_key = _select(request.encrypt_key, old.encrypt_key)
    if encrypt_key is not None and len(encrypt_key) > 512:
        raise BizError(ErrorCode.PARAM_INVALID, "Encrypt Key 过长")
    stored = FeishuSecrets(
        _required(_select(request.app_secret, old.app_secret), "App Secret", 512),
        _required(
            _select(request.verification_token, old.verification_token),
            "Verification Token",
            512,
        ),
        encrypt_key,
    )
    row.app_id = app_id
    row.tenant_id = tenant_id
    row.agent_id = request.agent_id
    row.status = status
    row.modifier_id = user_id
    row.credential_ref = _crypto().encrypt(stored.to_json())
    if binding_id is None:
        row.creator_id = user_id
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as error:
            raise BizError(ErrorCode.CONFLICT, "该飞书应用已绑定，请使用其他应用") from error
    else:
        if request.version is None or request.version != row.version:
            raise BizError(ErrorCode.CONFLICT, "绑定已被修改，请刷新后重试")
        row.version = row.version + 1
        await session.flush()
    return row


async def delete_binding(session: AsyncSession, tenant_id: int, binding_id: int) -> None:
    """确认存在后删除。"""
    await get_binding(session, tenant_id, binding_id)
    await session.execute(
        delete(FeishuRobotBinding).where(
            FeishuRobotBinding.tenant_id == tenant_id,
            FeishuRobotBinding.id == binding_id,
        )
    )


def secrets_of(row: FeishuRobotBinding) -> FeishuSecrets:
    """解密凭据。解密失败是状态错误，不改写成业务码。"""
    try:
        return FeishuSecrets.from_json(_crypto().decrypt(row.credential_ref))
    except Exception as error:
        raise RuntimeError("无法读取飞书凭据") from error


async def accept_inbox(
    session: AsyncSession,
    binding: FeishuRobotBinding,
    event: dict[str, object],
) -> None:
    """只收用户文本消息。重复 message_id 按幂等成功。"""
    message = event.get("message")
    sender = event.get("sender")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return
    if sender.get("sender_type") != "user" or message.get("message_type") != "text":
        return
    chat_type = str(message.get("chat_type") or "")
    if chat_type not in {"group", "p2p"}:
        return
    message_id = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if _MESSAGE_ID.fullmatch(message_id) is None or _CHAT_ID.fullmatch(chat_id) is None:
        return
    thread_id = str(message.get("thread_id") or "")
    if len(thread_id) > 100:
        return
    session.add(
        FeishuMessageInbox(
            binding_id=binding.id,
            tenant_id=binding.tenant_id,
            agent_id=binding.agent_id,
            message_id=message_id,
            payload=json.dumps(event, ensure_ascii=False),
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()


def _select(next_value: str | None, old: str | None) -> str | None:
    if next_value is not None and next_value.strip() != "":
        return next_value.strip()
    return old


def _required(value: str | None, name: str, limit: int) -> str:
    if value is None or value.strip() == "" or len(value) > limit:
        raise BizError(ErrorCode.PARAM_INVALID, "请填写有效的 " + name)
    return value.strip()


async def _view(session: AsyncSession, row: FeishuRobotBinding) -> FeishuBindingView:
    key = secrets_of(row).encrypt_key
    branding = await _current(session)
    base = effective_public_base_url(branding.domain, get_settings().public_base_url)
    return FeishuBindingView(
        id=row.id,
        app_id=row.app_id,
        agent_id=row.agent_id,
        status=row.status,
        version=row.version,
        encrypt_key_configured=key is not None and key.strip() != "",
        callback_url=base + "/api/integrations/feishu/callback?bindingId=" + str(row.id),
        last_success_at=row.last_success_at,
        last_error=row.last_error,
    )


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
    """列出飞书绑定。"""
    rows = await list_bindings(session, _workspace_id())
    return ok([await _view(session, row) for row in rows])


@router.post("")
async def create_route(
    body: FeishuBindingRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建飞书绑定。"""
    await require_selected(session, "FEISHU")
    row = await save_binding(session, _workspace_id(), _user_id(), None, body)
    await session.commit()
    return ok(await _view(session, row))


@router.put("/{id}")
async def update_route(
    id: int,
    body: FeishuBindingRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按版本更新飞书绑定。"""
    await require_selected(session, "FEISHU")
    row = await save_binding(session, _workspace_id(), _user_id(), id, body)
    await session.commit()
    return ok(await _view(session, row))


@router.delete("/{id}")
async def delete_route(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除飞书绑定。"""
    await delete_binding(session, _workspace_id(), id)
    await session.commit()
    return ok(None)


@callback_router.post("/api/integrations/feishu/callback")
async def feishu_callback(
    request: Request,
    binding_id: Annotated[int, Query(alias="bindingId")],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """飞书事件回调。验签失败 401，绑定不存在 404，不走 Result 信封。"""
    binding = await session.get(FeishuRobotBinding, binding_id)
    if binding is None:
        return Response(status_code=404)
    body = (await request.body()).decode()
    try:
        event = verify_callback(
            body,
            secrets_of(binding),
            request.headers.get("X-Lark-Request-Timestamp"),
            request.headers.get("X-Lark-Request-Nonce"),
            request.headers.get("X-Lark-Signature"),
            int(time.time()),
        )
    except SecurityError:
        return Response(status_code=401)
    if event.get("type") == "url_verification":
        challenge = event.get("challenge")
        text = "" if challenge is None else str(challenge)
        if text.strip() == "":
            return Response(status_code=400)
        return JSONResponse({"challenge": text})
    header = event.get("header")
    app_id = ""
    event_type = ""
    if isinstance(header, dict):
        app_id = "" if header.get("app_id") is None else str(header.get("app_id"))
        event_type = "" if header.get("event_type") is None else str(header.get("event_type"))
    if binding.app_id != app_id:
        return Response(status_code=403)
    nested = event.get("event")
    inbound = binding.status == "ENABLED" and event_type == "im.message.receive_v1"
    if inbound and isinstance(nested, dict):
        await accept_inbox(session, binding, nested)
        await session.commit()
    return JSONResponse({"code": 0})
