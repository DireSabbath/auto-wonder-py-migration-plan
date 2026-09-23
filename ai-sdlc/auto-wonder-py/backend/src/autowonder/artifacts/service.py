"""按派发列出用户可见产物。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.artifacts.classification import resolve_artifact_type, user_visible
from autowonder.artifacts.models import Artifact
from autowonder.artifacts.schemas import ArtifactView


def to_artifact_view(row: Artifact) -> ArtifactView:
    """登记行转成查询结果。展示类型按路径补全。"""
    return ArtifactView(
        id=row.id,
        workitem_id=row.workitem_id,
        dispatch_id=row.dispatch_id,
        name=row.name,
        type=resolve_artifact_type(row.type, row.name),
        size=row.size,
        gmt_create=row.gmt_create,
    )


async def list_by_dispatch(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> list[ArtifactView]:
    """同一派发的产物按 id 倒序，观测文件不返回。"""
    rows = await session.scalars(
        select(Artifact)
        .where(Artifact.tenant_id == tenant_id, Artifact.dispatch_id == dispatch_id)
        .order_by(Artifact.id.desc())
    )
    views: list[ArtifactView] = []
    for row in rows:
        if user_visible(row.name):
            views.append(to_artifact_view(row))
    return views
