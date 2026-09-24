"""人机协作时长。按工单事件还原人工段和数字员工段，再按完成日汇总。"""

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from autowonder.core.clock import SHANGHAI

_DATE_RANGE_MESSAGE = (
    "Invalid date range: start_date must be <= end_date and end_date <= dataThrough"
)


class Granularity(Enum):
    """趋势桶。周从 ISO 周一开始，月从当月 1 日开始。"""

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


@dataclass(frozen=True)
class ParticipationFact:
    """一张已完结工单的人工时长和数字员工时长，单位秒。"""

    workitem_id: int
    title: str | None
    completed_at: datetime
    total_duration_seconds: int
    human_duration_seconds: int
    agent_duration_seconds: int


@dataclass(frozen=True)
class ParticipationEvent:
    """还原时长用的一条生命周期事件。"""

    workitem_id: int
    title: str | None
    event_type: str
    detail_json: object
    inferred_to_type: str | None
    event_at: datetime
    terminal: bool


@dataclass(frozen=True)
class TrendBucket:
    """一个日期桶内的平均时长。"""

    label: str
    average_total_seconds: int
    average_human_seconds: int
    average_agent_seconds: int


@dataclass(frozen=True)
class ParticipationSummary:
    """区间内的样本、均值、P90、最慢尾部和趋势。"""

    eligible_facts: list[ParticipationFact]
    average_total_seconds: int
    average_human_seconds: int
    average_agent_seconds: int
    p90: ParticipationFact | None
    slow_tail: list[ParticipationFact]
    trend: list[TrendBucket]


@dataclass(frozen=True)
class ParsedSnapshot:
    """Redis 里已经解析的人机协作快照。"""

    generated_at: str | None
    data_through: str
    items: list[ParticipationFact]


def parse_granularity(value: str) -> Granularity:
    """``DAY`` / ``WEEK`` / ``MONTH``，大小写不敏感。"""
    name = value.upper()
    if name == "DAY":
        return Granularity.DAY
    if name == "WEEK":
        return Granularity.WEEK
    if name == "MONTH":
        return Granularity.MONTH
    raise ValueError(
        "No enum constant com.aliyun.autowonder.insights.participation."
        "HumanAgentParticipationCalculator.Granularity." + name
    )


def parse_assignment_type(detail: object) -> str | None:
    """从指派详情读取 toType。只接受 HUMAN 和 AGENT。"""
    if detail is None:
        return None
    payload = detail
    if isinstance(detail, str):
        if detail.strip() == "":
            return None
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    to_type = payload.get("toType")
    if to_type == "HUMAN" or to_type == "AGENT":
        return str(to_type)
    return None


def normalize_assignment_type(value: str | None) -> str | None:
    """把推断类型收成 HUMAN 或 AGENT。"""
    if value is None or value.strip() == "":
        return None
    normalized = value.strip().upper()
    if normalized == "HUMAN" or normalized == "AGENT":
        return normalized
    return None


def reconstruct(
    rows: list[ParticipationEvent],
    cutoff: datetime,
) -> list[ParticipationFact]:
    """按工单分组还原。首事件不是 CREATE，或完结越出截止时刻的工单丢弃。"""
    grouped: dict[int, list[ParticipationEvent]] = {}
    for row in rows:
        grouped.setdefault(row.workitem_id, []).append(row)
    facts: list[ParticipationFact] = []
    for events in grouped.values():
        fact = _reconstruct_one(events, cutoff)
        if fact is not None:
            facts.append(fact)
    return facts


def summarize(
    facts: list[ParticipationFact],
    start: date,
    end: date,
    granularity: Granularity,
    zone: ZoneInfo,
) -> ParticipationSummary:
    """只统计完成日落在闭区间内的工单。均值按整数秒截断。"""
    in_range = [fact for fact in facts if start <= _local_date(fact.completed_at, zone) <= end]
    if len(in_range) == 0:
        return ParticipationSummary(in_range, 0, 0, 0, None, [], [])
    count = len(in_range)
    average_total = sum(fact.total_duration_seconds for fact in in_range) // count
    average_human = sum(fact.human_duration_seconds for fact in in_range) // count
    average_agent = sum(fact.agent_duration_seconds for fact in in_range) // count
    ranked = sorted(in_range, key=_ascending_key)
    p90_rank = math.ceil(count * 0.90)
    p90 = ranked[p90_rank - 1]
    tail_size = max(1, math.ceil(count * 0.10))
    slow_tail = sorted(in_range, key=_descending_key)[:tail_size]
    return ParticipationSummary(
        in_range,
        average_total,
        average_human,
        average_agent,
        p90,
        slow_tail,
        _trend(in_range, granularity, zone),
    )


def require_date_range(start: date, end: date, data_through: date) -> None:
    """开始不能晚于结束，结束不能晚于快照覆盖日。"""
    if start > end or end > data_through:
        raise ValueError(_DATE_RANGE_MESSAGE)


def bucket_label(moment: datetime, granularity: Granularity, zone: ZoneInfo) -> str:
    """趋势标签是该桶起始日的 ISO 日期。"""
    local = _local_date(moment, zone)
    if granularity is Granularity.DAY:
        return local.isoformat()
    if granularity is Granularity.WEEK:
        monday = local - timedelta(days=local.weekday())
        return monday.isoformat()
    return local.replace(day=1).isoformat()


def snapshot_document(
    facts: list[ParticipationFact],
    data_through: str,
    generated_at: datetime,
) -> str:
    """快照 JSON。字段顺序与 Java Fastjson 的有序对象一致。"""
    items = [
        {
            "workitemId": fact.workitem_id,
            "title": fact.title,
            "completedAt": instant_text(fact.completed_at),
            "totalDurationSeconds": fact.total_duration_seconds,
            "humanDurationSeconds": fact.human_duration_seconds,
            "agentDurationSeconds": fact.agent_duration_seconds,
        }
        for fact in facts
    ]
    payload = {
        "schemaVersion": 1,
        "generatedAt": instant_text(generated_at),
        "dataThrough": data_through,
        "items": items,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_snapshot(raw: str) -> ParsedSnapshot | None:
    """版本不是 1 或正文无法解析时当作没有快照。"""
    try:
        root = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(root, dict) or root.get("schemaVersion") != 1:
        return None
    data_through = root.get("dataThrough")
    if not isinstance(data_through, str):
        return None
    generated_at = root.get("generatedAt")
    if generated_at is not None and not isinstance(generated_at, str):
        return None
    items = root.get("items")
    facts: list[ParticipationFact] = []
    if isinstance(items, list):
        for item in items:
            try:
                fact = _fact_from_item(item)
            except (TypeError, ValueError):
                return None
            if fact is None:
                return None
            facts.append(fact)
    return ParsedSnapshot(generated_at, data_through, facts)


def instant_text(moment: datetime) -> str:
    """写成 Java ``Instant.toString`` 的 UTC 文本。"""
    aware = _aware(moment)
    utc = aware.astimezone(ZoneInfo("UTC"))
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond != 0:
        fraction = f"{utc.microsecond:06d}".rstrip("0")
        text = text + "." + fraction
    return text + "Z"


def _fact_from_item(item: object) -> ParticipationFact | None:
    if not isinstance(item, dict):
        return None
    completed = item.get("completedAt")
    if not isinstance(completed, str):
        return None
    try:
        completed_at = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return None
    raw_title = item.get("title")
    title = None
    if isinstance(raw_title, str):
        title = raw_title
    return ParticipationFact(
        workitem_id=int(item.get("workitemId", 0)),
        title=title,
        completed_at=completed_at,
        total_duration_seconds=int(item.get("totalDurationSeconds", 0)),
        human_duration_seconds=int(item.get("humanDurationSeconds", 0)),
        agent_duration_seconds=int(item.get("agentDurationSeconds", 0)),
    )


def _reconstruct_one(
    events: list[ParticipationEvent],
    cutoff: datetime,
) -> ParticipationFact | None:
    if len(events) == 0 or events[0].event_type != "CREATE":
        return None
    first = events[0]
    created_at = _aware(first.event_at)
    owner = "HUMAN"
    cursor = created_at
    agent_seconds = 0
    human_seconds = 0
    terminal_at: datetime | None = None
    for event in events[1:]:
        if event.event_type == "ASSIGN":
            to_type = parse_assignment_type(event.detail_json)
            if to_type is None:
                to_type = normalize_assignment_type(event.inferred_to_type)
            if to_type is None:
                continue
            event_at = _aware(event.event_at)
            if event_at < cursor:
                return None
            interval = _seconds(cursor, event_at)
            if owner == "AGENT":
                agent_seconds = agent_seconds + interval
            else:
                human_seconds = human_seconds + interval
            owner = to_type
            cursor = event_at
            continue
        if event.event_type == "STATUS_CHANGE" and event.terminal:
            if terminal_at is not None:
                continue
            event_at = _aware(event.event_at)
            if event_at > _aware(cutoff):
                return None
            if event_at < cursor:
                return None
            interval = _seconds(cursor, event_at)
            if owner == "AGENT":
                agent_seconds = agent_seconds + interval
            else:
                human_seconds = human_seconds + interval
            terminal_at = event_at
            break
    if terminal_at is None:
        return None
    return ParticipationFact(
        first.workitem_id,
        first.title,
        terminal_at,
        _seconds(created_at, terminal_at),
        human_seconds,
        agent_seconds,
    )


def _trend(
    facts: list[ParticipationFact],
    granularity: Granularity,
    zone: ZoneInfo,
) -> list[TrendBucket]:
    buckets: dict[str, list[ParticipationFact]] = {}
    for fact in facts:
        label = bucket_label(fact.completed_at, granularity, zone)
        buckets.setdefault(label, []).append(fact)
    trend: list[TrendBucket] = []
    for label in sorted(buckets):
        bucket = buckets[label]
        count = len(bucket)
        trend.append(
            TrendBucket(
                label,
                sum(fact.total_duration_seconds for fact in bucket) // count,
                sum(fact.human_duration_seconds for fact in bucket) // count,
                sum(fact.agent_duration_seconds for fact in bucket) // count,
            )
        )
    return trend


def _ascending_key(fact: ParticipationFact) -> tuple[int, datetime, int]:
    return (fact.total_duration_seconds, fact.completed_at, fact.workitem_id)


def _descending_key(fact: ParticipationFact) -> tuple[int, float, int]:
    return (
        -fact.total_duration_seconds,
        -fact.completed_at.timestamp(),
        -fact.workitem_id,
    )


def _local_date(moment: datetime, zone: ZoneInfo) -> date:
    return _aware(moment).astimezone(zone).date()


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=SHANGHAI)
    return moment


def _seconds(start: datetime, end: datetime) -> int:
    return int((_aware(end) - _aware(start)).total_seconds())
