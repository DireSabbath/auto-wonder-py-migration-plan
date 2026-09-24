"""在 Java 和 Python 两个正在运行的 API 上各投出一条评论写回。

两边都要打开 Aone。假服务收评论，状态模版查询只回失败。
生成的用户编号和操作标记不参与正文比较。
"""

import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote_plus

import httpx

from autowonder.integrations.aone_codec import sign_aone

_COMMENT = "outbox-live-body"
_SECRET = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_CLIENT_KEY = "aw-outbox"
_JAVA_DB_PORT = 33060
_PYTHON_DB_PORT = 33061


def outbox_dual(python_url: str, java_url: str) -> dict[str, object]:
    """同一假 Aone 上比较两边的写回请求、成功状态和出站关联。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    chain = _Chain("http://" + str(host) + ":" + str(port))
    try:
        chain.walk(java_url.rstrip("/"), python_url.rstrip("/"))
    finally:
        chain.close()
        server.shutdown()
    return chain.verdict()


class _Handler(BaseHTTPRequestHandler):
    hits: list[dict[str, str]] = []

    def do_GET(self) -> None:
        self._take("GET")

    def do_POST(self) -> None:
        self._take("POST")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _take(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        path = self.path.split("?", 1)[0]
        _Handler.hits.append(
            {
                "method": method,
                "path": path,
                "ctype": self.headers.get("Content-Type", ""),
                "clientKey": self.headers.get("clientKey", ""),
                "region": self.headers.get("Ao-Region-Id", ""),
                "timestamp": self.headers.get("timestamp", ""),
                "signature": self.headers.get("signature", ""),
                "body": body,
            }
        )
        if path.endswith("/createComment"):
            payload = b'{"success":true,"result":{"id":88001}}'
        else:
            payload = b'{"success":false,"message":"bootstrap-unused"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Check:
    def __init__(self, name: str, passed: bool, note: str) -> None:
        self.name = name
        self.passed = passed
        self.note = note

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "pass": self.passed, "note": self.note}


class _Chain:
    def __init__(self, aone_base: str) -> None:
        self.aone_base = aone_base
        self.client = httpx.Client(timeout=30.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self._java_view: dict[str, object] | None = None
        self._python_view: dict[str, object] | None = None

    def close(self) -> None:
        self.client.close()

    def walk(self, java_url: str, python_url: str) -> None:
        self._java_view = self._stack("java", java_url, _JAVA_DB_PORT)
        if self.stopped != "":
            return
        self._python_view = self._stack("python", python_url, _PYTHON_DB_PORT)
        if self.stopped != "":
            return
        self._expect(
            "wire",
            self._java_view == self._python_view,
            "createComment path, charset, fields and normalized body match",
        )

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "outbox-dual",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    def _stack(self, name: str, origin: str, db_port: int) -> dict[str, object] | None:
        before = len(_Handler.hits)
        username = "aw-dual-" + name + "-" + str(int(time.time()))
        password = "dual-outbox-pass-1"
        self._api(
            "POST",
            origin + "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": username + "@example.invalid",
                "nickname": "AW Outbox",
            },
        )
        user_id = _json_get(self._document, "data.id")
        self._expect(name + "_register", _positive_id(user_id), "user id")
        if self.stopped != "":
            return None
        self._api(
            "POST",
            origin + "/api/auth/login",
            None,
            {"username": username, "password": password},
        )
        token = _json_get(self._document, "data.accessToken")
        self._expect(name + "_login", token != "", "access token")
        if self.stopped != "":
            return None
        self._api(
            "POST",
            origin + "/api/workspaces",
            token,
            {"name": "AW Outbox " + username, "description": "outbox"},
        )
        workspace_id = _json_get(self._document, "data.id")
        self._expect(name + "_workspace", _positive_id(workspace_id), "workspace id")
        if self.stopped != "":
            return None
        self._api("POST", origin + "/api/workspaces/" + workspace_id + "/switch", token, None)
        token = _json_get(self._document, "data.accessToken")
        self._expect(name + "_switch", token != "", "workspace token")
        if self.stopped != "":
            return None
        self._api(
            "POST",
            origin + "/api/workitems",
            token,
            {
                "workType": "REQ",
                "title": "outbox writeback",
                "contentMd": "send the comment",
                "priority": 2,
            },
        )
        workitem_id = _json_get(self._document, "data.id")
        self._expect(name + "_workitem", _positive_id(workitem_id), "work item id")
        if self.stopped != "":
            return None
        self._api(
            "POST",
            origin + "/api/integrations/aone/bindings",
            token,
            {
                "baseUrl": self.aone_base,
                "clientKey": _CLIENT_KEY,
                "accessSecret": _SECRET,
                "regionId": "1",
                "externalProjectId": "proj-outbox",
                "externalProjectName": "outbox",
                "writebackStaffId": "staff-1",
                "enabled": True,
            },
        )
        binding_id = _json_get(self._document, "data.id")
        self._expect(name + "_binding", _positive_id(binding_id), "binding id")
        if self.stopped != "":
            return None
        _sql(
            db_port,
            "INSERT INTO external_workitem_link "
            "(tenant_id, provider, binding_id, external_project_id, "
            "external_workitem_id, workitem_id) VALUES ("
            + workspace_id
            + ", 'AONE', "
            + binding_id
            + ", 'proj-outbox', 'WI-outbox', "
            + workitem_id
            + ")",
        )
        self._api(
            "POST",
            origin + "/api/workitems/" + workitem_id + "/comments",
            token,
            {"contentMd": _COMMENT},
        )
        comment_id = _json_get(self._document, "data.id")
        self._expect(name + "_comment", _positive_id(comment_id), "comment id")
        if self.stopped != "":
            _disable(db_port, binding_id)
            return None
        self._api(
            "POST",
            origin + "/api/integrations/aone/outbox/dispatch-now?limit=20",
            token,
            None,
        )
        row = ""
        for _ in range(12):
            row = _sql(
                db_port,
                "SELECT status FROM integration_outbox WHERE workitem_id=" + workitem_id,
            )
            if row == "SUCCEEDED":
                break
            time.sleep(0.5)
        link = _sql(
            db_port,
            "SELECT CONCAT(direction, ' ', external_comment_id) FROM external_comment_link "
            "WHERE workitem_comment_id=" + comment_id,
        )
        _disable(db_port, binding_id)
        creates = [
            item for item in _Handler.hits[before:] if item["path"].endswith("/createComment")
        ]
        self.facts[name + "Status"] = row
        self.facts[name + "Link"] = link
        self._expect(name + "_delivered", row == "SUCCEEDED" and len(creates) == 1, row)
        if self.stopped != "":
            return None
        self._expect(name + "_linked", link == "OUTBOUND 88001", link)
        if self.stopped != "":
            return None
        signed = _signature_ok(creates[0])
        self._expect(name + "_signature", signed, "Aone signature header matches the timestamp")
        if self.stopped != "":
            return None
        return _view(creates[0])

    def _expect(self, name: str, passed: bool, note: str) -> None:
        self.checks.append(_Check(name, passed, note))
        if passed:
            self.pass_count += 1
            return
        self.fail_count += 1
        if self.stopped == "":
            self.stopped = name

    def _api(
        self,
        method: str,
        url: str,
        token: str | None,
        body: dict[str, object] | None,
    ) -> None:
        headers: dict[str, str] = {}
        content = None
        if token is not None and token != "":
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body).encode()
        response = self.client.request(method, url, headers=headers, content=content)
        if response.content == b"":
            self._document: object = None
            return
        self._document = response.json()


def _view(item: dict[str, str]) -> dict[str, object]:
    form = {key: values[0] for key, values in parse_qs(item["body"]).items()}
    form["content"] = _norm(unquote_plus(form["content"]))
    return {
        "path": item["path"],
        "ctype": item["ctype"],
        "clientKey": item["clientKey"],
        "region": item["region"],
        "form": form,
    }


def _norm(text: str) -> str:
    text = re.sub(r"（ID: \d+）", "（ID:{id}）", text)
    return re.sub(r"<!-- aw-op:[0-9a-f]+ -->", "<!-- aw-op:{key} -->", text)


def _signature_ok(item: dict[str, str]) -> bool:
    timestamp = item["timestamp"]
    if timestamp == "":
        return False
    expected = sign_aone(_CLIENT_KEY, _SECRET, int(timestamp))
    return expected == item["signature"]


def _sql(port: int, statement: str) -> str:
    proc = subprocess.run(
        [
            "mysql",
            "--protocol=TCP",
            "-h127.0.0.1",
            "-P" + str(port),
            "-uroot",
            "-pautowonder",
            "autowonder",
            "-N",
            "-e",
            statement,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _disable(port: int, binding_id: str) -> None:
    _sql(port, "UPDATE external_project_binding SET enabled=0 WHERE id=" + binding_id)


def _json_get(document: object, dotted: str) -> str:
    current = document
    for key in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return ""
    if current is None:
        return ""
    return str(current)


def _positive_id(value: str) -> bool:
    return value.isdecimal() and value != "0"
