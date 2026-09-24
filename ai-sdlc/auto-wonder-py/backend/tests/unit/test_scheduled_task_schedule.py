"""Spring cron 与任务定义校验的验收向量。"""

from datetime import datetime, timezone

import pytest

from autowonder.core.errors import BizError
from autowonder.scheduledtasks.schedule import ScheduledTaskSchedule
from autowonder.scheduledtasks.validator import validate_definition, validate_modes

UTC = timezone.utc


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_next_shanghai_two_am_and_ten_minute_preview() -> None:
    """上海 02:00 与每 10 分钟预览和 Java 向量一致。"""
    schedule = ScheduledTaskSchedule()
    nxt = schedule.next("0 0 2 * * *", "Asia/Shanghai", _instant("2026-08-10T17:59:59Z"))
    assert nxt == _instant("2026-08-10T18:00:00Z")
    preview = schedule.preview("0 */10 * * * *", "Asia/Shanghai", _instant("2026-08-10T00:00:00Z"), 5)
    assert preview == [
        _instant("2026-08-10T00:10:00Z"),
        _instant("2026-08-10T00:20:00Z"),
        _instant("2026-08-10T00:30:00Z"),
        _instant("2026-08-10T00:40:00Z"),
        _instant("2026-08-10T00:50:00Z"),
    ]


def test_day_31_skips_short_months_and_dst_gap_is_not_a_fire() -> None:
    """没有 31 日的月份跳过；纽约夏令时缺口里的 02:30 不触发。"""
    schedule = ScheduledTaskSchedule()
    assert schedule.next("0 0 0 31 * *", "UTC", _instant("2026-04-01T00:00:00Z")) == _instant(
        "2026-05-31T00:00:00Z"
    )
    assert schedule.next(
        "0 30 2 * * *", "America/New_York", _instant("2026-03-08T06:59:59Z")
    ) == _instant("2026-03-09T06:30:00Z")


def test_invalid_cron_macro_zone_and_preview_count() -> None:
    """非法表达式、宏、时区和预览次数沿用 Java 错误码。"""
    schedule = ScheduledTaskSchedule()
    with pytest.raises(BizError) as cron:
        schedule.next("not a cron", "Asia/Shanghai", datetime.fromtimestamp(0, UTC))
    assert cron.value.code == "30003"
    with pytest.raises(BizError):
        schedule.next("@daily", "Asia/Shanghai", datetime.fromtimestamp(0, UTC))
    with pytest.raises(BizError):
        schedule.next("0 0 2 * * *", "Mars/Olympus_Mons", datetime.fromtimestamp(0, UTC))
    with pytest.raises(BizError) as preview:
        schedule.preview("0 0 2 * * *", "Asia/Shanghai", datetime.fromtimestamp(0, UTC), 0)
    assert preview.value.code == "30004"
    assert str(preview.value) == "预览次数必须在 1 到 100 之间"


def test_mode_and_definition_rules() -> None:
    """会话、重叠、截止时间和一次性任务的定义约束。"""
    with pytest.raises(BizError):
        validate_modes("CONTINUOUS", "ALLOW")
    validate_modes("CONTINUOUS", "QUEUE")
    with pytest.raises(BizError):
        validate_modes("SHARED", "SKIP")
