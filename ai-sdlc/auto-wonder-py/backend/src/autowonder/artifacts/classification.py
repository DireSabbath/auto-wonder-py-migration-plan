"""产物展示分类。只补全历史 FILE，已标明的类型保持原样。"""

import unicodedata


def resolve_artifact_type(artifact_type: str | None, name: str | None) -> str:
    """空白或 FILE 按路径分类，其余类型原样返回。"""
    if artifact_type is None or _is_blank(artifact_type):
        return classify_artifact(name)
    if _java_trim(artifact_type).lower() == "file":
        return classify_artifact(name)
    return artifact_type


def classify_artifact(name: str | None) -> str:
    """按产物路径前缀给出展示类型。"""
    if name is None:
        return "FILE"
    path = name.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("artifacts/"):
        path = path[len("artifacts/") :]
    if path.startswith("attempts/"):
        return "SNAPSHOT"
    if path.startswith("output/"):
        path = path[len("output/") :]
    if path.startswith("result/") or path.startswith("logs/") or path.startswith("traces/"):
        return "RUNTIME"
    if path.startswith("debug/"):
        return "DEBUG_LOG"
    if path.startswith("deliverables/"):
        return "DELIVERABLE"
    if path.startswith("patches/"):
        return "PATCH"
    if path.startswith("evidence/"):
        return "EVIDENCE"
    if path.startswith("handoff/"):
        return "HANDOFF"
    if path.startswith("learning_delta/"):
        return "LEARNING"
    return "FILE"


def user_visible(name: str | None) -> bool:
    """观测目录不出现在用户可见的产物列表里。"""
    if name is None:
        return True
    if name.startswith("observability/"):
        return False
    if "/observability/" in name:
        return False
    return True


def _is_blank(text: str) -> bool:
    for char in text:
        if not _is_java_whitespace(ord(char)):
            return False
    return True


def _is_java_whitespace(code_point: int) -> bool:
    if code_point in {0x00A0, 0x2007, 0x202F}:
        return False
    if code_point in {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F}:
        return True
    category = _category(code_point)
    if category == "Zs" or category == "Zl" or category == "Zp":
        return True
    return False


def _category(code_point: int) -> str:
    return unicodedata.category(chr(code_point))


def _java_trim(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and ord(text[start]) <= 0x20:
        start += 1
    while end > start and ord(text[end - 1]) <= 0x20:
        end -= 1
    return text[start:end]
