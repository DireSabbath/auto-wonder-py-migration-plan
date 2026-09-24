"""演进资产清单只放卡片和效用后验，不带回记忆正文或技能安装规格。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.evolution.jsontext import blank, java_trim
from autowonder.evolution.store import find_latest_evidence
from autowonder.memories.service import list_memories
from autowonder.repos.service import list_relations
from autowonder.skills.service import list_skills

_MEMORY = "MEMORY"
_SKILL = "SKILL"
_REPO_RELATION = "REPO_RELATION"
_UTILITY = "UTILITY"
_HINT_LIMIT = 160


async def manifest(
    session: AsyncSession,
    tenant_id: int,
    asset_type: str | None,
    context_key: str | None,
    limit: int | None,
) -> dict[str, object]:
    """缺省每种资产 20 条，最多 50 条。资产类型为空时三种都返回。"""
    bounded = _bound_limit(limit)
    requested = _normalize(asset_type)
    cards: list[dict[str, object]] = []
    if _includes(requested, _MEMORY):
        await _add_memories(session, cards, tenant_id, context_key, bounded)
    if _includes(requested, _SKILL):
        await _add_skills(session, cards, tenant_id, context_key, bounded)
    if _includes(requested, _REPO_RELATION):
        await _add_relations(session, cards, tenant_id, context_key, bounded)
    return {"contextKey": context_key, "limit": bounded, "cards": cards}


async def _add_memories(
    session: AsyncSession,
    cards: list[dict[str, object]],
    tenant_id: int,
    context_key: str | None,
    limit: int,
) -> None:
    memories = await list_memories(
        session,
        tenant_id,
        None,
        None,
        None,
        "ADOPTED",
        None,
        None,
        1,
        limit,
    )
    for memory in memories:
        card = _card(
            _MEMORY,
            memory.id,
            memory.title,
            _compact_category(memory.scope, memory.type),
            memory.title,
            "/api/memories/" + _java_text(memory.id),
            memory.version,
        )
        await _attach_posterior(session, card, tenant_id, context_key)
        cards.append(card)


async def _add_skills(
    session: AsyncSession,
    cards: list[dict[str, object]],
    tenant_id: int,
    context_key: str | None,
    limit: int,
) -> None:
    page = await list_skills(session, tenant_id, None, None, False, False, 1, limit)
    for skill in page.list_:
        card = _card(
            _SKILL,
            skill.id,
            skill.name,
            skill.type,
            _truncate(skill.description, _HINT_LIMIT),
            "/api/skills/" + _java_text(skill.id),
            skill.version,
        )
        await _attach_posterior(session, card, tenant_id, context_key)
        cards.append(card)


async def _add_relations(
    session: AsyncSession,
    cards: list[dict[str, object]],
    tenant_id: int,
    context_key: str | None,
    limit: int,
) -> None:
    count = 0
    for relation in await list_relations(session, tenant_id):
        if count >= limit:
            break
        name = (
            _java_text(relation.from_repo_id)
            + " "
            + _java_text(relation.relation_type)
            + " "
            + _java_text(relation.to_repo_id)
        )
        card = _card(
            _REPO_RELATION,
            relation.id,
            name,
            relation.relation_type,
            _truncate(relation.description, _HINT_LIMIT),
            "/api/repos/relations?repoId=" + _java_text(relation.from_repo_id),
            None,
        )
        await _attach_posterior(session, card, tenant_id, context_key)
        cards.append(card)
        count += 1


async def _attach_posterior(
    session: AsyncSession,
    card: dict[str, object],
    tenant_id: int,
    context_key: str | None,
) -> None:
    asset_id = card["assetId"]
    asset_type = card["assetType"]
    if not isinstance(asset_id, int) or context_key is None or blank(context_key):
        return
    if not isinstance(asset_type, str):
        return
    latest = await find_latest_evidence(
        session,
        tenant_id,
        asset_type,
        asset_id,
        _UTILITY,
        context_key,
    )
    if latest is None:
        return
    card["posteriorMean"] = latest.posterior_mean
    card["effectiveSampleSize"] = latest.effective_sample_size


def _card(
    asset_type: str,
    asset_id: int | None,
    name: str | None,
    category: str | None,
    trigger_hint: str | None,
    lazy_load_ref: str,
    version: int | None,
) -> dict[str, object]:
    return {
        "assetType": asset_type,
        "assetId": asset_id,
        "name": name,
        "category": category,
        "triggerHint": trigger_hint,
        "lazyLoadRef": lazy_load_ref,
        "version": version,
        "posteriorMean": None,
        "effectiveSampleSize": None,
    }


def _includes(requested: str | None, asset_type: str) -> bool:
    if requested is None:
        return True
    return requested == asset_type


def _normalize(asset_type: str | None) -> str | None:
    if asset_type is None or blank(asset_type):
        return None
    return java_trim(asset_type).upper()


def _bound_limit(limit: int | None) -> int:
    if limit is None:
        return 20
    bounded = limit
    if bounded < 1:
        bounded = 1
    if bounded > 50:
        bounded = 50
    return bounded


def _compact_category(scope: str | None, memory_type: str | None) -> str | None:
    if scope is None or blank(scope):
        return memory_type
    if memory_type is None or blank(memory_type):
        return scope
    return scope + "/" + memory_type


def _truncate(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-16-le")
    if len(encoded) // 2 <= max_length:
        return value
    return encoded[: max_length * 2].decode("utf-16-le", errors="surrogatepass")


def _java_text(value: object) -> str:
    if value is None:
        return "null"
    return str(value)
