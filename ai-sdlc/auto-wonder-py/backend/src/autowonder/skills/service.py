"""技能的创建、分页、更新和删除。分类只作为列表上的附加信息。"""

import logging
import re
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from autowonder.agents.models import Agent, AgentSkill, AgentVersion
from autowonder.categories.models import AssetCategoryRef
from autowonder.categories.service import (
    category_and_descendant_ids,
    map_category_ids,
    map_category_paths,
    remove_skill_category,
)
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.page import PageResult
from autowonder.db.rows import rowcount
from autowonder.skills.install_spec import (
    display_install_spec,
    normalize_install_spec,
    reject_packaged_capability,
)
from autowonder.skills.models import Skill
from autowonder.skills.schemas import CreateSkillRequest, SkillView, UpdateSkillRequest
from autowonder.users.models import User

logger = logging.getLogger(__name__)

_MISSING_CATEGORY_TABLE = re.compile(
    r"(?i)Table ['`]([^'`]*\.)?asset_category(?:_ref)?['`] doesn't exist"
)
_PAGE_CAP = 100


@dataclass(frozen=True)
class PackageReference:
    """已经上传到对象存储的技能包。"""

    oss_ref: str
    file_name: str | None
    size: int | None
    md5: str | None


def page_window(page: int, size: int) -> tuple[int, int]:
    """页码至少为 1，每页至少 1 条，最多 100 条。"""
    shown_page = page
    if shown_page < 1:
        shown_page = 1
    shown_size = size
    if shown_size < 1:
        shown_size = 1
    if shown_size > _PAGE_CAP:
        shown_size = _PAGE_CAP
    return shown_page, shown_size


async def create_skill(
    session: AsyncSession,
    request: CreateSkillRequest,
    tenant_id: int,
    user_id: int,
) -> SkillView:
    """按安装规格创建技能，并释放同名软删行占用的名称。"""
    if request.name is None or request.name.strip() == "":
        raise BizError(ErrorCode.SKILL_NAME_REQUIRED)
    if request.type is None or request.type.strip() == "":
        raise BizError(ErrorCode.SKILL_TYPE_REQUIRED)
    reject_packaged_capability(request.type)
    name = request.name.strip()
    if await _find_live_name(session, tenant_id, request.type, name) is not None:
        raise BizError(ErrorCode.SKILL_DUPLICATE_NAME)
    await _release_soft_deleted_name(session, tenant_id, request.type, name)
    skill = Skill(
        tenant_id=tenant_id,
        type=request.type,
        name=name,
        install_spec=normalize_install_spec(request.type, request.install_spec, None),
        description=request.description,
        source_type="INSTALL_SPEC",
        creator_id=user_id,
        version=0,
        is_deleted=0,
        gmt_create=now_local(),
        gmt_modified=now_local(),
    )
    session.add(skill)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        if _duplicate_key(error):
            raise BizError(ErrorCode.SKILL_DUPLICATE_NAME) from error
        raise
    await session.commit()
    return await _to_view(session, skill, {})


async def get_skill(session: AsyncSession, skill_id: int) -> SkillView:
    """按 id 读取未删除技能，并补上分类。"""
    skill = await _find_live(session, skill_id)
    if skill is None:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    view = await _to_view(session, skill, {})
    await _enrich_category(session, [view], skill.tenant_id)
    return view


async def list_skills(
    session: AsyncSession,
    tenant_id: int,
    skill_type: str | None,
    category_id: int | None,
    include_descendants: bool,
    uncategorized: bool,
    page: int,
    size: int,
) -> PageResult:
    """分页列出技能。分类筛选和未分类筛选不能同时使用。"""
    shown_page, shown_size = page_window(page, size)
    category_ids = await _resolve_category_filter(
        session,
        tenant_id,
        category_id,
        include_descendants,
        uncategorized,
    )
    rows = list(
        await session.scalars(
            _skill_query(tenant_id, skill_type, category_ids, uncategorized)
            .order_by(Skill.id.desc())
            .offset((shown_page - 1) * shown_size)
            .limit(shown_size)
        )
    )
    names: dict[int, str] = {}
    views = [await _to_view(session, row, names) for row in rows]
    await _enrich_category(session, views, tenant_id)
    total = await session.scalar(_count_query(tenant_id, skill_type, category_ids, uncategorized))
    counted = 0
    if total is not None:
        counted = int(total)
    return PageResult.model_validate(
        {
            "list": views,
            "total": counted,
            "pageNum": shown_page,
            "pageSize": shown_size,
        }
    )


async def update_skill(
    session: AsyncSession,
    skill_id: int,
    request: UpdateSkillRequest,
    tenant_id: int,
    user_id: int,
) -> SkillView:
    """按版本号更新。传入的 null 字段保留当前值。"""
    skill = await _require_skill(session, skill_id, tenant_id)
    skill_type = skill.type
    if request.type is not None:
        skill_type = request.type
    reject_packaged_capability(skill_type)
    name = skill.name
    if request.name is not None:
        name = request.name.strip()
    install_spec = skill.install_spec
    if request.install_spec is not None:
        install_spec = normalize_install_spec(skill_type, request.install_spec, skill.install_spec)
    description = skill.description
    if request.description is not None:
        description = request.description
    result = await session.execute(
        update(Skill)
        .where(
            Skill.id == skill_id,
            Skill.tenant_id == tenant_id,
            Skill.version == skill.version,
            Skill.is_deleted == 0,
        )
        .values(
            name=name,
            type=skill_type,
            install_spec=install_spec,
            description=description,
            version=Skill.version + 1,
            modifier_id=user_id,
            gmt_modified=now_local(),
        )
    )
    if rowcount(result) == 0:
        raise BizError(ErrorCode.SKILL_VERSION_CONFLICT)
    await session.commit()
    session.expire_all()
    return await get_skill(session, skill_id)


async def delete_skill(session: AsyncSession, skill_id: int, tenant_id: int, user_id: int) -> None:
    """软删除并释放名称。仍被数字员工版本引用时拒绝。"""
    skill = await session.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.is_deleted == 0).limit(1).with_for_update()
    )
    if skill is None or skill.tenant_id != tenant_id:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    if await _reference_count(session, skill_id, tenant_id) > 0:
        raise BizError(ErrorCode.SKILL_DELETE_IN_USE)
    result = await session.execute(
        update(Skill)
        .where(
            Skill.id == skill_id,
            Skill.tenant_id == tenant_id,
            Skill.version == skill.version,
            Skill.is_deleted == 0,
        )
        .values(
            is_deleted=1,
            name=func.concat("#deleted-", Skill.id),
            version=Skill.version + 1,
            modifier_id=user_id,
            gmt_modified=now_local(),
        )
    )
    if rowcount(result) == 0:
        raise BizError(ErrorCode.SKILL_VERSION_CONFLICT)
    await remove_skill_category(session, skill_id, tenant_id)
    await session.commit()


async def create_from_package_reference(
    session: AsyncSession,
    request: CreateSkillRequest,
    package: PackageReference | None,
    tenant_id: int,
    user_id: int,
) -> SkillView:
    """用已经上传的技能包创建技能。"""
    stored = _require_package(package)
    if request.name is None or request.name.strip() == "":
        raise BizError(ErrorCode.SKILL_NAME_REQUIRED)
    if request.type is None or request.type.strip() == "":
        raise BizError(ErrorCode.SKILL_TYPE_REQUIRED)
    reject_packaged_capability(request.type)
    name = request.name.strip()
    if await _find_live_name(session, tenant_id, request.type, name) is not None:
        raise BizError(ErrorCode.SKILL_DUPLICATE_NAME)
    await _release_soft_deleted_name(session, tenant_id, request.type, name)
    skill = Skill(
        tenant_id=tenant_id,
        type=request.type,
        name=name,
        install_spec=normalize_install_spec(request.type, request.install_spec, None),
        description=request.description,
        source_type="OSS_ZIP",
        package_oss_ref=stored.oss_ref,
        package_file_name=stored.file_name,
        package_size=stored.size,
        package_md5=stored.md5,
        creator_id=user_id,
        version=0,
        is_deleted=0,
        gmt_create=now_local(),
        gmt_modified=now_local(),
    )
    session.add(skill)
    await session.flush()
    await session.commit()
    return await _to_view(session, skill, {})


async def update_from_package_reference(
    session: AsyncSession,
    skill_id: int,
    request: UpdateSkillRequest,
    package: PackageReference | None,
    tenant_id: int,
    user_id: int,
) -> SkillView:
    """替换技能包，并按版本号更新。"""
    stored = _require_package(package)
    current = await _find_live(session, skill_id)
    if current is None or current.tenant_id != tenant_id:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    skill_type = current.type
    if request.type is not None:
        skill_type = request.type
    reject_packaged_capability(skill_type)
    install_spec = current.install_spec
    if request.install_spec is not None:
        install_spec = normalize_install_spec(
            skill_type,
            request.install_spec,
            current.install_spec,
        )
    name = current.name
    if request.name is not None:
        name = request.name.strip()
    description = current.description
    if request.description is not None:
        description = request.description
    result = await session.execute(
        update(Skill)
        .where(
            Skill.id == skill_id,
            Skill.tenant_id == tenant_id,
            Skill.version == current.version,
            Skill.is_deleted == 0,
        )
        .values(
            name=name,
            type=skill_type,
            description=description,
            source_type="OSS_ZIP",
            package_oss_ref=stored.oss_ref,
            package_file_name=stored.file_name,
            package_size=stored.size,
            package_md5=stored.md5,
            install_spec=install_spec,
            version=Skill.version + 1,
            modifier_id=user_id,
            gmt_modified=now_local(),
        )
    )
    if rowcount(result) == 0:
        raise BizError(ErrorCode.SKILL_VERSION_CONFLICT)
    await session.commit()
    session.expire_all()
    return await get_skill(session, skill_id)


def _require_package(package: PackageReference | None) -> PackageReference:
    if package is None or package.oss_ref.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID)
    return package


async def _resolve_category_filter(
    session: AsyncSession,
    tenant_id: int,
    category_id: int | None,
    include_descendants: bool,
    uncategorized: bool,
) -> list[int] | None:
    if category_id is not None and uncategorized:
        raise BizError(ErrorCode.PARAM_INVALID, "分类筛选与未分类筛选互斥，不能同时使用")
    if category_id is None:
        return None
    if category_id <= 0:
        raise BizError(ErrorCode.PARAM_INVALID, "分类 ID 不合法")
    if include_descendants:
        return await category_and_descendant_ids(session, tenant_id, category_id)
    return [category_id]


def _skill_query(
    tenant_id: int,
    skill_type: str | None,
    category_ids: list[int] | None,
    uncategorized: bool,
) -> Select[tuple[Skill]]:
    statement = select(Skill).where(Skill.is_deleted == 0, Skill.tenant_id == tenant_id)
    if skill_type is not None:
        statement = statement.where(Skill.type == skill_type)
    if category_ids is not None:
        statement = statement.where(_in_categories(tenant_id, category_ids))
    if uncategorized:
        statement = statement.where(~_any_category(tenant_id))
    return statement


def _count_query(
    tenant_id: int,
    skill_type: str | None,
    category_ids: list[int] | None,
    uncategorized: bool,
) -> Select[tuple[int]]:
    statement = (
        select(func.count())
        .select_from(Skill)
        .where(Skill.is_deleted == 0, Skill.tenant_id == tenant_id)
    )
    if skill_type is not None:
        statement = statement.where(Skill.type == skill_type)
    if category_ids is not None:
        statement = statement.where(_in_categories(tenant_id, category_ids))
    if uncategorized:
        statement = statement.where(~_any_category(tenant_id))
    return statement


def _in_categories(tenant_id: int, category_ids: list[int]) -> Exists:
    return (
        select(AssetCategoryRef.id)
        .where(
            AssetCategoryRef.tenant_id == tenant_id,
            AssetCategoryRef.asset_type == "SKILL",
            AssetCategoryRef.asset_id == Skill.id,
            AssetCategoryRef.is_deleted == 0,
            AssetCategoryRef.category_id.in_(category_ids),
        )
        .exists()
    )


def _any_category(tenant_id: int) -> Exists:
    return (
        select(AssetCategoryRef.id)
        .where(
            AssetCategoryRef.tenant_id == tenant_id,
            AssetCategoryRef.asset_type == "SKILL",
            AssetCategoryRef.asset_id == Skill.id,
            AssetCategoryRef.is_deleted == 0,
        )
        .exists()
    )


async def _enrich_category(
    session: AsyncSession,
    views: list[SkillView],
    tenant_id: int,
) -> None:
    if len(views) == 0:
        return
    skill_ids = [view.id for view in views if view.id is not None]
    try:
        category_ids = await map_category_ids(session, tenant_id, skill_ids)
    except OperationalError as error:
        if _missing_category_table(error):
            logger.warning(
                "Category tables unavailable; returning skills without category metadata,"
                " tenantId=%s",
                tenant_id,
            )
            return
        raise
    if len(category_ids) == 0:
        return
    paths = await map_category_paths(session, tenant_id)
    for view in views:
        if view.id is None:
            continue
        category_id = category_ids.get(view.id)
        if category_id is not None:
            view.category_id = category_id
            view.category_path = paths.get(category_id)


async def _to_view(
    session: AsyncSession,
    skill: Skill,
    names: dict[int, str],
) -> SkillView:
    modifier_id = skill.creator_id
    if skill.modifier_id is not None:
        modifier_id = skill.modifier_id
    source_type = "INSTALL_SPEC"
    if skill.source_type is not None and skill.source_type != "":
        source_type = skill.source_type
    return SkillView(
        id=skill.id,
        type=skill.type,
        name=skill.name,
        install_spec=display_install_spec(skill.install_spec),
        description=skill.description,
        source_type=source_type,
        package_oss_ref=skill.package_oss_ref,
        package_file_name=skill.package_file_name,
        package_size=skill.package_size,
        package_md5=skill.package_md5,
        version=skill.version,
        gmt_create=skill.gmt_create,
        gmt_modified=skill.gmt_modified,
        modifier_id=modifier_id,
        modifier_name=await _display_user_name(session, modifier_id, names),
        category_id=None,
        category_path=None,
    )


async def _display_user_name(
    session: AsyncSession,
    user_id: int | None,
    names: dict[int, str],
) -> str | None:
    if user_id is None:
        return None
    if user_id in names:
        return names[user_id]
    user = await session.scalar(
        select(User).where(User.id == user_id, User.is_deleted == 0).limit(1)
    )
    display_name = "用户 #" + str(user_id)
    if user is not None and user.nickname is not None and user.nickname.strip() != "":
        display_name = user.nickname
    elif user is not None and user.username is not None and user.username.strip() != "":
        display_name = user.username
    names[user_id] = display_name
    return display_name


async def _reference_count(session: AsyncSession, skill_id: int, tenant_id: int) -> int:
    counted = await session.scalar(
        select(func.count())
        .select_from(AgentSkill)
        .join(AgentVersion, AgentSkill.agent_version_id == AgentVersion.id)
        .join(
            Agent,
            (AgentVersion.agent_id == Agent.id) & (AgentVersion.tenant_id == Agent.tenant_id),
        )
        .where(
            AgentSkill.skill_id == skill_id,
            AgentSkill.tenant_id == tenant_id,
            or_(
                AgentVersion.id == Agent.online_version_id,
                AgentVersion.id == Agent.editing_version_id,
            ),
        )
    )
    if counted is None:
        return 0
    return int(counted)


async def _find_live(session: AsyncSession, skill_id: int) -> Skill | None:
    return await session.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.is_deleted == 0).limit(1)
    )


async def _require_skill(session: AsyncSession, skill_id: int, tenant_id: int) -> Skill:
    skill = await _find_live(session, skill_id)
    if skill is None or skill.tenant_id != tenant_id:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    return skill


async def _find_live_name(
    session: AsyncSession,
    tenant_id: int,
    skill_type: str,
    name: str,
) -> Skill | None:
    return await session.scalar(
        select(Skill)
        .where(
            Skill.tenant_id == tenant_id,
            Skill.type == skill_type,
            Skill.name == name,
            Skill.is_deleted == 0,
        )
        .limit(1)
    )


async def _release_soft_deleted_name(
    session: AsyncSession,
    tenant_id: int,
    skill_type: str,
    name: str,
) -> None:
    await session.execute(
        update(Skill)
        .where(
            Skill.tenant_id == tenant_id,
            Skill.type == skill_type,
            Skill.name == name,
            Skill.is_deleted == 1,
        )
        .values(name=func.concat("#deleted-", Skill.id))
    )


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None or not origin.args:
        return False
    return origin.args[0] == 1062


def _missing_category_table(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        args = getattr(current, "args", ())
        if args and args[0] == 1146 and _MISSING_CATEGORY_TABLE.search(str(current)) is not None:
            return True
        current = current.__cause__
    return False
