"""租户测试结束后释放异步连接池，避免连接留在已关闭的事件循环上。"""

import pytest

from autowonder.db.session import engine


@pytest.fixture(autouse=True)
async def release_async_engine():
    yield
    await engine.dispose()
