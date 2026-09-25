"""把 PENDING 派发打包并下发。锁、选执行器和失败分类对齐 ``DispatchService.runPending``。"""

import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentEnvironmentVariableRef, AgentVersion
from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.locks import release_lock, try_acquire_lock
from autowonder.db.rows import rowcount
from autowonder.db.session import SessionLocal
from autowonder.dispatch.enqueue import is_interaction
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery import (
    execution_source,
    fenced,
    find_dispatch,
    ready,
    retry_packaging,
    transition,
    waiting,
)
from autowonder.dispatch.selector import (
    ProtocolCompatibilityError,
    select_dispatch_executor,
    select_strict_executor,
    unavailable_reason,
    waiting_error,
    waiting_retryable,
)
from autowonder.dispatch.transport import send_task
from autowonder.environments.snapshot import EnvironmentSnapshotResolutionException
from autowonder.squads.models import Squad, SquadMember
from autowonder.storage.factory import resolve_bucket
from autowonder.storage.objects import ObjectStorageError, get_object_storage
from autowonder.taskpackages.assembler import assemble_workitem
from autowonder.taskpackages.context import TaskPackageResult
from autowonder.taskpackages.packager import TaskPackager, normalize_base_url, normalize_mcp_url
from autowonder.taskpackages.scheduled import assemble_scheduled
from autowonder.ws.frames import AGENT_ENVIRONMENT_VARIABLES_V1

logger = logging.getLogger(__name__)

LOCK_TTL_MS = 30_000
CAPACITY_LOCK_TTL_MS = 10_000
MAX_ERROR_CHARS = 512
_RESUME_EXECUTOR_MODES = frozenset(
    {"RETURNING_WORKER", "CONTINUOUS", "SIDE_INTERACTION", "CANONICAL_INTERACTION"}
)
_PERMANENT_STATE_PREFIXES = (
    "bound capability is missing",
    "capability config must be a JSON object",
    "COMMENT_REWORK_CONTEXT_MISSING:",
)


async def run_pending(session: AsyncSession, dispatch_id: int) -> bool:
    """打包并下发一条 PENDING。容量一旦占住就返回真，即使随后发送失败。"""
    lock_key = "dispatch:lock:" + str(dispatch_id)
    lock_owner = str(uuid.uuid4())
    if not await try_acquire_lock(lock_key, lock_owner, LOCK_TTL_MS):
        return False
    try:
        logger.info("dispatch runPending start dispatchId=%s", dispatch_id)
        dispatch = await _load(session, dispatch_id)
        if dispatch is None or dispatch.status != "PENDING":
            return False
        if await fenced(session, dispatch) or not await ready(session, dispatch):
            return False
        version = await _published_version(session, dispatch)
        if version is None:
            return False
        capacity_key = "dispatch:agent-capacity:" + str(dispatch.agent_id)
        capacity_owner = str(uuid.uuid4())
        if not await try_acquire_lock(capacity_key, capacity_owner, CAPACITY_LOCK_TTL_MS):
            await waiting(session, dispatch, "CAPACITY_LOCK_BUSY")
            logger.info(
                "dispatch pending dispatchId=%s reason=CAPACITY_LOCK_BUSY",
                dispatch.id,
            )
            return False
        try:
            executor_id = await _choose_executor(session, dispatch, version.id)
            if executor_id is None:
                return False
            if not await _move(
                session,
                dispatch,
                "PACKAGING",
                version.id,
                executor_id,
                None,
                None,
                None,
            ):
                return False
        finally:
            await release_lock(capacity_key, capacity_owner)
        await _freeze_debug_log(session, dispatch)
        try:
            package = await _build_package(session, dispatch, version)
        except Exception as packaging_failure:
            await _handle_packaging_failure(session, dispatch, packaging_failure)
            raise
        if not await _move(
            session,
            dispatch,
            "DISPATCHED",
            None,
            dispatch.executor_id,
            package.oss_ref,
            None,
            None,
        ):
            return True
        try:
            await send_task(session, dispatch, package)
        except EnvironmentSnapshotResolutionException as resolution_failure:
            await fail_and_drive(
                session,
                dispatch,
                "ENVIRONMENT_SNAPSHOT_INVALID: " + str(resolution_failure),
            )
            return False
        except ProtocolCompatibilityError as compatibility_failure:
            await fail_and_drive(session, dispatch, str(compatibility_failure))
            return False
        return True
    except Exception:
        logger.error("runPending failed dispatchId=%s", dispatch_id, exc_info=True)
        return False
    finally:
        await release_lock(lock_key, lock_owner)


async def drain_pending(session: AsyncSession, agent_id: int) -> None:
    """按创建时间从早到晚抽空该数字员工当前还能发出去的 PENDING。"""
    while True:
        pending = list(
            await session.scalars(
                select(Dispatch.id)
                .where(
                    Dispatch.agent_id == agent_id,
                    Dispatch.status == "PENDING",
                    Dispatch.is_deleted == 0,
                )
                .order_by(Dispatch.gmt_create.asc(), Dispatch.id.asc())
                .limit(20)
                .execution_options(populate_existing=True)
            )
        )
        if len(pending) == 0:
            return
        progressed = False
        for dispatch_id in pending:
            if await run_pending(session, dispatch_id):
                progressed = True
                continue
            after = await _load(session, dispatch_id)
            if after is None or after.status != "PENDING":
                progressed = True
        if not progressed:
            return


async def fail_and_drive(session: AsyncSession, dispatch: Dispatch, reason: str) -> None:
    """把派发打成失败。交互到此为止；定时运行回写失败；工单只记录停止。"""
    if not await _move(session, dispatch, "FAILED", None, None, None, None, reason):
        return
    if is_interaction(dispatch):
        return
    if execution_source(dispatch) == "SCHEDULED_TASK_RUN":
        from autowonder.scheduledtasks.runs import complete_from_dispatch

        await complete_from_dispatch(session, dispatch, False, None, reason)
        return
    logger.info(
        "sdlc onFail stop workitemId=%s stepId=%s",
        dispatch.workitem_id,
        dispatch.sdlc_step_id,
    )


async def on_unacknowledged_timeout(session: AsyncSession, dispatch: Dispatch) -> None:
    """已下发但没有 ACK 的行记为超时。定时运行同时结束。"""
    if dispatch.status != "DISPATCHED":
        return
    reason = "DISPATCH_ACK_TIMEOUT: 接单确认超时，旧执行已隔离，请确认外部操作后重试"
    if not await _move(session, dispatch, "TIMEOUT", None, None, None, None, reason):
        return
    if is_interaction(dispatch) or execution_source(dispatch) != "SCHEDULED_TASK_RUN":
        return
    from autowonder.scheduledtasks.runs import complete_from_dispatch

    await complete_from_dispatch(session, dispatch, False, None, "DISPATCH_ACK_TIMEOUT")


async def drive_dispatch(dispatch_id: int) -> None:
    """提交之后再拉起一条派发。调度失败不回滚已经提交的业务写入。"""
    try:
        async with SessionLocal() as session:
            await run_pending(session, dispatch_id)
    except Exception:
        logger.error("runPending failed dispatchId=%s", dispatch_id, exc_info=True)


def remember_pending(session: object, dispatch_id: int | None) -> None:
    """记下本会话里新出现的派发，等提交后再拉起。"""
    if dispatch_id is None:
        return
    info = getattr(session, "info", None)
    if info is None:
        return
    ids = info.get("pending_dispatch_ids")
    if ids is None:
        ids = []
        info["pending_dispatch_ids"] = ids
    ids.append(dispatch_id)


async def drive_remembered(session: AsyncSession) -> None:
    """拉起 ``remember_pending`` 记下的派发。单条失败不影响其余。"""
    info = getattr(session, "info", None)
    if info is None:
        return
    ids = info.pop("pending_dispatch_ids", [])
    for dispatch_id in ids:
        try:
            await run_pending(session, dispatch_id)
        except Exception:
            logger.error("runPending failed dispatchId=%s", dispatch_id, exc_info=True)


def permanent_package_input_failure(failure: BaseException) -> bool:
    """快照损坏、参数错误，以及明确的能力配置错误不再重试。"""
    business = _find_cause(failure, BizError)
    if (
        isinstance(business, BizError)
        and business.code == ErrorCode.SCHEDULED_TASK_INVALID_STATE.code
    ):
        return True
    if _find_cause(failure, ValueError) is not None:
        return True
    illegal = _find_cause(failure, RuntimeError)
    if not isinstance(illegal, RuntimeError):
        return False
    message = str(illegal)
    return message.startswith(_PERMANENT_STATE_PREFIXES)


def root_failure_message(failure: BaseException) -> str:
    """沿 ``__cause__`` 走到根异常。空白消息用类名。"""
    root: BaseException = failure
    while root.__cause__ is not None and root.__cause__ is not root:
        root = root.__cause__
    message = str(root)
    if message.strip() == "":
        return type(root).__name__
    return message


async def _published_version(session: AsyncSession, dispatch: Dispatch) -> AgentVersion | None:
    agent = await session.scalar(
        select(Agent).where(Agent.id == dispatch.agent_id, Agent.is_deleted == 0).limit(1)
    )
    if agent is None or agent.tenant_id != dispatch.tenant_id:
        await fail_and_drive(session, dispatch, waiting_error("AGENT_NOT_PUBLISHED"))
        return None
    scheduled = execution_source(dispatch) == "SCHEDULED_TASK_RUN"
    frozen = dispatch.agent_version_id is not None
    if scheduled and not frozen:
        await fail_and_drive(
            session,
            dispatch,
            _frozen_prefix(True) + "frozen agent version is missing",
        )
        return None
    selected = dispatch.agent_version_id if frozen else agent.online_version_id
    if selected is None or selected <= 0:
        if scheduled or frozen:
            text = _frozen_prefix(scheduled) + "frozen agent version is missing"
        else:
            text = waiting_error("AGENT_NOT_PUBLISHED")
        await fail_and_drive(session, dispatch, text)
        return None
    version = await session.scalar(
        select(AgentVersion)
        .where(AgentVersion.id == selected, AgentVersion.is_deleted == 0)
        .limit(1)
    )
    if (
        version is None
        or version.tenant_id != dispatch.tenant_id
        or version.agent_id != dispatch.agent_id
    ):
        if scheduled or frozen:
            text = _frozen_prefix(scheduled) + "frozen agent version is invalid"
        else:
            text = waiting_error("AGENT_VERSION_NOT_FOUND")
        await fail_and_drive(session, dispatch, text)
        return None
    return version


async def _choose_executor(
    session: AsyncSession, dispatch: Dispatch, version_id: int
) -> int | None:
    preferred = await _preferred_resume_executor(session, dispatch)
    required = None
    if await _has_environment_bindings(session, dispatch.tenant_id, version_id):
        required = AGENT_ENVIRONMENT_VARIABLES_V1
    selection_failure = None
    try:
        if dispatch.resume_mode == "CONTINUOUS" and preferred is not None:
            executor_id = await select_strict_executor(
                session, dispatch.agent_id, preferred, required
            )
        else:
            executor_id = await select_dispatch_executor(
                session,
                dispatch.agent_id,
                preferred,
                is_interaction(dispatch),
                required,
            )
    except ProtocolCompatibilityError as compatibility_failure:
        await fail_and_drive(session, dispatch, str(compatibility_failure))
        return None
    except Exception:
        logger.error(
            "executor selection failed dispatchId=%s agentId=%s",
            dispatch.id,
            dispatch.agent_id,
            exc_info=True,
        )
        executor_id = None
        selection_failure = "SELECTION_INTERNAL_ERROR"
    if executor_id is None:
        if selection_failure is not None:
            reason = selection_failure
        else:
            reason = await unavailable_reason(session, dispatch.agent_id)
        if reason == "SELECTION_INTERNAL_ERROR" or not waiting_retryable(reason):
            await fail_and_drive(session, dispatch, waiting_error(reason))
        else:
            await waiting(session, dispatch, reason)
        logger.info("dispatch not scheduled dispatchId=%s reason=%s", dispatch.id, reason)
        return None
    if (
        dispatch.resume_mode == "SIDE_INTERACTION"
        and preferred is not None
        and preferred != executor_id
    ):
        dispatch.resume_mode = "CANONICAL_INTERACTION"
        logger.info(
            "dispatch fork degraded to canonical dispatchId=%s sourceExecutorId=%s executorId=%s",
            dispatch.id,
            preferred,
            executor_id,
        )
    logger.info(
        "dispatch executor selected dispatchId=%s executorId=%s",
        dispatch.id,
        executor_id,
    )
    return executor_id


async def _preferred_resume_executor(session: AsyncSession, dispatch: Dispatch) -> int | None:
    if (
        dispatch.resume_from_dispatch_id is None
        or dispatch.resume_mode not in _RESUME_EXECUTOR_MODES
    ):
        return None
    source = await _load(session, dispatch.resume_from_dispatch_id)
    if (
        source is None
        or source.tenant_id != dispatch.tenant_id
        or execution_source(source) != execution_source(dispatch)
        or source.agent_id != dispatch.agent_id
    ):
        return None
    if dispatch.resume_mode != "CONTINUOUS" and source.workitem_id != dispatch.workitem_id:
        return None
    return source.executor_id


async def _has_environment_bindings(
    session: AsyncSession, tenant_id: int, version_id: int
) -> bool:
    found = await session.scalar(
        select(AgentEnvironmentVariableRef.id)
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == version_id,
        )
        .limit(1)
    )
    return found is not None


async def _freeze_debug_log(session: AsyncSession, dispatch: Dispatch) -> None:
    try:
        enabled = await session.scalar(
            select(func.count())
            .select_from(SquadMember)
            .join(Squad, Squad.id == SquadMember.squad_id)
            .where(
                SquadMember.agent_id == dispatch.agent_id,
                SquadMember.tenant_id == dispatch.tenant_id,
                Squad.tenant_id == dispatch.tenant_id,
                Squad.is_deleted == 0,
                Squad.debug_log_enabled == 1,
            )
        )
        if enabled is None or int(enabled) == 0:
            return
        result = await session.execute(
            update(Dispatch)
            .where(
                Dispatch.id == dispatch.id,
                Dispatch.tenant_id == dispatch.tenant_id,
                Dispatch.is_deleted == 0,
                Dispatch.status == "PACKAGING",
            )
            .values(debug_log_enabled=1)
        )
        await session.commit()
        if rowcount(result) == 0:
            logger.warning(
                "debug log freeze matched no row dispatchId=%s agentId=%s "
                "reason=DEBUG_LOG_FREEZE_NO_ROW",
                dispatch.id,
                dispatch.agent_id,
            )
            return
        dispatch.debug_log_enabled = 1
        logger.info(
            "dispatch debug log frozen dispatchId=%s agentId=%s",
            dispatch.id,
            dispatch.agent_id,
        )
    except Exception:
        logger.warning(
            "debug log freeze skipped dispatchId=%s reason=DEBUG_LOG_FREEZE_ERROR",
            dispatch.id,
            exc_info=True,
        )


async def _build_package(
    session: AsyncSession, dispatch: Dispatch, version: AgentVersion
) -> TaskPackageResult:
    source = execution_source(dispatch)
    if dispatch.tenant_id <= 0 or dispatch.workitem_id <= 0:
        raise ValueError("dispatch workspace and source id are required")
    if source == "SCHEDULED_TASK_RUN":
        context = await assemble_scheduled(session, dispatch, version)
    elif source == "WORKITEM":
        context = await assemble_workitem(session, dispatch, version)
    else:
        raise ValueError("unknown execution source type: " + source)
    return _packager().build(context)


async def _handle_packaging_failure(
    session: AsyncSession, dispatch: Dispatch, failure: BaseException
) -> None:
    storage_failure = _find_cause(failure, ObjectStorageError)
    if (
        isinstance(storage_failure, ObjectStorageError)
        and storage_failure.is_permanent_configuration_error()
    ):
        reason = "TASK_PACKAGE_STORAGE_CONFIG_ERROR: " + storage_failure.describe()
        logger.error(
            "dispatch packaging permanent failure dispatchId=%s reason=%s",
            dispatch.id,
            reason,
            exc_info=failure,
        )
        await fail_and_drive(session, dispatch, reason)
        return
    if permanent_package_input_failure(failure):
        reason = "TASK_PACKAGE_CONFIG_ERROR: " + root_failure_message(failure)
        logger.error(
            "dispatch packaging input failure dispatchId=%s reason=%s",
            dispatch.id,
            reason,
            exc_info=failure,
        )
        await fail_and_drive(session, dispatch, reason)
        return
    message = root_failure_message(failure)
    if not await retry_packaging(session, dispatch, message):
        await fail_and_drive(session, dispatch, "TASK_PACKAGE_RETRIES_EXHAUSTED: " + message)


async def _move(
    session: AsyncSession,
    dispatch: Dispatch,
    status: str,
    agent_version_id: int | None,
    executor_id: int | None,
    package_oss_ref: str | None,
    result_summary: str | None,
    error: str | None,
) -> bool:
    changed = await transition(
        session,
        dispatch,
        status,
        agent_version_id,
        executor_id,
        package_oss_ref,
        result_summary,
        _truncate_code_points(error, MAX_ERROR_CHARS),
    )
    if changed == 0:
        logger.info(
            "dispatch transition lost race dispatchId=%s targetStatus=%s",
            dispatch.id,
            status,
        )
        return False
    dispatch.status = status
    dispatch.version = dispatch.version + 1
    if agent_version_id is not None:
        dispatch.agent_version_id = agent_version_id
    if executor_id is not None:
        dispatch.executor_id = executor_id
    if package_oss_ref is not None:
        dispatch.package_oss_ref = package_oss_ref
    await _refresh(session, dispatch.id)
    logger.info(
        "dispatch transition dispatchId=%s status=%s version=%s",
        dispatch.id,
        status,
        dispatch.version,
    )
    return True


async def _load(session: AsyncSession, dispatch_id: int) -> Dispatch | None:
    attached = await session.get(Dispatch, dispatch_id)
    if attached is not None:
        await session.refresh(attached)
    return await find_dispatch(session, dispatch_id)


async def _refresh(session: AsyncSession, dispatch_id: int) -> None:
    attached = await session.get(Dispatch, dispatch_id)
    if attached is not None:
        await session.refresh(attached)


def _packager() -> TaskPackager:
    settings = get_settings()
    bucket = resolve_bucket(settings.oss_task_pkg_bucket, settings.oss_bucket)
    mcp_url = normalize_mcp_url(normalize_base_url(settings.public_base_url) + "/api/mcp")
    return TaskPackager(get_object_storage(), bucket, mcp_url)


def _frozen_prefix(scheduled: bool) -> str:
    if scheduled:
        return "SCHEDULED_TASK_INVALID_STATE(30005): "
    return "DISPATCH_FROZEN_AGENT_VERSION_INVALID: "


def _truncate_code_points(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    counted = 0
    for index, _character in enumerate(value):
        counted += 1
        if counted == limit:
            return value[: index + 1]
    return value


def _find_cause(failure: BaseException, kind: type[BaseException]) -> BaseException | None:
    cursor: BaseException | None = failure
    seen: set[int] = set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, kind):
            return cursor
        cursor = cursor.__cause__
    return None
