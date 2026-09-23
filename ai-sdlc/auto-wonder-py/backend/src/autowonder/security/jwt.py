"""PyJWT，claim 名与 jjwt ``JwtService`` 对齐，同一 secret 可互认。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from autowonder.config import get_settings


@dataclass
class TokenPayload:
    """访问令牌里的身份。``workspace_id`` 缺省时不写入 claim。"""

    user_id: int
    workspace_id: int | None
    jti: str | None


@dataclass
class ConversationClaims:
    """会话令牌绑定 agent 与 agentVersion，版本切换后旧令牌失效。"""

    user_id: int
    tenant_id: int
    purpose: str
    conversation_id: int
    agent_id: int
    agent_version_id: int


def _secret() -> str:
    return get_settings().jwt_secret


def _now() -> datetime:
    return datetime.now(UTC)


def sign_access(payload: TokenPayload) -> str:
    """签发 access token。有工作空间时才写入 ``workspace`` claim。"""
    settings = get_settings()
    issued_at = _now()
    body: dict[str, object] = {
        "jti": payload.jti,
        "sub": str(payload.user_id),
        "uid": payload.user_id,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=settings.jwt_access_ttl_seconds),
    }
    if payload.workspace_id is not None:
        body["workspace"] = payload.workspace_id
    return jwt.encode(body, _secret(), algorithm="HS256")


def parse_access(token: str) -> TokenPayload:
    """解析访问令牌。签名或过期失败时由 PyJWT 抛出。"""
    claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    workspace = claims.get("workspace")
    return TokenPayload(
        user_id=int(claims["uid"]),
        workspace_id=None if workspace is None else int(workspace),
        jti=claims.get("jti"),
    )


def sign_scoped(
    user_id: int,
    tenant_id: int,
    purpose: str,
    subject_id: int,
    ttl_seconds: int,
) -> str:
    """调度等场景的范围令牌，带 purpose 与 subjectId。"""
    now = _now()
    body = {
        "sub": str(user_id),
        "uid": user_id,
        "workspace": tenant_id,
        "purpose": purpose,
        "subjectId": subject_id,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(body, _secret(), algorithm="HS256")


def parse_scoped(token: str) -> dict[str, object]:
    """解析范围令牌。"""
    claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    return {
        "uid": int(claims["uid"]),
        "workspace": int(claims["workspace"]),
        "purpose": claims["purpose"],
        "subjectId": int(claims["subjectId"]),
    }


def sign_conversation(
    user_id: int,
    tenant_id: int,
    purpose: str,
    conversation_id: int,
    agent_id: int,
    agent_version_id: int,
    ttl_seconds: int,
) -> str:
    """会话令牌额外绑定 agent 与 agentVersion。"""
    now = _now()
    body = {
        "sub": str(user_id),
        "uid": user_id,
        "workspace": tenant_id,
        "purpose": purpose,
        "subjectId": conversation_id,
        "agentId": agent_id,
        "agentVersionId": agent_version_id,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(body, _secret(), algorithm="HS256")


def parse_conversation(token: str) -> ConversationClaims:
    """解析会话令牌。缺少 purpose 或身份 claim 时拒绝。"""
    claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    purpose = claims.get("purpose")
    if purpose is None:
        raise ValueError("claim purpose is missing")
    return ConversationClaims(
        user_id=_require_claim(claims, "uid"),
        tenant_id=_require_claim(claims, "workspace"),
        purpose=purpose,
        conversation_id=_require_claim(claims, "subjectId"),
        agent_id=_require_claim(claims, "agentId"),
        agent_version_id=_require_claim(claims, "agentVersionId"),
    )


def sign_user_purpose(user_id: int, purpose: str, ttl_seconds: int) -> str:
    """只绑定用户与用途的短令牌。"""
    now = _now()
    body = {
        "sub": str(user_id),
        "uid": user_id,
        "purpose": purpose,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(body, _secret(), algorithm="HS256")


def parse_user_purpose(token: str) -> dict[str, object]:
    """解析用户用途令牌，``exp`` 为秒。"""
    claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    return {
        "uid": int(claims["uid"]),
        "purpose": claims["purpose"],
        "exp": int(claims["exp"]),
    }


def _require_claim(claims: dict[str, object], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"claim {name} is missing")
    return int(value)
