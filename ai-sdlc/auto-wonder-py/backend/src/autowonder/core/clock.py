"""业务时间的单点：Asia/Shanghai 的 naive 本地时间。"""

from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_local() -> datetime:
    """返回当前上海本地时间，不带 tzinfo，对齐 MySQL DATETIME。"""
    return datetime.now(SHANGHAI).replace(tzinfo=None)
