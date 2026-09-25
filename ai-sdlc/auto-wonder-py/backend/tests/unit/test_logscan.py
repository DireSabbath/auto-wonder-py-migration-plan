"""logscan 把 ERROR / WARN 归到夹具保存的 request_id。"""

import json
from pathlib import Path

from verify.logscan import logscan


def test_attributed_json_warning_is_clean(tmp_path: Path) -> None:
    app_log = tmp_path / "app.log"
    app_log.write_text(_json_line("WARNING", "rid-1", "duplicate name") + "\n", encoding="utf-8")
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "register.json").write_text(
        json.dumps({"request_id": "rid-1"}),
        encoding="utf-8",
    )
    verdict = logscan(app_log, bodies, None, 0, 0)
    assert verdict["ok"] is True
    assert verdict["warnTotal"] == 1
    assert verdict["warnAttributed"] == 1
    assert verdict["warnUnattributed"] == 0


def test_unattributed_error_fails(tmp_path: Path) -> None:
    app_log = tmp_path / "app.log"
    app_log.write_text(_json_line("ERROR", "", "runPending failed") + "\n", encoding="utf-8")
    verdict = logscan(app_log, None, None, 0, 0)
    assert verdict["ok"] is False
    assert verdict["errorUnattributed"] == 1
    assert verdict["harnessRequestIds"] == 0


def test_java_pipe_warn_attributes(tmp_path: Path) -> None:
    app_log = tmp_path / "app.log"
    app_log.write_text(
        "2026-09-24 12:00:00.123|WARN|rid-2|GET /api/x|log|thread|duplicate\n",
        encoding="utf-8",
    )
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "probe.json").write_text(
        json.dumps({"request_id": "rid-2"}),
        encoding="utf-8",
    )
    verdict = logscan(app_log, bodies, None, 0, 0)
    assert verdict["ok"] is True
    assert verdict["warnAttributed"] == 1


def test_info_message_containing_error_word_is_ignored(tmp_path: Path) -> None:
    app_log = tmp_path / "app.log"
    app_log.write_text(
        _json_line("INFO", "rid-3", "ERROR was only in the message") + "\n",
        encoding="utf-8",
    )
    verdict = logscan(app_log, None, None, 0, 0)
    assert verdict["ok"] is True
    assert verdict["errorTotal"] == 0


def test_plain_error_word_is_unattributed(tmp_path: Path) -> None:
    app_log = tmp_path / "app.log"
    app_log.write_text("something ERROR happened\n", encoding="utf-8")
    verdict = logscan(app_log, None, None, 0, 0)
    assert verdict["ok"] is False
    assert verdict["errorUnattributed"] == 1


def test_redacted_body_does_not_explain_a_warning(tmp_path: Path) -> None:
    app_log = tmp_path / "app.log"
    app_log.write_text(_json_line("WARN", "rid-4", "rejected") + "\n", encoding="utf-8")
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "redacted-login.json").write_text(
        json.dumps({"request_id": "rid-4"}),
        encoding="utf-8",
    )
    verdict = logscan(app_log, bodies, None, 0, 0)
    assert verdict["ok"] is False
    assert verdict["harnessRequestIds"] == 0


def test_start_line_skips_earlier_warning(tmp_path: Path) -> None:
    app_log = tmp_path / "app.log"
    app_log.write_text(
        _json_line("WARNING", "", "startup") + "\n" + _json_line("INFO", "rid-5", "ready") + "\n",
        encoding="utf-8",
    )
    verdict = logscan(app_log, None, None, 1, 0)
    assert verdict["ok"] is True
    assert verdict["warnTotal"] == 0
    assert verdict["appLogStartLine"] == 1


def test_missing_log_fails(tmp_path: Path) -> None:
    verdict = logscan(tmp_path / "missing.log", None, None, 0, 0)
    assert verdict["ok"] is False
    assert verdict["detail"] == "no application log"


def _json_line(level: str, request_id: str, message: str) -> str:
    payload: dict[str, object] = {
        "level": level,
        "logger": "autowonder.test",
        "message": message,
    }
    if request_id != "":
        payload["request_id"] = request_id
    return json.dumps(payload)
