"""Alembic 只负责 stamp。DDL 的唯一来源是 Java 侧 schema。"""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

import autowonder.model_imports  # noqa: F401
from autowonder.config import get_settings
from autowonder.db.base import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式只记录版本号，不生成 DDL。"""
    context.configure(
        url=get_settings().resolved_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """在已有连接上跑迁移回调。"""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """用异步引擎连接，仍然不在这里建表。"""
    engine = create_async_engine(get_settings().resolved_database_url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
