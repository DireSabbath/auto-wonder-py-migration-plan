"""环境变量库。明文只加密落库，列表和审计都不带出原值。"""

from dataclasses import dataclass

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentEnvironmentVariableRef, AgentVersion
from autowonder.audits.service import AuditRecord, record_required
from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.environments.models import EnvironmentVariable
from autowonder.environments.names import normalize_description, validate_name
from autowonder.environments.schemas import (
    CreateEnvironmentVariableRequest,
    EnvironmentVariableValueView,
    EnvironmentVariableView,
    UpdateEnvironmentVariableRequest,
)
from autowonder.security.crypto import AesGcmSecretCrypto

_MASK = "**"


@dataclass
class VariableUse:
    """一条仍挂在在线版本或编辑草稿上的引用。"""

    agent_id: int | None
    agent_name: str | None
    version_no: int | None
    ref_type: str | None


def describe_references(references: list[VariableUse]) -> str:
    """删除被引用的环境变量时，列出数字员工和版本。"""
    parts: list[str] = []
    for ref in references:
        name = "数字员工"
        if ref.agent_name is not None and ref.agent_name.strip() != "":
            name = ref.agent_name
        kind = "编辑草稿"
        if ref.ref_type == "ONLINE":
            kind = "在线版本"
        version = ""
        if ref.version_no is not None:
            version = " v" + str(ref.version_no)
        parts.append(f"{name}(#{ref.agent_id}) {kind}{version}")
    detail = "；".join(parts)
    return (
        "环境变量仍被数字员工引用,无法删除:" + detail + "。请先在对应草稿解除挂载并发布后再删除。"
    )


def require_update_flag(update_value: bool | None) -> bool:
    """更新时必须显式说明要不要改值。"""
    if update_value is None:
        raise BizError(ErrorCode.PARAM_INVALID, "updateValue 参数必须显式提供")
    return update_value


async def list_variables(session: AsyncSession, tenant_id: int) -> list[EnvironmentVariableView]:
    """按名称列出仍有效的环境变量，值一律脱敏。"""
    rows = await session.scalars(
        select(EnvironmentVariable)
        .where(EnvironmentVariable.tenant_id == tenant_id, EnvironmentVariable.is_deleted == 0)
        .order_by(EnvironmentVariable.name.asc(), EnvironmentVariable.id.asc())
    )
    return [_to_view(variable) for variable in rows]


async def create_variable(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    request: CreateEnvironmentVariableRequest | None,
) -> EnvironmentVariableView:
    """加密后写入。同名（忽略大小写）拒绝。"""
    if request is None or request.value is None:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_VALUE_REQUIRED)
    name = validate_name(request.name)
    await _ensure_name_available(session, tenant_id, name, None)
    variable = EnvironmentVariable(
        tenant_id=tenant_id,
        name=name,
        credential_ref=_crypto().encrypt(request.value),
        description=normalize_description(request.description),
        creator_id=user_id,
        modifier_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(variable)
    try:
        await session.flush()
    except IntegrityError as error:
        if _duplicate_key(error):
            raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NAME_CONFLICT) from error
        raise
    persisted = await _reload(session, tenant_id, variable.id)
    await _audit(session, tenant_id, user_id, "CREATE", persisted)
    await session.commit()
    return _to_view(persisted)


async def update_variable(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    variable_id: int,
    request: UpdateEnvironmentVariableRequest | None,
) -> EnvironmentVariableView:
    """updateValue 为真时重写密文，否则只改名称和说明。"""
    current = await _require_active(session, tenant_id, variable_id)
    if request is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    update_value = require_update_flag(request.update_value)
    name = validate_name(request.name)
    await _ensure_name_available(session, tenant_id, name, variable_id)
    description = normalize_description(request.description)
    try:
        if update_value:
            if request.value is None:
                raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_VALUE_REQUIRED)
            updated = await _update_with_value(
                session,
                tenant_id,
                variable_id,
                name,
                description,
                _crypto().encrypt(request.value),
                user_id,
                current.version,
            )
        else:
            updated = await _update_metadata(
                session,
                tenant_id,
                variable_id,
                name,
                description,
                user_id,
                current.version,
            )
    except IntegrityError as error:
        if _duplicate_key(error):
            raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NAME_CONFLICT) from error
        raise
    if updated == 0:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_VERSION_CONFLICT)
    persisted = await _reload(session, tenant_id, variable_id)
    await _audit(session, tenant_id, user_id, "UPDATE", persisted)
    await session.commit()
    return _to_view(persisted)


async def reveal_variable(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    variable_id: int,
) -> EnvironmentVariableValueView:
    """解密明文，并记下一次查看审计。"""
    variable = await _require_active(session, tenant_id, variable_id)
    value = _crypto().decrypt(variable.credential_ref)
    await _audit(session, tenant_id, user_id, "REVEAL", variable)
    await session.commit()
    return EnvironmentVariableValueView(value=value)


async def delete_variable(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    variable_id: int,
) -> None:
    """仍被在线版本或编辑草稿挂载时不能删。删除把 is_deleted 写成自身 id。"""
    variable = await _lock_active(session, tenant_id, variable_id)
    if variable is None:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NOT_FOUND)
    references = await _list_references(session, tenant_id, variable_id)
    if len(references) > 0:
        raise BizError(
            ErrorCode.ENVIRONMENT_VARIABLE_DELETE_IN_USE,
            describe_references(references),
        )
    deleted = rowcount(
        await session.execute(
            update(EnvironmentVariable)
            .where(
                EnvironmentVariable.tenant_id == tenant_id,
                EnvironmentVariable.id == variable_id,
                EnvironmentVariable.is_deleted == 0,
                EnvironmentVariable.version == variable.version,
            )
            .values(
                is_deleted=EnvironmentVariable.id,
                modifier_id=user_id,
                version=EnvironmentVariable.version + 1,
            )
        )
    )
    if deleted == 0:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_VERSION_CONFLICT)
    await _audit(session, tenant_id, user_id, "DELETE", variable)
    await session.commit()


def _to_view(variable: EnvironmentVariable) -> EnvironmentVariableView:
    return EnvironmentVariableView(
        id=variable.id,
        name=variable.name,
        value=_MASK,
        description=variable.description,
        gmt_create=variable.gmt_create,
        gmt_modified=variable.gmt_modified,
        version=variable.version,
    )


def _crypto() -> AesGcmSecretCrypto:
    return AesGcmSecretCrypto(get_settings().secret_master_key)


async def _require_active(
    session: AsyncSession,
    tenant_id: int,
    variable_id: int,
) -> EnvironmentVariable:
    variable = await _find_active(session, tenant_id, variable_id)
    if variable is None:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NOT_FOUND)
    return variable


async def _reload(
    session: AsyncSession,
    tenant_id: int,
    variable_id: int,
) -> EnvironmentVariable:
    session.expire_all()
    return await _require_active(session, tenant_id, variable_id)


async def _find_active(
    session: AsyncSession,
    tenant_id: int,
    variable_id: int,
) -> EnvironmentVariable | None:
    return await session.scalar(
        select(EnvironmentVariable)
        .where(
            EnvironmentVariable.tenant_id == tenant_id,
            EnvironmentVariable.id == variable_id,
            EnvironmentVariable.is_deleted == 0,
        )
        .limit(1)
    )


async def _lock_active(
    session: AsyncSession,
    tenant_id: int,
    variable_id: int,
) -> EnvironmentVariable | None:
    return await session.scalar(
        select(EnvironmentVariable)
        .where(
            EnvironmentVariable.tenant_id == tenant_id,
            EnvironmentVariable.id == variable_id,
            EnvironmentVariable.is_deleted == 0,
        )
        .limit(1)
        .with_for_update()
    )


async def _ensure_name_available(
    session: AsyncSession,
    tenant_id: int,
    name: str,
    current_id: int | None,
) -> None:
    conflict = await session.scalar(
        select(EnvironmentVariable)
        .where(
            EnvironmentVariable.tenant_id == tenant_id,
            func.lower(EnvironmentVariable.name) == name.lower(),
            EnvironmentVariable.is_deleted == 0,
        )
        .limit(1)
    )
    if conflict is not None and conflict.id != current_id:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NAME_CONFLICT)


async def _update_metadata(
    session: AsyncSession,
    tenant_id: int,
    variable_id: int,
    name: str,
    description: str | None,
    user_id: int,
    version: int,
) -> int:
    return rowcount(
        await session.execute(
            update(EnvironmentVariable)
            .where(
                EnvironmentVariable.tenant_id == tenant_id,
                EnvironmentVariable.id == variable_id,
                EnvironmentVariable.is_deleted == 0,
                EnvironmentVariable.version == version,
            )
            .values(
                name=name,
                description=description,
                modifier_id=user_id,
                version=EnvironmentVariable.version + 1,
            )
        )
    )


async def _update_with_value(
    session: AsyncSession,
    tenant_id: int,
    variable_id: int,
    name: str,
    description: str | None,
    credential_ref: str,
    user_id: int,
    version: int,
) -> int:
    return rowcount(
        await session.execute(
            update(EnvironmentVariable)
            .where(
                EnvironmentVariable.tenant_id == tenant_id,
                EnvironmentVariable.id == variable_id,
                EnvironmentVariable.is_deleted == 0,
                EnvironmentVariable.version == version,
            )
            .values(
                name=name,
                description=description,
                credential_ref=credential_ref,
                modifier_id=user_id,
                version=EnvironmentVariable.version + 1,
            )
        )
    )


async def _list_references(
    session: AsyncSession,
    tenant_id: int,
    variable_id: int,
) -> list[VariableUse]:
    ref_type = case(
        (Agent.online_version_id == AgentVersion.id, "ONLINE"),
        else_="EDITING",
    ).label("ref_type")
    rows = await session.execute(
        select(Agent.id, Agent.name, AgentVersion.version_no, ref_type)
        .select_from(AgentEnvironmentVariableRef)
        .join(
            AgentVersion,
            and_(
                AgentVersion.id == AgentEnvironmentVariableRef.agent_version_id,
                AgentVersion.tenant_id == tenant_id,
            ),
        )
        .join(
            Agent,
            and_(Agent.id == AgentVersion.agent_id, Agent.tenant_id == tenant_id),
        )
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.environment_variable_id == variable_id,
            Agent.is_deleted == 0,
            AgentVersion.is_deleted == 0,
            or_(
                Agent.online_version_id == AgentVersion.id,
                Agent.editing_version_id == AgentVersion.id,
            ),
        )
        .order_by(Agent.id.asc(), AgentVersion.version_no.asc())
    )
    return [
        VariableUse(
            agent_id=row.id,
            agent_name=row.name,
            version_no=row.version_no,
            ref_type=row.ref_type,
        )
        for row in rows
    ]


async def _audit(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    action: str,
    variable: EnvironmentVariable,
) -> None:
    record = AuditRecord(
        tenant_id=tenant_id,
        actor_id=user_id,
        actor_type="HUMAN",
        module="ENVIRONMENT_VARIABLE",
        action=action,
        target_type="ENVIRONMENT_VARIABLE",
        target_id=variable.id,
        trigger_type="EVENT",
        trigger_source="WEB",
        event_type="ENVIRONMENT_VARIABLE_LIBRARY",
    )
    record.add("name", variable.name)
    await record_required(session, record)


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None or not origin.args:
        return False
    return origin.args[0] == 1062
