"""代码仓库、结论和仓库关系。查询与删除占用说明对齐 RepoService。"""

import json
from dataclasses import dataclass

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentRepoPerm, AgentVersion
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.repos.models import Repo, RepoConclusion, RepoRelation
from autowonder.repos.schemas import (
    CreateRelationRequest,
    CreateRepoRequest,
    RepoConclusionView,
    RepoRelationView,
    RepoView,
    UpdateConclusionRequest,
    UpdateRepoFields,
)

_ONLINE = "ONLINE"


@dataclass
class RepoUse:
    """仍挂在在线版本或编辑草稿上的仓库权限。"""

    agent_id: int | None
    agent_name: str | None
    version_no: int | None
    ref_type: str | None


def page_window(page: int, size: int) -> tuple[int, int]:
    """页码小于 1 时从第 1 页起；每页小于 1 时用 1，并且不超过 100。"""
    normalized_page = page
    if page < 1:
        normalized_page = 1
    normalized_size = size
    if size < 1:
        normalized_size = 1
    if normalized_size > 100:
        normalized_size = 100
    return (normalized_page - 1) * normalized_size, normalized_size


def require_repo_name(name: str | None) -> str:
    """创建时名称必填。"""
    if name is None or name.strip() == "":
        raise BizError(ErrorCode.REPO_NAME_REQUIRED)
    return name.strip()


def require_repo_url(url: str | None) -> str:
    """创建时地址必填。"""
    if url is None or url.strip() == "":
        raise BizError(ErrorCode.REPO_URL_REQUIRED)
    return url.strip()


def required_field(
    present: bool,
    value: str | None,
    current: str | None,
    missing: ErrorCode,
) -> str | None:
    """出现过的必填字段不能是空白；省略时保留原值。"""
    if not present:
        return current
    if value is None or value.strip() == "":
        raise BizError(missing)
    return value.strip()


def optional_field(present: bool, value: str | None, current: str | None) -> str | None:
    """出现过的可空字段按原样写入，包括 null。"""
    if present:
        return value
    return current


def describe_active_refs(refs: list[RepoUse]) -> str:
    """删除被引用的仓库时，列出数字员工和版本。"""
    parts: list[str] = []
    for ref in refs:
        name = "数字员工"
        if ref.agent_name is not None and ref.agent_name.strip() != "":
            name = ref.agent_name
        kind = "编辑草稿"
        if ref.ref_type == _ONLINE:
            kind = "在线版本"
        version = ""
        if ref.version_no is not None:
            version = " v" + str(ref.version_no)
        parts.append(f"{name}(#{ref.agent_id}) {kind}{version}")
    detail = "；".join(parts)
    return (
        "仓库仍被数字员工引用,无法删除:"
        + detail
        + "。请先在对应版本解除该仓库权限,或发布解除后的新版本再删除仓库。"
    )


async def create_repo(
    session: AsyncSession,
    request: CreateRepoRequest,
    tenant_id: int,
    user_id: int,
) -> RepoView:
    """插入未扫描仓库。插入语句不回读创建时间。"""
    repo = Repo(
        tenant_id=tenant_id,
        name=require_repo_name(request.name),
        url=require_repo_url(request.url),
        default_branch=request.default_branch,
        description=request.description,
        scan_status="UNSCANNED",
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(repo)
    await session.flush()
    view = _to_view(repo)
    view.gmt_create = None
    await session.commit()
    return view


async def get_repo(session: AsyncSession, repo_id: int) -> RepoView:
    """按 id 读取未删除仓库。"""
    return _to_view(await _require_repo(session, repo_id))


async def list_repos(
    session: AsyncSession,
    tenant_id: int,
    page: int,
    size: int,
) -> list[RepoView]:
    """按 id 倒序分页。"""
    offset, limit = page_window(page, size)
    rows = await session.scalars(
        select(Repo)
        .where(Repo.tenant_id == tenant_id, Repo.is_deleted == 0)
        .order_by(Repo.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [_to_view(repo) for repo in rows]


async def update_repo(
    session: AsyncSession,
    repo_id: int,
    fields: UpdateRepoFields,
    tenant_id: int,
    user_id: int,
) -> RepoView:
    """按字段是否出现更新。名称和地址出现时不能为空。"""
    repo = await _require_repo(session, repo_id)
    name = required_field(
        fields.name_present, fields.name, repo.name, ErrorCode.REPO_NAME_REQUIRED
    )
    url = required_field(fields.url_present, fields.url, repo.url, ErrorCode.REPO_URL_REQUIRED)
    default_branch = optional_field(
        fields.default_branch_present,
        fields.default_branch,
        repo.default_branch,
    )
    description = optional_field(fields.description_present, fields.description, repo.description)
    updated = rowcount(
        await session.execute(
            update(Repo)
            .where(
                Repo.id == repo_id,
                Repo.tenant_id == tenant_id,
                Repo.version == repo.version,
                Repo.is_deleted == 0,
            )
            .values(
                name=name,
                url=url,
                default_branch=default_branch,
                description=description,
                version=Repo.version + 1,
                modifier_id=user_id,
            )
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.REPO_VERSION_CONFLICT)
    await session.commit()
    session.expire_all()
    return await get_repo(session, repo_id)


async def delete_repo(session: AsyncSession, repo_id: int, tenant_id: int, user_id: int) -> None:
    """仍被数字员工版本引用时不能删。删除后一并清掉结论和关系。"""
    repo = await _require_repo(session, repo_id)
    refs = await _list_active_refs(session, repo_id, tenant_id)
    if len(refs) > 0:
        raise BizError(ErrorCode.REPO_DELETE_IN_USE, describe_active_refs(refs))
    deleted = rowcount(
        await session.execute(
            update(Repo)
            .where(
                Repo.id == repo_id,
                Repo.tenant_id == tenant_id,
                Repo.version == repo.version,
                Repo.is_deleted == 0,
            )
            .values(is_deleted=1, version=Repo.version + 1, modifier_id=user_id)
        )
    )
    if deleted == 0:
        raise BizError(ErrorCode.REPO_VERSION_CONFLICT)
    await session.execute(
        update(RepoConclusion)
        .where(
            RepoConclusion.repo_id == repo_id,
            RepoConclusion.tenant_id == tenant_id,
            RepoConclusion.is_deleted == 0,
        )
        .values(is_deleted=1)
    )
    await session.execute(
        update(RepoRelation)
        .where(
            RepoRelation.tenant_id == tenant_id,
            RepoRelation.is_deleted == 0,
            or_(RepoRelation.from_repo_id == repo_id, RepoRelation.to_repo_id == repo_id),
        )
        .values(is_deleted=1)
    )
    await session.commit()


async def start_scan(session: AsyncSession, repo_id: int, tenant_id: int, user_id: int) -> None:
    """把扫描状态改成 SCANNING。版本不匹配时这条更新不会生效。"""
    repo = await _require_repo(session, repo_id)
    await session.execute(
        update(Repo)
        .where(
            Repo.id == repo_id,
            Repo.tenant_id == tenant_id,
            Repo.version == repo.version,
            Repo.is_deleted == 0,
        )
        .values(scan_status="SCANNING", version=Repo.version + 1, modifier_id=user_id)
    )
    await session.commit()


async def get_conclusion(session: AsyncSession, repo_id: int) -> RepoConclusionView | None:
    """没有结论时返回 null。"""
    await _require_repo(session, repo_id)
    conclusion = await _find_conclusion(session, repo_id)
    if conclusion is None:
        return None
    return _to_conclusion(conclusion)


async def update_conclusion(
    session: AsyncSession,
    repo_id: int,
    request: UpdateConclusionRequest,
    tenant_id: int,
    user_id: int,
) -> RepoConclusionView:
    """没有结论时插入，已有结论时整行替换。"""
    await _require_repo(session, repo_id)
    existing = await _find_conclusion(session, repo_id)
    if existing is None:
        conclusion = RepoConclusion(
            tenant_id=tenant_id,
            repo_id=repo_id,
            purpose=request.purpose,
            key_business=_json_value(request.key_business),
            upstreams=_json_value(request.upstreams),
            downstreams=_json_value(request.downstreams),
            summary_md=request.summary_md,
            creator_id=user_id,
            version=0,
            is_deleted=0,
        )
        session.add(conclusion)
        await session.flush()
        view = _to_conclusion(conclusion)
        view.gmt_create = None
        await session.commit()
        return view
    updated = rowcount(
        await session.execute(
            update(RepoConclusion)
            .where(
                RepoConclusion.id == existing.id,
                RepoConclusion.tenant_id == tenant_id,
                RepoConclusion.version == existing.version,
                RepoConclusion.is_deleted == 0,
            )
            .values(
                purpose=request.purpose,
                key_business=_json_value(request.key_business),
                upstreams=_json_value(request.upstreams),
                downstreams=_json_value(request.downstreams),
                summary_md=request.summary_md,
                version=RepoConclusion.version + 1,
                modifier_id=user_id,
            )
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.REPO_VERSION_CONFLICT)
    await session.commit()
    session.expire_all()
    stored = await _find_conclusion(session, repo_id)
    if stored is None:
        raise BizError(ErrorCode.REPO_CONCLUSION_NOT_FOUND)
    return _to_conclusion(stored)


async def list_relations(session: AsyncSession, tenant_id: int) -> list[RepoRelationView]:
    """当前工作空间的全部仓库关系。"""
    rows = await session.scalars(
        select(RepoRelation).where(
            RepoRelation.tenant_id == tenant_id,
            RepoRelation.is_deleted == 0,
        )
    )
    return [_to_relation(item) for item in rows]


async def list_relations_by_repo(
    session: AsyncSession,
    tenant_id: int,
    repo_id: int,
) -> list[RepoRelationView]:
    """某一仓库作为起点或终点的关系。"""
    rows = await session.scalars(
        select(RepoRelation).where(
            RepoRelation.tenant_id == tenant_id,
            RepoRelation.is_deleted == 0,
            or_(RepoRelation.from_repo_id == repo_id, RepoRelation.to_repo_id == repo_id),
        )
    )
    return [_to_relation(item) for item in rows]


async def create_relation(
    session: AsyncSession,
    request: CreateRelationRequest,
    tenant_id: int,
    user_id: int,
) -> RepoRelationView:
    """两端仓库必须属于当前工作空间。已删除的同一关系会恢复。"""
    if request.from_repo_id == request.to_repo_id:
        raise BizError(ErrorCode.REPO_RELATION_SELF_REF)
    await _require_repo_in_tenant(session, request.from_repo_id, tenant_id)
    await _require_repo_in_tenant(session, request.to_repo_id, tenant_id)
    existing = await session.scalar(
        select(RepoRelation)
        .where(
            RepoRelation.tenant_id == tenant_id,
            RepoRelation.from_repo_id == request.from_repo_id,
            RepoRelation.to_repo_id == request.to_repo_id,
            RepoRelation.relation_type == request.relation_type,
        )
        .limit(1)
    )
    if existing is not None:
        if existing.is_deleted == 0:
            raise BizError(ErrorCode.REPO_RELATION_DUPLICATE)
        await session.execute(
            update(RepoRelation)
            .where(
                RepoRelation.id == existing.id,
                RepoRelation.tenant_id == tenant_id,
                RepoRelation.is_deleted == 1,
            )
            .values(
                is_deleted=0,
                description=request.description,
                ai_session_id=request.ai_session_id,
                modifier_id=user_id,
            )
        )
        await session.commit()
        session.expire_all()
        restored = await _find_relation(session, existing.id)
        if restored is None:
            raise BizError(ErrorCode.REPO_RELATION_NOT_FOUND)
        return _to_relation(restored)
    relation = RepoRelation(
        tenant_id=tenant_id,
        from_repo_id=request.from_repo_id,
        to_repo_id=request.to_repo_id,
        relation_type=request.relation_type,
        description=request.description,
        ai_session_id=request.ai_session_id,
        creator_id=user_id,
        is_deleted=0,
    )
    session.add(relation)
    await session.flush()
    view = _to_relation(relation)
    view.gmt_create = None
    await session.commit()
    return view


async def delete_relation(session: AsyncSession, relation_id: int, tenant_id: int) -> None:
    """软删除一条关系。"""
    relation = await _find_relation(session, relation_id)
    if relation is None or relation.tenant_id != tenant_id:
        raise BizError(ErrorCode.REPO_RELATION_NOT_FOUND)
    await session.execute(
        update(RepoRelation)
        .where(
            RepoRelation.id == relation_id,
            RepoRelation.tenant_id == tenant_id,
            RepoRelation.is_deleted == 0,
        )
        .values(is_deleted=1)
    )
    await session.commit()


def _to_view(repo: Repo) -> RepoView:
    return RepoView(
        id=repo.id,
        name=repo.name,
        url=repo.url,
        default_branch=repo.default_branch,
        description=repo.description,
        scan_status=repo.scan_status,
        version=repo.version,
        gmt_create=repo.gmt_create,
    )


def _to_conclusion(conclusion: RepoConclusion) -> RepoConclusionView:
    return RepoConclusionView(
        id=conclusion.id,
        repo_id=conclusion.repo_id,
        purpose=conclusion.purpose,
        key_business=_json_text(conclusion.key_business),
        upstreams=_json_text(conclusion.upstreams),
        downstreams=_json_text(conclusion.downstreams),
        summary_md=conclusion.summary_md,
        ai_session_id=conclusion.ai_session_id,
        version=conclusion.version,
        gmt_create=conclusion.gmt_create,
    )


def _to_relation(relation: RepoRelation) -> RepoRelationView:
    return RepoRelationView(
        id=relation.id,
        from_repo_id=relation.from_repo_id,
        to_repo_id=relation.to_repo_id,
        relation_type=relation.relation_type,
        description=relation.description,
        ai_session_id=relation.ai_session_id,
        gmt_create=relation.gmt_create,
    )


def _json_value(raw: str | None) -> object | None:
    if raw is None:
        return None
    return json.loads(raw)


def _json_text(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _require_repo(session: AsyncSession, repo_id: int) -> Repo:
    repo = await session.scalar(
        select(Repo).where(Repo.id == repo_id, Repo.is_deleted == 0).limit(1)
    )
    if repo is None:
        raise BizError(ErrorCode.REPO_NOT_FOUND)
    return repo


async def _require_repo_in_tenant(
    session: AsyncSession, repo_id: int | None, tenant_id: int
) -> Repo:
    repo = await session.scalar(
        select(Repo).where(Repo.id == repo_id, Repo.is_deleted == 0).limit(1)
    )
    if repo is None or repo.tenant_id != tenant_id:
        raise BizError(ErrorCode.REPO_NOT_FOUND)
    return repo


async def _find_conclusion(session: AsyncSession, repo_id: int) -> RepoConclusion | None:
    return await session.scalar(
        select(RepoConclusion)
        .where(RepoConclusion.repo_id == repo_id, RepoConclusion.is_deleted == 0)
        .limit(1)
    )


async def _find_relation(session: AsyncSession, relation_id: int) -> RepoRelation | None:
    return await session.scalar(
        select(RepoRelation)
        .where(RepoRelation.id == relation_id, RepoRelation.is_deleted == 0)
        .limit(1)
    )


async def _list_active_refs(session: AsyncSession, repo_id: int, tenant_id: int) -> list[RepoUse]:
    ref_type = case(
        (Agent.online_version_id == AgentVersion.id, _ONLINE),
        else_="EDITING",
    ).label("ref_type")
    rows = await session.execute(
        select(Agent.id, Agent.name, AgentVersion.version_no, ref_type)
        .select_from(AgentRepoPerm)
        .join(AgentVersion, AgentVersion.id == AgentRepoPerm.agent_version_id)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .where(
            AgentRepoPerm.repo_id == repo_id,
            AgentRepoPerm.tenant_id == tenant_id,
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
            AgentVersion.is_deleted == 0,
            or_(
                Agent.online_version_id == AgentVersion.id,
                Agent.editing_version_id == AgentVersion.id,
            ),
        )
        .order_by(Agent.id.asc(), AgentVersion.version_no.asc())
    )
    return [
        RepoUse(
            agent_id=row.id,
            agent_name=row.name,
            version_no=row.version_no,
            ref_type=row.ref_type,
        )
        for row in rows
    ]
