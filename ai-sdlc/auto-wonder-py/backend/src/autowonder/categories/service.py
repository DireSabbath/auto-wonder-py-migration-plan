"""项目分类树。层级、同级重名、移动成环和能力打标对齐 CategoryService。"""

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.audits.service import AuditRecord, record_required
from autowonder.categories.models import AssetCategory, AssetCategoryRef
from autowonder.categories.schemas import (
    CategoryView,
    CreateCategoryFields,
    SkillCategoryResult,
    UpdateCategoryFields,
)
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.skills.models import Skill

MAX_CATEGORY_DEPTH = 5
ASSET_TYPE_SKILL = "SKILL"
_PATH_SEPARATOR = " → "


def require_name(raw: str | None) -> str:
    """名称必填，去掉空白后最长 128 个 Java 字符。"""
    if raw is None or raw.strip() == "":
        raise BizError(ErrorCode.CATEGORY_NAME_REQUIRED)
    name = raw.strip()
    if _java_length(name) > 128:
        raise BizError(ErrorCode.PARAM_INVALID, "分类名称最长 128 个字符")
    return name


def require_parent(parent_id: int | None) -> int | None:
    """空上级表示顶级。非正 id 不合法。"""
    if parent_id is None:
        return None
    if parent_id <= 0:
        raise BizError(ErrorCode.PARAM_INVALID, "上级分类不合法")
    return parent_id


def trim_to_null(value: str | None) -> str | None:
    """空白说明写成 null。"""
    if value is None:
        return None
    trimmed = value.strip()
    if trimmed == "":
        return None
    return trimmed


def normalized_name(raw: str | None) -> str:
    """判断名称或上级是否变化时忽略大小写和两端空白。"""
    if raw is None:
        return ""
    return raw.strip().lower()


def sibling_conflicts(found: bool, sibling_id: int | None, exclude_id: int | None) -> bool:
    """查到的同级如果不是当前节点自己，就是重名。"""
    if not found:
        return False
    if exclude_id is None or sibling_id is None or sibling_id != exclude_id:
        return True
    return False


def path_of(node: AssetCategory, tree: dict[int, AssetCategory]) -> str:
    """从根到当前节点的名称，用箭头连接。"""
    segments: list[str] = []
    current: AssetCategory | None = node
    while current is not None:
        segments.append(current.name)
        parent_id = current.parent_id
        if parent_id is None:
            current = None
        else:
            current = tree.get(parent_id)
    segments.reverse()
    return _PATH_SEPARATOR.join(segments)


def subtree_height(category_id: int, tree: dict[int, AssetCategory]) -> int:
    """节点自身算 1 层，再加上最深子树。"""
    max_child = 0
    for node in tree.values():
        if node.parent_id == category_id:
            child_height = subtree_height(node.id, tree)
            if child_height > max_child:
                max_child = child_height
    return max_child + 1


def collect_descendants(
    tree: dict[int, AssetCategory],
    parent_id: int,
    ids: list[int],
) -> None:
    """按树的遍历顺序收集后代。"""
    for node in tree.values():
        if node.parent_id == parent_id and node.id not in ids:
            ids.append(node.id)
            collect_descendants(tree, node.id, ids)


async def list_categories(session: AsyncSession, tenant_id: int) -> list[CategoryView]:
    """按名称和 id 列出全树，并带上完整路径。"""
    tree = await _load_tree(session, tenant_id)
    return [_to_view(node, tree) for node in tree.values()]


async def get_category(session: AsyncSession, category_id: int, tenant_id: int) -> CategoryView:
    """读取一个分类。不在当前工作空间时视为不存在。"""
    tree = await _load_tree(session, tenant_id)
    node = tree.get(category_id)
    if node is None:
        raise BizError(ErrorCode.CATEGORY_NOT_FOUND)
    return _to_view(node, tree)


async def create_category(
    session: AsyncSession,
    fields: CreateCategoryFields,
    tenant_id: int,
    user_id: int,
) -> CategoryView:
    """新建分类。有上级时先锁上级链，避免并发把树加到第 6 层。"""
    name = require_name(fields.name)
    parent_id = require_parent(fields.parent_id)
    if parent_id is not None:
        parent_depth = await _lock_ancestor_chain(session, None, parent_id, tenant_id)
        if parent_depth + 1 > MAX_CATEGORY_DEPTH:
            raise BizError(
                ErrorCode.CATEGORY_DEPTH_EXCEEDED,
                "分类层级不能超过 " + str(MAX_CATEGORY_DEPTH) + " 层",
            )
    await _require_sibling_available(session, tenant_id, parent_id, name, None)
    node = AssetCategory(
        tenant_id=tenant_id,
        parent_id=parent_id,
        name=name,
        description=trim_to_null(fields.description),
        creator_id=user_id,
        is_deleted=0,
        version=0,
    )
    session.add(node)
    await session.flush()
    view = await get_category(session, node.id, tenant_id)
    await session.commit()
    return view


async def update_category(
    session: AsyncSession,
    category_id: int,
    fields: UpdateCategoryFields,
    tenant_id: int,
    user_id: int,
) -> CategoryView:
    """按出现的字段改名称、上级和说明。移动时检查环和深度。"""
    current = await _lock_category(session, category_id, tenant_id)
    if current is None:
        raise BizError(ErrorCode.CATEGORY_NOT_FOUND)
    name = current.name
    if fields.name_present:
        name = require_name(fields.name)
    parent_id = current.parent_id
    if fields.parent_id_present and fields.parent_id != current.parent_id:
        parent_id = require_parent(fields.parent_id)
        await _require_move_allowed(session, category_id, parent_id, tenant_id)
    if normalized_name(name) != normalized_name(current.name) or parent_id != current.parent_id:
        await _require_sibling_available(session, tenant_id, parent_id, name, category_id)
    description = current.description
    if fields.description_present:
        description = trim_to_null(fields.description)
    updated = rowcount(
        await session.execute(
            update(AssetCategory)
            .where(
                AssetCategory.id == category_id,
                AssetCategory.tenant_id == tenant_id,
                AssetCategory.version == current.version,
                AssetCategory.is_deleted == 0,
            )
            .values(
                parent_id=parent_id,
                name=name,
                description=description,
                version=AssetCategory.version + 1,
                modifier_id=user_id,
            )
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.PARAM_INVALID, "分类已被修改，请刷新后重试")
    session.expire_all()
    view = await get_category(session, category_id, tenant_id)
    await session.commit()
    return view


async def delete_category(
    session: AsyncSession,
    category_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """空节点物理删除。有子分类或能力打标时拒绝。"""
    node = await _lock_category(session, category_id, tenant_id)
    if node is None:
        raise BizError(ErrorCode.CATEGORY_NOT_FOUND)
    children = await _child_ids_for_update(session, tenant_id, category_id)
    if len(children) > 0:
        raise BizError(ErrorCode.CATEGORY_DELETE_IN_USE, "该分类包含子分类，请先迁移子分类")
    refs = await _ref_ids_for_update(session, tenant_id, category_id)
    if len(refs) > 0:
        raise BizError(
            ErrorCode.CATEGORY_DELETE_IN_USE,
            "该分类仍被能力引用，请先迁移或取消能力打标",
        )
    tree = await _load_tree(session, tenant_id)
    await session.execute(
        sql_delete(AssetCategory).where(
            AssetCategory.id == category_id,
            AssetCategory.tenant_id == tenant_id,
        )
    )
    await _audit_delete(session, tenant_id, user_id, category_id, path_of(node, tree))
    await session.commit()


async def set_skill_category(
    session: AsyncSession,
    skill_id: int,
    category_id: int | None,
    tenant_id: int,
    user_id: int,
) -> int | None:
    """设置或取消能力主分类。"""
    result = await _set_skill_category(session, skill_id, category_id, tenant_id, user_id)
    await session.commit()
    return result


async def batch_set_skill_category(
    session: AsyncSession,
    skill_ids: list[int] | None,
    category_id: int | None,
    tenant_id: int,
    user_id: int,
) -> list[SkillCategoryResult]:
    """逐项打标。单项业务失败记入结果，不中断整批。"""
    results: list[SkillCategoryResult] = []
    seen: list[int] = []
    if skill_ids is not None:
        for skill_id in skill_ids:
            if skill_id not in seen:
                seen.append(skill_id)
    for skill_id in seen:
        item = SkillCategoryResult(skill_id=skill_id)
        try:
            await _set_skill_category(session, skill_id, category_id, tenant_id, user_id)
        except BizError as error:
            item.success = False
            item.message = str(error)
        else:
            item.success = True
            if category_id is None:
                item.message = "已取消打标"
            else:
                item.message = "已设置分类"
        results.append(item)
    await session.commit()
    return results


async def remove_skill_category(session: AsyncSession, skill_id: int, tenant_id: int) -> None:
    """能力删除事务里清掉分类关联，由调用方提交。"""
    await session.execute(
        sql_delete(AssetCategoryRef).where(
            AssetCategoryRef.tenant_id == tenant_id,
            AssetCategoryRef.asset_type == ASSET_TYPE_SKILL,
            AssetCategoryRef.asset_id == skill_id,
        )
    )


async def map_category_ids(
    session: AsyncSession,
    tenant_id: int,
    asset_ids: list[int] | None,
) -> dict[int, int]:
    """能力 id 到主分类 id。空列表不查库。"""
    mapping: dict[int, int] = {}
    if asset_ids is None or len(asset_ids) == 0:
        return mapping
    rows = await session.scalars(
        select(AssetCategoryRef).where(
            AssetCategoryRef.tenant_id == tenant_id,
            AssetCategoryRef.asset_type == ASSET_TYPE_SKILL,
            AssetCategoryRef.asset_id.in_(asset_ids),
            AssetCategoryRef.is_deleted == 0,
        )
    )
    for ref in rows:
        mapping[ref.asset_id] = ref.category_id
    return mapping


async def map_category_paths(session: AsyncSession, tenant_id: int) -> dict[int, str]:
    """分类 id 到完整路径。"""
    tree = await _load_tree(session, tenant_id)
    paths: dict[int, str] = {}
    for node in tree.values():
        paths[node.id] = path_of(node, tree)
    return paths


async def category_and_descendant_ids(
    session: AsyncSession,
    tenant_id: int,
    category_id: int,
) -> list[int]:
    """分类自身和全部后代，供能力列表按子树筛选。"""
    tree = await _load_tree(session, tenant_id)
    if tree.get(category_id) is None:
        raise BizError(ErrorCode.CATEGORY_NOT_FOUND)
    ids = [category_id]
    collect_descendants(tree, category_id, ids)
    return ids


async def _set_skill_category(
    session: AsyncSession,
    skill_id: int,
    category_id: int | None,
    tenant_id: int,
    user_id: int,
) -> int | None:
    skill = await session.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.is_deleted == 0).limit(1).with_for_update()
    )
    if skill is None or skill.tenant_id != tenant_id:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    before = await session.scalar(
        select(AssetCategoryRef)
        .where(
            AssetCategoryRef.tenant_id == tenant_id,
            AssetCategoryRef.asset_type == ASSET_TYPE_SKILL,
            AssetCategoryRef.asset_id == skill_id,
            AssetCategoryRef.is_deleted == 0,
        )
        .limit(1)
    )
    before_id = None
    if before is not None:
        before_id = before.category_id
    if category_id is None:
        await session.execute(
            sql_delete(AssetCategoryRef).where(
                AssetCategoryRef.tenant_id == tenant_id,
                AssetCategoryRef.asset_type == ASSET_TYPE_SKILL,
                AssetCategoryRef.asset_id == skill_id,
            )
        )
    else:
        locked = await _lock_category(session, category_id, tenant_id)
        if locked is None:
            raise BizError(ErrorCode.CATEGORY_NOT_FOUND)
        statement = mysql_insert(AssetCategoryRef).values(
            tenant_id=tenant_id,
            asset_type=ASSET_TYPE_SKILL,
            asset_id=skill_id,
            category_id=category_id,
            creator_id=user_id,
            is_deleted=0,
        )
        statement = statement.on_duplicate_key_update(
            category_id=category_id,
            modifier_id=user_id,
            is_deleted=0,
            gmt_modified=text("CURRENT_TIMESTAMP(3)"),
        )
        await session.execute(statement)
    await _audit_skill_category(session, tenant_id, user_id, skill_id, before_id, category_id)
    return category_id


async def _require_move_allowed(
    session: AsyncSession,
    category_id: int,
    new_parent_id: int | None,
    tenant_id: int,
) -> None:
    if new_parent_id is None:
        return
    if new_parent_id == category_id:
        raise BizError(ErrorCode.CATEGORY_CYCLE_MOVE)
    parent_depth = await _lock_ancestor_chain(session, category_id, new_parent_id, tenant_id)
    height = subtree_height(category_id, await _load_tree(session, tenant_id))
    if parent_depth + height > MAX_CATEGORY_DEPTH:
        raise BizError(
            ErrorCode.CATEGORY_DEPTH_EXCEEDED,
            "移动后的分类层级不能超过 " + str(MAX_CATEGORY_DEPTH) + " 层",
        )


async def _lock_ancestor_chain(
    session: AsyncSession,
    forbidden_id: int | None,
    start_id: int,
    tenant_id: int,
) -> int:
    node = await _lock_category(session, start_id, tenant_id)
    if node is None:
        raise BizError(ErrorCode.CATEGORY_NOT_FOUND, "上级分类不存在")
    depth = 0
    while node is not None:
        depth = depth + 1
        if forbidden_id is not None and node.id == forbidden_id:
            raise BizError(ErrorCode.CATEGORY_CYCLE_MOVE)
        parent_id = node.parent_id
        if parent_id is None:
            node = None
        else:
            node = await _lock_category(session, parent_id, tenant_id)
    return depth


async def _require_sibling_available(
    session: AsyncSession,
    tenant_id: int,
    parent_id: int | None,
    name: str,
    exclude_id: int | None,
) -> None:
    sibling = await _lock_sibling(session, tenant_id, parent_id, name)
    found = sibling is not None
    sibling_id = None
    if sibling is not None:
        sibling_id = sibling.id
    if sibling_conflicts(found, sibling_id, exclude_id):
        raise BizError(ErrorCode.CATEGORY_DUPLICATE_NAME)


async def _load_tree(session: AsyncSession, tenant_id: int) -> dict[int, AssetCategory]:
    rows = await session.scalars(
        select(AssetCategory)
        .where(AssetCategory.tenant_id == tenant_id, AssetCategory.is_deleted == 0)
        .order_by(AssetCategory.name.asc(), AssetCategory.id.asc())
    )
    tree: dict[int, AssetCategory] = {}
    for node in rows:
        tree[node.id] = node
    return tree


async def _lock_category(
    session: AsyncSession,
    category_id: int,
    tenant_id: int,
) -> AssetCategory | None:
    return await session.scalar(
        select(AssetCategory)
        .where(
            AssetCategory.id == category_id,
            AssetCategory.tenant_id == tenant_id,
            AssetCategory.is_deleted == 0,
        )
        .limit(1)
        .with_for_update()
    )


async def _lock_sibling(
    session: AsyncSession,
    tenant_id: int,
    parent_id: int | None,
    name: str,
) -> AssetCategory | None:
    statement = select(AssetCategory).where(
        AssetCategory.tenant_id == tenant_id,
        AssetCategory.is_deleted == 0,
        AssetCategory.name == name,
    )
    if parent_id is None:
        statement = statement.where(AssetCategory.parent_id.is_(None))
    else:
        statement = statement.where(AssetCategory.parent_id == parent_id)
    return await session.scalar(statement.limit(1).with_for_update())


async def _child_ids_for_update(
    session: AsyncSession,
    tenant_id: int,
    parent_id: int,
) -> list[int]:
    rows = await session.scalars(
        select(AssetCategory.id)
        .where(
            AssetCategory.tenant_id == tenant_id,
            AssetCategory.parent_id == parent_id,
            AssetCategory.is_deleted == 0,
        )
        .with_for_update()
    )
    return list(rows)


async def _ref_ids_for_update(
    session: AsyncSession,
    tenant_id: int,
    category_id: int,
) -> list[int]:
    rows = await session.scalars(
        select(AssetCategoryRef.id)
        .where(
            AssetCategoryRef.tenant_id == tenant_id,
            AssetCategoryRef.category_id == category_id,
            AssetCategoryRef.is_deleted == 0,
        )
        .with_for_update()
    )
    return list(rows)


def _to_view(node: AssetCategory, tree: dict[int, AssetCategory]) -> CategoryView:
    return CategoryView(
        id=node.id,
        parent_id=node.parent_id,
        name=node.name,
        description=node.description,
        path=path_of(node, tree),
        version=node.version,
        gmt_create=node.gmt_create,
        gmt_modified=node.gmt_modified,
    )


async def _audit_delete(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    category_id: int,
    path: str,
) -> None:
    record = AuditRecord(
        tenant_id=tenant_id,
        actor_id=user_id,
        actor_type="HUMAN",
        module="category",
        action="category.delete",
        target_type="asset_category",
        target_id=category_id,
        trigger_type="API",
        event_type="category.delete",
    )
    record.add("path", path)
    await record_required(session, record)


async def _audit_skill_category(
    session: AsyncSession,
    tenant_id: int,
    user_id: int,
    skill_id: int,
    before_id: int | None,
    after_id: int | None,
) -> None:
    record = AuditRecord(
        tenant_id=tenant_id,
        actor_id=user_id,
        actor_type="HUMAN",
        module="category",
        action="skill.set_category",
        target_type="skill",
        target_id=skill_id,
        trigger_type="API",
        event_type="skill.set_category",
    )
    record.add("categoryBefore", before_id)
    record.add("categoryAfter", after_id)
    await record_required(session, record)


def _java_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2
