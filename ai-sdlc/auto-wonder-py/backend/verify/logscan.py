"""把应用日志里的 ERROR / WARN 归到夹具保存的 request_id 上。

对得上的诊断由这次夹具调用解释。对不上的 ERROR 或 WARN 让扫描失败。
JSON 行认 ``level`` 与 ``request_id``；Java 的管道布局和时间戳布局仍然认。
"""

import json
import re
from pathlib import Path

_PIPE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d+)\|(?P<level>[A-Z]+)\|"
    r"(?P<rid>[^|]*)\|(?P<endpoint>[^|]*)\|(?P<logger>[^|]*)\|"
    r"(?P<thread>[^|]*)\|(?P<msg>.*)$"
)
_STAMPED = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d+)\s+"
    r"(?P<level>ERROR|WARN|INFO|DEBUG|TRACE)\s+(?P<msg>.*)$"
)
_STACK = re.compile(r"^\s*(at\s|Caused by:|\.\.\.\s\d+\smore)|Exception|Throwable")
_WORD_ERROR = re.compile(r"\bERROR\b")
_WORD_WARN = re.compile(r"\bWARN\b")
_SHOWN = 30


def logscan(
    app_log: Path,
    bodies_dir: Path | None,
    file_log: Path | None,
    app_start: int,
    file_start: int,
) -> dict[str, object]:
    """扫描应用日志，未归到夹具 request_id 的 ERROR / WARN 记为失败。"""
    if not app_log.is_file():
        return {
            "command": "logscan",
            "ok": False,
            "detail": "no application log",
            "appLog": str(app_log),
        }
    calls = _request_ids(bodies_dir)
    app_lines, app_from = _window(_read_lines(app_log), app_start)
    file_lines: list[str] = []
    file_from = 0
    file_text = ""
    if file_log is not None:
        file_text = str(file_log)
        file_lines, file_from = _window(_read_lines(file_log), file_start)
    attributed: list[str] = []
    unattributed: list[str] = []
    counts = {"ERROR": 0, "WARN": 0}
    attributed_counts = {"ERROR": 0, "WARN": 0}
    stack = 0
    startup: list[str] = []
    sources = (
        ("stdout", app_lines, app_from),
        ("file", file_lines, file_from),
    )
    for source, lines, start in sources:
        for offset, line in enumerate(lines, start + 1):
            if _STACK.search(line) is not None:
                stack += 1
            if "Started Bootstrap" in line or "Tomcat started on port" in line:
                startup.append(f"{source}:{offset}|{line}")
            hit = _diagnostic(line)
            if hit is None:
                continue
            level, request_id, endpoint, message = hit
            counts[level] += 1
            call = ""
            if request_id != "":
                found = calls.get(request_id)
                if found is not None:
                    call = found
            if call != "":
                attributed_counts[level] += 1
                attributed.append(
                    f"ATTRIB|{level}|{source}:{offset}|rid={request_id}|call={call}"
                    f"|endpoint={endpoint}|{message}"
                )
            else:
                rid_text = request_id
                if rid_text == "":
                    rid_text = "<none>"
                endpoint_text = endpoint
                if endpoint_text == "":
                    endpoint_text = "<none>"
                unattributed.append(
                    f"UNATTRIB|{level}|{source}:{offset}|rid={rid_text}"
                    f"|endpoint={endpoint_text}|{line}"
                )
    error_unattributed = _count_prefix(unattributed, "UNATTRIB|ERROR")
    warn_unattributed = _count_prefix(unattributed, "UNATTRIB|WARN")
    return {
        "command": "logscan",
        "ok": error_unattributed == 0 and warn_unattributed == 0,
        "appLog": str(app_log),
        "appLogLines": len(app_lines),
        "appLogStartLine": app_from,
        "fileLog": file_text,
        "fileLogLines": len(file_lines),
        "fileLogStartLine": file_from,
        "harnessRequestIds": len(calls),
        "errorTotal": counts["ERROR"],
        "warnTotal": counts["WARN"],
        "errorAttributed": attributed_counts["ERROR"],
        "warnAttributed": attributed_counts["WARN"],
        "errorUnattributed": error_unattributed,
        "warnUnattributed": warn_unattributed,
        "stackAndExceptionLines": stack,
        "startupBanner": startup,
        "attributed": _shown(attributed),
        "unattributed": _shown(unattributed),
    }


def _request_ids(bodies_dir: Path | None) -> dict[str, str]:
    calls: dict[str, str] = {}
    if bodies_dir is None or not bodies_dir.is_dir():
        return calls
    for path in sorted(bodies_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if not name.endswith(".json") or name.startswith("redacted-"):
            continue
        document = _load_json(path)
        if not isinstance(document, dict):
            continue
        raw = document.get("request_id")
        if not isinstance(raw, str) or raw == "":
            continue
        calls.setdefault(raw, name[: -len(".json")])
    return calls


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", errors="replace") as stream:
        return stream.read().splitlines()


def _window(lines: list[str], start: int) -> tuple[list[str], int]:
    if start < 0 or start > len(lines):
        return lines, 0
    return lines[start:], start


def _diagnostic(line: str) -> tuple[str, str, str, str] | None:
    parsed = _json_line(line)
    if parsed is not None:
        level = _json_level(parsed.get("level"))
        if level is None:
            return None
        return level, _text(parsed.get("request_id")), "", _text(parsed.get("message"))
    matched = _PIPE.match(line)
    if matched is not None:
        level = matched.group("level")
        if level == "ERROR" or level == "WARN":
            return level, matched.group("rid"), matched.group("endpoint"), matched.group("msg")
        return None
    stamped = _STAMPED.match(line)
    if stamped is not None and stamped.group("level") in ("ERROR", "WARN"):
        return stamped.group("level"), "", "", stamped.group("msg")
    if _WORD_ERROR.search(line) is not None:
        return "ERROR", "", "", line
    if _WORD_WARN.search(line) is not None:
        return "WARN", "", "", line
    return None


def _json_line(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _json_level(value: object) -> str | None:
    if value == "ERROR":
        return "ERROR"
    if value == "WARN" or value == "WARNING":
        return "WARN"
    return None


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _count_prefix(rows: list[str], prefix: str) -> int:
    total = 0
    for row in rows:
        if row.startswith(prefix):
            total += 1
    return total


def _shown(rows: list[str]) -> list[str]:
    if len(rows) > _SHOWN:
        return rows[:_SHOWN]
    return rows
