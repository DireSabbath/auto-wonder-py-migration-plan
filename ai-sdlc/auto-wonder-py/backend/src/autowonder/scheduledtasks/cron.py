"""Spring 5.3 ``CronExpression`` 的六段 cron。

秒、分、时、日、月、周。``?`` 与 ``*`` 一样表示不限制。日和周同时被限制时，
两个字段都要命中。夏令时缺口按 ``ZonedDateTime`` 把本地时间向前拨过缺口。
"""

import calendar
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_MASK = (1 << 64) - 1
_MAX_ATTEMPTS = 366
_ASCII_WS = re.compile(r"[ \t\n\x0b\f\r]+")
_FULL_RANGE = {
    "second": (0, 59),
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "dow": (1, 7),
}
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def same_zoned(left: datetime, right: datetime) -> bool:
    """同一本地钟面、同一偏移。Python 的 ``==`` 会把夏令时重叠的两个瞬间看成相等。"""
    return left.replace(tzinfo=None) == right.replace(tzinfo=None) and left.utcoffset() == (
        right.utcoffset()
    )


def resolve_local(naive: datetime, zone: ZoneInfo, preferred: datetime) -> datetime:
    """对齐 ``ZonedDateTime.resolveLocal``：重叠优先原偏移，缺口向前拨。"""
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    first_offset = first.utcoffset()
    second_offset = second.utcoffset()
    first_wall = first.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
    if first_offset != second_offset and first_wall == naive:
        if preferred.utcoffset() == second_offset:
            return second
        return first
    if first_wall == naive:
        return first
    gap = first_wall - naive
    if gap.total_seconds() < 0:
        gap = -gap
    return (naive + gap).replace(tzinfo=zone)


def add_seconds(value: datetime, zone: ZoneInfo, seconds: int) -> datetime:
    """按绝对时长加秒，偏移随当地规则变化。"""
    return (value + timedelta(seconds=seconds)).astimezone(zone)


def add_days(value: datetime, zone: ZoneInfo, days: int) -> datetime:
    """按本地日历加天，保留钟面后再解析偏移。"""
    naive = value.replace(tzinfo=None) + timedelta(days=days)
    return resolve_local(naive, zone, value)


def add_months(value: datetime, zone: ZoneInfo, months: int) -> datetime:
    """按本地日历加月。目标月没有这一天时落到该月最后一天。"""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    naive = value.replace(tzinfo=None).replace(year=year, month=month, day=day)
    return resolve_local(naive, zone, value)


def field_value(value: datetime, kind: str) -> int:
    """读取 cron 字段。周从周一 1 到周日 7，与 ``java.time`` 一致。"""
    if kind == "second":
        return value.second
    if kind == "minute":
        return value.minute
    if kind == "hour":
        return value.hour
    if kind == "day":
        return value.day
    if kind == "month":
        return value.month
    return value.isoweekday()


def refined_range(value: datetime, kind: str) -> tuple[int, int]:
    """日字段用当月长度，其余字段用 cron 全范围。"""
    if kind == "day":
        return 1, calendar.monthrange(value.year, value.month)[1]
    return _FULL_RANGE[kind]


def with_field(value: datetime, zone: ZoneInfo, kind: str, goal: int) -> datetime:
    """把单个字段改成目标值，再按原偏移解析夏令时。"""
    naive = value.replace(tzinfo=None)
    if kind == "second":
        naive = naive.replace(second=goal)
    elif kind == "minute":
        naive = naive.replace(minute=goal)
    elif kind == "hour":
        naive = naive.replace(hour=goal)
    elif kind == "day":
        naive = naive.replace(day=goal)
    elif kind == "month":
        naive = naive.replace(month=goal)
    elif kind == "dow":
        naive = naive + timedelta(days=goal - naive.isoweekday())
    else:
        naive = naive.replace(microsecond=0)
    return resolve_local(naive, zone, value)


def add_base(value: datetime, zone: ZoneInfo, kind: str, amount: int) -> datetime:
    """按该字段的基本单位前进。时、分、秒是绝对时长，日和月是日历。"""
    if kind == "second":
        return add_seconds(value, zone, amount)
    if kind == "minute":
        return add_seconds(value, zone, amount * 60)
    if kind == "hour":
        return add_seconds(value, zone, amount * 3600)
    if kind == "day":
        return add_days(value, zone, amount)
    if kind == "month":
        return add_months(value, zone, amount)
    return add_days(value, zone, amount * 7)


def elapse_until(value: datetime, zone: ZoneInfo, kind: str, goal: int) -> datetime:
    """把字段拨到目标值；目标在当前范围内不存在时滚到下一轮。"""
    current = field_value(value, kind)
    lower, upper = refined_range(value, kind)
    if current < goal:
        if lower <= goal <= upper:
            return with_field(value, zone, kind, goal)
        return add_base(value, zone, kind, upper - current + 1)
    return add_base(value, zone, kind, goal + upper - current + 1 - lower)


def roll_forward(value: datetime, zone: ZoneInfo, kind: str) -> datetime:
    """滚到下一档更高字段，并把本字段设为最小值。"""
    if kind == "second":
        rolled = add_seconds(value, zone, 60)
        return with_field(rolled, zone, "second", 0)
    if kind == "minute":
        rolled = add_seconds(value, zone, 3600)
        return with_field(rolled, zone, "minute", 0)
    if kind == "hour":
        rolled = add_days(value, zone, 1)
        return with_field(rolled, zone, "hour", 0)
    if kind == "day":
        rolled = add_months(value, zone, 1)
        return with_field(rolled, zone, "day", 1)
    if kind == "month":
        rolled = add_months(value, zone, 12)
        return with_field(rolled, zone, "month", 1)
    rolled = add_days(value, zone, 7)
    return with_field(rolled, zone, "dow", 1)


def reset_lower(value: datetime, zone: ZoneInfo, kind: str) -> datetime:
    """把更低字段清到最小值。正在命中的字段本身保持不变。"""
    if kind == "month":
        value = with_field(value, zone, "day", 1)
    if kind in {"month", "day", "dow"}:
        value = with_field(value, zone, "hour", 0)
    if kind in {"month", "day", "dow", "hour"}:
        value = with_field(value, zone, "minute", 0)
    if kind in {"month", "day", "dow", "hour", "minute"}:
        value = with_field(value, zone, "second", 0)
    return with_field(value, zone, "nano", 0)


def _next_set_bit(bits: int, from_index: int) -> int:
    if from_index >= 64:
        return -1
    masked = bits & ((_MASK << from_index) & _MASK)
    if masked == 0:
        return -1
    return (masked & -masked).bit_length() - 1


def _set_bit(bits: int, index: int) -> int:
    return bits | (1 << index)


def _clear_bit(bits: int, index: int) -> int:
    return bits & ~(1 << index) & _MASK


def _set_range_bits(bits: int, minimum: int, maximum: int) -> int:
    if minimum == maximum:
        return _set_bit(bits, minimum)
    min_mask = (_MASK << minimum) & _MASK
    shift = (-(maximum + 1)) & 63
    max_mask = _MASK >> shift
    return bits | (min_mask & max_mask)


def _check_value(kind: str, number: int) -> int:
    if kind == "dow" and number == 0:
        return 0
    lower, upper = _FULL_RANGE[kind]
    if number < lower or number > upper:
        raise ValueError(f"{kind} {number} out of range")
    return number


def _parse_range(token: str, kind: str) -> tuple[int, int]:
    if token == "*":
        return _FULL_RANGE[kind]
    hyphen = token.find("-")
    if hyphen == -1:
        number = _check_value(kind, int(token))
        return number, number
    minimum = _check_value(kind, int(token[:hyphen]))
    maximum = _check_value(kind, int(token[hyphen + 1 :]))
    if kind == "dow" and minimum == 7:
        minimum = 0
    if minimum > maximum:
        raise ValueError(f"range {token}")
    return minimum, maximum


def _replace_ordinals(value: str, names: tuple[str, ...]) -> str:
    upper = value.upper()
    for index, name in enumerate(names):
        upper = upper.replace(name, str(index + 1))
    return upper


class BitsCronField:
    """用 64 位掩码表示的 cron 字段。"""

    def __init__(self, kind: str, bits: int) -> None:
        self.kind = kind
        self.bits = bits

    def next_or_same(self, temporal: datetime, zone: ZoneInfo) -> datetime | None:
        """下一个命中本字段的时间。已经命中时保留更低字段。"""
        current = field_value(temporal, self.kind)
        nxt = _next_set_bit(self.bits, current)
        if nxt == -1:
            temporal = roll_forward(temporal, zone, self.kind)
            nxt = _next_set_bit(self.bits, 0)
        if nxt == current:
            return temporal
        count = 0
        current = field_value(temporal, self.kind)
        while current != nxt:
            if count >= _MAX_ATTEMPTS:
                return None
            count += 1
            temporal = elapse_until(temporal, zone, self.kind, nxt)
            current = field_value(temporal, self.kind)
            nxt = _next_set_bit(self.bits, current)
            if nxt == -1:
                temporal = roll_forward(temporal, zone, self.kind)
                nxt = _next_set_bit(self.bits, 0)
        if count >= _MAX_ATTEMPTS:
            return None
        return reset_lower(temporal, zone, self.kind)


class ZeroSubsecondField:
    """纳秒字段只允许 0。微秒非 0 时滚到下一整秒。"""

    def next_or_same(self, temporal: datetime, zone: ZoneInfo) -> datetime | None:
        if temporal.microsecond == 0:
            return temporal
        rolled = add_seconds(temporal, zone, 1)
        return with_field(rolled, zone, "nano", 0)


def _parse_bits(value: str, kind: str) -> BitsCronField:
    if value == "":
        raise ValueError("empty cron field")
    if kind in {"day", "dow"} and value == "?":
        value = "*"
    bits = 0
    for field in value.split(","):
        slash = field.find("/")
        if slash == -1:
            minimum, maximum = _parse_range(field, kind)
            bits = _set_range_bits(bits, minimum, maximum)
            continue
        range_text = field[:slash]
        delta = int(field[slash + 1 :])
        if delta <= 0:
            raise ValueError("increment must be positive")
        minimum, maximum = _parse_range(range_text, kind)
        if "-" not in range_text:
            maximum = _FULL_RANGE[kind][1]
        if delta == 1:
            bits = _set_range_bits(bits, minimum, maximum)
        else:
            for number in range(minimum, maximum + 1, delta):
                bits = _set_bit(bits, number)
    if kind == "dow" and bits & 1:
        bits = _clear_bit(_set_bit(bits, 7), 0)
    return BitsCronField(kind, bits)


def _at_midnight(temporal: datetime, zone: ZoneInfo) -> datetime:
    naive = temporal.replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
    return resolve_local(naive, zone, temporal)


def _rollback_midnight(current: datetime, result: datetime, zone: ZoneInfo) -> datetime:
    if result.day == current.day:
        return current
    return _at_midnight(result, zone)


def _last_day(temporal: datetime, zone: ZoneInfo) -> datetime:
    last = calendar.monthrange(temporal.year, temporal.month)[1]
    result = with_field(temporal, zone, "day", last)
    return _rollback_midnight(temporal, result, zone)


def _is_weekday(day: int) -> bool:
    return day != 6 and day != 7


def _weekday_nearest(day_of_month: int, temporal: datetime, zone: ZoneInfo) -> datetime | None:
    current = temporal.day
    dow = temporal.isoweekday()
    if (
        (current == day_of_month and _is_weekday(dow))
        or (dow == 5 and current == day_of_month - 1)
        or (dow == 1 and current == day_of_month + 1)
        or (dow == 1 and day_of_month == 1 and current == 3)
    ):
        return temporal
    count = 0
    while count < _MAX_ATTEMPTS:
        count += 1
        if current == day_of_month:
            dow = temporal.isoweekday()
            if dow == 6:
                if day_of_month != 1:
                    temporal = add_days(temporal, zone, -1)
                else:
                    temporal = add_days(temporal, zone, 2)
            elif dow == 7:
                temporal = add_days(temporal, zone, 1)
            return _at_midnight(temporal, zone)
        temporal = elapse_until(temporal, zone, "day", day_of_month)
        current = temporal.day
    return None


def _day_of_week_in_month(ordinal: int, dow_value: int, temporal: datetime, zone: ZoneInfo) -> datetime:
    if ordinal >= 0:
        cursor = with_field(temporal, zone, "day", 1)
        diff = (dow_value - cursor.isoweekday() + 7) % 7
        diff += (ordinal - 1) * 7
        return add_days(cursor, zone, diff)
    last = calendar.monthrange(temporal.year, temporal.month)[1]
    cursor = with_field(temporal, zone, "day", last)
    days_diff = dow_value - cursor.isoweekday()
    if days_diff > 0:
        days_diff -= 7
    elif days_diff == 0:
        days_diff = 0
    days_diff -= (-ordinal - 1) * 7
    return add_days(cursor, zone, days_diff)


class QuartzCronField:
    """``L`` / ``W`` / ``#`` 字段。周字段滚月，日字段滚日。"""

    def __init__(self, kind: str, mode: str, arg: int, extra: int) -> None:
        self.kind = kind
        self.mode = mode
        self.arg = arg
        self.extra = extra

    def next_or_same(self, temporal: datetime, zone: ZoneInfo) -> datetime | None:
        result = self._adjust(temporal, zone)
        if result is None:
            return None
        if result.timestamp() < temporal.timestamp():
            if self.kind == "dow":
                temporal = roll_forward(temporal, zone, "day")
            else:
                temporal = roll_forward(temporal, zone, self.kind)
            result = self._adjust(temporal, zone)
            if result is not None:
                result = reset_lower(result, zone, self.kind)
        return result

    def _adjust(self, temporal: datetime, zone: ZoneInfo) -> datetime | None:
        if self.mode == "last-day":
            return _last_day(temporal, zone)
        if self.mode == "last-weekday":
            last = _last_day(temporal, zone)
            dow = last.isoweekday()
            if dow == 6:
                result = add_days(last, zone, -1)
            elif dow == 7:
                result = add_days(last, zone, -2)
            else:
                result = last
            return _rollback_midnight(temporal, result, zone)
        if self.mode == "last-offset":
            last = with_field(temporal, zone, "day", calendar.monthrange(temporal.year, temporal.month)[1])
            result = add_days(last, zone, self.arg)
            return _rollback_midnight(temporal, result, zone)
        if self.mode == "nearest":
            return _weekday_nearest(self.arg, temporal, zone)
        if self.mode == "last-dow":
            found = _day_of_week_in_month(-1, self.arg, temporal, zone)
            return _rollback_midnight(temporal, found, zone)
        found = _day_of_week_in_month(self.extra, self.arg, temporal, zone)
        return _rollback_midnight(temporal, found, zone)


class CompositeCronField:
    """逗号列表里混有 Quartz 记号时，取最早的候选。"""

    def __init__(self, fields: tuple[BitsCronField | QuartzCronField, ...]) -> None:
        self.fields = fields

    def next_or_same(self, temporal: datetime, zone: ZoneInfo) -> datetime | None:
        result: datetime | None = None
        for field in self.fields:
            candidate = field.next_or_same(temporal, zone)
            if candidate is None:
                continue
            if result is None or candidate.timestamp() < result.timestamp():
                result = candidate
        return result


def _parse_dow_number(value: str) -> int:
    number = int(value)
    if number == 0:
        number = 7
    if number < 1 or number > 7:
        raise ValueError(f"day of week {value}")
    return number


def _parse_quartz_day(value: str) -> QuartzCronField:
    marker = value.rfind("L")
    if marker != -1:
        if marker != 0:
            raise ValueError(f"characters before L in {value}")
        if len(value) == 2 and value[1] == "W":
            return QuartzCronField("day", "last-weekday", 0, 0)
        if len(value) == 1:
            return QuartzCronField("day", "last-day", 0, 0)
        offset = int(value[marker + 1 :])
        if offset >= 0:
            raise ValueError(f"offset {offset} must be negative")
        return QuartzCronField("day", "last-offset", offset, 0)
    marker = value.rfind("W")
    if marker == -1:
        raise ValueError(f"no L or W in {value}")
    if marker == 0 or marker != len(value) - 1:
        raise ValueError(f"malformed weekday {value}")
    day = _check_value("day", int(value[:marker]))
    return QuartzCronField("day", "nearest", day, 0)


def _parse_quartz_dow(value: str) -> QuartzCronField:
    marker = value.rfind("L")
    if marker != -1:
        if marker != len(value) - 1 or marker == 0:
            raise ValueError(f"malformed last weekday {value}")
        return QuartzCronField("dow", "last-dow", _parse_dow_number(value[:marker]), 0)
    marker = value.rfind("#")
    if marker <= 0 or marker == len(value) - 1:
        raise ValueError(f"malformed nth weekday {value}")
    ordinal = int(value[marker + 1 :])
    if ordinal <= 0:
        raise ValueError("ordinal must be positive")
    return QuartzCronField("dow", "nth", _parse_dow_number(value[:marker]), ordinal)


def _parse_day(value: str) -> BitsCronField | QuartzCronField | CompositeCronField:
    if "L" not in value and "W" not in value:
        return _parse_bits(value, "day")
    parts = value.split(",")
    if len(parts) == 1:
        return _parse_quartz_day(parts[0])
    fields: list[BitsCronField | QuartzCronField] = []
    for part in parts:
        if "L" in part or "W" in part:
            fields.append(_parse_quartz_day(part))
        else:
            fields.append(_parse_bits(part, "day"))
    return CompositeCronField(tuple(fields))


def _parse_dow(value: str) -> BitsCronField | QuartzCronField | CompositeCronField:
    replaced = _replace_ordinals(value, _DAYS)
    if "L" not in replaced and "#" not in replaced:
        return _parse_bits(replaced, "dow")
    parts = replaced.split(",")
    if len(parts) == 1:
        return _parse_quartz_dow(parts[0])
    fields: list[BitsCronField | QuartzCronField] = []
    for part in parts:
        if "L" in part or "#" in part:
            fields.append(_parse_quartz_dow(part))
        else:
            fields.append(_parse_bits(part, "dow"))
    return CompositeCronField(tuple(fields))


class CronExpression:
    """六段 cron。``next`` 从给定时间的下一微秒开始，不包含起点本身。"""

    def __init__(
        self,
        dow: BitsCronField | QuartzCronField | CompositeCronField,
        month: BitsCronField,
        day: BitsCronField | QuartzCronField | CompositeCronField,
        hour: BitsCronField,
        minute: BitsCronField,
        second: BitsCronField,
    ) -> None:
        self._fields = (dow, month, day, hour, minute, second, ZeroSubsecondField())

    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        """解析已经确认是六段的表达式。"""
        fields = [part for part in _ASCII_WS.split(expression.strip()) if part != ""]
        if len(fields) != 6:
            raise ValueError(f"expected 6 fields, found {len(fields)}")
        return cls(
            _parse_dow(fields[5]),
            _parse_bits(_replace_ordinals(fields[4], _MONTHS), "month"),
            _parse_day(fields[3]),
            _parse_bits(fields[2], "hour"),
            _parse_bits(fields[1], "minute"),
            _parse_bits(fields[0], "second"),
        )

    def next_after(self, temporal: datetime, zone: ZoneInfo) -> datetime | None:
        """严格晚于 ``temporal`` 的下一次命中。366 轮仍不稳定时没有下一次。"""
        cursor = temporal + timedelta(microseconds=1)
        for _ in range(_MAX_ATTEMPTS):
            result = cursor
            for field in self._fields:
                stepped = field.next_or_same(result, zone)
                if stepped is None:
                    return None
                result = stepped
            if same_zoned(result, cursor):
                return result
            cursor = result
        return None
