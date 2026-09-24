"""把一个冻结版本上的环境变量解析成短时快照。名称按字典序，明文只留在返回值里。"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy import Select, and_, case, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import AgentEnvironmentVariableRef
from autowonder.config import get_settings
from autowonder.environments.models import EnvironmentVariable
from autowonder.environments.names import is_reserved
from autowonder.security.crypto import AesGcmSecretCrypto

_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DECRYPT_FAILED = "Agent environment variable decryption failed"
Decrypt = Callable[[str], str | None]


class EnvironmentSnapshotResolutionException(Exception):
    """快照无法交给执行器。消息不含变量名、密文或明文。"""


@dataclass
class SnapshotRow:
    """一次连接查询里的引用和变量列。左连接缺失时变量列为 None。"""

    ref_tenant_id: int | None
    agent_version_id: int | None
    environment_variable_id: int | None
    variable_id: int | None
    variable_tenant_id: int | None
    name: str | None
    credential_ref: str | None


def resolution_snapshot_statement(tenant_id: int, agent_version_id: int) -> Select[Any]:
    """一条左连接取出引用和仍有效的变量。缺失变量排在前面，便于先拒绝。"""
    return (
        select(
            AgentEnvironmentVariableRef.tenant_id,
            AgentEnvironmentVariableRef.agent_version_id,
            AgentEnvironmentVariableRef.environment_variable_id,
            EnvironmentVariable.id,
            EnvironmentVariable.tenant_id,
            EnvironmentVariable.name,
            EnvironmentVariable.credential_ref,
        )
        .select_from(AgentEnvironmentVariableRef)
        .outerjoin(
            EnvironmentVariable,
            and_(
                EnvironmentVariable.id == AgentEnvironmentVariableRef.environment_variable_id,
                EnvironmentVariable.tenant_id == tenant_id,
                EnvironmentVariable.is_deleted == 0,
            ),
        )
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == agent_version_id,
        )
        .order_by(
            case((EnvironmentVariable.id.is_(None), 0), else_=1),
            EnvironmentVariable.name,
            AgentEnvironmentVariableRef.environment_variable_id,
            AgentEnvironmentVariableRef.id,
        )
    )


def resolve_rows(
    tenant_id: int,
    agent_version_id: int,
    rows: list[SnapshotRow],
    decrypt: Decrypt,
) -> Mapping[str, str]:
    """校验引用、名称和唯一性后再解密。空引用返回空快照，且不调用解密。"""
    if len(rows) == 0:
        return MappingProxyType({})
    chosen: dict[str, SnapshotRow] = {}
    seen: set[str] = set()
    for row in rows:
        _require_reference(tenant_id, agent_version_id, row)
        name = _require_name(row.name)
        normalized = name.upper()
        if normalized in seen:
            raise EnvironmentSnapshotResolutionException(
                "Agent environment variable names must be unique"
            )
        seen.add(normalized)
        chosen[name] = row
    snapshot: dict[str, str] = {}
    for name in sorted(chosen):
        snapshot[name] = _decrypt_value(decrypt, chosen[name].credential_ref)
    return MappingProxyType(snapshot)


async def resolve_snapshot(
    session: AsyncSession,
    tenant_id: int,
    agent_version_id: int,
    decrypt: Decrypt | None = None,
) -> Mapping[str, str]:
    """读取该版本的连接结果并解析。

    没有引用时返回空快照，不读取主密钥。有引用且省略解密函数时使用配置里的主密钥。
    """
    result = await session.execute(resolution_snapshot_statement(tenant_id, agent_version_id))
    rows = [
        SnapshotRow(
            ref_tenant_id=item[0],
            agent_version_id=item[1],
            environment_variable_id=item[2],
            variable_id=item[3],
            variable_tenant_id=item[4],
            name=item[5],
            credential_ref=item[6],
        )
        for item in result.all()
    ]
    if len(rows) == 0:
        return MappingProxyType({})
    cipher = decrypt
    if cipher is None:
        cipher = _crypto().decrypt
    return resolve_rows(tenant_id, agent_version_id, rows, cipher)


def _require_reference(tenant_id: int, agent_version_id: int, row: SnapshotRow) -> None:
    matched = (
        row.ref_tenant_id == tenant_id
        and row.agent_version_id == agent_version_id
        and row.variable_id is not None
        and row.variable_id == row.environment_variable_id
        and row.variable_tenant_id == tenant_id
    )
    if not matched:
        raise EnvironmentSnapshotResolutionException(
            "Agent environment variable reference is unavailable"
        )


def _require_name(name: str | None) -> str:
    if name is None or len(name) > 128 or _NAME_PATTERN.fullmatch(name) is None:
        raise EnvironmentSnapshotResolutionException("Agent environment variable name is invalid")
    if is_reserved(name):
        raise EnvironmentSnapshotResolutionException("Agent environment variable name is reserved")
    return name


def _decrypt_value(decrypt: Decrypt, credential_ref: str | None) -> str:
    if credential_ref is None:
        raise EnvironmentSnapshotResolutionException(_DECRYPT_FAILED)
    try:
        value = decrypt(credential_ref)
    except Exception:
        raise EnvironmentSnapshotResolutionException(_DECRYPT_FAILED) from None
    if value is None:
        raise EnvironmentSnapshotResolutionException(_DECRYPT_FAILED)
    return value


def _crypto() -> AesGcmSecretCrypto:
    return AesGcmSecretCrypto(get_settings().secret_master_key)
