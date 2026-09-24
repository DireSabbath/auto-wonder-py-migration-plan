"""为仓库扫描和澄清准备本地 git 工作区。"""

import asyncio
import logging
import os
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.ai.models import AiSession
from autowonder.repos.models import Repo

logger = logging.getLogger(__name__)

_UNSAFE_DIR = re.compile(r"[^a-zA-Z0-9._-]")


def repo_dir_name(name: str | None, repo_id: int) -> str:
    """仓库目录名只保留字母、数字和 ``._-``。没有名称时用编号。"""
    if name is None:
        return str(repo_id)
    return _UNSAFE_DIR.sub("_", name)


def build_clone_command(
    git_binary: str,
    url: str,
    default_branch: str | None,
    repo_dir: Path,
) -> list[str]:
    """浅克隆一条命令。有默认分支时加上 ``--branch`` 和 ``--single-branch``。"""
    command = [git_binary, "clone", "--depth", "1"]
    if default_branch is not None and default_branch.strip() != "":
        command.extend(["--branch", default_branch, "--single-branch"])
    command.extend([url, str(repo_dir)])
    return command


async def prepare_repo_scan(session: AsyncSession, row: AiSession, work_dir: Path) -> Path:
    """把会话指向的仓库克隆到 ``work_dir/repo``。"""
    if row.scene != "REPO_SCAN":
        return work_dir.resolve()
    if row.biz_ref_type != "REPO" or row.biz_ref_id is None:
        raise RuntimeError("REPO_SCAN session missing repo reference")
    repo = await session.get(Repo, row.biz_ref_id)
    if repo is None or repo.tenant_id != row.tenant_id:
        raise RuntimeError("repo not found for scan: " + str(row.biz_ref_id))
    if repo.url is None or repo.url.strip() == "":
        raise RuntimeError("repo url is empty: " + str(row.biz_ref_id))
    repo_dir = (work_dir / "repo").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    await _clone(repo, repo_dir, row.id)
    return repo_dir


async def prepare_multi_repo(session: AsyncSession, tenant_id: int, work_dir: Path) -> Path:
    """克隆工作空间里最多 100 个仓库。单个失败不影响其余仓库。"""
    repos = list(
        (
            await session.scalars(
                select(Repo)
                .where(Repo.tenant_id == tenant_id, Repo.is_deleted == 0)
                .order_by(Repo.id.desc())
                .limit(100)
            )
        ).all()
    )
    if len(repos) == 0:
        logger.info("multi-repo prep: no repos found for tenantId=%s", tenant_id)
        return work_dir.resolve()
    repos_dir = (work_dir / "repos").resolve()
    repos_dir.mkdir(parents=True, exist_ok=True)
    cloned = 0
    for repo in repos:
        if repo.url is None or repo.url.strip() == "":
            logger.warning(
                "multi-repo prep: skip repo with empty url repoId=%s name=%s",
                repo.id,
                repo.name,
            )
            continue
        repo_dir = repos_dir / repo_dir_name(repo.name, repo.id)
        try:
            await _clone(repo, repo_dir, None)
        except Exception as error:
            logger.warning(
                "multi-repo prep: clone failed repoId=%s name=%s error=%s",
                repo.id,
                repo.name,
                error,
            )
            continue
        cloned += 1
    logger.info(
        "multi-repo prep done tenantId=%s total=%s cloned=%s",
        tenant_id,
        len(repos),
        cloned,
    )
    return repos_dir


async def _clone(repo: Repo, repo_dir: Path, session_id: int | None) -> None:
    command = build_clone_command(
        os.environ.get("AUTOWONDER_AI_GIT_BINARY", "git"),
        repo.url,
        repo.default_branch,
        repo_dir,
    )
    logger.info(
        "repo clone step=clone_start repoId=%s repoName=%s repoUrl=%s branch=%s "
        "targetDir=%s sessionId=%s",
        repo.id,
        repo.name,
        repo.url,
        repo.default_branch,
        repo_dir,
        session_id,
    )
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    timeout = int(os.environ.get("AUTOWONDER_AI_REPO_CLONE_TIMEOUT_SEC", "120"))
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=repo_dir.parent,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("git clone timeout after " + str(timeout) + "s") from None
    if process.returncode != 0:
        err = stderr.decode()
        logger.warning(
            "repo clone step=clone_failed repoId=%s exitCode=%s stdout=%s stderr=%s",
            repo.id,
            process.returncode,
            stdout.decode(),
            err,
        )
        raise RuntimeError(
            "git clone failed with exit code " + str(process.returncode) + ": " + err
        )
    logger.info("repo clone step=clone_done repoId=%s targetDir=%s", repo.id, repo_dir)
