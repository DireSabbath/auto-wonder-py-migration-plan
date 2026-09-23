"""洞察的日期窗、人机协作还原和路由。这些检查不访问数据库或 Redis。"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from autowonder.core.clock import SHANGHAI
from autowonder.core.errors import BizError
from autowonder.insights.participation import (
    Granularity,
    ParticipationEvent,
    ParticipationFact,
    bucket_label,
    parse_granularity,
    parse_snapshot,
    reconstruct,
    require_date_range,
    snapshot_document,
    summarize,
)
from autowonder.insights.service import (
    _historical_name,
    compute_since,
    days_from_range,
    delivery_bounds,
    divide_credits,
)
from autowonder.main import create_app

_ZONE = ZoneInfo("Asia/Shanghai")
_DATE_RANGE = "Invalid date range: start_date must be <= end_date and end_date <= dataThrough"
_GRANULARITY = (
    "No enum constant com.aliyun.autowonder.insights.participation."
    "HumanAgentParticipationCalculator.Granularity.HOUR"
)


def _at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=SHANGHAI)


def _create(workitem_id: int, title: str, moment: datetime) -> ParticipationEvent:
    return ParticipationEvent(workitem_id, title, "CREATE", None, None, moment, False)


def _assign(
    workitem_id: int,
    moment: datetime,
    detail: str | None,
    inferred: str | None = None,
) -> ParticipationEvent:
    return ParticipationEvent(
        workitem_id,
        None,
        "ASSIGN",
        detail,
        inferred,
        moment,
        False,
    )


def _terminal(workitem_id: int, moment: datetime) -> ParticipationEvent:
    return ParticipationEvent(
        workitem_id,
        None,
        "STATUS_CHANGE",
        None,
        None,
        moment,
        True,
    )


def test_days_and_since_keep_clock_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """7 天、90 天，其余 30 天，并且保留当前钟点。"""
    monkeypatch.setattr(
        "autowonder.insights.service.now_local",
        lambda: datetime(2026, 7, 15, 8, 30, 1),
    )
    assert days_from_range("7d") == 7
    assert days_from_range("90d") == 90
    assert days_from_range("30d") == 30
    assert days_from_range("other") == 30
    assert compute_since("7d") == datetime(2026, 7, 8, 8, 30, 1)
    assert compute_since("90d") == datetime(2026, 4, 16, 8, 30, 1)
    assert compute_since("30d") == datetime(2026, 6, 15, 8, 30, 1)


def test_divide_credits_uses_half_up() -> None:
    """积分均值保留两位，四舍五入。除数不是正数时为 0。"""
    assert divide_credits(Decimal("1"), 8) == Decimal("0.13")
    assert divide_credits(Decimal("10"), 4) == Decimal("2.50")
    assert divide_credits(Decimal("1"), 0) == Decimal(0)


def test_delivery_bounds_follow_shanghai_week() -> None:
    """缺省从本周一到今天。恰好 365 天允许，再早一天拒绝。"""
    today = date(2026, 9, 23)
    start, end, monday = delivery_bounds(today, None, None)
    assert monday == date(2026, 9, 21)
    assert start == monday
    assert end == today
    allowed_start, allowed_end, _monday = delivery_bounds(today, "2025-09-23", "2026-09-23")
    assert allowed_start == date(2025, 9, 23)
    assert allowed_end == today
    with pytest.raises(BizError) as blank:
        delivery_bounds(today, "", None)
    assert blank.value.code == "10001"
    with pytest.raises(BizError) as inverted:
        delivery_bounds(today, "2026-09-24", "2026-09-23")
    assert inverted.value.code == "10001"
    with pytest.raises(BizError) as future:
        delivery_bounds(today, "2026-09-23", "2026-09-24")
    assert future.value.code == "10001"
    with pytest.raises(BizError) as too_long:
        delivery_bounds(today, "2025-09-22", "2026-09-23")
    assert too_long.value.code == "10001"


def test_historical_member_names() -> None:
    """没有成员行时，空 id 和历史 id 使用固定名称。"""
    assert _historical_name(None) == "未归属成员"
    assert _historical_name(12) == "历史成员 #12"


def test_reconstruct_human_agent_and_legacy_handoff() -> None:
    """CREATE 后指派给数字员工，再完结。历史指派用推断类型。"""
    created = _at(1, 10)
    to_agent = created + timedelta(hours=8)
    back_human = created + timedelta(hours=40)
    done = created + timedelta(hours=48)
    rows = [
        _create(1, "test-1", created),
        _assign(1, to_agent, '{"fromType":"HUMAN","toType":"AGENT"}'),
        _assign(1, back_human, '{"fromType":"AGENT","toType":"HUMAN"}'),
        _terminal(1, done),
    ]
    facts = reconstruct(rows, done + timedelta(seconds=1))
    assert len(facts) == 1
    assert facts[0].total_duration_seconds == 48 * 3600
    assert facts[0].human_duration_seconds == 16 * 3600
    assert facts[0].agent_duration_seconds == 32 * 3600

    legacy_created = _at(1, 10)
    first_assign = legacy_created + timedelta(seconds=4)
    second_assign = legacy_created + timedelta(hours=2)
    legacy_done = legacy_created + timedelta(hours=80)
    legacy = [
        _create(28518, "historical handoff", legacy_created),
        _assign(28518, first_assign, '{"fromType":"HUMAN","toType":"AGENT"}'),
        _assign(28518, second_assign, None, "HUMAN"),
        _terminal(28518, legacy_done),
    ]
    legacy_facts = reconstruct(legacy, legacy_created + timedelta(days=30))
    assert len(legacy_facts) == 1
    human = (first_assign - legacy_created) + (legacy_done - second_assign)
    assert legacy_facts[0].human_duration_seconds == int(human.total_seconds())
    assert legacy_facts[0].agent_duration_seconds == int(
        (second_assign - first_assign).total_seconds()
    )


def test_reconstruct_drops_open_and_post_cutoff_workitems() -> None:
    """没有完结，或完结晚于截止时刻的工单不进入快照。"""
    created = _at(1, 10)
    assigned = created + timedelta(hours=4)
    open_rows = [
        _create(1, "test-1", created),
        _assign(1, assigned, '{"toType":"AGENT"}'),
    ]
    assert reconstruct(open_rows, assigned + timedelta(days=1)) == []
    done = created + timedelta(days=3)
    late = [
        _create(1, "test-1", created),
        _terminal(1, done),
    ]
    assert reconstruct(late, created + timedelta(days=2)) == []


def test_reconstruct_ignores_assignment_after_terminal() -> None:
    """第一次完结之后的指派不改时长。"""
    created = _at(1, 10)
    assigned = created + timedelta(hours=2)
    done = created + timedelta(hours=6)
    later = created + timedelta(hours=10)
    rows = [
        _create(1, "test-post-terminal", created),
        _assign(1, assigned, '{"fromType":"HUMAN","toType":"AGENT"}'),
        _terminal(1, done),
        _assign(1, later, '{"fromType":"AGENT","toType":"HUMAN"}'),
    ]
    facts = reconstruct(rows, created + timedelta(days=30))
    assert len(facts) == 1
    assert facts[0].total_duration_seconds == int((done - created).total_seconds())
    assert facts[0].human_duration_seconds == int((assigned - created).total_seconds())
    assert facts[0].agent_duration_seconds == int((done - assigned).total_seconds())


def test_summarize_p90_tail_and_week_bucket() -> None:
    """P90 取第 9 名，最慢尾部只有最长的一张。周标签是周一。"""
    base = datetime(2026, 7, 1, 10, 0, tzinfo=SHANGHAI)
    facts = [
        ParticipationFact(
            index,
            "w" + str(index),
            base + timedelta(hours=index),
            index * 3600,
            0,
            0,
        )
        for index in range(1, 11)
    ]
    summary = summarize(
        facts,
        date(2026, 7, 1),
        date(2026, 7, 31),
        Granularity.MONTH,
        _ZONE,
    )
    assert len(summary.eligible_facts) == 10
    assert summary.p90 is not None
    assert summary.p90.workitem_id == 9
    assert len(summary.slow_tail) == 1
    assert summary.slow_tail[0].workitem_id == 10
    assert summary.average_total_seconds == 19800
    empty = summarize([], date(2026, 1, 1), date(2026, 1, 31), Granularity.DAY, _ZONE)
    assert empty.average_total_seconds == 0
    assert empty.p90 is None
    assert empty.trend == []
    moment = datetime(2026, 8, 4, 15, 0, tzinfo=SHANGHAI)
    assert bucket_label(moment, Granularity.DAY, _ZONE) == "2026-08-04"
    assert bucket_label(moment, Granularity.WEEK, _ZONE) == "2026-08-03"
    assert bucket_label(moment, Granularity.MONTH, _ZONE) == "2026-08-01"
    week = summarize(
        [ParticipationFact(1, "week", moment, 10, 4, 6)],
        date(2026, 8, 1),
        date(2026, 8, 31),
        Granularity.WEEK,
        _ZONE,
    )
    assert week.trend[0].label == "2026-08-03"


def test_granularity_and_date_range_errors() -> None:
    """非法粒度沿用 Java 枚举文案，日期越界沿用 IllegalArgumentException 文案。"""
    assert parse_granularity("week") is Granularity.WEEK
    with pytest.raises(ValueError) as granularity:
        parse_granularity("hour")
    assert str(granularity.value) == _GRANULARITY
    require_date_range(date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 2))
    with pytest.raises(ValueError) as inverted:
        require_date_range(date(2026, 8, 2), date(2026, 8, 1), date(2026, 8, 3))
    assert str(inverted.value) == _DATE_RANGE
    with pytest.raises(ValueError) as after_snapshot:
        require_date_range(date(2026, 8, 1), date(2026, 8, 4), date(2026, 8, 3))
    assert str(after_snapshot.value) == _DATE_RANGE


def test_snapshot_document_roundtrip_and_version_mismatch() -> None:
    """快照字段顺序与 Fastjson 一致。版本不是 1 时当作没有快照。"""
    fact = ParticipationFact(
        9,
        None,
        datetime(2026, 8, 1, 10, 0, tzinfo=SHANGHAI),
        100,
        40,
        60,
    )
    document = snapshot_document(
        [fact],
        "2026-08-01",
        datetime(2026, 8, 2, 3, 0, tzinfo=SHANGHAI),
    )
    assert document == (
        '{"schemaVersion":1,"generatedAt":"2026-08-01T19:00:00Z",'
        '"dataThrough":"2026-08-01","items":[{"workitemId":9,"title":null,'
        '"completedAt":"2026-08-01T02:00:00Z","totalDurationSeconds":100,'
        '"humanDurationSeconds":40,"agentDurationSeconds":60}]}'
    )
    parsed = parse_snapshot(document)
    assert parsed is not None
    assert parsed.generated_at == "2026-08-01T19:00:00Z"
    assert parsed.data_through == "2026-08-01"
    assert parsed.items[0].workitem_id == 9
    assert parsed.items[0].title is None
    assert parsed.items[0].total_duration_seconds == 100
    assert parse_snapshot('{"schemaVersion":2,"dataThrough":"2026-08-01"}') is None
    assert parse_snapshot("{") is None


def test_insight_routes_match_java_and_require_login() -> None:
    """洞察路径与 Java 一致。用量回填还没有存储实现，因此不注册。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/insights/metrics" in paths
    assert "/api/insights/audit" in paths
    assert "/api/insights/workers" in paths
    assert "/api/insights/human-agent-participation" in paths
    assert "/api/insights/human-agent-participation/slowest" in paths
    assert "post" in paths["/api/insights/human-agent-participation/refresh"]
    assert "/api/insights/member-delivery" in paths
    assert "/api/insights/usage/backfill" not in paths
    response = client.get("/api/insights/metrics")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    delivery = client.get("/api/insights/member-delivery")
    assert delivery.status_code == 401
    assert delivery.json()["code"] == "10401"
