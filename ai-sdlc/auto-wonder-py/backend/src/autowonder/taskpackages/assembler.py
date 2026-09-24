"""从工单和冻结版本装配 ``PackageContext``。

查询都按派发上的租户过滤。后台线程没有请求租户，不能靠会话过滤器兜住。
"""

import json
import logging
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import (
    Agent,
    AgentMemoryRef,
    AgentRepoPerm,
    AgentSkill,
    AgentVersion,
)
from autowonder.artifacts.documents import TYPE as REQUIREMENT_DOC
from autowonder.artifacts.models import Artifact
from autowonder.clarifications.models import Clarification
from autowonder.dispatch.enqueue import is_interaction
from autowonder.dispatch.models import Dispatch
from autowonder.memories.models import Memory
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.repos.models import Repo, RepoRelation
from autowonder.sdlcs.models import SdlcStep
from autowonder.skills.models import Skill
from autowonder.squads.models import SquadMember
from autowonder.statemachines.models import StatusNode
from autowonder.taskpackages.context import (
    PackageContext,
    TaskArtifactRef,
    TaskComment,
    TeammateOutput,
)
from autowonder.users.models import User
from autowonder.workitems.models import Workitem, WorkitemComment
from autowonder.workspaces.models import OrgMember

logger = logging.getLogger(__name__)

_MAX_MEMORIES = 50
_PLATFORM = "PLATFORM"
_INTERACTION_HEADER = "# Side Interaction Conversation\n\n"


async def assemble_workitem(
    session: AsyncSession, dispatch: Dispatch, version: AgentVersion
) -> PackageContext:
    """装配工单派发。定时运行不走这里，避免用相同数值误查工单。"""
    logger.info(
        "package assemble dispatchId=%s workitemId=%s agentId=%s",
        dispatch.id,
        dispatch.workitem_id,
        dispatch.agent_id,
    )
    tenant_id = dispatch.tenant_id
    ctx = PackageContext(
        tenant_id=tenant_id,
        dispatch_id=dispatch.id,
        workitem_id=dispatch.workitem_id,
        agent_id=dispatch.agent_id,
        sdlc_step_id=dispatch.sdlc_step_id,
        attempt=dispatch.attempt,
        executor_id=dispatch.executor_id,
        idempotency_key=dispatch.idempotency_key,
        agent_version_id=version.id,
        role_code=version.role_code,
        role_name=version.role_name,
    )
    workitem = await session.get(Workitem, dispatch.workitem_id)
    if workitem is not None and workitem.tenant_id == tenant_id:
        ctx.workitem_title = workitem.title
        ctx.workitem_content_md = workitem.content_md
        ctx.work_type = workitem.work_type
        ctx.sdlc_id = workitem.sdlc_id
        ctx.workitem_status = await _workitem_status(session, workitem)
    clarification = await session.scalar(
        select(Clarification).where(Clarification.workitem_id == dispatch.workitem_id).limit(1)
    )
    if clarification is not None and clarification.tenant_id == tenant_id:
        ctx.clarification_md = clarification.content_md
    comments = await _comments(session, tenant_id, dispatch.workitem_id)
    ctx.comments = comments
    ctx.comments_md = _comments_markdown(comments)
    ctx.interaction_context_md = await _interaction_context(session, dispatch)
    ctx.requirement_documents = await _requirement_documents(
        session, tenant_id, dispatch.workitem_id
    )
    ctx.identity = _identity(version)
    agent = None if dispatch.agent_id is None else await session.get(Agent, dispatch.agent_id)
    ctx.repos = await _repos(session, tenant_id, version.id, agent)
    ctx.repo_map = await _repo_map(session, tenant_id, ctx.repos)
    ctx.skills = await _capabilities(session, tenant_id, version.id)
    ctx.sdlc = await _sdlc(session, tenant_id, dispatch.sdlc_step_id)
    interaction = is_interaction(dispatch)
    comment_rework = dispatch.resume_mode == "COMMENT_REWORK"
    legacy_source = (
        dispatch.resume_from_dispatch_id
        if interaction or comment_rework
        else _parse_source_dispatch_id(dispatch.idempotency_key)
    )
    source_id = (
        dispatch.delivery_source_dispatch_id
        if dispatch.delivery_source_dispatch_id is not None
        else legacy_source
    )
    recovery_source = (
        dispatch.resume_mode == "RECOVERY"
        or interaction
        or comment_rework
        or (
            dispatch.idempotency_key is not None
            and dispatch.idempotency_key.startswith("continue:")
        )
    )
    ctx.source_dispatch_id = source_id
    ctx.teammates = await _teammates(
        session, tenant_id, dispatch.workitem_id, dispatch.id, source_id, recovery_source
    )
    ctx.source_revision_artifacts = await _source_revisions(
        session, tenant_id, dispatch.workitem_id, dispatch.id, source_id, recovery_source
    )
    ctx.roster = await _roster(session, tenant_id, dispatch.agent_id, workitem)
    ctx.memory = await _memory(session, tenant_id, version.id)
    logger.info(
        "package assembled dispatchId=%s repos=%s skills=%s teammates=%s",
        dispatch.id,
        0 if ctx.repos is None else len(ctx.repos),
        0 if ctx.skills is None else len(ctx.skills),
        0 if ctx.teammates is None else len(ctx.teammates),
    )
    return ctx


async def _comments(session: AsyncSession, tenant_id: int, workitem_id: int) -> list[TaskComment]:
    rows = (
        await session.scalars(
            select(WorkitemComment)
            .where(
                WorkitemComment.tenant_id == tenant_id,
                WorkitemComment.source_type == "WORKITEM",
                WorkitemComment.workitem_id == workitem_id,
            )
            .order_by(WorkitemComment.gmt_create.desc(), WorkitemComment.id.desc())
        )
    ).all()
    snapshots: list[TaskComment] = []
    for comment in rows:
        if comment.tenant_id != tenant_id or comment.id <= 0:
            continue
        snapshots.append(
            TaskComment(comment.id, comment.author_type, comment.author_ref, comment.content_md)
        )
    return snapshots


def _comments_markdown(comments: Sequence[TaskComment]) -> str | None:
    if len(comments) == 0:
        return None
    parts = ["# Workitem Comments\n\n"]
    for comment in comments:
        body = "" if comment.content_md is None else comment.content_md.strip()
        parts.append(
            "## Comment "
            + str(comment.id)
            + " · "
            + str(comment.author_type)
            + " "
            + str(comment.author_ref)
            + "\n\n"
            + body
            + "\n\n"
        )
    return "".join(parts)


async def _interaction_context(session: AsyncSession, dispatch: Dispatch) -> str | None:
    rework = await _comment_rework_source(session, dispatch)
    if rework is None or rework.idempotency_key is None:
        return None
    source_id = _prefixed_id(rework.idempotency_key, "interaction-rework:")
    if source_id is None:
        return None
    source = await session.get(Dispatch, source_id)
    if (
        source is None
        or source.resume_mode != "SIDE_INTERACTION"
        or source.resume_from_dispatch_id is None
    ):
        return None
    canonical_source = source.resume_from_dispatch_id
    rows = (
        await session.scalars(
            select(WorkitemCommentDelivery)
            .where(
                WorkitemCommentDelivery.tenant_id == dispatch.tenant_id,
                WorkitemCommentDelivery.source_type == "WORKITEM",
                WorkitemCommentDelivery.workitem_id == dispatch.workitem_id,
            )
            .order_by(WorkitemCommentDelivery.id.asc())
        )
    ).all()
    if len(rows) == 0:
        return None
    parts = [_INTERACTION_HEADER]
    for guidance in rows:
        if (
            guidance.dispatch_id is None
            or guidance.dispatch_id > source_id
            or guidance.tenant_id != dispatch.tenant_id
            or guidance.workitem_id != dispatch.workitem_id
            or guidance.target_agent_id != source.agent_id
        ):
            continue
        interaction = await session.get(Dispatch, guidance.dispatch_id)
        if (
            interaction is None
            or interaction.resume_mode != "SIDE_INTERACTION"
            or interaction.resume_from_dispatch_id != canonical_source
        ):
            continue
        await _append_comment(
            session, parts, dispatch.tenant_id, dispatch.workitem_id, guidance.comment_id
        )
        await _append_comment(
            session, parts, dispatch.tenant_id, dispatch.workitem_id, guidance.reply_comment_id
        )
    markdown = "".join(parts)
    if markdown == _INTERACTION_HEADER:
        return None
    return markdown


async def _append_comment(
    session: AsyncSession,
    parts: list[str],
    tenant_id: int,
    workitem_id: int,
    comment_id: int | None,
) -> None:
    if comment_id is None:
        return
    comment = await session.get(WorkitemComment, comment_id)
    if comment is None or comment.tenant_id != tenant_id or comment.workitem_id != workitem_id:
        return
    body = "" if comment.content_md is None else comment.content_md.strip()
    parts.append(
        "## Comment "
        + str(comment.id)
        + " · "
        + comment.author_type
        + " "
        + str(comment.author_ref)
        + "\n\n"
        + body
        + "\n\n"
    )


async def _comment_rework_source(session: AsyncSession, dispatch: Dispatch) -> Dispatch | None:
    current: Dispatch | None = dispatch
    visited: set[int] = set()
    while current is not None:
        if current.resume_mode == "COMMENT_REWORK":
            return current
        if current.resume_mode != "RECOVERY" or current.resume_from_dispatch_id is None:
            return None
        if current.resume_from_dispatch_id in visited:
            return None
        visited.add(current.resume_from_dispatch_id)
        source = await session.get(Dispatch, current.resume_from_dispatch_id)
        if (
            source is None
            or source.tenant_id != dispatch.tenant_id
            or source.workitem_id != dispatch.workitem_id
        ):
            return None
        current = source
    return None


def _prefixed_id(value: str | None, prefix: str) -> int | None:
    if value is None or not value.startswith(prefix):
        return None
    try:
        return int(value[len(prefix) :])
    except ValueError:
        return None


def _parse_source_dispatch_id(idempotency_key: str | None) -> int | None:
    prefix = None
    if idempotency_key is not None and idempotency_key.startswith("handoff:"):
        prefix = "handoff:"
    elif idempotency_key is not None and idempotency_key.startswith("continue:"):
        prefix = "continue:"
    if prefix is None or idempotency_key is None:
        return None
    try:
        source_id = int(idempotency_key[len(prefix) :])
    except ValueError:
        logger.warning("invalid source dispatch idempotency key ignored key=%s", idempotency_key)
        return None
    if source_id <= 0:
        return None
    return source_id


async def _requirement_documents(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> list[TaskArtifactRef]:
    rows = (
        await session.scalars(
            select(Artifact)
            .where(
                Artifact.tenant_id == tenant_id,
                Artifact.source_type == "WORKITEM",
                Artifact.workitem_id == workitem_id,
                Artifact.type == REQUIREMENT_DOC,
            )
            .order_by(Artifact.id.asc())
        )
    ).all()
    refs: list[TaskArtifactRef] = []
    for artifact in rows:
        if (
            artifact.tenant_id != tenant_id
            or artifact.workitem_id != workitem_id
            or artifact.name is None
            or artifact.oss_ref is None
        ):
            continue
        refs.append(TaskArtifactRef(name=artifact.name, oss_ref=artifact.oss_ref))
    return refs


async def _memory(session: AsyncSession, tenant_id: int, version_id: int) -> dict[str, str]:
    refs = (
        await session.scalars(
            select(AgentMemoryRef)
            .where(AgentMemoryRef.agent_version_id == version_id)
            .order_by(AgentMemoryRef.memory_id.asc())
        )
    ).all()
    ordered = [
        ref
        for ref in refs
        if ref.memory_id is not None and ref.memory_id > 0 and ref.tenant_id == tenant_id
    ]
    ordered.sort(key=lambda ref: ref.memory_id)
    output: dict[str, str] = {}
    seen: set[int] = set()
    for ref in ordered:
        if len(output) >= _MAX_MEMORIES:
            break
        if ref.memory_id in seen:
            continue
        seen.add(ref.memory_id)
        memory = await session.get(Memory, ref.memory_id)
        if (
            memory is None
            or memory.tenant_id != tenant_id
            or memory.status != "ADOPTED"
            or memory.content_md is None
            or memory.content_md.strip() == ""
        ):
            continue
        output["mem_id_" + str(ref.memory_id)] = memory.content_md
    return output


def _identity(version: AgentVersion) -> dict[str, object]:
    raw = version.identity_json
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip() != "":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {
        "name": version.role_name,
        "roleCode": version.role_code,
        "businessBackground": version.business_background,
        "responsibilities": version.responsibilities,
    }


async def _repos(
    session: AsyncSession, tenant_id: int, version_id: int, agent: Agent | None
) -> list[dict[str, object]]:
    if agent is not None and agent.tenant_id == tenant_id and agent.kind == _PLATFORM:
        rows = (
            await session.scalars(
                select(Repo).where(Repo.tenant_id == tenant_id, Repo.is_deleted == 0)
            )
        ).all()
        return [_repo_entry(repo, False, None) for repo in rows if repo.tenant_id == tenant_id]
    perms = (
        await session.scalars(
            select(AgentRepoPerm).where(AgentRepoPerm.agent_version_id == version_id)
        )
    ).all()
    repos: list[dict[str, object]] = []
    for perm in perms:
        if perm.tenant_id != tenant_id:
            continue
        repo = await session.get(Repo, perm.repo_id)
        if repo is None or repo.tenant_id != tenant_id:
            continue
        patterns = _branch_patterns(perm.allowed_branch_patterns)
        repos.append(_repo_entry(repo, perm.perm_level.upper() == "WRITE", patterns))
    return repos


def _branch_patterns(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    if isinstance(raw, str) and raw.strip() != "":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str)]
    return None


def _repo_entry(repo: Repo, writable: bool, patterns: list[str] | None) -> dict[str, object]:
    entry: dict[str, object] = {
        "repoId": repo.id,
        "name": repo.name,
        "url": repo.url,
    }
    if repo.default_branch is not None and repo.default_branch.strip() != "":
        entry["ref"] = repo.default_branch.strip()
    entry["path"] = repo.name
    entry["mode"] = "eager" if writable else "lazy"
    entry["allowCommit"] = writable
    entry["allowPush"] = writable
    if patterns:
        entry["allowedBranchPatterns"] = patterns
    entry["allowNetwork"] = True
    return entry


async def _repo_map(
    session: AsyncSession, tenant_id: int, bound: list[dict[str, object]] | None
) -> dict[str, object] | None:
    if not bound:
        return None
    bound_ids: list[int] = []
    seen_ids: set[int] = set()
    for repo in bound:
        raw = repo.get("repoId")
        if isinstance(raw, int) and raw not in seen_ids:
            seen_ids.add(raw)
            bound_ids.append(raw)
    relations_by_id: dict[int, RepoRelation] = {}
    for repo_id in bound_ids:
        rows = (
            await session.scalars(
                select(RepoRelation).where(
                    RepoRelation.tenant_id == tenant_id,
                    RepoRelation.is_deleted == 0,
                    (RepoRelation.from_repo_id == repo_id) | (RepoRelation.to_repo_id == repo_id),
                )
            )
        ).all()
        for relation in rows:
            if relation.tenant_id == tenant_id and relation.id not in relations_by_id:
                relations_by_id[relation.id] = relation
    relations: list[dict[str, object]] = []
    for relation in sorted(relations_by_id.values(), key=lambda item: item.id):
        item = await _repo_relation(session, tenant_id, relation)
        if item is not None:
            relations.append(item)
    return {"boundRepoIds": bound_ids, "relations": relations}


async def _repo_relation(
    session: AsyncSession, tenant_id: int, relation: RepoRelation
) -> dict[str, object] | None:
    source = await session.get(Repo, relation.from_repo_id)
    target = await session.get(Repo, relation.to_repo_id)
    if (
        source is None
        or target is None
        or source.tenant_id != tenant_id
        or target.tenant_id != tenant_id
    ):
        logger.warning(
            "skip invalid repo relation relationId=%s tenantId=%s", relation.id, tenant_id
        )
        return None
    item: dict[str, object] = {
        "id": relation.id,
        "fromRepoId": relation.from_repo_id,
        "fromRepoName": source.name,
        "toRepoId": relation.to_repo_id,
        "toRepoName": target.name,
        "relationType": relation.relation_type,
    }
    if relation.description is not None and relation.description.strip() != "":
        item["description"] = relation.description
    return item


async def _capabilities(
    session: AsyncSession, tenant_id: int, version_id: int
) -> list[dict[str, object]]:
    bindings = (
        await session.scalars(select(AgentSkill).where(AgentSkill.agent_version_id == version_id))
    ).all()
    capabilities: list[dict[str, object]] = []
    for binding in bindings:
        if binding.skill_id is None or binding.tenant_id != tenant_id:
            continue
        skill = await session.get(Skill, binding.skill_id)
        if skill is None:
            logger.warning(
                "skip deleted bound capability agentVersionId=%s skillId=%s",
                version_id,
                binding.skill_id,
            )
            continue
        if skill.tenant_id != tenant_id:
            raise RuntimeError(
                "bound capability is missing or belongs to another tenant: "
                + str(binding.skill_id)
            )
        item: dict[str, object] = {
            "id": skill.id,
            "type": skill.type,
            "name": skill.name,
            "description": skill.description,
            "version": 0 if skill.version is None else skill.version,
            "required": True,
        }
        if skill.package_oss_ref is not None and skill.package_oss_ref.strip() != "":
            item["packageOssRef"] = skill.package_oss_ref
            item["packageMd5"] = skill.package_md5
        if skill.install_spec is not None:
            item["config"] = _capability_config(skill)
        capabilities.append(item)
    capabilities.sort(key=_capability_order)
    return capabilities


def _capability_order(row: dict[str, object]) -> tuple[str, str, int]:
    return (str(row.get("type")), str(row.get("name")), cast(int, row["id"]))


def _capability_config(skill: Skill) -> dict[str, object]:
    raw = skill.install_spec
    parsed: object = raw
    if isinstance(raw, str):
        if raw.strip() == "":
            raise RuntimeError("capability config must be a JSON object: " + str(skill.id))
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        return parsed
    if skill.type.upper() == "SKILL" and isinstance(raw, str):
        return {"instructions": raw}
    raise RuntimeError("capability config must be a JSON object: " + str(skill.id))


async def _sdlc(
    session: AsyncSession, tenant_id: int, step_id: int | None
) -> dict[str, object]:
    if step_id is None:
        return {
            "workflow": "interaction-only",
            "currentStepId": "interaction",
            "steps": [
                {
                    "id": "interaction",
                    "name": "用户评论交互",
                    "kind": "interaction",
                    "required": True,
                    "checklist": [],
                    "gatePolicy": {},
                }
            ],
            "outputContract": {},
        }
    current = await session.get(SdlcStep, step_id)
    if current is None or current.tenant_id != tenant_id:
        return {}
    rows = (
        await session.scalars(
            select(SdlcStep)
            .where(SdlcStep.sdlc_id == current.sdlc_id, SdlcStep.tenant_id == tenant_id)
            .order_by(SdlcStep.step_order.asc(), SdlcStep.id.asc())
        )
    ).all()
    steps: list[dict[str, object]] = []
    for step in rows:
        if step.tenant_id != tenant_id:
            continue
        item: dict[str, object] = {
            "id": str(step.id),
            "name": step.name,
            "kind": step.kind,
            "required": step.required != 0,
            "instruction": step.instruction_md,
            "checklist": _checklist(step.checklist_json),
            "gatePolicy": _json_map(step.gate_policy_json),
        }
        if step.timeout_seconds is not None:
            item["timeoutSeconds"] = step.timeout_seconds
        if step.retry_budget is not None:
            item["retryBudget"] = step.retry_budget
        steps.append(item)
    return {
        "sdlcId": str(current.sdlc_id),
        "workflow": "agent-internal-workflow",
        "currentStepId": str(current.id),
        "steps": steps,
        "outputContract": {"reviewerHandoffRequired": True},
    }


def _checklist(raw: object) -> list[dict[str, object]]:
    items = _json_list(raw)
    result: list[dict[str, object]] = []
    for index, item in enumerate(items):
        mapped: dict[str, object]
        if isinstance(item, str):
            mapped = {"id": "cl_" + str(index), "text": item}
        elif isinstance(item, dict):
            mapped = dict(item)
        else:
            mapped = {}
        mapped["checked"] = False
        mapped.pop("status", None)
        mapped.pop("reason", None)
        result.append(mapped)
    return result


def _json_list(raw: object) -> list[object]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str) and raw.strip() != "":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("invalid sdlc checklist json ignored")
            return []
        if isinstance(parsed, list):
            return parsed
    return []


def _json_map(raw: object) -> dict[str, object]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip() != "":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("invalid sdlc gate policy json ignored")
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _workitem_status(session: AsyncSession, workitem: Workitem) -> dict[str, object]:
    if workitem.template_id is None:
        return {}
    result: dict[str, object] = {}
    if workitem.status_node_id is not None:
        current = await session.get(StatusNode, workitem.status_node_id)
        if current is not None:
            result["currentStatus"] = _status_node(current)
    nodes = (
        await session.scalars(
            select(StatusNode)
            .where(StatusNode.template_id == workitem.template_id)
            .order_by(StatusNode.sort.asc(), StatusNode.id.asc())
        )
    ).all()
    result["statuses"] = [_status_node(node) for node in nodes]
    return result


def _status_node(node: StatusNode) -> dict[str, object]:
    return {"nodeId": node.id, "code": node.code, "name": node.name, "category": node.category}


async def _roster(
    session: AsyncSession, tenant_id: int, self_agent_id: int, workitem: Workitem | None
) -> dict[str, object]:
    digital: list[dict[str, object]] = []
    seen: set[int] = set()
    memberships = (
        await session.scalars(select(SquadMember).where(SquadMember.agent_id == self_agent_id))
    ).all()
    for membership in memberships:
        if membership.tenant_id != tenant_id:
            continue
        mates = (
            await session.scalars(
                select(SquadMember).where(SquadMember.squad_id == membership.squad_id)
            )
        ).all()
        for mate in mates:
            if (
                mate.tenant_id != tenant_id
                or mate.agent_id == self_agent_id
                or mate.agent_id in seen
            ):
                continue
            seen.add(mate.agent_id)
            agent = await session.get(Agent, mate.agent_id)
            if agent is None or agent.tenant_id != tenant_id:
                continue
            role_code = None
            role_name = None
            if agent.online_version_id is not None:
                peer = await session.get(AgentVersion, agent.online_version_id)
                if peer is not None and peer.tenant_id == tenant_id:
                    role_code = peer.role_code
                    role_name = peer.role_name
            digital.append({"agentId": agent.id, "roleCode": role_code, "roleName": role_name})
    humans: list[dict[str, object]] = []
    if workitem is not None and workitem.tenant_id == tenant_id:
        members = (
            await session.scalars(
                select(OrgMember)
                .where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.is_deleted == 0,
                    OrgMember.status == 0,
                )
                .order_by(OrgMember.joined_at.asc())
            )
        ).all()
        for member in members:
            user = await session.get(User, member.user_id)
            if user is None or user.status != 0:
                continue
            name = user.username
            if user.nickname is not None and user.nickname.strip() != "":
                name = user.nickname
            human: dict[str, object] = {"userId": user.id, "name": name}
            if user.id == workitem.assign_operator_id:
                human["relation"] = "指派操作人"
                human["role"] = "需求决策人"
            elif (
                workitem.assignee_type is not None
                and workitem.assignee_type.upper() == "HUMAN"
                and user.id == workitem.assignee_ref
            ):
                human["relation"] = "assignee"
            else:
                human["relation"] = "空间成员"
            humans.append(human)
    return {"digitalTeammates": digital, "humanTeammates": humans}


async def _teammates(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    self_dispatch_id: int,
    source_id: int | None,
    recovery_source: bool,
) -> list[TeammateOutput]:

    if source_id is None or source_id == self_dispatch_id:
        return []
    source = await session.get(Dispatch, source_id)
    if (
        source is None
        or source.tenant_id != tenant_id
        or source.workitem_id != workitem_id
        or (not recovery_source and source.status != "SUCCEEDED")
    ):
        return []
    teammate = TeammateOutput(
        agent_id=str(source.agent_id),
        dispatch_id=str(source.id),
        conclusion_md=source.result_summary,
    )
    if source.agent_version_id is not None:
        peer = await session.get(AgentVersion, source.agent_version_id)
        if peer is not None and peer.tenant_id == tenant_id:
            teammate.role_name = peer.role_name
    artifacts = (
        await session.scalars(
            select(Artifact)
            .where(Artifact.tenant_id == tenant_id, Artifact.dispatch_id == source.id)
            .order_by(Artifact.id.desc())
        )
    ).all()
    refs: list[TaskArtifactRef] = []
    for artifact in artifacts:
        if artifact.tenant_id != tenant_id or not _handoff_artifact(artifact):
            continue
        refs.append(TaskArtifactRef(name=artifact.name, oss_ref=artifact.oss_ref))
    teammate.artifacts = refs
    return [teammate]


def _handoff_artifact(artifact: Artifact) -> bool:
    if artifact.name is None or artifact.name.strip() == "" or artifact.oss_ref is None:
        return False
    if artifact.oss_ref.strip() == "":
        return False
    if artifact.size is not None and artifact.size <= 0:
        return False
    path = artifact.name.replace("\\", "/")
    while path.startswith("/"):
        path = path[1:]
    if path.startswith("artifacts/attempts/"):
        return False
    if path.startswith("artifacts/output/"):
        path = path[len("artifacts/output/") :]
    if (
        path.startswith("artifacts/attempts/")
        or path.startswith("observability/")
        or path.startswith("result/")
        or path.startswith("learning_delta/")
    ):
        return False
    if path in ("handoff/metadata.json", "handoff/summary.md"):
        return False
    return not path.endswith("deliverables/runtime-source-revision.json")


async def _source_revisions(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    self_dispatch_id: int,
    source_id: int | None,
    recovery_source: bool,
) -> list[TaskArtifactRef]:
    delivery: list[TaskArtifactRef] = []
    visited: set[int] = set()
    current_id = source_id
    direct = True
    while (
        current_id is not None
        and current_id != self_dispatch_id
        and current_id not in visited
        and len(visited) < 64
    ):
        visited.add(current_id)
        source = await session.get(Dispatch, current_id)
        if (
            source is None
            or source.tenant_id != tenant_id
            or source.workitem_id != workitem_id
            or (direct and not recovery_source and source.status != "SUCCEEDED")
        ):
            break
        artifacts = (
            await session.scalars(
                select(Artifact).where(
                    Artifact.tenant_id == tenant_id, Artifact.dispatch_id == current_id
                )
            )
        ).all()
        for artifact in artifacts:
            if artifact.tenant_id != tenant_id or artifact.name is None:
                continue
            if not artifact.name.replace("\\", "/").endswith(
                "deliverables/runtime-source-revision.json"
            ):
                continue
            delivery.append(TaskArtifactRef(name=artifact.name, oss_ref=artifact.oss_ref))
        current_id = (
            source.delivery_source_dispatch_id
            if source.delivery_source_dispatch_id is not None
            else _parse_source_dispatch_id(source.idempotency_key)
        )
        direct = False
    return delivery
