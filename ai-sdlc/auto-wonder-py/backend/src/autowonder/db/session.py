"""异步会话。连接建立后把会话时区设为 +08:00。"""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from autowonder.config import get_settings
from autowonder.db.tenant import install_tenant_criteria

_settings = get_settings()
engine = create_async_engine(_settings.resolved_database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
install_tenant_criteria()


@event.listens_for(engine.sync_engine, "connect")
def _set_time_zone(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("SET time_zone = '+08:00'")
    cursor.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：一个请求一个会话。"""
    async with SessionLocal() as session:
        yield session


__all__ = ["SessionLocal", "engine", "get_session"]
