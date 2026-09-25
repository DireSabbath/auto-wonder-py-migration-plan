"""工作空间隔离。读路径互不可见，写入的 tenant_id 保持调用方赋的值。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents import models as agent_models
from autowonder.ai import models as ai_models
from autowonder.aiusage import models as aiusage_models
from autowonder.categories import models as category_models
from autowonder.clarifications import models as clarification_models
from autowonder.core.context import RequestContext, reset_context, set_context
from autowonder.db.session import SessionLocal
from autowonder.db.tenant import TENANT_MODELS
from autowonder.db.tenant_tables import TENANT_TABLES
from autowonder.debuglogs import models as debuglog_models
from autowonder.dispatch import models as dispatch_models
from autowonder.environments import models as environment_models
from autowonder.evolution import models as evolution_models
from autowonder.memories import models as memory_models
from autowonder.notifications import models as notification_models
from autowonder.repos import models as repo_models
from autowonder.sdlcs import models as sdlc_models
from autowonder.settings.models import SystemSetting
from autowonder.skills import models as skill_models
from autowonder.squads import models as squad_models
from autowonder.statemachines import models as statemachine_models
from autowonder.users.models import User
from autowonder.workitems import models as workitem_models

_KEYS = ("tenant-probe-a", "tenant-probe-b", "tenant-probe-explicit")
_USERNAME = "tenant-probe-user"
_MODEL_MODULES = (
    ai_models,
    agent_models,
    aiusage_models,
    category_models,
    clarification_models,
    debuglog_models,
    dispatch_models,
    environment_models,
    evolution_models,
    memory_models,
    notification_models,
    repo_models,
    sdlc_models,
    skill_models,
    squad_models,
    statemachine_models,
    workitem_models,
)


@asynccontextmanager
async def _workspace(workspace_id: int) -> AsyncIterator[None]:
    token = set_context(RequestContext(workspace_id=workspace_id))
    try:
        yield
    finally:
        reset_context(token)


async def _purge() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(SystemSetting).where(SystemSetting.setting_key.in_(_KEYS)))
        await session.execute(delete(User).where(User.username == _USERNAME))
        await session.commit()


def test_registered_models_cover_the_tenant_tables() -> None:
    """38 张清单里的表都有 ORM 模型，隔离条件才能盖住它们。"""
    assert len(_MODEL_MODULES) == 17
    names = {model.__tablename__ for model in TENANT_MODELS}
    assert names == TENANT_TABLES


async def test_read_path_hides_the_other_workspace() -> None:
    """工作空间 A 读不到 B 的设置，反过来也一样。"""
    await _purge()
    try:
        async with SessionLocal() as session:
            session.add(_setting(11, "tenant-probe-a"))
            session.add(_setting(22, "tenant-probe-b"))
            await session.commit()
            owned = {row.setting_key: row.id for row in await _probe_rows(session)}
        async with SessionLocal() as session:
            async with _workspace(11):
                visible = await _probe_rows(session)
                hidden = await session.get(SystemSetting, owned["tenant-probe-b"])
            assert [row.setting_key for row in visible] == ["tenant-probe-a"]
            assert hidden is None
        async with SessionLocal() as session:
            async with _workspace(22):
                visible = await _probe_rows(session)
                hidden = await session.get(SystemSetting, owned["tenant-probe-a"])
            assert [row.setting_key for row in visible] == ["tenant-probe-b"]
            assert hidden is None
    finally:
        await _purge()


async def test_unscoped_scan_sees_every_tenant() -> None:
    """没有请求工作空间时，扫描能看见各个租户的行。"""
    await _purge()
    try:
        async with SessionLocal() as session:
            session.add(_setting(11, "tenant-probe-a"))
            session.add(_setting(22, "tenant-probe-b"))
            await session.commit()
            keys = [row.setting_key for row in await _probe_rows(session)]
        assert keys == ["tenant-probe-a", "tenant-probe-b"]
    finally:
        await _purge()


async def test_insert_stores_the_assigned_tenant_id() -> None:
    """当前工作空间是 11 时，显式写入的 22 仍按 22 落库。"""
    await _purge()
    try:
        async with SessionLocal() as session:
            async with _workspace(11):
                session.add(_setting(22, "tenant-probe-explicit"))
                await session.commit()
        async with SessionLocal() as session:
            stored = (await _probe_rows(session))[0]
            assert stored.tenant_id == 22
        async with SessionLocal() as session:
            async with _workspace(11):
                assert await _probe_rows(session) == []
        async with SessionLocal() as session:
            async with _workspace(22):
                visible = await _probe_rows(session)
            assert [row.setting_key for row in visible] == ["tenant-probe-explicit"]
    finally:
        await _purge()


async def test_global_user_is_visible_in_every_workspace() -> None:
    """user 不在 38 张表里，两个工作空间都能读到同一行。"""
    await _purge()
    try:
        async with SessionLocal() as session:
            session.add(User(username=_USERNAME, password_hash="x"))
            await session.commit()
        async with SessionLocal() as session:
            async with _workspace(11):
                left = await session.scalar(select(User).where(User.username == _USERNAME))
            async with _workspace(22):
                right = await session.scalar(select(User).where(User.username == _USERNAME))
        assert left is not None and right is not None
        assert left.id == right.id
    finally:
        await _purge()


def _setting(tenant_id: int, key: str) -> SystemSetting:
    return SystemSetting(
        tenant_id=tenant_id,
        setting_group="AI",
        setting_key=key,
        value_json={"probe": key},
    )


async def _probe_rows(session: AsyncSession) -> list[SystemSetting]:
    rows = await session.scalars(
        select(SystemSetting)
        .where(SystemSetting.setting_key.in_(_KEYS))
        .order_by(SystemSetting.setting_key.asc())
    )
    return list(rows.all())
