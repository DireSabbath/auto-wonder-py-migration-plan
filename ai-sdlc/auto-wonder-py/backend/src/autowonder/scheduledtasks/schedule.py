"""定时任务的 cron 与时区。非法表达式和时区都是 30003。"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.evolution.jsontext import java_trim
from autowonder.scheduledtasks.cron import CronExpression

_ASCII_WS = re.compile(r"[ \t\n\x0b\f\r]+")

MAX_PREVIEW_COUNT = 100
_ZONE_IDS = available_timezones()


class ScheduledTaskSchedule:
    """计算下一次触发，并按 Spring cron 预览未来若干次。"""

    def next(self, expression: str | None, timezone_name: str | None, after: datetime) -> datetime:
        """``after`` 之后的下一次触发，结果是 UTC。"""
        zone = _parse_zone(timezone_name)
        cron = _parse_expression(expression)
        found = cron.next_after(after.astimezone(zone), zone)
        if found is None:
            raise BizError(ErrorCode.SCHEDULED_TASK_CRON_INVALID)
        return found.astimezone(timezone.utc)

    def preview(
        self,
        expression: str | None,
        timezone_name: str | None,
        after: datetime,
        count: int,
    ) -> list[datetime]:
        """从 ``after`` 起向后取严格递增的触发时间。"""
        if count <= 0 or count > MAX_PREVIEW_COUNT:
            raise BizError(
                ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED,
                f"预览次数必须在 1 到 {MAX_PREVIEW_COUNT} 之间",
            )
        found: list[datetime] = []
        seen: set[datetime] = set()
        cursor = after
        for _ in range(count):
            nxt = self.next(expression, timezone_name, cursor)
            if nxt in seen or nxt <= cursor:
                raise BizError(ErrorCode.SCHEDULED_TASK_CRON_INVALID)
            seen.add(nxt)
            found.append(nxt)
            cursor = nxt
        return found

    def validate(self, expression: str | None, timezone_name: str | None) -> None:
        """表达式和时区都能解析才通过。"""
        _parse_expression(expression)
        _parse_zone(timezone_name)


def _parse_expression(expression: str | None) -> CronExpression:
    if expression is None or java_is_blank(expression):
        raise BizError(ErrorCode.SCHEDULED_TASK_CRON_INVALID)
    trimmed = java_trim(expression)
    if len(_ASCII_WS.split(trimmed)) != 6:
        raise BizError(ErrorCode.SCHEDULED_TASK_CRON_INVALID)
    try:
        return CronExpression.parse(trimmed)
    except ValueError as error:
        raise BizError(ErrorCode.SCHEDULED_TASK_CRON_INVALID) from error


def _parse_zone(timezone_name: str | None) -> ZoneInfo:
    if timezone_name is None or java_is_blank(timezone_name):
        raise BizError(ErrorCode.SCHEDULED_TASK_CRON_INVALID)
    name = java_trim(timezone_name)
    if name not in _ZONE_IDS:
        raise BizError(ErrorCode.SCHEDULED_TASK_CRON_INVALID)
    return ZoneInfo(name)
