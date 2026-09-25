"""外部协作快照。列表用来源创建者，详情用整张协作卡片。"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.integrations.models import ExternalPrincipal, ExternalWorkitemLink
from autowonder.workitems.schemas import (
    ExternalCollaborationView,
    ExternalPrincipalRelationView,
    ExternalPrincipalView,
)
from autowonder.workitems.view import prefer_link


async def find_collaboration(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> ExternalCollaborationView | None:
    """没有外部链接时返回空。多条链接时 AONE 优先。"""
    result = await session.scalars(
        select(ExternalWorkitemLink).where(
            ExternalWorkitemLink.tenant_id == tenant_id,
            ExternalWorkitemLink.workitem_id == workitem_id,
        )
    )
    link = prefer_link(list(result.all()))
    if link is None:
        return None
    principals = await _load_principals(session, _collect_ids(link))
    return _collaboration(link, principals)


async def source_creators(
    session: AsyncSession, links_by_workitem: dict[int, list[ExternalWorkitemLink]]
) -> dict[int, ExternalPrincipalView]:
    """一页工单只查一次提出者。没有提出者的工单不出现在结果里。"""
    preferred: dict[int, ExternalWorkitemLink] = {}
    for workitem_id, candidates in links_by_workitem.items():
        chosen = prefer_link(candidates)
        if chosen is not None:
            preferred[workitem_id] = chosen
    principal_ids: set[int] = set()
    for link in preferred.values():
        if link.reporter_principal_id is not None:
            principal_ids.add(link.reporter_principal_id)
    principals = await _load_principals(session, principal_ids)
    creators: dict[int, ExternalPrincipalView] = {}
    for workitem_id, link in preferred.items():
        reporter = None
        if link.reporter_principal_id is not None:
            reporter = _principal(principals.get(link.reporter_principal_id))
        if reporter is not None:
            creators[workitem_id] = reporter
    return creators


async def principal_name(session: AsyncSession, principal_id: int | None) -> str | None:
    """时间线作者名。没有展示名时按平台拼一个可读称呼。"""
    return _name(await _load_principal(session, principal_id))


async def principal_display_name(session: AsyncSession, principal_id: int | None) -> str | None:
    """评论作者展示名，带平台和工号。"""
    return _display_name(await _load_principal(session, principal_id))


async def principal_compact_display_name(
    session: AsyncSession, principal_id: int | None
) -> str | None:
    """业务负责人变更里的短展示名。"""
    return _compact_name(await _load_principal(session, principal_id))


def _collaboration(
    link: ExternalWorkitemLink, principals: dict[int, ExternalPrincipal]
) -> ExternalCollaborationView:
    relations: list[ExternalPrincipalRelationView] = []
    for snapshot in _snapshots(link.principal_relations_json):
        people: list[ExternalPrincipalView] = []
        for principal_id in _principal_ids(snapshot.get("principal_ids")):
            person = _principal(principals.get(principal_id))
            if person is not None:
                people.append(person)
        if len(people) == 0:
            continue
        relations.append(
            ExternalPrincipalRelationView(
                source_key=_text(snapshot.get("source_key")),
                display_name=_text(snapshot.get("display_name")),
                principals=people,
            )
        )
    reporter = None
    if link.reporter_principal_id is not None:
        reporter = _principal(principals.get(link.reporter_principal_id))
    business_owner = None
    if link.business_owner_principal_id is not None:
        business_owner = _principal(principals.get(link.business_owner_principal_id))
    return ExternalCollaborationView(
        provider=link.provider,
        external_project_id=link.external_project_id,
        external_workitem_id=link.external_workitem_id,
        external_url=link.external_url,
        source_status_id=link.source_status_id,
        source_status_name=link.source_status_name,
        source_lifecycle=link.source_lifecycle,
        reporter=reporter,
        business_owner=business_owner,
        principal_relations=relations,
        last_sync_at=link.last_sync_at,
        sync_status=link.sync_status,
        last_error_code=link.last_error_code,
        last_error=link.last_error,
    )


def _collect_ids(link: ExternalWorkitemLink) -> set[int]:
    principal_ids: set[int] = set()
    if link.reporter_principal_id is not None:
        principal_ids.add(link.reporter_principal_id)
    if link.business_owner_principal_id is not None:
        principal_ids.add(link.business_owner_principal_id)
    for snapshot in _snapshots(link.principal_relations_json):
        for principal_id in _principal_ids(snapshot.get("principal_ids")):
            principal_ids.add(principal_id)
    return principal_ids


def _snapshots(raw: object) -> list[dict[str, object]]:
    """存下来的关系不是数组时，卡片其余字段仍然返回。"""
    payload = raw
    if isinstance(raw, str):
        if java_is_blank(raw):
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, list):
        return []
    snapshots: list[dict[str, object]] = []
    for item in payload:
        if isinstance(item, dict):
            snapshots.append(item)
    return snapshots


def _principal_ids(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    principal_ids: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            principal_ids.append(item)
    return principal_ids


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _principal(row: ExternalPrincipal | None) -> ExternalPrincipalView | None:
    if row is None:
        return None
    return ExternalPrincipalView(
        id=row.id,
        provider=row.provider,
        subject_id=row.subject_id,
        display_name=row.display_name,
    )


def _name(principal: ExternalPrincipal | None) -> str | None:
    if principal is None:
        return None
    if principal.display_name is not None and not java_is_blank(principal.display_name):
        return principal.display_name
    if principal.provider == "AONE":
        return "Aone 用户（staffId: " + principal.subject_id + "）"
    if not java_is_blank(principal.provider):
        return principal.provider + " 用户（ID: " + principal.subject_id + "）"
    return principal.subject_id


def _display_name(principal: ExternalPrincipal | None) -> str | None:
    if principal is None:
        return None
    if principal.display_name is None or java_is_blank(principal.display_name):
        return _name(principal)
    provider = principal.provider
    if provider == "AONE":
        provider = "Aone"
    if java_is_blank(provider):
        return principal.display_name + "（" + principal.subject_id + "）"
    return principal.display_name + "（" + provider + " · " + principal.subject_id + "）"


def _compact_name(principal: ExternalPrincipal | None) -> str | None:
    if principal is None:
        return None
    if (
        principal.display_name is not None
        and not java_is_blank(principal.display_name)
        and not java_is_blank(principal.subject_id)
    ):
        return principal.display_name + "（" + principal.subject_id + "）"
    return _name(principal)


async def _load_principals(
    session: AsyncSession, principal_ids: set[int]
) -> dict[int, ExternalPrincipal]:
    if len(principal_ids) == 0:
        return {}
    result = await session.scalars(
        select(ExternalPrincipal).where(ExternalPrincipal.id.in_(principal_ids))
    )
    return {row.id: row for row in result.all()}


async def _load_principal(
    session: AsyncSession, principal_id: int | None
) -> ExternalPrincipal | None:
    if principal_id is None:
        return None
    return await session.get(ExternalPrincipal, principal_id)
