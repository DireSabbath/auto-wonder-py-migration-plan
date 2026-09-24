"""MCP 凭证主体。个人令牌不带工作空间，任务令牌必须带上。"""

from dataclasses import dataclass
from enum import Enum

from autowonder.api.access import WorkspaceAccessLevel


class CredentialType(Enum):
    """长效个人令牌、调度令牌或会话令牌。"""

    LONG_LIVED = "LONG_LIVED"
    DISPATCH = "DISPATCH"
    CONVERSATION = "CONVERSATION"


@dataclass(frozen=True)
class Principal:
    """一次 MCP 调用的身份。个人凭证的工作空间和级别都为空。"""

    workspace_id: int | None
    user_id: int
    token_id: int
    access_level: WorkspaceAccessLevel | None
    credential_type: CredentialType

    def __post_init__(self) -> None:
        personal = self.credential_type is CredentialType.LONG_LIVED
        if personal != (self.workspace_id is None) or personal != (self.access_level is None):
            raise ValueError("personal credentials carry no workspace, task-scoped ones must")

    def is_workspace_scoped(self) -> bool:
        """调度和会话令牌钉在签发时的工作空间。"""
        return self.credential_type is not CredentialType.LONG_LIVED

    @staticmethod
    def personal(user_id: int, token_id: int) -> "Principal":
        """长效个人令牌。"""
        return Principal(None, user_id, token_id, None, CredentialType.LONG_LIVED)
