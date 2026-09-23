"""debug 日志对象名。roleCode 按服务端 UTF-16 规则归一。"""

from autowonder.debuglogs.sanitizer import java_is_blank, java_strip

OBJECT_PREFIX = "debug/"
MAX_ROLE_CODE_RUNES = 64


def sanitize_role_code(role_code: str | None, agent_id: int) -> str:
    """非法字符换成 ``-``。补充平面字符按 UTF-16 长度换成同样多个 ``-``。"""
    if role_code is None or java_is_blank(role_code):
        return "agent-" + str(agent_id)
    stripped = java_strip(role_code)
    parts: list[str] = []
    kept = 0
    for char in stripped:
        if kept == MAX_ROLE_CODE_RUNES:
            break
        code_point = ord(char)
        kept += 1
        if _is_key_safe(code_point):
            parts.append(char)
        else:
            parts.append("-" * _utf16_len(code_point))
    return "".join(parts)


def workitem_object_key(workitem_id: int, role_code: str, run_no: int) -> str:
    """工单日志键：``debug/{workitemId}/{role}-run-{n}.log.gz``。"""
    return OBJECT_PREFIX + str(workitem_id) + "/" + _file_name(role_code, run_no)


def scheduled_object_key(task_id: int, run_id: int, role_code: str, run_no: int) -> str:
    """定时任务日志键：``debug/scheduled-{taskId}-run-{runId}/{role}-run-{n}.log.gz``。"""
    return (
        OBJECT_PREFIX
        + "scheduled-"
        + str(task_id)
        + "-run-"
        + str(run_id)
        + "/"
        + _file_name(role_code, run_no)
    )


def _file_name(role_code: str, run_no: int) -> str:
    return role_code + "-run-" + str(run_no) + ".log.gz"


def _utf16_len(code_point: int) -> int:
    if code_point > 0xFFFF:
        return 2
    return 1


def _is_key_safe(code_point: int) -> bool:
    if 65 <= code_point <= 90:
        return True
    if 97 <= code_point <= 122:
        return True
    if 48 <= code_point <= 57:
        return True
    return code_point == 95 or code_point == 45
