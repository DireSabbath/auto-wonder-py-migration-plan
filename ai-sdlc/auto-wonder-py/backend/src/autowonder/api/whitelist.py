"""鉴权白名单，逐条对应 ``AuthFilter``。"""

import re

WHITELIST_EXACTS = ("/api/mcp",)
DINGTALK_CALLBACK_PATH = "/api/integrations/dingtalk/callback"
INTEGRATION_CAPABILITIES_PATH = "/api/integrations/capabilities"
PLATFORM_BRANDING_PUBLIC_PATH = "/api/platform/branding/public"
PLATFORM_BRANDING_LOGO_PATH = "/api/platform/branding/logo"
WORKSPACE_SWITCH_PATH = re.compile(r"^/api/workspaces/[0-9]+/switch$")
WORKSPACE_LIFECYCLE_PATH = re.compile(r"^/api/workspaces/[0-9]+$")
WORKSPACE_RESTORE_PATH = re.compile(r"^/api/workspaces/[0-9]+/restore$")
WORKSPACE_RECYCLE_BIN_PATH = "/api/workspaces/recycle-bin"
CLI_WORKITEM_UPLOAD_PATH = re.compile(r"^/api/cli/workitems/[0-9]+/requirement-documents$")
CLI_SCHEDULED_TASK_UPLOAD_PATH = re.compile(r"^/api/cli/scheduled-tasks/[0-9]+/documents$")
CLI_WORKITEM_DOWNLOAD_INDEX_PATH = re.compile(
    r"^/api/cli/workitems/[0-9]+/requirement-documents/index$"
)
CLI_WORKITEM_DOWNLOAD_CONTENT_PATH = re.compile(
    r"^/api/cli/workitems/[0-9]+/requirement-documents/[0-9]+/content$"
)
PERSONAL_MCP_PATH_TOKEN_PATH = re.compile(r"^/api/mcp/awmcp_[A-Za-z0-9_-]{43}(?:/(?:rpc/?)?)?$")
PERSONAL_MCP_TOKEN_PREFIX = "/api/mcp/tokens"
PERSONAL_USER_API_PREFIX = "/api/users/me/"
WHITELIST_PREFIXES = ("/api/auth/", "/api/hello", "/api/daemon/", "/api/mcp/rpc", "/api/mcp/tools")


def normalize_trailing_slash(path: str) -> str:
    """去掉多余的结尾斜杠，根路径保持不变。"""
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def is_whitelisted(method: str, path: str) -> bool:
    """这些路径不要求 Bearer 访问令牌。"""
    if method.upper() == "POST" and path == "/api/integrations/feishu/callback":
        return True
    if path in WHITELIST_EXACTS or path == DINGTALK_CALLBACK_PATH:
        return True
    if method.upper() == "GET" and path in {
        PLATFORM_BRANDING_PUBLIC_PATH,
        PLATFORM_BRANDING_LOGO_PATH,
        INTEGRATION_CAPABILITIES_PATH,
    }:
        return True
    if method.upper() == "POST" and (
        CLI_WORKITEM_UPLOAD_PATH.fullmatch(path) or CLI_SCHEDULED_TASK_UPLOAD_PATH.fullmatch(path)
    ):
        return True
    if method.upper() == "GET" and (
        CLI_WORKITEM_DOWNLOAD_INDEX_PATH.fullmatch(path)
        or CLI_WORKITEM_DOWNLOAD_CONTENT_PATH.fullmatch(path)
    ):
        return True
    if PERSONAL_MCP_PATH_TOKEN_PATH.fullmatch(path):
        return True
    return any(path.startswith(prefix) for prefix in WHITELIST_PREFIXES)


def is_login_only_request(method: str, path: str) -> bool:
    """已登录即可，不要求令牌里的工作空间仍然有效。"""
    method_name = method.upper()
    normalized = normalize_trailing_slash(path)
    if normalized.startswith(PERSONAL_MCP_TOKEN_PREFIX):
        return True
    if normalized.startswith(PERSONAL_USER_API_PREFIX):
        return True
    if method_name == "GET" and normalized == WORKSPACE_RECYCLE_BIN_PATH:
        return True
    if method_name in {"PUT", "DELETE"} and WORKSPACE_LIFECYCLE_PATH.fullmatch(normalized):
        return True
    if method_name == "POST" and WORKSPACE_RESTORE_PATH.fullmatch(normalized):
        return True
    if method_name == "POST" and normalized == "/api/workspaces":
        return True
    if method_name == "GET" and normalized == "/api/workspaces/mine":
        return True
    return method_name == "POST" and WORKSPACE_SWITCH_PATH.fullmatch(normalized) is not None


def is_deactivation_revoke_request(method: str, path: str) -> bool:
    """注销撤销接口允许已停用账号带着旧令牌进来。"""
    return (
        method.upper() == "POST"
        and normalize_trailing_slash(path) == "/api/users/me/deactivation/revoke"
    )
