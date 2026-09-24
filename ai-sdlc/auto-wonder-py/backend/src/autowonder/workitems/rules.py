"""工单列表、健康和标签规则，口径对齐 Java 服务与 MyBatis 过滤。"""

import json
from datetime import datetime

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.evolution.jsontext import java_trim

WORK_TYPES = frozenset({"REQ", "TASK", "BUG"})
STATUS_CATEGORIES = frozenset({"NEW", "IN_PROGRESS", "PENDING_DECISION", "DONE"})
SCHEDULED_FILTERS = frozenset({"ALL", "PENDING", "TRIGGERED"})
ACTIVE_DISPATCH_STATUSES = frozenset({"PACKAGING", "DISPATCHED", "ACKED", "RUNNING"})
TERMINAL_DISPATCH_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})
FAILED_TERMINAL = frozenset({"FAILED", "TIMEOUT", "CANCELED"})
MAX_TAGS = 20
MAX_TAG_LENGTH = 32
LONG_MAX = 9223372036854775807

_DONE_TOKENS = ("完成", "关闭", "发布", "DONE", "CLOSED", "FIXED", "RELEASED", "PUBLISHED")
NAME_DONE_PLAIN = ("完成", "关闭", "发布")
NAME_DONE_UPPER = ("DONE", "CLOSED", "FIXED", "PUBLISHED")
NOT_DONE_CODE_UPPER = ("DONE", "CLOSED", "RELEASED")
NOT_DONE_NAME_PLAIN = ("完成", "关闭", "发布")
NOT_DONE_NAME_UPPER = ("DONE", "CLOSED", "FIXED", "RELEASED")
PROGRESS_PLAIN = ("执行", "开发", "验证")
PROGRESS_UPPER = ("PROGRESS", "RUNNING")
DECISION_PLAIN = ("决策", "审核", "阻塞")
DECISION_UPPER = ("DECISION", "REVIEW")


def page_bounds(page: int, size: int) -> tuple[int, int, int]:
    """页码小于 1 视为 1，每页小于 1 视为 20，并且不超过 200。"""
    safe_page = page
    if safe_page < 1:
        safe_page = 1
    safe_size = size
    if safe_size < 1:
        safe_size = 20
    if safe_size > 200:
        safe_size = 200
    return safe_page, safe_size, (safe_page - 1) * safe_size


def normalize_status_category(value: str | None) -> str | None:
    """看板列取值归一化。空白和未知值表示不过滤。"""
    if value is None or java_is_blank(value):
        return None
    folded = java_trim(value).upper()
    if folded in STATUS_CATEGORIES:
        return folded
    return None


def normalize_scheduled_start(value: str | None) -> str | None:
    """定时筛选取值归一化。空白和未知值表示不过滤。"""
    if value is None or java_is_blank(value):
        return None
    folded = java_trim(value).upper()
    if folded in SCHEDULED_FILTERS:
        return folded
    return None


def keyword_text(value: str | None) -> str | None:
    """``String.trim`` 后为空则不按标题过滤。"""
    if value is None:
        return None
    trimmed = java_trim(value)
    if trimmed == "":
        return None
    return trimmed


def keyword_id(value: str) -> int | None:
    """全数字且落在 Java long 范围内时，同时按工单 id 匹配。"""
    for char in value:
        if char < "0" or char > "9":
            return None
    parsed = int(value)
    if parsed > LONG_MAX:
        return None
    return parsed


def contains_done_token(value: str | None) -> bool:
    """完成态文案。大小写按 Java ``toUpperCase`` 后包含判断。"""
    if value is None or java_is_blank(value):
        return False
    folded = value.upper()
    for token in _DONE_TOKENS:
        if token in folded:
            return True
    return False


def is_done_node(category: str | None, code: str | None, name: str | None) -> bool:
    """状态节点本身已完成。"""
    if category is not None and category.upper() == "DONE":
        return True
    if contains_done_token(code):
        return True
    return contains_done_token(name)


def is_terminal_dispatch(status: str | None) -> bool:
    """调度终态。空状态不是终态。"""
    if status is None:
        return False
    return status in TERMINAL_DISPATCH_STATUSES


def failure_label(status: str) -> str:
    """失败终态的中文标签。"""
    if status == "TIMEOUT":
        return "超时"
    if status == "CANCELED":
        return "被取消"
    return "失败"


def evaluate_health(
    category: str | None,
    dispatch_status: str | None,
    modified_ms: int | None,
    now_ms: int,
    stuck_threshold_ms: int,
) -> tuple[str, str | None]:
    """进行中的工单在最近一次调度失败或停住时标为 STUCK。"""
    if category != "IN_PROGRESS" or dispatch_status is None:
        return "OK", None
    if dispatch_status in FAILED_TERMINAL:
        label = failure_label(dispatch_status)
        reason = "最近一次执行" + label + "，流程已停止且无自动恢复，请人工介入"
        return "STUCK", reason
    if not is_terminal_dispatch(dispatch_status) and modified_ms is not None:
        idle = now_ms - modified_ms
        if idle > stuck_threshold_ms:
            minutes = idle // 60_000
            return "STUCK", f"执行已卡住超过 {minutes} 分钟无进展，请人工介入"
    return "OK", None


def scheduled_phase(
    scheduled_start_at: datetime | None,
    triggered_at: datetime | None,
    done: bool,
    latest_status: str | None,
    now: datetime,
) -> str | None:
    """从未定时的工单保持空阶段。待触发先于完成态。"""
    if scheduled_start_at is None and triggered_at is None:
        return None
    if scheduled_start_at is not None and scheduled_start_at > now:
        return "PENDING"
    if done:
        return "DONE"
    if latest_status is not None and not is_terminal_dispatch(latest_status):
        return "RUNNING"
    return "READY"


def pending_decision(assignee_type: str | None, latest_status: str | None, done: bool) -> bool:
    """人工持单、最近一次调度成功且未完成。"""
    if assignee_type != "HUMAN":
        return False
    if latest_status != "SUCCEEDED":
        return False
    if done:
        return False
    return True


def name_has(value: str | None, plain: tuple[str, ...], upper: tuple[str, ...]) -> bool:
    """状态名关键词。中文按原文，拉丁按大写。"""
    if value is None:
        return False
    for token in plain:
        if token in value:
            return True
    folded = value.upper()
    for token in upper:
        if token in folded:
            return True
    return False


def utf16_length(value: str) -> int:
    """Java ``String.length`` 的 UTF-16 码元数。"""
    return len(value.encode("utf-16-le")) // 2


def normalize_tags(tags: list[str | None] | None) -> list[str]:
    """去空白、去重并保持原顺序。超长或超过 20 个拒绝。"""
    if tags is None:
        return []
    seen: list[str] = []
    known: set[str] = set()
    for tag in tags:
        if tag is None:
            continue
        trimmed = java_trim(tag)
        if trimmed == "":
            continue
        if utf16_length(trimmed) > MAX_TAG_LENGTH:
            raise BizError(ErrorCode.PARAM_INVALID)
        if trimmed not in known:
            known.add(trimmed)
            seen.append(trimmed)
        if len(seen) > MAX_TAGS:
            raise BizError(ErrorCode.PARAM_INVALID)
    return seen


def parse_tags(value: object) -> list[str]:
    """库存标签。空白、坏 JSON 和非字符串数组按空列表读出。"""
    if value is None:
        return []
    raw = value
    if isinstance(value, str):
        if java_is_blank(value):
            return []
        try:
            raw = json.loads(value)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    parsed: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return []
        parsed.append(item)
    return parsed


def assignment_detail(from_type: str | None, to_type: str | None) -> dict[str, str] | None:
    """只记录 HUMAN/AGENT。两边都不是时不写详情。"""
    detail: dict[str, str] = {}
    if from_type == "HUMAN" or from_type == "AGENT":
        detail["fromType"] = from_type
    if to_type == "HUMAN" or to_type == "AGENT":
        detail["toType"] = to_type
    if len(detail) == 0:
        return None
    return detail


def is_after(moment: datetime, now: datetime) -> bool:
    """严格晚于当前时间，对齐 ``Date.after``。"""
    return moment > now
