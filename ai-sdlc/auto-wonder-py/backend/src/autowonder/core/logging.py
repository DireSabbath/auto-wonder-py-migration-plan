"""JSON 日志。应用在启动时调用 ``configure_logging``。"""

import json
import logging

from autowonder.core.context import current_request_id


class JsonFormatter(logging.Formatter):
    """把日志记录收成一行 JSON，并带上当前 request_id。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": current_request_id(),
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """把根日志配置成 JSON 行。"""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
