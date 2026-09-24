"""AI 用量的周期、计数键和配额判断。这些检查不访问数据库。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from autowonder.aiusage.service import (
    calls_key,
    current_period,
    quota_blocks,
    quota_view,
    resolved_period,
    stored_limit,
    tokens_key,
)
from autowonder.main import create_app


def test_period_keys_and_quota_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """月份按上海本地时间；读取配额时 0 表示用尽，写入 Lua 时空上限变成 0。"""
    monkeypatch.setattr(
        "autowonder.aiusage.service.now_local",
        lambda: datetime(2026, 7, 15, 8, 0, 0),
    )
    assert current_period() == "2026-07"
    assert resolved_period(None) == "2026-07"
    assert resolved_period("") == ""
    assert calls_key(3, "2026-07") == "ai:usage:3:2026-07:calls"
    assert tokens_key(3, "2026-07") == "ai:usage:3:2026-07:tokens"
    assert stored_limit(None) == 0
    assert stored_limit(12) == 12
    assert quota_blocks(None, 5, None, 5) is False
    assert quota_blocks(0, 0, None, 0) is True
    assert quota_blocks(2, 1, 10, 10) is True
    assert quota_blocks(2, 1, 10, 9) is False
    view = quota_view(None)
    assert view.period_type == "MONTH"
    assert view.max_calls is None
    assert view.max_tokens is None
    assert view.concurrency_limit is None


def test_ai_usage_routes_match_java_and_require_login() -> None:
    """用量和配额路径与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/ai-usage" in paths
    assert "/api/ai-usage/quota" in paths
    assert "put" in paths["/api/ai-usage/quota"]
    response = client.get("/api/ai-usage")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
