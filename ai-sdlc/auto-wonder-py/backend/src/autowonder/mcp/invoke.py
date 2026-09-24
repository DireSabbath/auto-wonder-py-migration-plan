"""MCP 工具目录过滤与调用，对应 Java ``McpToolService``。"""

import base64
import binascii
import copy
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.schemas import (
    CreateAgentRequest,
    MemoryRefRequest,
    RepoPermRequest,
    SkillRequest,
    UpdateAgentRequest,
    UpdateConfigRequest,
)
from autowonder.agents.service import (
    add_memory_ref,
    add_repo_perm,
    add_skill,
    approve_agent,
    create_agent,
    delete_agent,
    edit_config,
    get_agent,
    get_version,
    list_agents,
    list_versions,
    offline_agent,
    online_agent,
    remove_memory_ref,
    remove_repo_perm,
    remove_skill,
    submit_agent,
    update_agent,
)
from autowonder.api.access import WorkspaceAccessLevel
from autowonder.artifacts.cli_tokens import CredentialType as CliCredentialType
from autowonder.artifacts.cli_tokens import mint_download_token, mint_upload_token
from autowonder.artifacts.documents import (
    ArtifactOwner,
    delete_requirement_document,
    list_requirement_documents,
    upload_mcp,
)
from autowonder.artifacts.models import Artifact
from autowonder.audits.service import AuditRecord, record_required
from autowonder.categories.schemas import CreateCategoryFields, UpdateCategoryFields
from autowonder.categories.service import (
    batch_set_skill_category,
    create_category,
    delete_category,
    get_category,
    list_categories,
    set_skill_category,
    update_category,
)
from autowonder.core.context import current
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.continue_run import continue_dispatch
from autowonder.dispatch.models import Dispatch, DispatchRuntimeEvent
from autowonder.dispatch.pause_request import request_workitem_pause
from autowonder.dispatch.recovery import (
    cancel,
    close,
    execution_source,
    find_dispatch,
    reopen,
    state,
)
from autowonder.dispatch.trace import load_activities, load_projected_trace
from autowonder.dispatch.trace_artifact import (
    load_observation,
    load_outline_if_present,
    load_turn,
)
from autowonder.executors.catalog import read_catalog
from autowonder.executors.launch import build_for_executor, get_launch_config, update_launch_config
from autowonder.executors.options import (
    AUTO_MODEL,
    CLIENT_KIND_QODER_CLI,
    CLIENT_KIND_QODER_CN_CLI,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MEMORY_MODE,
    DEFAULT_MODEL,
    FALLBACK_MODELS,
    MEMORY_MODE_NONE,
    MEMORY_MODE_PLATFORM,
    MEMORY_MODE_PROVIDER_LOCAL,
    PROVIDER_QODER,
    PROVIDER_QODER_CN,
    ModelOption,
    choose_model,
    default_reasoning_effort,
    require_creatable_client_kind,
    resolve_provider,
)
from autowonder.executors.schemas import (
    CreateExecutorRequest,
    UpdateExecutorLaunchConfigRequest,
)
from autowonder.executors.service import (
    create_executor,
    delete_executor,
    executor_detail,
    executor_token,
    list_all,
    list_by_agent,
)
from autowonder.guidance.service import create_for_comment
from autowonder.mcp.catalog import list_tools
from autowonder.mcp.principal import CredentialType, Principal
from autowonder.mcp.skills import get_platform_skill, list_platform_skills
from autowonder.memories.schemas import CreateMemoryRequest, ReviewRequest, UpdateMemoryRequest
from autowonder.memories.service import (
    count_pending_reviews,
    create_from_mcp,
    create_memory,
    delete_memory,
    deprecate_from_mcp,
    get_scoped,
    list_memories,
    review_memory,
    update_memory,
)
from autowonder.platform.service import is_system_admin
from autowonder.repos.schemas import CreateRelationRequest, CreateRepoRequest, UpdateRepoFields
from autowonder.repos.service import (
    create_relation,
    create_repo,
    delete_relation,
    delete_repo,
    get_repo,
    list_relations,
    list_relations_by_repo,
    list_repos,
    update_repo,
)
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.scheduledtasks.comments import (
    add_human_comment,
    add_run_agent_comment,
    list_run_comments,
    publish_run_mentions,
)
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.scheduledtasks.runs import mark_cancel_intent, pause_active, transition_run
from autowonder.scheduledtasks.schemas import (
    CreateScheduledTaskRequest,
    ScheduledTaskRunView,
    UpdateScheduledTaskRequest,
)
from autowonder.scheduledtasks.service import (
    archive_task,
    aware_utc,
    create_task,
    delete_task,
    enable_task,
    get_task,
    list_runs,
    list_tasks,
    pause_task,
    preview_times,
    task_health,
    update_task,
)
from autowonder.scheduledtasks.trigger import fire_manual
from autowonder.sdlcs.schemas import (
    CreateSdlcRequest,
    CreateStepRequest,
    ReorderStepsRequest,
    UpdateSdlcRequest,
    UpdateStepRequest,
)
from autowonder.sdlcs.service import (
    add_step,
    create_sdlc,
    delete_sdlc,
    delete_step,
    disable_sdlc,
    enable_sdlc,
    get_sdlc,
    list_sdlcs,
    reorder_steps,
    update_sdlc,
    update_step,
)
from autowonder.skills.package import (
    create_from_uploaded_package,
    inspect_package,
    pack_directory,
    skill_bucket,
    update_uploaded_package,
    upload_mcp_package,
)
from autowonder.skills.schemas import CreateSkillRequest, UpdateSkillRequest
from autowonder.skills.service import (
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    update_skill,
)
from autowonder.squads.schemas import AddMembersRequest, CreateSquadRequest
from autowonder.squads.service import (
    add_members,
    create_squad,
    get_squad,
    list_squads,
    remove_member,
)
from autowonder.statemachines.service import get_template, list_templates
from autowonder.storage.objects import get_object_storage
from autowonder.workitems.comments import (
    add_agent_comment,
    add_comment,
    list_comments,
    publish_mentions,
)
from autowonder.workitems.models import Workitem
from autowonder.workitems.schemas import AddCommentRequest, CommentView, CreateWorkitemRequest
from autowonder.workitems.service import (
    AssignmentActor,
    assign,
    assign_as,
    create,
    create_with_origin,
    delete_workitem,
    get_workitem,
    list_workitems,
    transition,
    update_content,
)
from autowonder.workspaces.service import (
    exact_access_level,
    find_member,
    get_current,
    list_by_user,
)

LIST_PROJECTS = "autowonder.list_projects"
CREATE_WORKITEM = "autowonder.create_workitem"
LIST_WORKITEMS = "autowonder.list_workitems"
GET_WORKITEM = "autowonder.get_workitem"
UPDATE_WORKITEM = "autowonder.update_workitem"
DELETE_WORKITEM = "autowonder.delete_workitem"
ASSIGN_WORKITEM = "autowonder.assign_workitem"
ADD_WORKITEM_COMMENT = "autowonder.add_workitem_comment"
LIST_WORKITEM_COMMENTS = "autowonder.list_workitem_comments"
UPLOAD_WORKITEM_DOCUMENT = "autowonder.upload_workitem_document"
WORKITEM_CLI_UPLOAD_TOKEN = "autowonder.workitem_cli_upload_token"
WORKITEM_CLI_DOWNLOAD_TOKEN = "autowonder.workitem_cli_download_token"
LIST_WORKITEM_DOCUMENTS = "autowonder.list_workitem_documents"
DELETE_WORKITEM_DOCUMENT = "autowonder.delete_workitem_document"
TRANSITION_WORKITEM = "autowonder.transition_workitem"
PAUSE_WORKITEM = "autowonder.pause_workitem"
RESUME_WORKITEM = "autowonder.resume_workitem"
LIST_STATUS_TEMPLATES = "autowonder.list_status_templates"
GET_STATUS_TEMPLATE = "autowonder.get_status_template"
CREATE_SDLC = "autowonder.create_sdlc"
LIST_SDLCS = "autowonder.list_sdlcs"
GET_SDLC = "autowonder.get_sdlc"
UPDATE_SDLC = "autowonder.update_sdlc"
DELETE_SDLC = "autowonder.delete_sdlc"
ADD_SDLC_STEP = "autowonder.add_sdlc_step"
UPDATE_SDLC_STEP = "autowonder.update_sdlc_step"
DELETE_SDLC_STEP = "autowonder.delete_sdlc_step"
REORDER_SDLC_STEPS = "autowonder.reorder_sdlc_steps"
ENABLE_SDLC = "autowonder.enable_sdlc"
DISABLE_SDLC = "autowonder.disable_sdlc"
CREATE_AGENT = "autowonder.create_agent"
LIST_AGENTS = "autowonder.list_agents"
GET_AGENT = "autowonder.get_agent"
DELETE_AGENT = "autowonder.delete_agent"
UPDATE_AGENT = "autowonder.update_agent"
SUBMIT_AGENT_FOR_REVIEW = "autowonder.submit_agent_for_review"
PUBLISH_AGENT = "autowonder.publish_agent"
GET_AGENT_VERSION = "autowonder.get_agent_version"
UPDATE_AGENT_CONFIG = "autowonder.update_agent_config"
GET_AGENT_VERSION_STATUS = "autowonder.get_agent_version_status"
BIND_AGENT_REPOS = "autowonder.bind_agent_repos"
BIND_AGENT_SKILLS = "autowonder.bind_agent_skills"
BIND_AGENT_MEMORIES = "autowonder.bind_agent_memories"
UNBIND_AGENT_REPOS = "autowonder.unbind_agent_repos"
UNBIND_AGENT_SKILLS = "autowonder.unbind_agent_skills"
UNBIND_AGENT_MEMORIES = "autowonder.unbind_agent_memories"
CREATE_SKILL = "autowonder.create_skill"
LIST_SKILLS = "autowonder.list_skills"
GET_SKILL = "autowonder.get_skill"
UPDATE_SKILL = "autowonder.update_skill"
DELETE_SKILL = "autowonder.delete_skill"
INSPECT_SKILL_PACKAGE = "autowonder.inspect_skill_package"
UPLOAD_SKILL_PACKAGE = "autowonder.upload_skill_package"
CREATE_SKILL_FROM_PACKAGE = "autowonder.create_skill_from_package"
UPDATE_SKILL_PACKAGE = "autowonder.update_skill_package"
LIST_PLATFORM_SKILLS = "autowonder.list_platform_skills"
INSTALL_PLATFORM_SKILL = "autowonder.install_platform_skill"
LIST_CATEGORIES = "autowonder.list_categories"
GET_CATEGORY = "autowonder.get_category"
CREATE_CATEGORY = "autowonder.create_category"
UPDATE_CATEGORY = "autowonder.update_category"
DELETE_CATEGORY = "autowonder.delete_category"
SET_SKILL_CATEGORY = "autowonder.set_skill_category"
BATCH_SET_SKILL_CATEGORY = "autowonder.batch_set_skill_category"
CREATE_MEMORY = "autowonder.create_memory"
SEARCH_MEMORIES = "autowonder.search_memories"
GET_MEMORY = "autowonder.get_memory"
UPDATE_MEMORY = "autowonder.update_memory"
DEPRECATE_MEMORY = "autowonder.deprecate_memory"
REVIEW_MEMORY = "autowonder.review_memory"
COUNT_PENDING_MEMORIES = "autowonder.count_pending_memories"
DELETE_MEMORY = "autowonder.delete_memory"
LIST_REPOS = "autowonder.list_repos"
GET_REPO = "autowonder.get_repo"
LIST_REPO_RELATIONS = "autowonder.list_repo_relations"
CREATE_REPO_RELATION = "autowonder.create_repo_relation"
DELETE_REPO_RELATION = "autowonder.delete_repo_relation"
CREATE_REPO = "autowonder.create_repo"
UPDATE_REPO = "autowonder.update_repo"
DELETE_REPO = "autowonder.delete_repo"
CREATE_SQUAD = "autowonder.create_squad"
LIST_SQUADS = "autowonder.list_squads"
GET_SQUAD = "autowonder.get_squad"
ADD_AGENT_TO_SQUAD = "autowonder.add_agent_to_squad"
REMOVE_AGENT_FROM_SQUAD = "autowonder.remove_agent_from_squad"
GET_DELIVERY_RECOVERY = "autowonder.get_delivery_recovery"
CONTROL_DELIVERY = "autowonder.control_delivery"
GET_DISPATCH_RUNTIME_TRACE = "autowonder.get_dispatch_runtime_trace"
GET_DISPATCH_ACTIVITIES = "autowonder.get_dispatch_activities"
GET_DISPATCH_TURN = "autowonder.get_dispatch_turn"
GET_DISPATCH_OBSERVATION = "autowonder.get_dispatch_observation"
PAUSE_DISPATCH = "autowonder.pause_dispatch"
SET_AGENT_DEFAULT_SDLC = "autowonder.set_agent_default_sdlc"
CREATE_SCHEDULED_TASK = "autowonder.create_scheduled_task"
LIST_SCHEDULED_TASKS = "autowonder.list_scheduled_tasks"
GET_SCHEDULED_TASK = "autowonder.get_scheduled_task"
UPDATE_SCHEDULED_TASK = "autowonder.update_scheduled_task"
TRANSITION_SCHEDULED_TASK = "autowonder.transition_scheduled_task"
GET_SCHEDULED_TASK_RUN = "autowonder.get_scheduled_task_run"
ADD_SCHEDULED_TASK_RUN_COMMENT = "autowonder.add_scheduled_task_run_comment"
LIST_SCHEDULED_TASK_RUNS = "autowonder.list_scheduled_task_runs"
DELETE_SCHEDULED_TASK = "autowonder.delete_scheduled_task"
LIST_EXECUTORS = "autowonder.list_executors"
GET_EXECUTOR = "autowonder.get_executor"
LIST_EXECUTOR_CLIENT_KINDS = "autowonder.list_executor_client_kinds"
GET_EXECUTOR_LAUNCH_OPTIONS = "autowonder.get_executor_launch_options"
CREATE_EXECUTOR = "autowonder.create_executor"
GET_EXECUTOR_TOKEN = "autowonder.get_executor_token"
DELETE_EXECUTOR = "autowonder.delete_executor"
BUILD_EXECUTOR_LAUNCH_COMMAND = "autowonder.build_executor_launch_command"
GET_EXECUTOR_LAUNCH_CONFIG = "autowonder.get_executor_launch_config"
UPDATE_EXECUTOR_LAUNCH_CONFIG = "autowonder.update_executor_launch_config"

EXECUTOR_MODEL_DESCRIPTION = (
    "Optional. Qoder model id; ignored for non-Qoder executors. Model ids are assembled "
    "server-side from the provider catalog that Redis refreshes automatically, so never "
    "hardcode one: omit this argument and the server resolves a currently usable id, or call "
    "autowonder.get_executor_launch_options for the live list with labels and defaults. An id "
    "you pass explicitly that the catalog no longer offers is rejected with an error instead of "
    "being silently replaced, and the persisted value is returned in the response."
)

_TRANSITION_SCHEDULED_TASK_ACTIONS = {
    "enable",
    "pause",
    "archive",
    "run-now",
    "pause-run",
    "resume-run",
    "cancel-run",
}
_SCHEDULED_TASK_LIST_STATUSES = {"ACTIVE", "PAUSED", "EXHAUSTED", "ARCHIVED"}
_DISPATCH_FORBIDDEN_SCHEDULED_TASK_TOOLS = {
    CREATE_SCHEDULED_TASK,
    LIST_SCHEDULED_TASKS,
    UPDATE_SCHEDULED_TASK,
    TRANSITION_SCHEDULED_TASK,
    DELETE_SCHEDULED_TASK,
}
_DISPATCH_FORBIDDEN_EXECUTOR_TOOLS = {
    CREATE_EXECUTOR,
    GET_EXECUTOR_TOKEN,
    DELETE_EXECUTOR,
    BUILD_EXECUTOR_LAUNCH_COMMAND,
    GET_EXECUTOR_LAUNCH_CONFIG,
    UPDATE_EXECUTOR_LAUNCH_CONFIG,
}
_MEMORY_SCOPE_AGENT = "AGENT"
_MEMORY_SCOPES = {_MEMORY_SCOPE_AGENT, "SQUAD", "ORG"}
_AGENT_UPDATE_FIELDS = {
    "name",
    "roleName",
    "roleCode",
    "businessBackground",
    "responsibilities",
    "sdlcId",
    "evolutionMode",
}
_INT_MAX = 2_147_483_647
_LAUNCH_OVERRIDE_FIELDS = (
    "memoryMode",
    "model",
    "reasoningEffort",
    "contextWindow",
    "maxConcurrentDispatches",
)


@dataclass(frozen=True)
class ToolAccess:
    """工具要求的访问级别，以及是否必须落在某个工作空间。"""

    level: WorkspaceAccessLevel
    workspace_scoped: bool


def _workspace_tool(level: WorkspaceAccessLevel) -> ToolAccess:
    return ToolAccess(level, True)


def _global_tool(level: WorkspaceAccessLevel) -> ToolAccess:
    return ToolAccess(level, False)


_READ = WorkspaceAccessLevel.READ_ONLY
_WRITE = WorkspaceAccessLevel.READ_WRITE
_ADMIN = WorkspaceAccessLevel.ADMIN

TOOL_ACCESS: dict[str, ToolAccess] = {
    LIST_PROJECTS: _global_tool(_READ),
    CREATE_WORKITEM: _workspace_tool(_WRITE),
    LIST_WORKITEMS: _workspace_tool(_READ),
    GET_WORKITEM: _workspace_tool(_READ),
    UPDATE_WORKITEM: _workspace_tool(_WRITE),
    DELETE_WORKITEM: _workspace_tool(_WRITE),
    ASSIGN_WORKITEM: _workspace_tool(_WRITE),
    ADD_WORKITEM_COMMENT: _workspace_tool(_WRITE),
    LIST_WORKITEM_COMMENTS: _workspace_tool(_READ),
    UPLOAD_WORKITEM_DOCUMENT: _workspace_tool(_WRITE),
    WORKITEM_CLI_UPLOAD_TOKEN: _workspace_tool(_WRITE),
    WORKITEM_CLI_DOWNLOAD_TOKEN: _workspace_tool(_READ),
    LIST_WORKITEM_DOCUMENTS: _workspace_tool(_READ),
    DELETE_WORKITEM_DOCUMENT: _workspace_tool(_WRITE),
    TRANSITION_WORKITEM: _workspace_tool(_WRITE),
    PAUSE_WORKITEM: _workspace_tool(_WRITE),
    RESUME_WORKITEM: _workspace_tool(_WRITE),
    LIST_STATUS_TEMPLATES: _workspace_tool(_READ),
    GET_STATUS_TEMPLATE: _workspace_tool(_READ),
    CREATE_SDLC: _workspace_tool(_WRITE),
    LIST_SDLCS: _workspace_tool(_READ),
    GET_SDLC: _workspace_tool(_READ),
    UPDATE_SDLC: _workspace_tool(_WRITE),
    DELETE_SDLC: _workspace_tool(_WRITE),
    ADD_SDLC_STEP: _workspace_tool(_WRITE),
    UPDATE_SDLC_STEP: _workspace_tool(_WRITE),
    DELETE_SDLC_STEP: _workspace_tool(_WRITE),
    REORDER_SDLC_STEPS: _workspace_tool(_WRITE),
    ENABLE_SDLC: _workspace_tool(_WRITE),
    DISABLE_SDLC: _workspace_tool(_WRITE),
    CREATE_AGENT: _workspace_tool(_WRITE),
    LIST_AGENTS: _workspace_tool(_READ),
    GET_AGENT: _workspace_tool(_READ),
    DELETE_AGENT: _workspace_tool(_WRITE),
    UPDATE_AGENT: _workspace_tool(_WRITE),
    SUBMIT_AGENT_FOR_REVIEW: _workspace_tool(_WRITE),
    PUBLISH_AGENT: _workspace_tool(_WRITE),
    GET_AGENT_VERSION: _workspace_tool(_READ),
    UPDATE_AGENT_CONFIG: _workspace_tool(_WRITE),
    GET_AGENT_VERSION_STATUS: _workspace_tool(_READ),
    BIND_AGENT_REPOS: _workspace_tool(_WRITE),
    BIND_AGENT_SKILLS: _workspace_tool(_WRITE),
    BIND_AGENT_MEMORIES: _workspace_tool(_WRITE),
    UNBIND_AGENT_REPOS: _workspace_tool(_WRITE),
    UNBIND_AGENT_SKILLS: _workspace_tool(_WRITE),
    UNBIND_AGENT_MEMORIES: _workspace_tool(_WRITE),
    CREATE_SKILL: _workspace_tool(_WRITE),
    LIST_SKILLS: _workspace_tool(_READ),
    GET_SKILL: _workspace_tool(_READ),
    UPDATE_SKILL: _workspace_tool(_WRITE),
    DELETE_SKILL: _workspace_tool(_WRITE),
    INSPECT_SKILL_PACKAGE: _global_tool(_READ),
    UPLOAD_SKILL_PACKAGE: _workspace_tool(_WRITE),
    CREATE_SKILL_FROM_PACKAGE: _workspace_tool(_WRITE),
    UPDATE_SKILL_PACKAGE: _workspace_tool(_WRITE),
    LIST_PLATFORM_SKILLS: _global_tool(_READ),
    INSTALL_PLATFORM_SKILL: _workspace_tool(_WRITE),
    LIST_CATEGORIES: _workspace_tool(_READ),
    GET_CATEGORY: _workspace_tool(_READ),
    CREATE_CATEGORY: _workspace_tool(_ADMIN),
    UPDATE_CATEGORY: _workspace_tool(_ADMIN),
    DELETE_CATEGORY: _workspace_tool(_ADMIN),
    SET_SKILL_CATEGORY: _workspace_tool(_WRITE),
    BATCH_SET_SKILL_CATEGORY: _workspace_tool(_WRITE),
    CREATE_MEMORY: _workspace_tool(_WRITE),
    SEARCH_MEMORIES: _workspace_tool(_READ),
    GET_MEMORY: _workspace_tool(_READ),
    UPDATE_MEMORY: _workspace_tool(_WRITE),
    DEPRECATE_MEMORY: _workspace_tool(_WRITE),
    REVIEW_MEMORY: _workspace_tool(_WRITE),
    COUNT_PENDING_MEMORIES: _workspace_tool(_READ),
    DELETE_MEMORY: _workspace_tool(_WRITE),
    LIST_REPOS: _workspace_tool(_READ),
    GET_REPO: _workspace_tool(_READ),
    LIST_REPO_RELATIONS: _workspace_tool(_READ),
    CREATE_REPO_RELATION: _workspace_tool(_WRITE),
    DELETE_REPO_RELATION: _workspace_tool(_WRITE),
    CREATE_REPO: _workspace_tool(_WRITE),
    UPDATE_REPO: _workspace_tool(_WRITE),
    DELETE_REPO: _workspace_tool(_WRITE),
    LIST_SQUADS: _workspace_tool(_READ),
    GET_SQUAD: _workspace_tool(_READ),
    ADD_AGENT_TO_SQUAD: _workspace_tool(_WRITE),
    REMOVE_AGENT_FROM_SQUAD: _workspace_tool(_WRITE),
    CREATE_SQUAD: _workspace_tool(_WRITE),
    GET_DELIVERY_RECOVERY: _workspace_tool(_READ),
    CONTROL_DELIVERY: _workspace_tool(_WRITE),
    GET_DISPATCH_RUNTIME_TRACE: _workspace_tool(_READ),
    GET_DISPATCH_ACTIVITIES: _workspace_tool(_READ),
    GET_DISPATCH_TURN: _workspace_tool(_READ),
    GET_DISPATCH_OBSERVATION: _workspace_tool(_READ),
    PAUSE_DISPATCH: _workspace_tool(_WRITE),
    SET_AGENT_DEFAULT_SDLC: _workspace_tool(_WRITE),
    CREATE_SCHEDULED_TASK: _workspace_tool(_WRITE),
    LIST_SCHEDULED_TASKS: _workspace_tool(_READ),
    GET_SCHEDULED_TASK: _workspace_tool(_READ),
    UPDATE_SCHEDULED_TASK: _workspace_tool(_WRITE),
    TRANSITION_SCHEDULED_TASK: _workspace_tool(_WRITE),
    GET_SCHEDULED_TASK_RUN: _workspace_tool(_READ),
    LIST_SCHEDULED_TASK_RUNS: _workspace_tool(_READ),
    ADD_SCHEDULED_TASK_RUN_COMMENT: _workspace_tool(_WRITE),
    DELETE_SCHEDULED_TASK: _workspace_tool(_WRITE),
    LIST_EXECUTORS: _workspace_tool(_READ),
    GET_EXECUTOR: _workspace_tool(_READ),
    LIST_EXECUTOR_CLIENT_KINDS: _workspace_tool(_READ),
    GET_EXECUTOR_LAUNCH_OPTIONS: _workspace_tool(_READ),
    CREATE_EXECUTOR: _workspace_tool(_ADMIN),
    GET_EXECUTOR_TOKEN: _workspace_tool(_ADMIN),
    DELETE_EXECUTOR: _workspace_tool(_ADMIN),
    BUILD_EXECUTOR_LAUNCH_COMMAND: _workspace_tool(_ADMIN),
    GET_EXECUTOR_LAUNCH_CONFIG: _workspace_tool(_ADMIN),
    UPDATE_EXECUTOR_LAUNCH_CONFIG: _workspace_tool(_ADMIN),
}


@dataclass
class ToolExecutionContext:
    """一次工具调用解析后的工作空间、用户和调度边界。"""

    workspace_id: int | None
    user_id: int
    access_level: WorkspaceAccessLevel | None
    token_id: int
    credential_type: CredentialType
    dispatch: Dispatch | None


def tool_access(name: str) -> ToolAccess:
    """未登记的工具名直接拒绝。"""
    access = TOOL_ACCESS.get(name)
    if access is None:
        raise BizError(ErrorCode.MCP_TOOL_NOT_FOUND)
    return access


async def list_tools_for_principal(
    session: AsyncSession,
    principal: Principal,
) -> list[dict[str, Any]]:
    """按凭证级别裁剪工具，并改写 workspaceId 与模型说明。"""
    tools = [copy.deepcopy(tool) for tool in list_tools()]
    scope_level = principal.access_level
    if scope_level is None:
        workspaces = await list_by_user(session, principal.user_id)
        read_desc = _compact_workspace_description(workspaces, False)
        write_desc = _compact_workspace_description(workspaces, True)
        if read_desc is not None:
            tools = _apply_workspace_descriptions(tools, read_desc, write_desc)
        return await _apply_executor_model_descriptions(tools)
    tools = [
        tool for tool in tools if scope_level.allows(tool_access(str(tool["name"])).level)
    ]
    scoped = await get_current(session, principal.workspace_id)  # type: ignore[arg-type]
    workspace_name = "null" if scoped.name is None else scoped.name
    desc = "Workspace: " + str(principal.workspace_id) + "=" + workspace_name
    described = _apply_workspace_descriptions(tools, desc, desc)
    return await _apply_executor_model_descriptions(described)


async def invoke_tool(
    session: AsyncSession,
    principal: Principal,
    name: str,
    arguments: dict[str, Any] | None,
) -> object:
    """解析工作空间和调度边界后调用工具，并在结束后恢复上下文。"""
    safe_args = {} if arguments is None else arguments
    context = await _resolve_dispatch_boundary(
        session,
        await _resolve_execution_context(session, principal, name, safe_args),
    )
    ambient = current()
    previous_workspace = ambient.workspace_id
    previous_level = ambient.access_level
    if context.workspace_id is not None:
        ambient.workspace_id = context.workspace_id
        level = context.access_level
        ambient.access_level = None if level is None else level.name
    try:
        result = await _invoke(session, context, name, safe_args)
        await _audit_run_tool(session, context, name, result, None)
        await session.commit()
        from autowonder.dispatch.pending import drive_remembered

        await drive_remembered(session)
        return result
    except Exception as failure:
        schema_not_ready = (
            isinstance(failure, BizError)
            and failure.code == ErrorCode.SCHEDULED_TASK_SCHEMA_NOT_READY.code
        )
        if not schema_not_ready:
            await _audit_run_tool(session, context, name, None, failure)
        raise
    finally:
        ambient.workspace_id = previous_workspace
        ambient.access_level = previous_level


async def _resolve_execution_context(
    session: AsyncSession,
    principal: Principal,
    name: str,
    args: dict[str, Any],
) -> ToolExecutionContext:
    access = tool_access(name)
    requested = _workspace_id_argument(args)
    if principal.is_workspace_scoped():
        scope_workspace_id = principal.workspace_id
        if requested is not None and requested != scope_workspace_id:
            raise BizError(ErrorCode.NO_PERMISSION, "任务作用域令牌不能访问其他工作空间")
        scope_level = principal.access_level
        if scope_level is None:
            raise BizError(ErrorCode.NO_PERMISSION)
        if principal.credential_type is CredentialType.CONVERSATION:
            live = await _active_access_level(session, scope_workspace_id, principal.user_id)
            scope_level = _minimum_level(scope_level, live)
        if not scope_level.allows(access.level):
            raise BizError(ErrorCode.NO_PERMISSION)
        return ToolExecutionContext(
            scope_workspace_id,
            principal.user_id,
            scope_level,
            principal.token_id,
            principal.credential_type,
            None,
        )
    if not access.workspace_scoped:
        return ToolExecutionContext(
            None,
            principal.user_id,
            None,
            principal.token_id,
            principal.credential_type,
            None,
        )
    if requested is None:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "工作空间域工具必须传入 workspaceId，可通过 autowonder.list_projects 获取",
        )
    member_level = await _active_access_level(session, requested, principal.user_id)
    if not member_level.allows(access.level):
        raise BizError(ErrorCode.NO_PERMISSION)
    return ToolExecutionContext(
        requested,
        principal.user_id,
        member_level,
        principal.token_id,
        principal.credential_type,
        None,
    )


async def _resolve_dispatch_boundary(
    session: AsyncSession,
    context: ToolExecutionContext,
) -> ToolExecutionContext:
    if not _is_dispatch(context):
        return context
    dispatch = await find_dispatch(session, -context.token_id)
    if (
        dispatch is None
        or dispatch.tenant_id != context.workspace_id
        or dispatch.agent_id is None
        or dispatch.agent_id <= 0
    ):
        raise BizError(ErrorCode.NO_PERMISSION)
    if execution_source(dispatch) == "SCHEDULED_TASK_RUN":
        require_scheduled_capability()
    return ToolExecutionContext(
        context.workspace_id,
        context.user_id,
        context.access_level,
        context.token_id,
        context.credential_type,
        dispatch,
    )


async def _active_access_level(
    session: AsyncSession,
    workspace_id: int | None,
    user_id: int,
) -> WorkspaceAccessLevel:
    member = await find_member(session, workspace_id, user_id)  # type: ignore[arg-type]
    if member is not None and member.status == 0 and member.is_deleted == 0:
        return exact_access_level(member.access_level)
    if await is_system_admin(session, user_id):
        return WorkspaceAccessLevel.ADMIN
    raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)


def _minimum_level(
    left: WorkspaceAccessLevel,
    right: WorkspaceAccessLevel,
) -> WorkspaceAccessLevel:
    if left.value <= right.value:
        return left
    return right


def _compact_workspace_description(workspaces: list[Any], write_only: bool) -> str | None:
    ordered = sorted(workspaces, key=lambda item: item.id)
    if write_only:
        kept = []
        for workspace in ordered:
            level_name = workspace.access_level
            if level_name is None:
                continue
            if exact_access_level(level_name).allows(WorkspaceAccessLevel.READ_WRITE):
                kept.append(workspace)
        ordered = kept
    if len(ordered) == 0:
        return None
    parts = [str(workspace.id) + "=" + str(workspace.name) for workspace in ordered]
    return "Workspace: " + ";".join(parts)


def _apply_workspace_descriptions(
    tools: list[dict[str, Any]],
    read_desc: str | None,
    write_desc: str | None,
) -> list[dict[str, Any]]:
    for tool in tools:
        access = tool_access(str(tool["name"]))
        if not access.workspace_scoped:
            continue
        desc = write_desc if access.level.allows(WorkspaceAccessLevel.READ_WRITE) else read_desc
        if desc is None:
            desc = "Workspace: none"
        _replace_property_description(tool, "workspaceId", desc)
    return tools


async def _apply_executor_model_descriptions(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    live = await _live_model_id_suffix()
    if live is None:
        return tools
    for tool in tools:
        name = tool.get("name")
        if name in {CREATE_EXECUTOR, UPDATE_EXECUTOR_LAUNCH_CONFIG}:
            _replace_property_description(tool, "model", EXECUTOR_MODEL_DESCRIPTION + live)
    return tools


async def _live_model_id_suffix() -> str | None:
    parts: list[str] = []
    for label, provider in (("qoder", PROVIDER_QODER), ("qodercn", PROVIDER_QODER_CN)):
        ids = await _live_model_ids(provider)
        if len(ids) > 0:
            parts.append(label + ": " + ", ".join(ids))
    if len(parts) == 0:
        return None
    return " Current ids — " + "; ".join(parts) + "."


async def _live_model_ids(provider: str) -> list[str]:
    try:
        catalog = await read_catalog(provider)
    except Exception:
        return []
    return [item.id for item in catalog.models]


def _replace_property_description(
    tool: dict[str, Any],
    prop: str,
    description: str,
) -> None:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    existing = properties.get(prop)
    if not isinstance(existing, dict):
        return
    replaced = dict(existing)
    replaced["description"] = description
    new_properties = dict(properties)
    new_properties[prop] = replaced
    new_schema = dict(schema)
    new_schema["properties"] = new_properties
    tool["inputSchema"] = new_schema


async def _invoke(
    session: AsyncSession,
    context: ToolExecutionContext,
    name: str,
    args: dict[str, Any],
) -> object:
    if _is_dispatch(context) and (
        name in _DISPATCH_FORBIDDEN_SCHEDULED_TASK_TOOLS
        or name in _DISPATCH_FORBIDDEN_EXECUTOR_TOOLS
    ):
        raise BizError(ErrorCode.NO_PERMISSION)
    workspace_id = _workspace(context)
    user_id = context.user_id
    if name == LIST_PROJECTS:
        return await _list_projects(session, context)
    if name == CREATE_WORKITEM:
        return await _create_workitem(session, context, args)
    if name == LIST_WORKITEMS:
        page = await list_workitems(
            session,
            workspace_id,
            user_id,
            _text(args, "workType"),
            _lng(args, "statusNodeId"),
            _text(args, "statusCategory"),
            _text(args, "assigneeType"),
            _lng(args, "assigneeRef"),
            _bool(args, "pendingDecisionOnly", False),
            _text(args, "mineScope"),
            _text(args, "keyword"),
            _text(args, "tag"),
            None,
            _integer(args, "page", 1),
            _integer(args, "size", 20),
        )
        return page.list_
    if name in {
        GET_DISPATCH_RUNTIME_TRACE,
        GET_DISPATCH_ACTIVITIES,
        GET_DISPATCH_TURN,
        GET_DISPATCH_OBSERVATION,
    }:
        return await _read_dispatch_trace(session, context, name, args)
    if name == GET_WORKITEM:
        return await get_workitem(session, _required_long(args, "id"), workspace_id, user_id)
    if name == UPDATE_WORKITEM:
        return await update_content(
            session,
            _required_long(args, "id"),
            _text(args, "title"),
            _text(args, "contentMd"),
            workspace_id,
            user_id,
        )
    if name == DELETE_WORKITEM:
        await delete_workitem(session, _required_long(args, "id"), workspace_id, user_id)
        return {"deleted": True}
    if name == ASSIGN_WORKITEM:
        return await _assign_workitem(session, context, args)
    if name == ADD_WORKITEM_COMMENT:
        return await _add_workitem_comment(session, context, args)
    if name == LIST_WORKITEM_COMMENTS:
        return await _list_workitem_comments(session, context, args)
    if name == UPLOAD_WORKITEM_DOCUMENT:
        return await _upload_document(session, context, args)
    if name == WORKITEM_CLI_UPLOAD_TOKEN:
        return await mint_upload_token(
            session,
            CliCredentialType(context.credential_type.value),
            user_id,
            _required_long(args, "id"),
        )
    if name == WORKITEM_CLI_DOWNLOAD_TOKEN:
        return await mint_download_token(
            session,
            CliCredentialType(context.credential_type.value),
            user_id,
            _required_long(args, "id"),
        )
    if name == LIST_WORKITEM_DOCUMENTS:
        return await _list_documents(session, context, args)
    if name == DELETE_WORKITEM_DOCUMENT:
        await _delete_document(session, context, args)
        return {"deleted": True}
    if name in {TRANSITION_WORKITEM, PAUSE_WORKITEM, RESUME_WORKITEM}:
        return await transition(
            session,
            _required_long(args, "id"),
            _required_long(args, "toNodeId"),
            workspace_id,
            user_id,
            None,
            None,
        )
    if name == LIST_STATUS_TEMPLATES:
        return await list_templates(session, workspace_id, _required_string(args, "workType"))
    if name == GET_STATUS_TEMPLATE:
        return await get_template(session, _required_long(args, "id"))
    if name == CREATE_SDLC:
        return await create_sdlc(
            session,
            CreateSdlcRequest.model_validate(args),
            workspace_id,
            user_id,
        )
    if name == LIST_SDLCS:
        return await list_sdlcs(
            session,
            workspace_id,
            _text(args, "workType"),
            _text(args, "status"),
            _squad_filter(args),
            _integer(args, "page", 1),
            _integer(args, "size", 20),
        )
    if name == GET_SDLC:
        return await get_sdlc(session, _required_long(args, "id"))
    if name == UPDATE_SDLC:
        return await update_sdlc(
            session,
            _required_long(args, "id"),
            UpdateSdlcRequest.model_validate(args),
            workspace_id,
            user_id,
        )
    if name == DELETE_SDLC:
        await delete_sdlc(session, _required_long(args, "id"), workspace_id, user_id)
        return {"deleted": True}
    if name == ADD_SDLC_STEP:
        return await add_step(
            session,
            _required_long(args, "sdlcId"),
            CreateStepRequest.model_validate(args),
            workspace_id,
            user_id,
        )
    if name == UPDATE_SDLC_STEP:
        return await update_step(
            session,
            _required_long(args, "sdlcId"),
            _required_long(args, "stepId"),
            UpdateStepRequest.model_validate(args),
            workspace_id,
            user_id,
        )
    if name == DELETE_SDLC_STEP:
        await delete_step(
            session,
            _required_long(args, "sdlcId"),
            _required_long(args, "stepId"),
            workspace_id,
            user_id,
        )
        return {"deleted": True}
    if name == REORDER_SDLC_STEPS:
        await reorder_steps(
            session,
            _required_long(args, "sdlcId"),
            ReorderStepsRequest.model_validate(args),
            workspace_id,
            user_id,
        )
        return {"reordered": True}
    if name == ENABLE_SDLC:
        return await enable_sdlc(
            session,
            _required_long(args, "id"),
            workspace_id,
            user_id,
            _lng(args, "statusTemplateId"),
        )
    if name == DISABLE_SDLC:
        await disable_sdlc(session, _required_long(args, "id"), workspace_id, user_id)
        return {"disabled": True}
    if name == CREATE_AGENT:
        return await create_agent(
            session,
            CreateAgentRequest.model_validate(_normalize_agent_args(args)),
            workspace_id,
            user_id,
        )
    if name == LIST_AGENTS:
        return await list_agents(
            session,
            workspace_id,
            _text(args, "status"),
            None,
            _squad_filter(args),
            _integer(args, "page", 1),
            _integer(args, "size", 20),
        )
    if name == GET_AGENT:
        return await get_agent(session, _required_long(args, "id"), workspace_id)
    if name == DELETE_AGENT:
        await delete_agent(session, _required_long(args, "id"), workspace_id, user_id)
        return {"deleted": True}
    if name == UPDATE_AGENT:
        return await _update_agent(session, context, args)
    if name == SUBMIT_AGENT_FOR_REVIEW:
        return await submit_agent(session, _required_long(args, "id"), workspace_id, user_id)
    if name == PUBLISH_AGENT:
        return await approve_agent(
            session,
            _required_long(args, "id"),
            workspace_id,
            user_id,
            None,
        )
    if name == GET_AGENT_VERSION:
        return await get_version(
            session,
            _required_long(args, "agentId"),
            _required_long(args, "versionNo"),
            workspace_id,
        )
    if name == UPDATE_AGENT_CONFIG:
        normalized = _normalize_agent_args(args)
        return await edit_config(
            session,
            _required_long(args, "agentId"),
            UpdateConfigRequest.model_validate(normalized),
            workspace_id,
            user_id,
            _present_agent_fields(normalized),
        )
    if name == GET_AGENT_VERSION_STATUS:
        agent_id = _required_long(args, "id")
        return {
            "agent": await get_agent(session, agent_id, workspace_id),
            "versions": await list_versions(session, agent_id, workspace_id),
        }
    if name == BIND_AGENT_REPOS:
        return await _bind_repos(session, context, args)
    if name == BIND_AGENT_SKILLS:
        return await _bind_skills(session, context, args)
    if name == BIND_AGENT_MEMORIES:
        return await _bind_memories(session, context, args)
    if name == UNBIND_AGENT_REPOS:
        return await _unbind_repos(session, context, args)
    if name == UNBIND_AGENT_SKILLS:
        return await _unbind_skills(session, context, args)
    if name == UNBIND_AGENT_MEMORIES:
        return await _unbind_memories(session, context, args)
    if name == CREATE_SKILL:
        return await create_skill(
            session,
            CreateSkillRequest.model_validate(args),
            workspace_id,
            user_id,
        )
    if name == LIST_SKILLS:
        return await list_skills(
            session,
            workspace_id,
            _text(args, "type"),
            _positive_category_id(args, "categoryId", True),
            _bool(args, "includeDescendants", True),
            _bool(args, "uncategorized", False),
            _integer(args, "page", 1),
            _integer(args, "size", 20),
        )
    if name == GET_SKILL:
        return await get_skill(session, _required_long(args, "id"))
    if name == UPDATE_SKILL:
        return await update_skill(
            session,
            _required_long(args, "id"),
            UpdateSkillRequest.model_validate(args),
            workspace_id,
            user_id,
        )
    if name == DELETE_SKILL:
        await delete_skill(session, _required_long(args, "id"), workspace_id, user_id)
        return {"deleted": True}
    if name == INSPECT_SKILL_PACKAGE:
        return inspect_package(_package_file_name(args), _package_bytes(args))
    if name == UPLOAD_SKILL_PACKAGE:
        uploaded = upload_mcp_package(
            get_object_storage(),
            skill_bucket(),
            _package_file_name(args),
            _package_bytes(args),
            _text(args, "type"),
            _text(args, "name"),
            _text(args, "description"),
            _string_list(args, "providers"),
            _text(args, "expectedMd5"),
            workspace_id,
        )
        return _uploaded_package(uploaded)
    if name == CREATE_SKILL_FROM_PACKAGE:
        return await create_from_uploaded_package(
            session,
            get_object_storage(),
            skill_bucket(),
            _required_string(args, "packageOssRef"),
            _text(args, "type"),
            _text(args, "name"),
            _text(args, "description"),
            _string_list(args, "providers"),
            _text(args, "expectedMd5"),
            _text(args, "idempotencyKey"),
            workspace_id,
            user_id,
        )
    if name == UPDATE_SKILL_PACKAGE:
        return await update_uploaded_package(
            session,
            get_object_storage(),
            skill_bucket(),
            _required_long(args, "id"),
            _required_string(args, "packageOssRef"),
            _text(args, "name"),
            _text(args, "description"),
            _string_list(args, "providers"),
            _text(args, "expectedMd5"),
            _text(args, "idempotencyKey"),
            workspace_id,
            user_id,
        )
    if name == LIST_PLATFORM_SKILLS:
        return list_platform_skills()
    if name == LIST_CATEGORIES:
        return await _list_categories(session, context, args)
    if name == GET_CATEGORY:
        return await get_category(session, _required_category_id(args, "id"), workspace_id)
    if name == CREATE_CATEGORY:
        return await create_category(
            session,
            CreateCategoryFields(
                _required_string(args, "name"),
                _positive_category_id(args, "parentId", True),
                _text(args, "description"),
            ),
            workspace_id,
            user_id,
        )
    if name == UPDATE_CATEGORY:
        return await update_category(
            session,
            _required_category_id(args, "id"),
            _category_update(args),
            workspace_id,
            user_id,
        )
    if name == DELETE_CATEGORY:
        await delete_category(
            session,
            _required_category_id(args, "id"),
            workspace_id,
            user_id,
        )
        return {"deleted": True}
    if name == SET_SKILL_CATEGORY:
        category_id = await set_skill_category(
            session,
            _required_category_id(args, "skillId"),
            _required_nullable_category_id(args),
            workspace_id,
            user_id,
        )
        return {"categoryId": category_id}
    if name == BATCH_SET_SKILL_CATEGORY:
        return await batch_set_skill_category(
            session,
            _category_skill_ids(args),
            _required_nullable_category_id(args),
            workspace_id,
            user_id,
        )
    if name == CREATE_MEMORY:
        return await _create_memory(session, context, args)
    if name == SEARCH_MEMORIES:
        return await _search_memories(session, context, args)
    if name == GET_MEMORY:
        return await get_scoped(session, _required_long(args, "id"), workspace_id)
    if name == UPDATE_MEMORY:
        memory_id = _required_long(args, "id")
        await get_scoped(session, memory_id, workspace_id)
        return await update_memory(
            session,
            memory_id,
            UpdateMemoryRequest.model_validate(args),
            workspace_id,
            user_id,
        )
    if name == DEPRECATE_MEMORY:
        memory_id = _required_long(args, "id")
        await get_scoped(session, memory_id, workspace_id)
        return await deprecate_from_mcp(
            session,
            memory_id,
            _text(args, "comment"),
            workspace_id,
            user_id,
        )
    if name == REVIEW_MEMORY:
        memory_id = _required_long(args, "id")
        await get_scoped(session, memory_id, workspace_id)
        await review_memory(
            session,
            memory_id,
            ReviewRequest.model_validate(args),
            workspace_id,
            user_id,
        )
        return await get_scoped(session, memory_id, workspace_id)
    if name == COUNT_PENDING_MEMORIES:
        return {"count": await count_pending_reviews(session, workspace_id)}
    if name == DELETE_MEMORY:
        memory_id = _required_long(args, "id")
        await get_scoped(session, memory_id, workspace_id)
        await delete_memory(session, memory_id, workspace_id, user_id)
        return {"deleted": True}
    if name == LIST_REPOS:
        return await list_repos(
            session,
            workspace_id,
            _integer(args, "page", 1),
            _integer(args, "size", 100),
        )
    if name == GET_REPO:
        return await get_repo(session, _required_long(args, "id"))
    if name == LIST_REPO_RELATIONS:
        repo_id = _lng(args, "repoId")
        if repo_id is not None:
            await get_repo(session, repo_id)
            return await list_relations_by_repo(session, workspace_id, repo_id)
        return await list_relations(session, workspace_id)
    if name == CREATE_REPO_RELATION:
        return await create_relation(
            session,
            CreateRelationRequest(
                from_repo_id=_required_long(args, "fromRepoId"),
                to_repo_id=_required_long(args, "toRepoId"),
                relation_type=_required_string(args, "relationType"),
                description=_text(args, "description"),
            ),
            workspace_id,
            user_id,
        )
    if name == DELETE_REPO_RELATION:
        await delete_relation(session, _required_long(args, "id"), workspace_id)
        return {"deleted": True}
    if name == CREATE_REPO:
        return await create_repo(
            session,
            CreateRepoRequest(
                name=_required_string(args, "name"),
                url=_required_string(args, "url"),
                default_branch=_text(args, "defaultBranch"),
                description=_text(args, "description"),
            ),
            workspace_id,
            user_id,
        )
    if name == UPDATE_REPO:
        return await update_repo(
            session,
            _required_long(args, "id"),
            _repo_update(args),
            workspace_id,
            user_id,
        )
    if name == DELETE_REPO:
        await delete_repo(session, _required_long(args, "id"), workspace_id, user_id)
        return {"deleted": True}
    if name == LIST_SQUADS:
        return await list_squads(session, _integer(args, "page", 1), _integer(args, "size", 20))
    if name == GET_SQUAD:
        return await get_squad(session, _required_long(args, "id"))
    if name == ADD_AGENT_TO_SQUAD:
        await add_members(
            session,
            _required_long(args, "squadId"),
            AddMembersRequest(agent_ids=[_required_long(args, "agentId")]),
            workspace_id,
        )
        return {"added": True}
    if name == REMOVE_AGENT_FROM_SQUAD:
        await remove_member(
            session,
            _required_long(args, "squadId"),
            _required_long(args, "agentId"),
            workspace_id,
        )
        return {"removed": True}
    if name == CREATE_SQUAD:
        return await create_squad(
            session,
            CreateSquadRequest(
                name=_required_string(args, "name"),
                description=_text(args, "description"),
            ),
            workspace_id,
            user_id,
        )
    if name == SET_AGENT_DEFAULT_SDLC:
        return await _set_agent_default_sdlc(session, context, args)
    if name == INSTALL_PLATFORM_SKILL:
        return await _install_platform_skill(session, context, _required_string(args, "skillId"))
    if name == GET_DELIVERY_RECOVERY:
        return await state(session, workspace_id, _required_long(args, "workitemId"))
    if name == CONTROL_DELIVERY:
        return await _control_delivery(session, context, args)
    if name == PAUSE_DISPATCH:
        dispatch = await request_workitem_pause(
            session,
            workspace_id,
            _required_long(args, "workitemId"),
            _required_long(args, "dispatchId"),
            user_id,
        )
        return {"dispatchId": dispatch.id, "status": dispatch.status}
    if name == CREATE_SCHEDULED_TASK:
        return await _create_scheduled_task(session, context, args)
    if name == LIST_SCHEDULED_TASKS:
        return await _list_scheduled_tasks(session, context, args)
    if name == GET_SCHEDULED_TASK:
        return await _get_scheduled_task(session, context, args)
    if name == UPDATE_SCHEDULED_TASK:
        return await _update_scheduled_task(session, context, args)
    if name == TRANSITION_SCHEDULED_TASK:
        return await _transition_scheduled_task(session, context, args)
    if name == GET_SCHEDULED_TASK_RUN:
        return await _get_scheduled_task_run(session, context, args)
    if name == ADD_SCHEDULED_TASK_RUN_COMMENT:
        return await _add_scheduled_task_run_comment(session, context, args)
    if name == LIST_SCHEDULED_TASK_RUNS:
        return await _list_scheduled_task_runs(session, context, args)
    if name == DELETE_SCHEDULED_TASK:
        return await _delete_scheduled_task(session, context, args)
    if name == LIST_EXECUTORS:
        return await _list_executors(session, context, args)
    if name == GET_EXECUTOR:
        return await executor_detail(session, _required_long(args, "id"), workspace_id)
    if name == LIST_EXECUTOR_CLIENT_KINDS:
        return _creatable_client_kinds()
    if name == GET_EXECUTOR_LAUNCH_OPTIONS:
        return await _launch_options(_required_string(args, "clientKind"))
    if name == CREATE_EXECUTOR:
        return await _create_executor(session, context, args)
    if name == GET_EXECUTOR_TOKEN:
        executor_id = _required_long(args, "id")
        token = await executor_token(session, executor_id, workspace_id)
        return {"id": executor_id, "token": token}
    if name == DELETE_EXECUTOR:
        await delete_executor(session, _required_long(args, "id"), workspace_id, user_id)
        return {"deleted": True}
    if name == BUILD_EXECUTOR_LAUNCH_COMMAND:
        _reject_launch_overrides(args)
        return await build_for_executor(
            session,
            _required_long(args, "id"),
            workspace_id,
            _text(args, "os"),
            _bool(args, "debug", False),
            _text(args, "shell"),
        )
    if name == GET_EXECUTOR_LAUNCH_CONFIG:
        return await get_launch_config(
            session,
            _required_long(args, "id"),
            workspace_id,
            user_id,
        )
    if name == UPDATE_EXECUTOR_LAUNCH_CONFIG:
        return await _update_launch_config(session, context, args)
    raise BizError(ErrorCode.MCP_TOOL_NOT_FOUND)


async def _list_projects(session: AsyncSession, context: ToolExecutionContext) -> object:
    if context.workspace_id is None:
        return await list_by_user(session, context.user_id)
    view = await get_current(session, context.workspace_id)
    level = context.access_level
    if level is not None:
        view.access_level = level.name
    return [view]


async def _create_workitem(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    scheduled = _iso_instant(args, "scheduledStartAt")
    request = CreateWorkitemRequest.model_validate(args)
    request.scheduled_start_at = scheduled
    workspace_id = _workspace(context)
    if _is_dispatch(context):
        dispatch = _require_dispatch(context)
        if execution_source(dispatch) == "SCHEDULED_TASK_RUN":
            return await create_with_origin(
                session,
                request,
                workspace_id,
                context.user_id,
                "SCHEDULED_TASK_RUN",
                dispatch.workitem_id,
                scheduled,
            )
    return await create(session, request, workspace_id, context.user_id, scheduled)


async def _assign_workitem(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    workitem_id = _required_long(args, "id")
    scheduled = _iso_instant(args, "scheduledStartAt")
    workspace_id = _workspace(context)
    if _is_dispatch(context):
        dispatch = _require_dispatch_scope(context, workitem_id)
        agent = await get_agent(session, dispatch.agent_id, workspace_id)
        name = agent.name
        if name is None or name.strip() == "":
            name = "数字人"
        return await assign_as(
            session,
            workitem_id,
            _required_string(args, "assigneeType"),
            _lng(args, "assigneeRef"),
            _lng(args, "sdlcId"),
            _lng(args, "squadId"),
            scheduled,
            workspace_id,
            context.user_id,
            AssignmentActor("AGENT", dispatch.agent_id, name),
        )
    return await assign(
        session,
        workitem_id,
        _required_string(args, "assigneeType"),
        _lng(args, "assigneeRef"),
        _lng(args, "sdlcId"),
        _lng(args, "squadId"),
        scheduled,
        workspace_id,
        context.user_id,
    )


async def _add_workitem_comment(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    request = AddCommentRequest.model_validate(args)
    workitem_id = _required_long(args, "id")
    workspace_id = _workspace(context)
    owner = _require_dispatch(context) if _is_dispatch(context) else None
    if owner is not None and execution_source(owner) == "SCHEDULED_TASK_RUN":
        comment = await _add_scheduled_run_dispatch_comment(
            session,
            context,
            owner,
            workitem_id,
            request.content_md,
            request.target_agent_ids,
            request.target_human_ids,
        )
    elif owner is not None:
        scoped = _require_dispatch_scope(context, workitem_id)
        comment, notices = await add_agent_comment(
            session,
            workitem_id,
            request.content_md,
            request.target_human_ids,
            workspace_id,
            scoped.agent_id,
            context.user_id,
        )
        await publish_mentions(session, notices)
    else:
        comment, notices = await add_comment(
            session,
            workitem_id,
            request.content_md,
            request.target_human_ids,
            workspace_id,
            context.user_id,
        )
        await publish_mentions(session, notices)
    if owner is None or execution_source(owner) == "WORKITEM":
        await create_for_comment(
            session,
            workspace_id,
            workitem_id,
            comment.id,
            request.content_md,
            request.target_agent_ids,
            context.user_id,
        )
    return comment


async def _list_workitem_comments(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    workitem_id = _required_long(args, "id")
    if _is_dispatch(context):
        owner = _require_dispatch(context)
        if execution_source(owner) == "SCHEDULED_TASK_RUN":
            if owner.workitem_id != workitem_id:
                raise BizError(ErrorCode.NO_PERMISSION)
            return await list_run_comments(session, _workspace(context), workitem_id)
    return await list_comments(session, workitem_id)


async def _upload_document(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    owner_id = _required_long(args, "id")
    filename = _required_string(args, "filename")
    payload = _document_bytes(args)
    workspace_id = _workspace(context)
    source_path = _text(args, "sourcePath")
    if _scheduled_task_document_source(args):
        require_scheduled_capability()
        if _is_dispatch(context):
            raise BizError(ErrorCode.NO_PERMISSION)
        return await upload_mcp(
            session,
            ArtifactOwner("SCHEDULED_TASK", owner_id),
            filename,
            payload,
            workspace_id,
            context.user_id,
            source_path,
        )
    return await upload_mcp(
        session,
        ArtifactOwner("WORKITEM", owner_id),
        filename,
        payload,
        workspace_id,
        context.user_id,
        source_path,
    )


async def _list_documents(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    owner_id = _required_long(args, "id")
    workspace_id = _workspace(context)
    if _scheduled_task_document_source(args):
        require_scheduled_capability()
        if _is_dispatch(context):
            await _require_dispatch_run_of_task(session, context, owner_id)
        return await list_requirement_documents(
            session,
            ArtifactOwner("SCHEDULED_TASK", owner_id),
            workspace_id,
        )
    return await list_requirement_documents(
        session,
        ArtifactOwner("WORKITEM", owner_id),
        workspace_id,
    )


async def _delete_document(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> None:
    owner_id = _required_long(args, "id")
    artifact_id = _required_long(args, "artifactId")
    workspace_id = _workspace(context)
    if _scheduled_task_document_source(args):
        require_scheduled_capability()
        if _is_dispatch(context):
            raise BizError(ErrorCode.NO_PERMISSION)
        await delete_requirement_document(
            session,
            ArtifactOwner("SCHEDULED_TASK", owner_id),
            artifact_id,
            workspace_id,
            context.user_id,
        )
        return
    await delete_requirement_document(
        session,
        ArtifactOwner("WORKITEM", owner_id),
        artifact_id,
        workspace_id,
        context.user_id,
    )


async def _update_agent(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    normalized = _normalize_agent_args(args)
    action = _text(args, "lifecycleAction")
    workspace_id = _workspace(context)
    agent_id = _required_long(args, "id")
    if action is not None:
        if len(_present_agent_fields(normalized)) > 0:
            raise BizError(
                ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
                "lifecycleAction 与字段更新参数互斥，请只传其中一类",
            )
        lowered = action.strip().lower()
        if lowered == "offline":
            return await offline_agent(session, agent_id, workspace_id, context.user_id)
        if lowered == "online":
            return await online_agent(session, agent_id, workspace_id, context.user_id)
        raise BizError(
            ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
            "lifecycleAction 仅支持 offline/online",
        )
    return await update_agent(
        session,
        agent_id,
        UpdateAgentRequest.model_validate(normalized),
        workspace_id,
        context.user_id,
        _present_agent_fields(normalized),
    )


async def _bind_repos(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    agent_id = _required_long(args, "agentId")
    repo_ids = _required_long_list(args, "repoIds")
    level = _text(args, "permLevel")
    workspace_id = _workspace(context)
    for repo_id in repo_ids:
        await add_repo_perm(
            session,
            agent_id,
            RepoPermRequest(repo_id=repo_id, perm_level=level),
            workspace_id,
            context.user_id,
        )
    return {"repoIds": repo_ids}


async def _bind_skills(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    agent_id = _required_long(args, "agentId")
    skill_ids = _required_long_list(args, "skillIds")
    workspace_id = _workspace(context)
    for skill_id in skill_ids:
        await add_skill(
            session,
            agent_id,
            SkillRequest(skill_id=skill_id),
            workspace_id,
            context.user_id,
        )
    return {"skillIds": skill_ids}


async def _bind_memories(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    agent_id = _required_long(args, "agentId")
    memory_ids = _required_long_list(args, "memoryIds")
    source = _text(args, "source")
    workspace_id = _workspace(context)
    for memory_id in memory_ids:
        await add_memory_ref(
            session,
            agent_id,
            MemoryRefRequest(memory_id=memory_id, source=source),
            workspace_id,
            context.user_id,
        )
    return {"memoryIds": memory_ids}


async def _unbind_repos(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    agent_id = _required_long(args, "agentId")
    repo_ids = _required_long_list(args, "repoIds")
    workspace_id = _workspace(context)
    for repo_id in repo_ids:
        await remove_repo_perm(session, agent_id, repo_id, workspace_id, context.user_id)
    return {"repoIds": repo_ids}


async def _unbind_skills(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    agent_id = _required_long(args, "agentId")
    skill_ids = _required_long_list(args, "skillIds")
    workspace_id = _workspace(context)
    for skill_id in skill_ids:
        await remove_skill(session, agent_id, skill_id, workspace_id, context.user_id)
    return {"skillIds": skill_ids}


async def _unbind_memories(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    agent_id = _required_long(args, "agentId")
    memory_ids = _required_long_list(args, "memoryIds")
    workspace_id = _workspace(context)
    for memory_id in memory_ids:
        await remove_memory_ref(session, agent_id, memory_id, workspace_id, context.user_id)
    return {"memoryIds": memory_ids}


async def _list_categories(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    categories = await list_categories(session, _workspace(context))
    keyword = _text(args, "keyword")
    if keyword is None or keyword.strip() == "":
        return categories
    matched = []
    for category in categories:
        name = category.name
        path = category.path
        if (name is not None and keyword in name) or (path is not None and keyword in path):
            matched.append(category)
    return matched


async def _create_memory(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    request = CreateMemoryRequest.model_validate(args)
    workspace_id = _workspace(context)
    if not _is_dispatch(context):
        request.scope = _required_memory_scope(request.scope)
        return await create_memory(session, request, workspace_id, context.user_id)
    dispatch = _require_dispatch(context)
    if _memory_scope(request.scope, _MEMORY_SCOPE_AGENT) != _MEMORY_SCOPE_AGENT:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    request.scope = _MEMORY_SCOPE_AGENT
    request.owner_ref = dispatch.agent_id
    return await create_from_mcp(
        session,
        request,
        workspace_id,
        dispatch.id,
        dispatch.workitem_id,
        dispatch.agent_id,
        context.user_id,
        _memory_dedupe_key(dispatch.id, _text(args, "idempotencyKey"), request),
    )


async def _search_memories(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    status = _text(args, "status")
    return await list_memories(
        session,
        _workspace(context),
        _memory_scope(_text(args, "scope"), None),
        _lng(args, "ownerRef"),
        _text(args, "type"),
        "ADOPTED" if status is None else status,
        _text(args, "keyword"),
        None,
        _integer(args, "page", 1),
        _integer(args, "size", 20),
    )


async def _set_agent_default_sdlc(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    agent_id = _required_long(args, "agentId")
    sdlc_id = _required_long(args, "sdlcId")
    workspace_id = _workspace(context)
    agent = await get_agent(session, agent_id, workspace_id)
    version = await edit_config(
        session,
        agent_id,
        UpdateConfigRequest(
            role_name=agent.role_name,
            role_code=agent.role_code,
            business_background=agent.business_background,
            responsibilities=agent.responsibilities,
            sdlc_id=sdlc_id,
        ),
        workspace_id,
        context.user_id,
        {"roleName", "roleCode", "businessBackground", "responsibilities", "sdlcId"},
    )
    return {"agentId": agent_id, "editingVersionId": version.id, "sdlcId": sdlc_id}


async def _install_platform_skill(
    session: AsyncSession,
    context: ToolExecutionContext,
    skill_id: str,
) -> object:
    skill = get_platform_skill(skill_id)
    request = CreateSkillRequest(
        type=skill.type,
        name=skill.name,
        description=skill.description,
        install_spec=skill.install_spec,
    )
    workspace_id = _workspace(context)
    try:
        return await create_skill(session, request, workspace_id, context.user_id)
    except BizError as error:
        if error.code != ErrorCode.SKILL_DUPLICATE_NAME.code:
            raise
        page = await list_skills(session, workspace_id, skill.type, None, True, False, 1, 100)
        for existing in page.list_:
            if existing.name == skill.name:
                return existing
        raise


async def _control_delivery(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    if context.credential_type is not CredentialType.LONG_LIVED:
        raise BizError(ErrorCode.NO_PERMISSION)
    workitem_id = _required_long(args, "workitemId")
    force = args.get("force") is True
    action = _required_string(args, "action")
    workspace_id = _workspace(context)
    user_id = context.user_id
    if action == "close":
        return await close(session, workspace_id, workitem_id, user_id, force)
    if action == "reopen":
        return await reopen(session, workspace_id, workitem_id, user_id)
    if action == "cancel":
        return await cancel(
            session,
            workspace_id,
            workitem_id,
            _required_long(args, "dispatchId"),
            user_id,
            force,
        )
    if action == "retry":
        await continue_dispatch(
            session,
            workspace_id,
            workitem_id,
            _required_long(args, "dispatchId"),
            user_id,
        )
        return await state(session, workspace_id, workitem_id)
    raise BizError(ErrorCode.CONFLICT, "不支持的恢复操作")


async def _read_dispatch_trace(
    session: AsyncSession,
    context: ToolExecutionContext,
    name: str,
    args: dict[str, Any],
) -> object:
    dispatch_id = _required_long(args, "dispatchId")
    if dispatch_id <= 0:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    target = await find_dispatch(session, dispatch_id)
    workspace_id = _workspace(context)
    if target is None or target.tenant_id != workspace_id:
        raise BizError(ErrorCode.DISPATCH_NOT_FOUND)
    if _is_dispatch(context):
        owner = _require_dispatch(context)
        if execution_source(owner) != execution_source(target) or (
            owner.workitem_id != target.workitem_id
        ):
            raise BizError(ErrorCode.NO_PERMISSION)
    if execution_source(target) == "SCHEDULED_TASK_RUN":
        require_scheduled_capability()
    if name == GET_DISPATCH_RUNTIME_TRACE:
        after_seq = _lng(args, "afterSeq")
        if after_seq is not None and after_seq < 0:
            raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
        outline = await load_outline_if_present(session, workspace_id, dispatch_id)
        if outline is not None:
            return outline
        live = await load_projected_trace(session, workspace_id, dispatch_id, after_seq)
        live.source = "LIVE"
        return live
    if name == GET_DISPATCH_ACTIVITIES:
        return await load_activities(session, workspace_id, dispatch_id)
    if name == GET_DISPATCH_TURN:
        return await load_turn(
            session,
            workspace_id,
            dispatch_id,
            _required_string(args, "traceId"),
        )
    if name == GET_DISPATCH_OBSERVATION:
        return await load_observation(
            session,
            workspace_id,
            dispatch_id,
            _required_string(args, "observationId"),
        )
    raise BizError(ErrorCode.MCP_TOOL_NOT_FOUND)


async def _create_scheduled_task(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    require_scheduled_capability()
    request = CreateScheduledTaskRequest(
        name=_required_string(args, "name"),
        instruction_md=_required_string(args, "instructionMd"),
        squad_id=_required_long(args, "squadId"),
        initial_agent_id=_required_long(args, "initialAgentId"),
        schedule_type=_required_string(args, "scheduleType"),
        timezone=_required_string(args, "timezone"),
        cron_expression=_text(args, "cronExpression"),
        run_at=_iso_instant(args, "runAt"),
        session_mode=_text(args, "sessionMode"),
        overlap_policy=_text(args, "overlapPolicy"),
        misfire_policy=_text(args, "misfirePolicy"),
        initial_status=_text(args, "initialStatus"),
    )
    task = await create_task(session, request, _workspace(context), context.user_id)
    payload = task.model_dump(by_alias=True)
    if task.schedule_type == "CRON" and task.cron_expression is not None:
        payload["nextFirePreviews"] = [
            _instant_text(moment)
            for moment in preview_times(task.cron_expression, task.timezone, 5)
        ]
    return payload


async def _list_scheduled_tasks(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    require_scheduled_capability()
    status = _text(args, "status")
    if status is not None and status.strip() != "":
        status = status.strip().upper()
        if status not in _SCHEDULED_TASK_LIST_STATUSES:
            raise BizError(
                ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
                "status 仅支持 ACTIVE/PAUSED/EXHAUSTED/ARCHIVED",
            )
    else:
        status = None
    size = _integer(args, "size", 20)
    offset = _integer(args, "offset", 0)
    if size < 1 or size > 100 or offset < 0:
        raise BizError(
            ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
            "size 必须在 1-100 之间且 offset 不能为负数",
        )
    page = await list_tasks(
        session,
        _workspace(context),
        status,
        None,
        _lng(args, "squadId"),
        _text(args, "keyword"),
        size,
        offset,
    )
    return {"list": page.list_, "total": page.total, "offset": offset, "size": size}


async def _get_scheduled_task(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    require_scheduled_capability()
    task_id = _required_long(args, "id")
    workspace_id = _workspace(context)
    if _is_dispatch(context):
        await _require_dispatch_run_of_task(session, context, task_id)
    task = await get_task(session, task_id, workspace_id)
    payload = task.model_dump(by_alias=True)
    if _bool(args, "includeRuns", True):
        payload["recentRuns"] = await list_runs(session, workspace_id, task_id, 10, 0)
    health = await task_health(session, workspace_id, task_id)
    payload["health"] = {"completed30d": health.completed30d, "success30d": health.success30d}
    if _bool(args, "includeDocuments", False):
        payload["documents"] = await list_requirement_documents(
            session,
            ArtifactOwner("SCHEDULED_TASK", task_id),
            workspace_id,
        )
    return payload


async def _update_scheduled_task(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    require_scheduled_capability()
    task_id = _required_long(args, "id")
    version = _required_scheduled_version(args)
    workspace_id = _workspace(context)
    current_task = await get_task(session, task_id, workspace_id)
    _require_scheduled_owner(context, current_task.creator_id)
    requested_type = _text(args, "scheduleType")
    if requested_type is not None:
        requested_type = requested_type.strip()
        if requested_type == "":
            requested_type = None
    switched = requested_type is not None and requested_type != current_task.schedule_type
    if switched:
        cron_expression = _text(args, "cronExpression")
        run_at = _iso_instant(args, "runAt")
    else:
        cron_expression = _text_or(args, "cronExpression", current_task.cron_expression)
        run_at = _iso_instant(args, "runAt")
        if run_at is None:
            run_at = current_task.run_at
    request = UpdateScheduledTaskRequest(
        version=version,
        name=_text_or(args, "name", current_task.name),
        instruction_md=_text_or(args, "instructionMd", current_task.instruction_md),
        squad_id=_long_or(args, "squadId", current_task.squad_id),
        initial_agent_id=_long_or(args, "initialAgentId", current_task.initial_agent_id),
        timezone=_text_or(args, "timezone", current_task.timezone),
        session_mode=_text_or(args, "sessionMode", current_task.session_mode),
        overlap_policy=_text_or(args, "overlapPolicy", current_task.overlap_policy),
        misfire_policy=_text_or(args, "misfirePolicy", current_task.misfire_policy),
        start_deadline_seconds=_int_or(
            args,
            "startDeadlineSeconds",
            current_task.start_deadline_seconds,
        ),
        affinity_timeout_seconds=_int_or(
            args,
            "affinityTimeoutSeconds",
            current_task.affinity_timeout_seconds,
        ),
        schedule_type=current_task.schedule_type if requested_type is None else requested_type,
        cron_expression=cron_expression,
        run_at=run_at,
    )
    updated = await update_task(session, task_id, request, workspace_id, context.user_id)
    return updated.model_dump(by_alias=True)


async def _list_scheduled_task_runs(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    require_scheduled_capability()
    task_id = _required_long(args, "id")
    size = _integer(args, "size", 20)
    offset = _integer(args, "offset", 0)
    if size < 1 or size > 100 or offset < 0:
        raise BizError(
            ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
            "size 必须在 1-100 之间且 offset 不能为负数",
        )
    workspace_id = _workspace(context)
    if _is_dispatch(context):
        await _require_dispatch_run_of_task(session, context, task_id)
    await get_task(session, task_id, workspace_id)
    runs = await list_runs(session, workspace_id, task_id, size, offset)
    return {"list": runs, "offset": offset, "size": size}


async def _delete_scheduled_task(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, bool]:
    require_scheduled_capability()
    task_id = _required_long(args, "id")
    version = _required_scheduled_version(args)
    workspace_id = _workspace(context)
    current_task = await get_task(session, task_id, workspace_id)
    _require_scheduled_owner(context, current_task.creator_id)
    await delete_task(session, task_id, version, workspace_id, context.user_id)
    return {"deleted": True}


async def _transition_scheduled_task(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    require_scheduled_capability()
    task_id = _required_long(args, "id")
    action = _required_string(args, "action").strip().lower()
    if action not in _TRANSITION_SCHEDULED_TASK_ACTIONS:
        raise BizError(
            ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
            "action 仅支持 enable/pause/archive/run-now/pause-run/resume-run/cancel-run",
        )
    version = _required_scheduled_version(args)
    workspace_id = _workspace(context)
    if action in {"enable", "pause", "archive"}:
        current_task = await get_task(session, task_id, workspace_id)
        _require_scheduled_owner(context, current_task.creator_id)
        if action == "enable":
            updated = await enable_task(session, task_id, version, workspace_id, context.user_id)
        elif action == "pause":
            updated = await pause_task(session, task_id, version, workspace_id, context.user_id)
        else:
            updated = await archive_task(session, task_id, version, workspace_id, context.user_id)
        return updated.model_dump(by_alias=True)
    if action == "run-now":
        request_id = _required_string(args, "requestId")
        current_task = await get_task(session, task_id, workspace_id)
        _require_scheduled_owner(context, current_task.creator_id)
        if current_task.version != version:
            raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
        run = await fire_manual(session, workspace_id, task_id, request_id)
        return _run_view(run).model_dump(by_alias=True)
    return await _transition_scheduled_run(
        session,
        context,
        action,
        version,
        _required_long(args, "runId"),
    )


async def _transition_scheduled_run(
    session: AsyncSession,
    context: ToolExecutionContext,
    action: str,
    version: int,
    run_id: int,
) -> dict[str, Any]:
    workspace_id = _workspace(context)
    existing = await _require_run(session, workspace_id, run_id)
    _require_scheduled_owner(context, existing.owner_id)
    if action == "pause-run":
        await pause_active(session, workspace_id, run_id, context.user_id, False)
        updated = await transition_run(
            session,
            workspace_id,
            run_id,
            version,
            "PAUSED",
            context.user_id,
        )
        return _run_view(updated).model_dump(by_alias=True)
    if action == "resume-run":
        await transition_run(session, workspace_id, run_id, version, "QUEUED", context.user_id)
        current_run = await _require_run(session, workspace_id, run_id)
        return _run_view(current_run).model_dump(by_alias=True)
    if version != existing.version:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    marked = await mark_cancel_intent(session, existing, context.user_id)
    if not marked:
        raise BizError(ErrorCode.SCHEDULED_TASK_VERSION_CONFLICT)
    awaiting = await pause_active(session, workspace_id, run_id, context.user_id, True)
    current_run = await _require_run(session, workspace_id, run_id)
    if current_run.status == "CANCELED":
        return _run_view(current_run).model_dump(by_alias=True)
    target = "PAUSED" if awaiting else "CANCELED"
    updated = await transition_run(
        session,
        workspace_id,
        run_id,
        existing.version,
        target,
        context.user_id,
    )
    return _run_view(updated).model_dump(by_alias=True)


async def _get_scheduled_task_run(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    require_scheduled_capability()
    run_id = _required_long(args, "runId")
    workspace_id = _workspace(context)
    if _is_dispatch(context):
        owner = _require_dispatch(context)
        if owner.workitem_id != run_id:
            raise BizError(ErrorCode.NO_PERMISSION)
    run = await _require_run(session, workspace_id, run_id)
    dispatches = await _run_dispatches(session, workspace_id, run_id)
    executor_id = None if len(dispatches) == 0 else dispatches[-1].executor_id
    payload = _run_view(run).model_dump(by_alias=True)
    payload["squadId"] = run.squad_id
    payload["initialAgentId"] = run.initial_agent_id
    payload["sessionMode"] = run.session_mode
    payload["resumeFromRunId"] = run.resume_from_run_id
    payload["ownerId"] = run.owner_id
    payload["snapshot"] = run.execution_snapshot_json
    payload["executorId"] = executor_id
    if _bool(args, "includeEvents", True):
        events: list[dict[str, Any]] = []
        for dispatch in dispatches:
            rows = await session.scalars(
                select(DispatchRuntimeEvent).where(
                    DispatchRuntimeEvent.tenant_id == workspace_id,
                    DispatchRuntimeEvent.dispatch_id == dispatch.id,
                )
            )
            events.extend(_event_dict(row) for row in rows)
        payload["events"] = events
    if _bool(args, "includeArtifacts", True):
        artifacts = await session.scalars(
            select(Artifact).where(
                Artifact.tenant_id == workspace_id,
                Artifact.source_type == "SCHEDULED_TASK_RUN",
                Artifact.workitem_id == run_id,
            )
        )
        payload["artifacts"] = [_artifact_dict(row) for row in artifacts]
    if _bool(args, "includeComments", True):
        payload["comments"] = await list_run_comments(session, workspace_id, run_id)
    if _bool(args, "includeDerivedWorkitems", False):
        payload["derivedWorkitems"] = await _derived_workitems(session, context, run_id)
    return payload


async def _add_scheduled_task_run_comment(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    require_scheduled_capability()
    run_id = _required_long(args, "runId")
    content_md = _required_string(args, "contentMd")
    if _is_dispatch(context):
        return await _add_scheduled_run_dispatch_comment(
            session,
            context,
            _require_dispatch(context),
            run_id,
            content_md,
            None,
            None,
        )
    comment, notices = await add_human_comment(
        session,
        _workspace(context),
        run_id,
        context.user_id,
        content_md,
        [],
        [],
    )
    await publish_run_mentions(session, notices)
    return comment


async def _add_scheduled_run_dispatch_comment(
    session: AsyncSession,
    context: ToolExecutionContext,
    dispatch: Dispatch,
    run_id: int,
    content_md: str | None,
    target_agent_ids: list[int | None] | None,
    target_human_ids: list[int | None] | None,
) -> CommentView:
    if dispatch.workitem_id != run_id:
        raise BizError(ErrorCode.NO_PERMISSION)
    agents = _present_ids(target_agent_ids)
    humans = _present_ids(target_human_ids)
    body = "" if content_md is None else content_md
    comment, notices = await add_run_agent_comment(
        session,
        _workspace(context),
        run_id,
        dispatch.agent_id,
        body,
        agents,
        humans,
    )
    await publish_run_mentions(session, notices)
    return comment


async def _list_executors(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    workspace_id = _workspace(context)
    agent_id = _lng(args, "agentId")
    squad_id = _lng(args, "squadId")
    if squad_id is not None:
        in_squad = await list_all(session, workspace_id, [squad_id])
        if agent_id is None:
            return in_squad
        return [item for item in in_squad if item.agent_id == agent_id]
    if agent_id is None:
        return await list_all(session, workspace_id, None)
    return await list_by_agent(session, agent_id, workspace_id)


async def _create_executor(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    client_kind = require_creatable_client_kind(
        _required_string(args, "clientKind"),
        ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
    )
    request = CreateExecutorRequest(
        name=_required_string(args, "name"),
        client_kind=client_kind,
        memory_mode=_text(args, "memoryMode"),
        max_concurrent_dispatches=_executor_concurrency(args),
        model=_text(args, "model"),
        reasoning_effort=_text(args, "reasoningEffort"),
        context_window=_text(args, "contextWindow"),
    )
    return await create_executor(
        session,
        _required_long(args, "agentId"),
        request,
        _workspace(context),
        context.user_id,
    )


async def _update_launch_config(
    session: AsyncSession,
    context: ToolExecutionContext,
    args: dict[str, Any],
) -> object:
    version = _lng(args, "version")
    if version is None:
        raise BizError(
            ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
            "version 必填，取自 " + GET_EXECUTOR_LAUNCH_CONFIG,
        )
    request = UpdateExecutorLaunchConfigRequest(
        version=version,
        memory_mode=_text(args, "memoryMode"),
        max_concurrent_dispatches=_executor_concurrency(args),
        model=_text(args, "model"),
        reasoning_effort=_text(args, "reasoningEffort"),
        context_window=_text(args, "contextWindow"),
    )
    return await update_launch_config(
        session,
        _required_long(args, "id"),
        _workspace(context),
        request,
        context.user_id,
    )


async def _launch_options(client_kind: str) -> dict[str, Any]:
    canonical = require_creatable_client_kind(client_kind, ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    provider = resolve_provider(canonical)
    models, refreshed_at = await _launch_models(provider)
    default_model = choose_model(models, DEFAULT_MODEL)
    default_create = choose_model(models, AUTO_MODEL)
    return {
        "clientKind": canonical,
        "provider": provider,
        "models": [{"value": item.value, "label": item.label} for item in models],
        "reasoningEfforts": _named_options(
            ("max", "Max"),
            ("xhigh", "Extra High"),
            ("high", "High"),
            ("medium", "Medium"),
            ("low", "Low"),
            ("none", "None"),
        ),
        "contextWindows": _named_options(
            ("1000000", "1M"),
            ("400000", "400K"),
            ("260000", "260K"),
        ),
        "memoryModes": [
            {
                "value": MEMORY_MODE_PLATFORM,
                "label": "平台记忆（推荐）",
                "description": "由 AutoWonder 注入平台记忆",
            },
            {
                "value": MEMORY_MODE_PROVIDER_LOCAL,
                "label": "本机 Agent 记忆",
                "description": "使用执行器本机的 Agent 记忆",
            },
            {
                "value": MEMORY_MODE_NONE,
                "label": "关闭记忆",
                "description": "不注入任何记忆",
            },
        ],
        "defaultModel": default_model,
        "defaultCreateModel": default_create,
        "defaultReasoningEffort": default_reasoning_effort(default_model),
        "defaultContextWindow": DEFAULT_CONTEXT_WINDOW,
        "defaultMemoryMode": DEFAULT_MEMORY_MODE,
        "modelCatalogLastSuccessfulAt": refreshed_at,
    }


async def _launch_models(provider: str) -> tuple[list[ModelOption], datetime | None]:
    try:
        catalog = await read_catalog(provider)
    except Exception:
        return list(FALLBACK_MODELS), None
    options = [ModelOption(item.id, item.name) for item in catalog.models]
    if len(options) == 0:
        return list(FALLBACK_MODELS), None
    return options, catalog.last_successful_at


def _creatable_client_kinds() -> list[dict[str, str]]:
    return [
        {
            "value": CLIENT_KIND_QODER_CLI,
            "label": "Qoder CLI",
            "description": "国际版 Qoder CLI 执行器",
        },
        {
            "value": CLIENT_KIND_QODER_CN_CLI,
            "label": "Qoder CLI CN",
            "description": "国内版 Qoder CLI 执行器",
        },
    ]


def _named_options(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in pairs]


async def _derived_workitems(
    session: AsyncSession,
    context: ToolExecutionContext,
    run_id: int,
) -> list[Any]:
    workspace_id = _workspace(context)
    rows = await session.scalars(
        select(Workitem.id).where(
            Workitem.tenant_id == workspace_id,
            Workitem.origin_type == "SCHEDULED_TASK_RUN",
            Workitem.origin_id == run_id,
            Workitem.is_deleted == 0,
        )
    )
    views: list[Any] = []
    for workitem_id in rows:
        views.append(await get_workitem(session, workitem_id, workspace_id, context.user_id))
    return views


async def _run_dispatches(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
) -> list[Dispatch]:
    rows = await session.scalars(
        select(Dispatch)
        .where(
            Dispatch.tenant_id == workspace_id,
            Dispatch.source_type == "SCHEDULED_TASK_RUN",
            Dispatch.workitem_id == run_id,
            Dispatch.is_deleted == 0,
        )
        .order_by(Dispatch.id.asc())
    )
    return list(rows)


async def _require_run(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
) -> ScheduledTaskRun:
    run = await session.get(ScheduledTaskRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
    return run


async def _require_dispatch_run_of_task(
    session: AsyncSession,
    context: ToolExecutionContext,
    task_id: int,
) -> None:
    dispatch = _require_dispatch(context)
    run = await session.get(ScheduledTaskRun, dispatch.workitem_id)
    if run is None or run.workspace_id != _workspace(context) or run.scheduled_task_id != task_id:
        raise BizError(ErrorCode.NO_PERMISSION)


def _require_scheduled_owner(context: ToolExecutionContext, owner_id: int | None) -> None:
    if owner_id != context.user_id and context.access_level is not WorkspaceAccessLevel.ADMIN:
        raise BizError(ErrorCode.NO_PERMISSION)


def _run_view(run: ScheduledTaskRun) -> ScheduledTaskRunView:
    return ScheduledTaskRunView(
        id=run.id,
        scheduled_task_id=run.scheduled_task_id,
        trigger_type=run.trigger_type,
        scheduled_at=aware_utc(run.scheduled_at),
        started_at=aware_utc(run.started_at),
        finished_at=aware_utc(run.finished_at),
        status=run.status,
        skip_reason=run.skip_reason,
        current_agent_id=run.current_agent_id,
        sdlc_id=run.sdlc_id,
        current_step_id=run.current_step_id,
        degraded_resume=run.degraded_resume == 1,
        degraded_reason=run.degraded_reason,
        result_summary=run.result_summary,
        error=run.error,
        version=run.version,
        gmt_create=run.gmt_create,
        gmt_modified=run.gmt_modified,
    )


def _event_dict(row: DispatchRuntimeEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenantId": row.tenant_id,
        "workitemId": row.workitem_id,
        "dispatchId": row.dispatch_id,
        "agentId": row.agent_id,
        "eventId": row.event_id,
        "seq": row.seq,
        "eventType": row.event_type,
        "stepId": row.step_id,
        "stepKey": row.step_key,
        "stepOrder": row.step_order,
        "stepName": row.step_name,
        "message": row.message,
        "error": row.error,
        "detailJson": row.detail_json,
        "eventTime": row.event_time,
        "gmtCreate": row.gmt_create,
    }


def _artifact_dict(row: Artifact) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenantId": row.tenant_id,
        "sourceType": row.source_type,
        "workitemId": row.workitem_id,
        "dispatchId": row.dispatch_id,
        "name": row.name,
        "type": row.type,
        "ossRef": row.oss_ref,
        "size": row.size,
        "metaJson": row.meta_json,
        "gmtCreate": row.gmt_create,
    }


async def _audit_run_tool(
    session: AsyncSession,
    context: ToolExecutionContext,
    tool: str,
    result: object,
    failure: BaseException | None,
) -> None:
    if not _is_dispatch(context):
        return
    dispatch = context.dispatch
    if dispatch is None or execution_source(dispatch) != "SCHEDULED_TASK_RUN":
        return
    record = AuditRecord(
        tenant_id=_workspace(context),
        actor_id=dispatch.agent_id,
        actor_type="AGENT",
        module="MCP",
        action="TOOL_CALL",
        target_type="scheduled_task_run",
        target_id=dispatch.workitem_id,
        trigger_type="EVENT",
        trigger_source="MCP",
        event_type="mcp.tool",
    )
    record.add("tool", tool).add("dispatchId", dispatch.id)
    record.add("runId", dispatch.workitem_id).add("agentId", dispatch.agent_id)
    record.add("success", failure is None)
    if result is not None:
        record.add("resultType", type(result).__name__)
    run = await session.get(ScheduledTaskRun, dispatch.workitem_id)
    if run is not None and run.workspace_id == context.workspace_id:
        record.add("taskId", run.scheduled_task_id)
    if failure is not None:
        record.add("error", type(failure).__name__)
    await record_required(session, record)


def _is_dispatch(context: ToolExecutionContext) -> bool:
    return context.credential_type is CredentialType.DISPATCH


def _require_dispatch(context: ToolExecutionContext) -> Dispatch:
    dispatch = context.dispatch
    if dispatch is None:
        raise BizError(ErrorCode.NO_PERMISSION)
    return dispatch


def _present_ids(values: list[int | None] | None) -> list[int]:
    if values is None:
        return []
    return [item for item in values if item is not None]


def _iso_error(key: str) -> BizError:
    return BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID, key + " 必须是 ISO-8601 时间")


def _require_dispatch_scope(context: ToolExecutionContext, workitem_id: int) -> Dispatch:
    dispatch = _require_dispatch(context)
    if dispatch.workitem_id != workitem_id:
        raise BizError(ErrorCode.NO_PERMISSION)
    return dispatch


def _workspace(context: ToolExecutionContext) -> int:
    return context.workspace_id  # type: ignore[return-value]


def _uploaded_package(uploaded: Any) -> dict[str, Any]:
    return {
        "packageOssRef": uploaded.package_oss_ref,
        "fileName": uploaded.file_name,
        "packageSize": uploaded.size,
        "packageMd5": uploaded.md5,
        "packageSha256": uploaded.sha256,
        "type": uploaded.type,
        "name": uploaded.name,
        "description": uploaded.description,
    }


def _package_file_name(args: dict[str, Any]) -> str:
    if "files" not in args:
        return _required_string(args, "fileName")
    name = _text(args, "fileName")
    if name is None or name.strip() == "":
        return "directory.zip"
    if not name.lower().endswith(".zip"):
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return name


def _package_bytes(args: dict[str, Any]) -> bytes:
    if "files" in args:
        files = args.get("files")
        if "contentBase64" in args or not isinstance(files, dict):
            raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
        contents: dict[str, str] = {}
        for path, content in files.items():
            if not isinstance(path, str) or not isinstance(content, str):
                raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
            contents[path] = content
        return pack_directory(contents)
    encoded = _required_string(args, "contentBase64")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID) from error


def _document_bytes(args: dict[str, Any]) -> bytes:
    encoded = _text(args, "contentBase64")
    if encoded is not None and encoded.strip() != "":
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID) from error
    content = _text(args, "contentMd")
    if content is None:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return content.encode("utf-8")


def _reject_launch_overrides(args: dict[str, Any]) -> None:
    for field in _LAUNCH_OVERRIDE_FIELDS:
        value = args.get(field)
        if value is not None and str(value).strip() != "":
            raise BizError(
                ErrorCode.EXECUTOR_LAUNCH_CONFIG_OVERRIDE_REJECTED,
                field
                + " 不支持在生成启动命令时临时覆盖，请先调用 "
                + UPDATE_EXECUTOR_LAUNCH_CONFIG
                + " 修改启动配置",
            )


def _executor_concurrency(args: dict[str, Any]) -> int | None:
    value = args.get("maxConcurrentDispatches")
    if value is None:
        return None
    message = "maxConcurrentDispatches 必须为 1 到 10 的整数"
    try:
        number = _exact_int(value)
    except (ValueError, ArithmeticError, InvalidOperation) as error:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID, message) from error
    if number < 1 or number > 10:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID, message)
    return number


def _exact_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        decimal = Decimal(str(value))
        return int(decimal.to_integral_exact())
    decimal = Decimal(str(value).strip())
    return int(decimal.to_integral_exact())


def _normalize_agent_args(args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    if "soulMd" in args:
        normalized["businessBackground"] = args.get("soulMd")
    if "agentMd" in args:
        normalized["responsibilities"] = args.get("agentMd")
    normalized.pop("soulMd", None)
    normalized.pop("agentMd", None)
    return normalized


def _present_agent_fields(normalized: dict[str, Any]) -> set[str]:
    return {field for field in _AGENT_UPDATE_FIELDS if field in normalized}


def _required_memory_scope(scope: str | None) -> str:
    normalized = _memory_scope(scope, None)
    if normalized is None:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return normalized


def _memory_scope(scope: str | None, default_scope: str | None) -> str | None:
    if scope is None or scope.strip() == "":
        return default_scope
    normalized = scope.strip().upper()
    if normalized not in _MEMORY_SCOPES:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return normalized


def _memory_dedupe_key(
    dispatch_id: int,
    idempotency_key: str | None,
    request: CreateMemoryRequest,
) -> str:
    if idempotency_key is None or idempotency_key.strip() == "":
        content = "" if request.content_md is None else request.content_md
        title = "" if request.title is None else request.title
        key = hashlib.sha256((title + "\n" + content).encode("utf-8")).hexdigest()
    else:
        key = idempotency_key.strip()
    return "dispatch:" + str(dispatch_id) + ":mcp:" + key


def _category_update(args: dict[str, Any]) -> UpdateCategoryFields:
    fields = UpdateCategoryFields()
    if "name" in args:
        fields.name_present = True
        fields.name = _text(args, "name")
    if "parentId" in args:
        fields.parent_id_present = True
        fields.parent_id = _positive_category_id(args, "parentId", True)
    if "description" in args:
        fields.description_present = True
        fields.description = _text(args, "description")
    return fields


def _repo_update(args: dict[str, Any]) -> UpdateRepoFields:
    fields = UpdateRepoFields()
    if "name" in args:
        fields.name_present = True
        fields.name = _text(args, "name")
    if "url" in args:
        fields.url_present = True
        fields.url = _text(args, "url")
    if "defaultBranch" in args:
        fields.default_branch_present = True
        fields.default_branch = _text(args, "defaultBranch")
    if "description" in args:
        fields.description_present = True
        fields.description = _text(args, "description")
    return fields


def _scheduled_task_document_source(args: dict[str, Any]) -> bool:
    source_type = _text(args, "sourceType")
    if source_type is None:
        return False
    normalized = source_type.strip().upper()
    if normalized == "WORKITEM":
        return False
    if normalized == "SCHEDULED_TASK":
        return True
    raise BizError(
        ErrorCode.MCP_TOOL_ARGUMENT_INVALID,
        "sourceType 仅支持 WORKITEM/SCHEDULED_TASK",
    )


def _workspace_id_argument(args: dict[str, Any]) -> int | None:
    workspace_id = _lng(args, "workspaceId")
    if workspace_id is None:
        return None
    if workspace_id <= 0:
        raise BizError(ErrorCode.PARAM_INVALID, "workspaceId 必须是正整数")
    return workspace_id


def _squad_filter(args: dict[str, Any]) -> list[int] | None:
    squad_id = _lng(args, "squadId")
    if squad_id is None:
        return None
    return [squad_id]


def _text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _text_or(args: dict[str, Any], key: str, fallback: str | None) -> str | None:
    value = _text(args, key)
    if value is None:
        return fallback
    return value


def _required_string(args: dict[str, Any], key: str) -> str:
    value = _text(args, key)
    if value is None or value.strip() == "":
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return value


def _bool(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)


def _string_list(args: dict[str, Any], key: str) -> list[str] | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return [str(item) for item in value]


def _required_long_list(args: dict[str, Any], key: str) -> list[int]:
    value = args.get(key)
    if not isinstance(value, list) or len(value) == 0:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    parsed: list[int] = []
    for item in value:
        number = _lng({"value": item}, "value")
        if number is None:
            raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
        parsed.append(number)
    return parsed


def _lng(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except ValueError as error:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID) from error


def _required_long(args: dict[str, Any], key: str) -> int:
    value = _lng(args, key)
    if value is None:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return value


def _long_or(args: dict[str, Any], key: str, fallback: int | None) -> int | None:
    value = _lng(args, key)
    if value is None:
        return fallback
    return value


def _integer(args: dict[str, Any], key: str, default: int) -> int:
    value = _lng(args, key)
    if value is None:
        return default
    return (value + 2**31) % 2**32 - 2**31


def _int_or(args: dict[str, Any], key: str, fallback: int | None) -> int | None:
    value = _lng(args, key)
    if value is None:
        return fallback
    if value < -_INT_MAX - 1 or value > _INT_MAX:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID, key + " 超出整数范围")
    return value


def _required_scheduled_version(args: dict[str, Any]) -> int:
    version = _lng(args, "version")
    if version is None or version < 0 or version > _INT_MAX:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID, "version 必须提供且不能为负数")
    return version


def _positive_category_id(args: dict[str, Any], key: str, nullable: bool) -> int | None:
    value = args.get(key)
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    try:
        number = _exact_int(value)
    except (ValueError, ArithmeticError, InvalidOperation) as error:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID) from error
    if number <= 0:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return number


def _required_category_id(args: dict[str, Any], key: str) -> int:
    number = _positive_category_id(args, key, False)
    if number is None:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return number


def _required_nullable_category_id(args: dict[str, Any]) -> int | None:
    if "categoryId" not in args:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    return _positive_category_id(args, "categoryId", True)


def _category_skill_ids(args: dict[str, Any]) -> list[int]:
    value = args.get("skillIds")
    if not isinstance(value, list) or len(value) == 0:
        raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)
    seen: list[int] = []
    for item in value:
        skill_id = _required_category_id({"id": item}, "id")
        if skill_id not in seen:
            seen.append(skill_id)
    return seen


def _iso_instant(args: dict[str, Any], key: str) -> datetime | None:
    value = _text(args, key)
    if value is None or value.strip() == "":
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise _iso_error(key) from error
    if parsed.tzinfo is None:
        raise _iso_error(key)
    return parsed.astimezone(UTC)


def _instant_text(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = aware.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text
