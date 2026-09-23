"""记忆的创建、审核、分组和来源幂等。写入规则对齐 MemoryService。"""

import json
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.expression import delete

from autowonder.agents.models import Agent, AgentMemoryRef
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.memories.distribution import distribute
from autowonder.memories.models import Memory, MemoryReview
from autowonder.memories.schemas import (
    CreateMemoryRequest,
    ImportFromArtifactRequest,
    MemoryGroupView,
    MemoryReviewView,
    MemoryView,
    ReviewRequest,
    UpdateMemoryRequest,
)
from autowonder.squads.models import SquadMember

_SCOPES = {"AGENT", "SQUAD", "ORG"}
_LIST_CAP = 100
_GROUP_CAP = 50
GROUPED_MEMORY_FETCH_LIMIT = 2000
_DELETE_COMMENT = "软删除记忆并移除员工绑定；历史执行快照保持不变"


@dataclass
class ScopeChange:
    """已采纳记忆要改成的范围。"""

    scope: str
    owner_ref: int | None


@dataclass
class ReviewPlan:
    """审核将要写入的状态、正文和范围。"""

    status: str
    content_md: str | None
    promoted_scope: str | None
    promoted_owner: int | None
    effective_scope: str | None
    effective_owner: int | None


@dataclass
class _GroupSummary:
    scope: str
    owner_ref: int | None
    total: int


def normalize_scope(scope: str | None) -> str | None:
    """空白范围视为未提供。其余只接受 AGENT、SQUAD、ORG。"""
    if scope is None or scope.strip() == "":
        return None
    normalized = scope.strip().upper()
    if normalized not in _SCOPES:
        raise BizError(ErrorCode.PARAM_INVALID)
    return normalized


def require_title(title: str | None) -> str:
    """标题去掉两端空白后不能为空。"""
    if title is None or title.strip() == "":
        raise BizError(ErrorCode.MEMORY_TITLE_REQUIRED)
    return title.strip()


def manual_scope(scope: str | None, owner_ref: int | None) -> tuple[str, int | None]:
    """手工创建必须有范围。组织范围不保留 owner，其他范围必须有 owner。"""
    normalized = normalize_scope(scope)
    if normalized is None or (normalized != "ORG" and owner_ref is None):
        raise BizError(ErrorCode.PARAM_INVALID)
    if normalized == "ORG":
        return normalized, None
    return normalized, owner_ref


def escape_like_wildcards(keyword: str | None) -> str | None:
    """给 LIKE 通配符和反斜杠加反斜杠。空串和 null 原样返回。"""
    if keyword is None or keyword == "":
        return keyword
    pieces: list[str] = []
    for char in keyword:
        if char in {"\\", "%", "_"}:
            pieces.append("\\")
        pieces.append(char)
    return "".join(pieces)


def page_window(page: int, size: int, cap: int) -> tuple[int, int]:
    """页码小于 1 时从第 1 页起；每页小于 1 时用 1，并且不超过上限。"""
    normalized_page = page
    if page < 1:
        normalized_page = 1
    normalized_size = size
    if size < 1:
        normalized_size = 1
    if normalized_size > cap:
        normalized_size = cap
    return (normalized_page - 1) * normalized_size, normalized_size


def group_key(scope: str, owner_ref: int | None) -> str:
    """分组键。没有 owner 时冒号后面留空。"""
    if owner_ref is None:
        return scope + ":"
    return scope + ":" + str(owner_ref)


def scope_change_comment(
    old_scope: str,
    old_owner: int | None,
    new_scope: str,
    new_owner: int | None,
) -> str:
    """范围变更审核意见。空 owner 写成 null。"""
    return (
        "范围变更: " + _ref_text(old_scope, old_owner) + " -> " + _ref_text(new_scope, new_owner)
    )


def scope_change(
    current_scope: str,
    current_owner: int | None,
    current_status: str,
    requested_scope: str | None,
    requested_owner: int | None,
) -> ScopeChange | None:
    """没有新范围时不变更。未采纳的记忆不能改范围。"""
    normalized = normalize_scope(requested_scope)
    if normalized is None:
        return None
    if normalized != "ORG" and requested_owner is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    owner_ref = requested_owner
    if normalized == "ORG":
        owner_ref = None
    if normalized == current_scope and owner_ref == current_owner:
        return None
    if current_status != "ADOPTED":
        raise BizError(ErrorCode.MEMORY_SCOPE_CHANGE_NOT_ADOPTED)
    return ScopeChange(scope=normalized, owner_ref=owner_ref)


def require_decision(decision: str | None) -> str:
    """审核决定只接受 ADOPT 和 REJECT。"""
    if decision != "ADOPT" and decision != "REJECT":
        raise BizError(ErrorCode.PARAM_INVALID, "decision 必须为 ADOPT 或 REJECT")
    return decision


def require_pending(status: str) -> None:
    """只有待审核记忆可以采纳或拒绝。"""
    if status != "PENDING":
        raise BizError(ErrorCode.MEMORY_NOT_PENDING)


def plan_review(
    decision: str,
    edited_content_md: str | None,
    scope: str | None,
    owner_ref: int | None,
    current_scope: str | None,
    current_owner: int | None,
) -> ReviewPlan:
    """采纳可以改正文和范围。拒绝不改记忆正文，也不校验范围。"""
    if decision == "ADOPT":
        return _adopt_plan(
            edited_content_md,
            scope,
            owner_ref,
            current_scope,
            current_owner,
        )
    return ReviewPlan(
        status="REJECTED",
        content_md=None,
        promoted_scope=None,
        promoted_owner=None,
        effective_scope=None,
        effective_owner=None,
    )


def mcp_existing_action(
    status: str,
    existing_title: str,
    existing_content: str | None,
    title: str,
    content: str | None,
) -> str:
    """待审核记忆原地更新。已审记忆只有标题和正文都相同才直接返回。"""
    if status == "PENDING":
        return "update"
    if existing_title == title and existing_content == content:
        return "keep"
    return "reject"


def mcp_source_ref(dispatch_id: int, workitem_id: int, agent_id: int) -> str:
    """MCP 来源按 dispatchId、workitemId、agentId 的顺序编码。"""
    return json.dumps(
        {"dispatchId": dispatch_id, "workitemId": workitem_id, "agentId": agent_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def evolution_source_ref(proposal_id: int) -> str:
    """进化提案来源是固定形状的 JSON 文本。"""
    return '{"proposalId":' + str(proposal_id) + "}"


def artifact_source_ref(artifact_id: int | None) -> str:
    """产物来源只在 id 有值时写入 artifactId。"""
    payload: dict[str, int] = {}
    if artifact_id is not None:
        payload["artifactId"] = artifact_id
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def source_ref_text(value: object | None) -> str | None:
    """把 JSON 列读回 Java 字符串。对象按 MySQL 的键序输出。"""
    if value is None:
        return None
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        if value:
            return "true"
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def stored_source_ref(text: str | None) -> object | None:
    """来源文本按 JSON 写入列。数字文本落成 JSON 数字。"""
    if text is None:
        return None
    return json.loads(text)


async def create_memory(
    session: AsyncSession,
    request: CreateMemoryRequest,
    tenant_id: int,
    user_id: int,
) -> MemoryView:
    """手工记忆进入待审核。响应不回读数据库默认时间。"""
    title = require_title(request.title)
    scope, owner_ref = manual_scope(request.scope, request.owner_ref)
    memory_id = await _insert_memory(
        session,
        tenant_id=tenant_id,
        scope=scope,
        owner_ref=owner_ref,
        memory_type=request.type,
        title=title,
        content_md=request.content_md,
        status="PENDING",
        source="MANUAL",
        source_ref=None,
        source_dedupe_key=None,
        creator_id=user_id,
    )
    await session.commit()
    return _inserted_view(
        memory_id,
        scope,
        owner_ref,
        request.type,
        title,
        request.content_md,
        "PENDING",
        "MANUAL",
        None,
    )


async def create_from_learning_delta(
    session: AsyncSession,
    request: CreateMemoryRequest,
    tenant_id: int,
    dispatch_id: int,
    entry_index: int,
) -> MemoryView:
    """学习增量按调度和条目下标幂等写入，创建人记为系统用户 0。"""
    title = require_title(request.title)
    source_ref = str(dispatch_id)
    dedupe_key = "dispatch:" + str(dispatch_id) + ":entry:" + str(entry_index)
    memory_id = await _insert_memory(
        session,
        tenant_id=tenant_id,
        scope=request.scope,
        owner_ref=request.owner_ref,
        memory_type=request.type,
        title=title,
        content_md=request.content_md,
        status="PENDING",
        source="LEARNING_DELTA",
        source_ref=source_ref,
        source_dedupe_key=dedupe_key,
        creator_id=0,
    )
    await session.commit()
    return _inserted_view(
        memory_id,
        request.scope,
        request.owner_ref,
        request.type,
        title,
        request.content_md,
        "PENDING",
        "LEARNING_DELTA",
        source_ref,
    )


async def create_from_mcp(
    session: AsyncSession,
    request: CreateMemoryRequest,
    tenant_id: int,
    dispatch_id: int,
    workitem_id: int,
    agent_id: int,
    user_id: int,
    dedupe_key: str,
) -> MemoryView:
    """相同幂等键的待审核记忆原地更新。已审记忆正文变了就拒绝。"""
    title = require_title(request.title)
    existing = await _find_dedupe(session, tenant_id, "MCP", dedupe_key)
    if existing is not None:
        return await _reuse_mcp_memory(
            session,
            existing,
            request,
            tenant_id,
            title,
            user_id,
        )
    source_ref = mcp_source_ref(dispatch_id, workitem_id, agent_id)
    memory_id = await _insert_memory(
        session,
        tenant_id=tenant_id,
        scope=request.scope,
        owner_ref=request.owner_ref,
        memory_type=request.type,
        title=title,
        content_md=request.content_md,
        status="PENDING",
        source="MCP",
        source_ref=source_ref,
        source_dedupe_key=dedupe_key,
        creator_id=user_id,
    )
    view = await get_scoped(session, memory_id, tenant_id)
    await session.commit()
    return view


async def create_from_evolution_proposal(
    session: AsyncSession,
    request: CreateMemoryRequest,
    tenant_id: int,
    proposal_id: int,
    user_id: int,
) -> MemoryView:
    """进化提案直接写成已采纳记忆。响应仍是插入前的对象。"""
    title = require_title(request.title)
    source_ref = evolution_source_ref(proposal_id)
    dedupe_key = "evolution-proposal:" + str(proposal_id)
    memory_id = await _insert_memory(
        session,
        tenant_id=tenant_id,
        scope=request.scope,
        owner_ref=request.owner_ref,
        memory_type=request.type,
        title=title,
        content_md=request.content_md,
        status="ADOPTED",
        source="EVOLUTION_PROPOSAL",
        source_ref=source_ref,
        source_dedupe_key=dedupe_key,
        creator_id=user_id,
    )
    await session.commit()
    return _inserted_view(
        memory_id,
        request.scope,
        request.owner_ref,
        request.type,
        title,
        request.content_md,
        "ADOPTED",
        "EVOLUTION_PROPOSAL",
        source_ref,
    )


async def get_memory(session: AsyncSession, memory_id: int) -> MemoryView:
    """按 id 读取未删除记忆。工作空间条件由租户拦截加上。"""
    session.expire_all()
    row = await _find(session, memory_id)
    if row is None:
        raise BizError(ErrorCode.MEMORY_NOT_FOUND)
    return _view_from_row(row)


async def get_scoped(session: AsyncSession, memory_id: int, tenant_id: int) -> MemoryView:
    """读取记忆，并再核对工作空间。"""
    session.expire_all()
    row = await _find(session, memory_id)
    if row is None or row.tenant_id != tenant_id:
        raise BizError(ErrorCode.MEMORY_NOT_FOUND)
    return _view_from_row(row)


async def list_memories(
    session: AsyncSession,
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    status: str | None,
    keyword: str | None,
    visible_agent_ref: int | None,
    page: int,
    size: int,
) -> list[MemoryView]:
    """列表默认排除已拒绝。关键字里的 LIKE 通配符会先转义。"""
    offset, limit = page_window(page, size, _LIST_CAP)
    statement = _filtered(
        select(Memory),
        tenant_id,
        scope,
        owner_ref,
        memory_type,
        status,
    )
    escaped = escape_like_wildcards(keyword)
    if escaped is not None and escaped != "":
        pattern = "%" + escaped + "%"
        statement = statement.where(
            or_(Memory.title.like(pattern), Memory.content_md.like(pattern))
        )
    if visible_agent_ref is not None:
        statement = statement.where(
            or_(Memory.scope != "AGENT", Memory.owner_ref == visible_agent_ref)
        )
    rows = await session.scalars(statement.order_by(Memory.id.desc()).offset(offset).limit(limit))
    return [_view_from_row(row) for row in rows]


async def count_list(
    session: AsyncSession,
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    status: str | None,
) -> int:
    """计数使用和列表相同的筛选，但不吃关键字。"""
    counted = await session.scalar(
        _filtered(
            select(func.count()).select_from(Memory),
            tenant_id,
            scope,
            owner_ref,
            memory_type,
            status,
        )
    )
    return cast(int, counted)


async def count_pending_reviews(session: AsyncSession, tenant_id: int) -> int:
    """当前工作空间里待审核记忆的数量。"""
    counted = await session.scalar(
        select(func.count())
        .select_from(Memory)
        .where(
            Memory.tenant_id == tenant_id,
            Memory.status == "PENDING",
            Memory.is_deleted == 0,
        )
    )
    return cast(int, counted)


async def list_grouped(
    session: AsyncSession,
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    status: str | None,
    page: int,
    size: int,
) -> list[MemoryGroupView]:
    """先分页分组，再取这些组里最新的一批记忆。员工名解析不到就留空。"""
    offset, limit = page_window(page, size, _GROUP_CAP)
    summaries = await _group_summaries(
        session,
        tenant_id,
        scope,
        owner_ref,
        memory_type,
        status,
        offset,
        limit,
    )
    if len(summaries) == 0:
        return []
    memories = await _memories_for_groups(
        session,
        tenant_id,
        summaries,
        memory_type,
        status,
    )
    by_group: dict[str, list[MemoryView]] = {}
    for memory in memories:
        key = group_key(memory.scope, memory.owner_ref)
        bucket = by_group.get(key)
        if bucket is None:
            bucket = []
            by_group[key] = bucket
        bucket.append(_view_from_row(memory))
    names = await _agent_names(session, tenant_id, summaries)
    groups: list[MemoryGroupView] = []
    for summary in summaries:
        owner_name = None
        if summary.scope == "AGENT" and summary.owner_ref is not None:
            owner_name = names.get(summary.owner_ref)
        grouped_memories = by_group.get(group_key(summary.scope, summary.owner_ref))
        if grouped_memories is None:
            grouped_memories = []
        groups.append(
            MemoryGroupView(
                scope=summary.scope,
                owner_ref=summary.owner_ref,
                owner_name=owner_name,
                total=summary.total,
                memories=grouped_memories,
            )
        )
    return groups


async def count_groups(
    session: AsyncSession,
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    status: str | None,
) -> int:
    """分组数量。筛选和分组列表一致。"""
    grouped = (
        _filtered(
            select(Memory.scope, Memory.owner_ref),
            tenant_id,
            scope,
            owner_ref,
            memory_type,
            status,
        )
        .group_by(Memory.scope, Memory.owner_ref)
        .subquery()
    )
    counted = await session.scalar(select(func.count()).select_from(grouped))
    return cast(int, counted)


async def update_memory(
    session: AsyncSession,
    memory_id: int,
    request: UpdateMemoryRequest,
    tenant_id: int,
    user_id: int,
) -> MemoryView:
    """先改标题正文和类型。范围只有已采纳记忆能改，并记一条审核。"""
    row = await _find(session, memory_id)
    if row is None:
        raise BizError(ErrorCode.MEMORY_NOT_FOUND)
    title = row.title
    if request.title is not None:
        title = request.title.strip()
    content_md = row.content_md
    if request.content_md is not None:
        content_md = request.content_md
    memory_type = row.type
    if request.type is not None:
        memory_type = request.type
    changed = scope_change(
        row.scope,
        row.owner_ref,
        row.status,
        request.scope,
        request.owner_ref,
    )
    updated = await _update_fields(
        session,
        memory_id,
        tenant_id,
        title,
        content_md,
        memory_type,
        row.version,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.MEMORY_VERSION_CONFLICT)
    if changed is not None:
        await _apply_scope_change(session, row, changed, tenant_id, user_id)
    view = await get_memory(session, memory_id)
    await session.commit()
    return view


async def delete_memory(
    session: AsyncSession,
    memory_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """软删除记忆，去掉员工绑定，并留下删除审核。"""
    row = await _find(session, memory_id)
    if row is None or row.tenant_id != tenant_id:
        raise BizError(ErrorCode.MEMORY_NOT_FOUND)
    updated = await _soft_delete(session, memory_id, tenant_id, row.version, user_id)
    if updated == 0:
        raise BizError(ErrorCode.MEMORY_VERSION_CONFLICT)
    await session.execute(
        delete(AgentMemoryRef).where(
            AgentMemoryRef.memory_id == memory_id,
            AgentMemoryRef.tenant_id == tenant_id,
        )
    )
    await _insert_review(session, tenant_id, memory_id, user_id, "DELETE", None, _DELETE_COMMENT)
    await session.commit()


async def review_memory(
    session: AsyncSession,
    memory_id: int,
    request: ReviewRequest,
    tenant_id: int,
    user_id: int,
) -> None:
    """采纳或拒绝。采纳可以先改类型，再改状态和范围，然后分发。"""
    row = await _find(session, memory_id)
    if row is None or row.tenant_id != tenant_id:
        raise BizError(ErrorCode.MEMORY_NOT_FOUND)
    decision = require_decision(request.decision)
    require_pending(row.status)
    plan = plan_review(
        decision,
        request.edited_content_md,
        request.scope,
        request.owner_ref,
        row.scope,
        row.owner_ref,
    )
    version = row.version
    if decision == "ADOPT" and request.edited_type is not None and request.edited_type != row.type:
        updated = await _update_fields(
            session,
            memory_id,
            tenant_id,
            row.title,
            row.content_md,
            request.edited_type,
            version,
            user_id,
        )
        if updated == 0:
            raise BizError(ErrorCode.MEMORY_VERSION_CONFLICT)
        version = version + 1
    updated = await _update_status(
        session,
        memory_id,
        tenant_id,
        plan.status,
        plan.content_md,
        plan.promoted_scope,
        plan.promoted_owner,
        version,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.MEMORY_VERSION_CONFLICT)
    await _insert_review(
        session,
        tenant_id,
        memory_id,
        user_id,
        decision,
        request.edited_content_md,
        request.comment,
    )
    if decision == "ADOPT":
        await distribute(
            session,
            memory_id=memory_id,
            tenant_id=tenant_id,
            status="ADOPTED",
            scope=cast(str, plan.effective_scope),
            owner_ref=plan.effective_owner,
            user_id=user_id,
        )
    await session.commit()


async def deprecate_from_mcp(
    session: AsyncSession,
    memory_id: int,
    comment: str | None,
    tenant_id: int,
    user_id: int,
) -> MemoryView:
    """把记忆标成拒绝。已经拒绝的不能再弃用。"""
    row = await _find(session, memory_id)
    if row is None or row.tenant_id != tenant_id:
        raise BizError(ErrorCode.MEMORY_NOT_FOUND)
    if row.status == "REJECTED":
        raise BizError(ErrorCode.MEMORY_ALREADY_REVIEWED)
    updated = await _update_status(
        session,
        memory_id,
        tenant_id,
        "REJECTED",
        None,
        None,
        None,
        row.version,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.MEMORY_VERSION_CONFLICT)
    await _insert_review(session, tenant_id, memory_id, user_id, "REJECT", None, comment)
    view = await get_scoped(session, memory_id, tenant_id)
    await session.commit()
    return view


async def list_reviews(session: AsyncSession, memory_id: int) -> list[MemoryReviewView]:
    """一条记忆的审核记录，新的在前。"""
    rows = await session.scalars(
        select(MemoryReview)
        .where(MemoryReview.memory_id == memory_id)
        .order_by(MemoryReview.gmt_create.desc())
    )
    return [_review_view(row) for row in rows]


async def import_from_artifact(
    session: AsyncSession,
    request: ImportFromArtifactRequest,
    tenant_id: int,
    user_id: int,
) -> MemoryView:
    """产物导入不规范化范围，状态是待审核。"""
    title = require_title(request.title)
    source_ref = artifact_source_ref(request.artifact_id)
    memory_id = await _insert_memory(
        session,
        tenant_id=tenant_id,
        scope=request.scope,
        owner_ref=request.owner_ref,
        memory_type=request.type,
        title=title,
        content_md=request.content_md,
        status="PENDING",
        source="ARTIFACT",
        source_ref=source_ref,
        source_dedupe_key=None,
        creator_id=user_id,
    )
    await session.commit()
    return _inserted_view(
        memory_id,
        request.scope,
        request.owner_ref,
        request.type,
        title,
        request.content_md,
        "PENDING",
        "ARTIFACT",
        source_ref,
    )


async def list_applicable(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
) -> list[Memory]:
    """员工能用的已采纳记忆：组织、本人，以及所在小队。"""
    member_match = (
        select(SquadMember.id)
        .where(
            SquadMember.tenant_id == tenant_id,
            SquadMember.squad_id == Memory.owner_ref,
            SquadMember.agent_id == agent_id,
        )
        .exists()
    )
    rows = await session.scalars(
        select(Memory)
        .where(
            Memory.tenant_id == tenant_id,
            Memory.status == "ADOPTED",
            Memory.is_deleted == 0,
            or_(
                Memory.scope == "ORG",
                and_(Memory.scope == "AGENT", Memory.owner_ref == agent_id),
                and_(Memory.scope == "SQUAD", member_match),
            ),
        )
        .order_by(Memory.id.asc())
    )
    return list(rows)


def _adopt_plan(
    edited_content_md: str | None,
    scope: str | None,
    owner_ref: int | None,
    current_scope: str | None,
    current_owner: int | None,
) -> ReviewPlan:
    promoted_scope = normalize_scope(scope)
    promoted_owner = None
    if promoted_scope is not None:
        promoted_owner = owner_ref
        if promoted_scope == "ORG":
            promoted_owner = None
    effective_scope = promoted_scope
    if effective_scope is None:
        effective_scope = normalize_scope(current_scope)
    if promoted_scope is not None:
        effective_owner = promoted_owner
    else:
        effective_owner = current_owner
        if effective_scope == "ORG":
            effective_owner = None
    if effective_scope is None or (effective_scope != "ORG" and effective_owner is None):
        raise BizError(ErrorCode.PARAM_INVALID)
    return ReviewPlan(
        status="ADOPTED",
        content_md=edited_content_md,
        promoted_scope=promoted_scope,
        promoted_owner=promoted_owner,
        effective_scope=effective_scope,
        effective_owner=effective_owner,
    )


def _ref_text(scope: str, owner_ref: int | None) -> str:
    if owner_ref is None:
        return scope + "(null)"
    return scope + "(" + str(owner_ref) + ")"


def _inserted_view(
    memory_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    title: str,
    content_md: str | None,
    status: str,
    source: str,
    source_ref: str | None,
) -> MemoryView:
    return MemoryView(
        id=memory_id,
        scope=scope,
        owner_ref=owner_ref,
        type=memory_type,
        title=title,
        content_md=content_md,
        status=status,
        source=source,
        source_ref=source_ref,
        version=0,
        gmt_create=None,
        gmt_modified=None,
    )


def _view_from_row(row: Memory) -> MemoryView:
    return MemoryView(
        id=row.id,
        scope=row.scope,
        owner_ref=row.owner_ref,
        type=row.type,
        title=row.title,
        content_md=row.content_md,
        status=row.status,
        source=row.source,
        source_ref=source_ref_text(row.source_ref),
        version=row.version,
        gmt_create=row.gmt_create,
        gmt_modified=row.gmt_modified,
    )


def _review_view(row: MemoryReview) -> MemoryReviewView:
    return MemoryReviewView(
        id=row.id,
        memory_id=row.memory_id,
        reviewer_id=row.reviewer_id,
        decision=row.decision,
        edited_content_md=row.edited_content_md,
        comment=row.comment,
        gmt_create=row.gmt_create,
    )


def _filtered(
    statement: Select[Any],
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    status: str | None,
) -> Select[Any]:
    statement = statement.where(Memory.tenant_id == tenant_id, Memory.is_deleted == 0)
    if scope is not None:
        statement = statement.where(Memory.scope == scope)
    if owner_ref is not None:
        statement = statement.where(Memory.owner_ref == owner_ref)
    if memory_type is not None:
        statement = statement.where(Memory.type == memory_type)
    if status is not None:
        statement = statement.where(Memory.status == status)
    else:
        statement = statement.where(Memory.status != "REJECTED")
    return statement


async def _insert_memory(
    session: AsyncSession,
    *,
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    title: str,
    content_md: str | None,
    status: str,
    source: str,
    source_ref: str | None,
    source_dedupe_key: str | None,
    creator_id: int,
) -> int:
    statement = mysql_insert(Memory).values(
        tenant_id=tenant_id,
        scope=scope,
        owner_ref=owner_ref,
        type=memory_type,
        title=title,
        content_md=content_md,
        status=status,
        source=source,
        source_ref=stored_source_ref(source_ref),
        source_dedupe_key=source_dedupe_key,
        creator_id=creator_id,
        is_deleted=0,
        version=0,
    )
    inserted = statement.inserted
    statement = statement.on_duplicate_key_update(
        id=text("LAST_INSERT_ID(id)"),
        scope=inserted.scope,
        owner_ref=inserted.owner_ref,
        type=inserted.type,
        title=inserted.title,
        content_md=inserted.content_md,
        gmt_modified=text("CURRENT_TIMESTAMP"),
        version=text("version + 1"),
    )
    result = await session.execute(statement)
    return cast(CursorResult[Any], result).lastrowid


async def _find(session: AsyncSession, memory_id: int) -> Memory | None:
    return await session.scalar(
        select(Memory).where(Memory.id == memory_id, Memory.is_deleted == 0).limit(1)
    )


async def _find_dedupe(
    session: AsyncSession,
    tenant_id: int,
    source: str,
    dedupe_key: str,
) -> Memory | None:
    return await session.scalar(
        select(Memory)
        .where(
            Memory.tenant_id == tenant_id,
            Memory.source == source,
            Memory.source_dedupe_key == dedupe_key,
            Memory.is_deleted == 0,
        )
        .limit(1)
    )


async def _reuse_mcp_memory(
    session: AsyncSession,
    existing: Memory,
    request: CreateMemoryRequest,
    tenant_id: int,
    title: str,
    user_id: int,
) -> MemoryView:
    action = mcp_existing_action(
        existing.status,
        existing.title,
        existing.content_md,
        title,
        request.content_md,
    )
    if action == "keep":
        return _view_from_row(existing)
    if action == "reject":
        raise BizError(ErrorCode.MEMORY_ALREADY_REVIEWED)
    updated = await _update_fields(
        session,
        existing.id,
        tenant_id,
        title,
        request.content_md,
        request.type,
        existing.version,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.MEMORY_VERSION_CONFLICT)
    view = await get_scoped(session, existing.id, tenant_id)
    await session.commit()
    return view


async def _apply_scope_change(
    session: AsyncSession,
    row: Memory,
    changed: ScopeChange,
    tenant_id: int,
    user_id: int,
) -> None:
    updated = await _update_status(
        session,
        row.id,
        tenant_id,
        row.status,
        None,
        changed.scope,
        changed.owner_ref,
        row.version + 1,
        user_id,
    )
    if updated == 0:
        raise BizError(ErrorCode.MEMORY_VERSION_CONFLICT)
    await _insert_review(
        session,
        tenant_id,
        row.id,
        user_id,
        "SCOPE_CHANGE",
        None,
        scope_change_comment(row.scope, row.owner_ref, changed.scope, changed.owner_ref),
    )
    await distribute(
        session,
        memory_id=row.id,
        tenant_id=tenant_id,
        status=row.status,
        scope=changed.scope,
        owner_ref=changed.owner_ref,
        user_id=user_id,
    )


async def _group_summaries(
    session: AsyncSession,
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
    memory_type: str | None,
    status: str | None,
    offset: int,
    limit: int,
) -> list[_GroupSummary]:
    latest_id = func.max(Memory.id)
    rows = await session.execute(
        _filtered(
            select(
                Memory.scope,
                Memory.owner_ref,
                func.count().label("total"),
                latest_id.label("latest_id"),
            ),
            tenant_id,
            scope,
            owner_ref,
            memory_type,
            status,
        )
        .group_by(Memory.scope, Memory.owner_ref)
        .order_by(latest_id.desc())
        .offset(offset)
        .limit(limit)
    )
    summaries: list[_GroupSummary] = []
    for row in rows:
        summaries.append(
            _GroupSummary(scope=row.scope, owner_ref=row.owner_ref, total=int(row.total))
        )
    return summaries


async def _memories_for_groups(
    session: AsyncSession,
    tenant_id: int,
    summaries: list[_GroupSummary],
    memory_type: str | None,
    status: str | None,
) -> list[Memory]:
    pairs = []
    for summary in summaries:
        if summary.owner_ref is None:
            pairs.append(and_(Memory.scope == summary.scope, Memory.owner_ref.is_(None)))
        else:
            pairs.append(
                and_(Memory.scope == summary.scope, Memory.owner_ref == summary.owner_ref)
            )
    statement = select(Memory).where(
        Memory.tenant_id == tenant_id,
        Memory.is_deleted == 0,
        or_(*pairs),
    )
    if memory_type is not None:
        statement = statement.where(Memory.type == memory_type)
    if status is not None:
        statement = statement.where(Memory.status == status)
    else:
        statement = statement.where(Memory.status != "REJECTED")
    rows = await session.scalars(
        statement.order_by(Memory.id.desc()).limit(GROUPED_MEMORY_FETCH_LIMIT)
    )
    return list(rows)


async def _agent_names(
    session: AsyncSession,
    tenant_id: int,
    summaries: list[_GroupSummary],
) -> dict[int, str]:
    agent_ids: list[int] = []
    seen: set[int] = set()
    for summary in summaries:
        if summary.scope != "AGENT" or summary.owner_ref is None:
            continue
        if summary.owner_ref in seen:
            continue
        seen.add(summary.owner_ref)
        agent_ids.append(summary.owner_ref)
    if len(agent_ids) == 0:
        return {}
    rows = await session.scalars(
        select(Agent).where(
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
            Agent.id.in_(agent_ids),
        )
    )
    return {agent.id: agent.name for agent in rows}


async def _update_fields(
    session: AsyncSession,
    memory_id: int,
    tenant_id: int,
    title: str,
    content_md: str | None,
    memory_type: str | None,
    version: int,
    user_id: int,
) -> int:
    return rowcount(
        await session.execute(
            update(Memory)
            .where(
                Memory.id == memory_id,
                Memory.tenant_id == tenant_id,
                Memory.version == version,
                Memory.is_deleted == 0,
            )
            .values(
                title=title,
                content_md=content_md,
                type=memory_type,
                version=Memory.version + 1,
                modifier_id=user_id,
            )
        )
    )


async def _update_status(
    session: AsyncSession,
    memory_id: int,
    tenant_id: int,
    status: str,
    content_md: str | None,
    scope: str | None,
    owner_ref: int | None,
    version: int,
    user_id: int,
) -> int:
    values: dict[str, Any] = {
        "status": status,
        "version": Memory.version + 1,
        "modifier_id": user_id,
    }
    if content_md is not None:
        values["content_md"] = content_md
    if scope is not None:
        values["scope"] = scope
        values["owner_ref"] = owner_ref
    return rowcount(
        await session.execute(
            update(Memory)
            .where(
                Memory.id == memory_id,
                Memory.tenant_id == tenant_id,
                Memory.version == version,
                Memory.is_deleted == 0,
            )
            .values(**values)
        )
    )


async def _soft_delete(
    session: AsyncSession,
    memory_id: int,
    tenant_id: int,
    version: int,
    user_id: int,
) -> int:
    return rowcount(
        await session.execute(
            update(Memory)
            .where(
                Memory.id == memory_id,
                Memory.tenant_id == tenant_id,
                Memory.version == version,
                Memory.is_deleted == 0,
            )
            .values(is_deleted=1, version=Memory.version + 1, modifier_id=user_id)
        )
    )


async def _insert_review(
    session: AsyncSession,
    tenant_id: int,
    memory_id: int,
    user_id: int,
    decision: str,
    edited_content_md: str | None,
    comment: str | None,
) -> None:
    session.add(
        MemoryReview(
            tenant_id=tenant_id,
            memory_id=memory_id,
            reviewer_id=user_id,
            decision=decision,
            edited_content_md=edited_content_md,
            comment=comment,
        )
    )
    await session.flush()
