"""步骤检查项定义校验。执行结果不能反过来放宽检查项。"""

import json

from autowonder.core.errors import BizError, ErrorCode


def validate_checklist(raw: str | None) -> None:
    """空白检查项视为未配置。有内容时必须是文本或带 id、text 的对象数组。"""
    if raw is None or raw.strip() == "":
        return
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _invalid("不是合法的 JSON") from error
    if not isinstance(items, list):
        raise _invalid("必须为数组")
    seen: set[str] = set()
    for index, item in enumerate(items):
        if isinstance(item, str) and item.strip() != "":
            item_id = f"cl_{index}"
        elif isinstance(item, dict) and _text(item, "id") and _text(item, "text"):
            item_id = item["id"]
            if "allowNotApplicable" in item and not isinstance(item["allowNotApplicable"], bool):
                raise _invalid(f"检查项 {item_id} 的 allowNotApplicable 必须为布尔值")
            if item.get("allowNotApplicable") is True and not _text(item, "notApplicableWhen"):
                raise _invalid(f"检查项 {item_id} 允许不适用时必须填写 notApplicableWhen 条件")
        else:
            raise _invalid(f"第 {index + 1} 项必须为非空文本或包含非空 id、text 的对象")
        if item_id in seen:
            raise _invalid(f"检查项 id 重复: {item_id}")
        seen.add(item_id)


def _text(item: dict[str, object], field: str) -> bool:
    value = item.get(field)
    if not isinstance(value, str):
        return False
    return value.strip() != ""


def _invalid(message: str) -> BizError:
    return BizError(ErrorCode.PARAM_INVALID, "checklistJson " + message)
