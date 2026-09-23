"""仓库连接测试。用本机 git ls-remote 验证读取权限，失败时返回结果而不是抛错。"""

import os
import re
import subprocess
import tempfile

from autowonder.config import get_settings
from autowonder.repos.schemas import ConnectionTestRequest, ConnectionTestView

_GENERIC = "连接测试失败，请检查仓库地址、网络和本机 git 权限"
_TIMEOUT = "连接测试超时，请检查仓库地址、网络和本机 git 权限"
_WARNING = re.compile(r"(?m)^Warning: Permanently added .*$\n?")


def build_ls_remote_command(
    git_binary: str,
    url: str,
    default_branch: str | None,
) -> list[str]:
    """只列出指定分支；没有分支时列出全部 heads。"""
    command = [git_binary, "ls-remote", "--heads", url]
    if default_branch is not None and default_branch.strip() != "":
        command.append(default_branch.strip())
    return command


def sanitize_git_error(err: str | None) -> str:
    """去掉主机指纹警告，并把 git 输出收成一条失败说明。"""
    if err is None or err.strip() == "":
        return _GENERIC
    trimmed = _WARNING.sub("", err).strip()
    if len(trimmed) > 500:
        trimmed = trimmed[:500]
    return "连接测试失败：" + trimmed


def probe_connection(request: ConnectionTestRequest | None) -> ConnectionTestView:
    """在空目录里执行 git，避免继承调用方仓库的配置。"""
    if request is None or request.url is None or request.url.strip() == "":
        return ConnectionTestView(success=False, message="仓库地址不能为空")
    settings = get_settings()
    command = build_ls_remote_command(
        settings.repo_test_git_binary,
        request.url,
        request.default_branch,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="autowonder-repo-test-") as temp_dir:
            completed = subprocess.run(
                command,
                cwd=temp_dir,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                capture_output=True,
                timeout=settings.repo_test_timeout_sec,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return ConnectionTestView(success=False, message=_TIMEOUT)
    except Exception as error:
        return ConnectionTestView(success=False, message="连接测试失败：" + str(error))
    if completed.returncode == 0:
        return ConnectionTestView(success=True, message="连接成功，已验证读取权限")
    return ConnectionTestView(
        success=False,
        message=sanitize_git_error(completed.stderr.decode("utf-8", errors="replace")),
    )
