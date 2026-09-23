"""工单澄清材料。没有记录时返回空正文和版本 0。"""

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.clarifications.models import Clarification
from autowonder.clarifications.schemas import ClarificationView, empty_clarification


async def get_clarification(session: AsyncSession, workitem_id: int) -> ClarificationView:
    """按工单读取澄清。没有行时不插入。"""
    stored = await _find(session, workitem_id)
    if stored is None:
        return empty_clarification(workitem_id)
    return _to_view(stored)


async def put_clarification(
    session: AsyncSession,
    workitem_id: int,
    content_md: str | None,
    tenant_id: int,
    user_id: int,
) -> ClarificationView:
    """没有记录时插入，已有记录时替换正文并加版本。"""
    existing = await _find(session, workitem_id)
    if existing is None:
        session.add(
            Clarification(
                tenant_id=tenant_id,
                workitem_id=workitem_id,
                content_md=content_md,
                version=0,
            )
        )
        await session.flush()
    else:
        await session.execute(
            update(Clarification)
            .where(Clarification.id == existing.id, Clarification.tenant_id == tenant_id)
            .values(
                content_md=content_md,
                version=Clarification.version + 1,
                gmt_modified=text("CURRENT_TIMESTAMP(3)"),
            )
        )
    await session.commit()
    session.expire_all()
    stored = await _find(session, workitem_id)
    if stored is None:
        raise RuntimeError("clarification row missing after write")
    return _to_view(stored)


def _to_view(stored: Clarification) -> ClarificationView:
    return ClarificationView(
        workitem_id=stored.workitem_id,
        content_md=stored.content_md,
        version=stored.version,
        gmt_modified=stored.gmt_modified,
    )


async def _find(session: AsyncSession, workitem_id: int) -> Clarification | None:
    return await session.scalar(
        select(Clarification).where(Clarification.workitem_id == workitem_id).limit(1)
    )
