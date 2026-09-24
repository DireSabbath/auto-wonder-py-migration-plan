"""从 ``ai:queue:global`` 取出排队会话，调用 CLI，并写成待确认。"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.ai.cli_executor import CliResult
from autowonder.ai.models import AiMessage, AiSession
from autowonder.ai.prompts import allowed_tools, blank, system_base, user_prompt, with_api_suffix
from autowonder.ai.repo_prep import prepare_multi_repo, prepare_repo_scan
from autowonder.ai.session import cli_executor
from autowonder.ai.stream import publish_delta, publish_result, publish_status
from autowonder.aiusage.service import record_usage
from autowonder.core.redis import redis_client
from autowonder.db.rows import rowcount
from autowonder.db.session import SessionLocal
from autowonder.repos.models import Repo, RepoConclusion
from autowonder.workitems.models import Workitem, WorkitemComment

logger = logging.getLogger(__name__)

QUEUE = "ai:queue:global"
_NODE_ID = uuid.uuid4().hex[:12]
_WORK_ROOT = Path("/tmp/aiw")


class _Stop:
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []


def start_workers() -> _Stop:
    """按池大小启动轮询。生产入口调用，``create_app`` 不调用。"""
    stop = _Stop()
    size = int(os.environ.get("AUTOWONDER_AI_WORKER_POOL_SIZE", "3"))
    for _ in range(size):
        stop.tasks.append(asyncio.create_task(_poll_loop(stop.event)))
    logger.info("AiWorkerPool started with %s workers", size)
    return stop


async def stop_workers(stop: _Stop) -> None:
    """停下轮询任务。"""
    stop.event.set()
    for task in stop.tasks:
        task.cancel()
    for task in stop.tasks:
        try:
            await task
        except asyncio.CancelledError:
            continue


async def execute_session(session_id: int, user_input: str | None) -> None:
    """执行一条仍处于 QUEUED 的会话。版本被别人改过时直接返回。"""
    async with SessionLocal() as session:
        row = await session.get(AiSession, session_id)
        if row is None or row.is_deleted == 1 or row.status != "QUEUED":
            status = "NOT_FOUND" if row is None else row.status
            logger.info("ai worker skip non-queued sessionId=%s status=%s", session_id, status)
            return
        scene = row.scene
        tenant_id = row.tenant_id
        version = row.version
        cli_ref = row.cli_session_ref
        biz_ref_type = row.biz_ref_type
        biz_ref_id = row.biz_ref_id
        base = system_base(scene, await _clarification_extra(session, row))
    if scene == "CLARIFICATION" and base is not None:
        logger.info(
            "clarification systemPrompt built sessionId=%s bizRefId=%s tenantId=%s "
            "promptLen=%s\n%s",
            session_id,
            biz_ref_id,
            tenant_id,
            len(base),
            base,
        )
    if base is None:
        logger.info(
            "ai session exec failed sessionId=%s reason=unsupported_scene scene=%s",
            session_id,
            scene,
        )
        await _mark_failed(session_id, tenant_id, "unsupported scene: " + scene, version)
        return
    system_prompt = with_api_suffix(base)
    logger.info(
        "AI session step=system_prompt_built sessionId=%s scene=%s systemPromptLen=%s",
        session_id,
        scene,
        len(system_prompt),
    )
    running = await _mark_running(session_id, tenant_id, cli_ref, version)
    if running != 1:
        return
    current_version = version + 1
    logger.info(
        "AI session execution started sessionId=%s scene=%s bizRefType=%s bizRefId=%s nodeId=%s",
        session_id,
        scene,
        biz_ref_type,
        biz_ref_id,
        _NODE_ID,
    )
    await publish_status(session_id, tenant_id, "RUNNING")
    work_dir = _WORK_ROOT / str(session_id)
    terminal = False
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        prompt, cli_dir = await _prompt_and_dir(
            session_id,
            tenant_id,
            scene,
            biz_ref_id,
            cli_ref,
            user_input,
            work_dir,
        )
        if blank(prompt):
            reason = "resume" if cli_ref is not None else "initial"
            logger.warning(
                "AI session skip empty prompt sessionId=%s isResume=%s",
                session_id,
                cli_ref is not None,
            )
            await _mark_failed(
                session_id,
                tenant_id,
                "empty prompt on " + reason,
                current_version,
            )
            await publish_status(session_id, tenant_id, "FAILED")
            terminal = True
            return
        shown = prompt or ""
        logger.info(
            "AI session step=prompt_ready sessionId=%s scene=%s promptLen=%s cliWorkDir=%s "
            "isResume=%s promptPreview=%s",
            session_id,
            scene,
            len(shown),
            cli_dir,
            cli_ref is not None,
            shown[:200],
        )
        result = await cli_executor().execute(
            shown,
            cli_ref,
            str(cli_dir),
            allowed_tools(scene),
            None if cli_ref is not None else system_prompt,
            lambda text: publish_delta(session_id, tenant_id, text),
        )
        current_version = await _save_cli_ref(session_id, tenant_id, result, current_version)
        if result.exit_code != 0:
            await _fail_result(
                session_id,
                tenant_id,
                scene,
                biz_ref_type,
                biz_ref_id,
                result,
                current_version,
            )
            terminal = True
            return
        await _save_success(session_id, tenant_id, result, current_version)
        await publish_status(session_id, tenant_id, "WAIT_USER")
        logger.info(
            "AI session waiting for user sessionId=%s scene=%s hasJson=%s",
            session_id,
            scene,
            result.extracted_json is not None,
        )
        async with SessionLocal() as session:
            await record_usage(session, tenant_id, scene, 0, 0)
            await session.commit()
    except Exception as error:
        logger.error("session execution error id=%s", session_id, exc_info=True)
        await _mark_failed(session_id, tenant_id, _truncate(_message(error), 500), current_version)
        await _mark_repo_failed(tenant_id, scene, biz_ref_type, biz_ref_id)
        await publish_status(session_id, tenant_id, "FAILED")
        terminal = True
    finally:
        if terminal:
            _cleanup(work_dir)


async def _poll_loop(stop: asyncio.Event) -> None:
    timeout = int(os.environ.get("AUTOWONDER_AI_QUEUE_POLL_SEC", "5"))
    while not stop.is_set():
        try:
            payload = await cast(
                Awaitable[list[str] | None],
                redis_client().brpop([QUEUE], timeout=timeout),
            )
            if payload is None:
                continue
            raw = payload[1]
            try:
                session_id = int(raw)
            except ValueError:
                logger.warning("invalid queue payload: %s", raw)
                continue
            logger.info("ai worker picked sessionId=%s", session_id)
            await execute_session(session_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not stop.is_set():
                logger.error("worker poll error", exc_info=True)


async def _prompt_and_dir(
    session_id: int,
    tenant_id: int,
    scene: str,
    biz_ref_id: int | None,
    cli_ref: str | None,
    user_input: str | None,
    work_dir: Path,
) -> tuple[str | None, Path]:
    resume = cli_ref is not None
    prompt_input = user_input
    cli_dir = work_dir
    if resume and prompt_input is None:
        prompt_input = await _latest_user_message(session_id)
    async with SessionLocal() as session:
        row = await session.get(AiSession, session_id)
        if row is None:
            return None, cli_dir
        if scene == "REPO_SCAN":
            if not resume:
                logger.info(
                    "AI session step=repo_prepare_start sessionId=%s repoId=%s",
                    session_id,
                    biz_ref_id,
                )
                cli_dir = await prepare_repo_scan(session, row, work_dir)
                prompt_input = str(cli_dir)
                logger.info(
                    "AI session step=repo_prepare_done sessionId=%s repoId=%s repoPath=%s",
                    session_id,
                    biz_ref_id,
                    cli_dir,
                )
            else:
                repo_dir = work_dir / "repo"
                if repo_dir.is_dir():
                    cli_dir = repo_dir
        elif scene == "CLARIFICATION" and not resume:
            logger.info(
                "AI session step=multi_repo_prepare_start sessionId=%s tenantId=%s",
                session_id,
                tenant_id,
            )
            cli_dir = await prepare_multi_repo(session, tenant_id, work_dir)
            logger.info(
                "AI session step=multi_repo_prepare_done sessionId=%s reposPath=%s",
                session_id,
                cli_dir,
            )
    if resume:
        return prompt_input, cli_dir
    if scene == "REPO_SCAN":
        logger.info(
            "repo scan buildUserPrompt sessionId=%s bizRefId=%s userInput=%s",
            session_id,
            biz_ref_id,
            prompt_input,
        )
    return user_prompt(scene, prompt_input), cli_dir


async def _clarification_extra(session: AsyncSession, row: AiSession) -> str:
    if row.scene != "CLARIFICATION":
        return ""
    extra = await _workitem_context(session, row)
    extra += await _repo_context(session, row.tenant_id)
    return extra


async def _workitem_context(session: AsyncSession, row: AiSession) -> str:
    if row.biz_ref_type != "WORKITEM" or row.biz_ref_id is None:
        return ""
    try:
        workitem = await session.get(Workitem, row.biz_ref_id)
        if workitem is None:
            return ""
        text = "\n\n--- 需求信息 ---\n标题: "
        if workitem.title is None:
            text += "\n"
        else:
            text += workitem.title + "\n"
        if workitem.content_md is not None and workitem.content_md.strip() != "":
            text += "内容:\n" + workitem.content_md + "\n"
        comments = list(
            (
                await session.scalars(
                    select(WorkitemComment)
                    .where(
                        WorkitemComment.tenant_id == row.tenant_id,
                        WorkitemComment.source_type == "WORKITEM",
                        WorkitemComment.workitem_id == workitem.id,
                    )
                    .order_by(WorkitemComment.gmt_create.desc(), WorkitemComment.id.desc())
                )
            ).all()
        )
        if len(comments) > 0:
            text += "\n评论:\n"
            for comment in comments:
                author = "unknown" if comment.author_ref is None else str(comment.author_ref)
                content = "null" if comment.content_md is None else comment.content_md
                text += "- " + author + ": " + content + "\n"
        logger.info(
            "clarification context appended workitemId=%s commentsCount=%s",
            workitem.id,
            len(comments),
        )
        return text
    except Exception:
        logger.warning(
            "failed to append workitem context bizRefId=%s",
            row.biz_ref_id,
            exc_info=True,
        )
        return ""


async def _repo_context(session: AsyncSession, tenant_id: int) -> str:
    try:
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
            return ""
        text = "\n\n--- 项目仓库 ---\n"
        for repo in repos:
            text += "仓库: " + repo.name
            if repo.url is not None:
                text += " (" + repo.url + ")"
            text += "\n"
            if repo.description is not None and repo.description.strip() != "":
                text += "  描述: " + repo.description + "\n"
            conclusion = await session.scalar(
                select(RepoConclusion)
                .where(RepoConclusion.repo_id == repo.id, RepoConclusion.is_deleted == 0)
                .limit(1)
            )
            if conclusion is None:
                continue
            text += _conclusion_lines(conclusion.purpose, "  简介: ")
            text += _conclusion_lines(_as_text(conclusion.key_business), "  关键业务: ")
            text += _conclusion_lines(_as_text(conclusion.upstreams), "  上游: ")
            text += _conclusion_lines(_as_text(conclusion.downstreams), "  下游: ")
        logger.info("clarification context appended repoCount=%s", len(repos))
        return text
    except Exception:
        logger.warning("failed to append repo context tenantId=%s", tenant_id, exc_info=True)
        return ""


def _conclusion_lines(value: str | None, label: str) -> str:
    if value is None:
        return ""
    return label + value + "\n"


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


async def _latest_user_message(session_id: int) -> str | None:
    async with SessionLocal() as session:
        messages = list(
            (
                await session.scalars(
                    select(AiMessage)
                    .where(AiMessage.session_id == session_id)
                    .order_by(AiMessage.seq.asc())
                )
            ).all()
        )
    for message in reversed(messages):
        if message.role == "USER":
            return message.content
    return None


async def _mark_running(
    session_id: int,
    tenant_id: int,
    cli_ref: str | None,
    version: int,
) -> int:
    async with SessionLocal() as session:
        result = await session.execute(
            update(AiSession)
            .where(
                AiSession.id == session_id,
                AiSession.tenant_id == tenant_id,
                AiSession.status == "QUEUED",
                AiSession.version == version,
            )
            .values(
                status="RUNNING",
                node_id=_NODE_ID,
                cli_session_ref=cli_ref,
                version=version + 1,
            )
        )
        changed = rowcount(result)
        if changed == 1:
            await session.commit()
        return changed


async def _save_cli_ref(
    session_id: int,
    tenant_id: int,
    result: CliResult,
    version: int,
) -> int:
    if result.cli_session_id is None:
        return version
    async with SessionLocal() as session:
        updated = await session.execute(
            update(AiSession)
            .where(
                AiSession.id == session_id,
                AiSession.tenant_id == tenant_id,
                AiSession.version == version,
            )
            .values(cli_session_ref=result.cli_session_id, version=version + 1)
        )
        if rowcount(updated) != 1:
            return version
        await session.commit()
    return version + 1


async def _save_success(
    session_id: int,
    tenant_id: int,
    result: CliResult,
    version: int,
) -> None:
    text = result.full_text
    async with SessionLocal() as session:
        if text is not None and text.strip() != "":
            seq = await session.scalar(
                select(func.max(AiMessage.seq)).where(AiMessage.session_id == session_id)
            )
            session.add(
                AiMessage(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    seq=1 if seq is None else seq + 1,
                    role="AI",
                    content=text,
                )
            )
            await session.flush()
        extracted = result.extracted_json
        if extracted is not None:
            await session.execute(
                update(AiSession)
                .where(
                    AiSession.id == session_id,
                    AiSession.tenant_id == tenant_id,
                    AiSession.version == version,
                )
                .values(result_json=json.loads(extracted), status="WAIT_USER", version=version + 1)
            )
            await session.commit()
            await publish_result(session_id, tenant_id, extracted)
            return
        await session.execute(
            update(AiSession)
            .where(
                AiSession.id == session_id,
                AiSession.tenant_id == tenant_id,
                AiSession.status == "RUNNING",
                AiSession.version == version,
            )
            .values(status="WAIT_USER", version=version + 1)
        )
        await session.commit()


async def _fail_result(
    session_id: int,
    tenant_id: int,
    scene: str,
    biz_ref_type: str | None,
    biz_ref_id: int | None,
    result: CliResult,
    version: int,
) -> None:
    logger.warning(
        "AI session execution failed sessionId=%s scene=%s error=%s",
        session_id,
        scene,
        result.error,
    )
    await _mark_failed(session_id, tenant_id, _truncate(result.error, 500), version)
    await _mark_repo_failed(tenant_id, scene, biz_ref_type, biz_ref_id)
    await publish_status(session_id, tenant_id, "FAILED")


async def _mark_failed(session_id: int, tenant_id: int, error: str | None, version: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(AiSession)
            .where(
                AiSession.id == session_id,
                AiSession.tenant_id == tenant_id,
                AiSession.version == version,
            )
            .values(status="FAILED", error=error, version=version + 1)
        )
        await session.commit()


async def _mark_repo_failed(
    tenant_id: int,
    scene: str,
    biz_ref_type: str | None,
    biz_ref_id: int | None,
) -> None:
    if scene != "REPO_SCAN" or biz_ref_type != "REPO" or biz_ref_id is None:
        return
    async with SessionLocal() as session:
        repo = await session.get(Repo, biz_ref_id)
        if repo is None:
            return
        await session.execute(
            update(Repo)
            .where(
                Repo.id == repo.id,
                Repo.tenant_id == tenant_id,
                Repo.version == repo.version,
            )
            .values(scan_status="FAILED", version=Repo.version + 1)
        )
        await session.commit()


def _cleanup(work_dir: Path) -> None:
    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)
    except Exception:
        logger.warning("cleanup failed: %s", work_dir, exc_info=True)


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit]


def _message(error: Exception) -> str:
    text = str(error)
    if text == "":
        return error.__class__.__name__
    return text
