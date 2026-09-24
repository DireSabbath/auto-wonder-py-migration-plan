"""在两个正在跑的 API 上走完四条 CLI 场景。

Java 和 Python 的 AI worker 都要指向同一支假 CLI。
会话由各自的队列工人执行，验收脚本只走 HTTP。
"""

import json
import subprocess
import time
from pathlib import Path

import httpx


def cli_dual(python_url: str, java_url: str) -> dict[str, object]:
    """比较澄清文本、SDLC 名称、记忆标题和仓库扫描状态。"""
    chain = _Chain()
    try:
        chain.walk(java_url.rstrip("/"), python_url.rstrip("/"))
    finally:
        chain.close()
    return chain.verdict()


class _Check:
    def __init__(self, name: str, passed: bool, note: str) -> None:
        self.name = name
        self.passed = passed
        self.note = note

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "pass": self.passed, "note": self.note}


class _Chain:
    def __init__(self) -> None:
        self.client = httpx.Client(timeout=60.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self._document: object = None

    def close(self) -> None:
        self.client.close()

    def walk(self, java_url: str, python_url: str) -> None:
        java = self._stack("java", java_url)
        if self.stopped != "":
            return
        python = self._stack("python", python_url)
        if self.stopped != "":
            return
        self._expect(
            "outcomes",
            java == python,
            "clarification, SDLC, memory and repo scan match",
        )

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "cli-dual",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    def _stack(self, name: str, origin: str) -> dict[str, object] | None:
        # 两个栈的工作目录都是 /tmp/aiw/{sessionId}。先挪走旧目录，避免后一次克隆撞上已有检出。
        _park_workdirs()
        username = "aw-cli-" + name + "-" + str(int(time.time()))
        password = "cli-dual-pass-1"
        token = self._login_workspace(name, origin, username, password)
        if token is None:
            return None
        workitem_id = self._workitem(name, origin, token)
        if workitem_id is None:
            return None
        repo_id = self._repo(name, origin, token)
        if repo_id is None:
            return None
        scenes = {
            "CLARIFICATION": {
                "scene": "CLARIFICATION",
                "bizRefType": "WORKITEM",
                "bizRefId": int(workitem_id),
                "input": "请把这段需求澄清成可执行说明",
            },
            "SDLC_GEN": {"scene": "SDLC_GEN", "input": "直接生成一个内部流程"},
            "MEMORY_IMPORT": {
                "scene": "MEMORY_IMPORT",
                "input": "请提炼记忆条目：锁要有过期时间",
            },
            "REPO_SCAN": {
                "scene": "REPO_SCAN",
                "bizRefType": "REPO",
                "bizRefId": int(repo_id),
            },
        }
        results: dict[str, str] = {}
        for scene, body in scenes.items():
            session_id = self._create(name, origin, token, scene, body)
            if session_id is None:
                return None
            ready = self._wait(name, origin, token, scene, session_id)
            if ready is None:
                return None
            results[scene] = ready
        for scene, result_json in results.items():
            self._confirm(name, origin, token, scene, result_json)
            if self.stopped != "":
                return None
        return self._outcomes(name, origin, token, workitem_id, repo_id)

    def _login_workspace(
        self,
        name: str,
        origin: str,
        username: str,
        password: str,
    ) -> str | None:
        self._api(
            "POST",
            origin + "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": username + "@example.invalid",
                "nickname": "AW CLI",
            },
        )
        self._expect(
            name + "_register",
            _positive_id(_json_get(self._document, "data.id")),
            "user id",
        )
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
            {"name": "AW CLI " + username, "description": "cli scenes"},
        )
        workspace_id = _json_get(self._document, "data.id")
        self._expect(name + "_workspace", _positive_id(workspace_id), "workspace id")
        if self.stopped != "":
            return None
        self._api("POST", origin + "/api/workspaces/" + workspace_id + "/switch", token, None)
        token = _json_get(self._document, "data.accessToken")
        self._expect(name + "_switch", token != "", "workspace token")
        if token == "":
            return None
        return token

    def _workitem(self, name: str, origin: str, token: str) -> str | None:
        self._api(
            "POST",
            origin + "/api/workitems",
            token,
            {
                "workType": "REQ",
                "title": "登录失败",
                "contentMd": "用户无法登录",
                "priority": 2,
            },
        )
        workitem_id = _json_get(self._document, "data.id")
        self._expect(name + "_workitem", _positive_id(workitem_id), "work item id")
        if workitem_id == "":
            return None
        return workitem_id

    def _repo(self, name: str, origin: str, token: str) -> str | None:
        path = _git_repo(name)
        self._api(
            "POST",
            origin + "/api/repos",
            token,
            {
                "name": "cli-scene-repo",
                "url": path.as_uri(),
                "defaultBranch": "main",
                "description": "local clone source",
            },
        )
        repo_id = _json_get(self._document, "data.id")
        self._expect(name + "_repo", _positive_id(repo_id), "repo id")
        if repo_id == "":
            return None
        return repo_id

    def _create(
        self,
        name: str,
        origin: str,
        token: str,
        scene: str,
        body: dict[str, object],
    ) -> str | None:
        self._api("POST", origin + "/api/ai/sessions", token, body)
        session_id = _json_get(self._document, "data")
        self._expect(name + "_" + scene + "_queued", _positive_id(session_id), "session id")
        if session_id == "":
            return None
        self.facts[name + scene + "SessionId"] = session_id
        return session_id

    def _wait(
        self,
        name: str,
        origin: str,
        token: str,
        scene: str,
        session_id: str,
    ) -> str | None:
        status = ""
        error = ""
        deadline = time.time() + 45
        while time.time() < deadline:
            self._api("GET", origin + "/api/ai/sessions/" + session_id, token, None)
            status = _json_get(self._document, "data.status")
            error = _json_get(self._document, "data.error")
            if status in {"WAIT_USER", "FAILED", "CANCELED", "COMPLETED"}:
                break
            time.sleep(0.5)
        self.facts[name + scene + "Status"] = status
        self._expect(
            name + "_" + scene + "_wait",
            status == "WAIT_USER",
            status + " " + error,
        )
        if status != "WAIT_USER":
            return None
        return _result_text(self._document)

    def _confirm(
        self,
        name: str,
        origin: str,
        token: str,
        scene: str,
        result_json: str,
    ) -> None:
        session_id = str(self.facts[name + scene + "SessionId"])
        self._api(
            "POST",
            origin + "/api/ai/sessions/" + session_id + "/confirm",
            token,
            {"resultJson": result_json},
        )
        confirmed = _json_get(self._document, "success") == "true"
        note = "confirm stores the result"
        if not confirmed:
            note = _json_get(self._document, "code") + " " + _json_get(self._document, "message")
        self._expect(name + "_" + scene + "_confirm", confirmed, note)

    def _outcomes(
        self,
        name: str,
        origin: str,
        token: str,
        workitem_id: str,
        repo_id: str,
    ) -> dict[str, object] | None:
        self._api(
            "GET",
            origin + "/api/workitems/" + workitem_id + "/clarification",
            token,
            None,
        )
        clarified = _json_get(self._document, "data.contentMd")
        self._api("GET", origin + "/api/sdlcs?status=DRAFT", token, None)
        sdlc = _has_name(self._document, "cli-scene-sdlc")
        self._api("GET", origin + "/api/memories?status=PENDING", token, None)
        memory = _has_title(self._document, "锁要过期")
        self._api("GET", origin + "/api/repos/" + repo_id, token, None)
        scan = _json_get(self._document, "data.scanStatus")
        self.facts[name + "Clarification"] = clarified
        self.facts[name + "Sdlc"] = sdlc
        self.facts[name + "Memory"] = memory
        self.facts[name + "Repo"] = scan
        outcome = {
            "clarification": clarified,
            "sdlc": sdlc,
            "memory": memory,
            "repo": scan,
        }
        expected = {
            "clarification": "登录失败需要复现步骤",
            "sdlc": True,
            "memory": True,
            "repo": "CONCLUDED",
        }
        self._expect(name + "_saved", outcome == expected, json.dumps(outcome, ensure_ascii=False))
        if outcome != expected:
            return None
        return outcome

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
            self._document = None
            return
        self._document = response.json()


def _park_workdirs() -> None:
    root = Path("/tmp/aiw")
    root.mkdir(exist_ok=True)
    root.rename(Path("/tmp") / ("aiw-parked-" + str(time.time_ns())))


def _git_repo(name: str) -> Path:
    path = Path("/tmp/aw-cli-repo-" + name + "-" + str(int(time.time())))
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "aw@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "AW"],
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("locks\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return path


def _result_text(document: object) -> str:
    current = _child(document, "data.resultJson")
    if isinstance(current, str):
        return current
    return json.dumps(current, ensure_ascii=False)


def _has_name(document: object, name: str) -> bool:
    return _field(document, "name", name)


def _has_title(document: object, title: str) -> bool:
    return _field(document, "title", title)


def _field(document: object, key: str, expected: str) -> bool:
    if not isinstance(document, dict):
        return False
    data = document.get("data")
    if not isinstance(data, list):
        return False
    for item in data:
        if isinstance(item, dict) and item.get(key) == expected:
            return True
    return False


def _child(document: object, dotted: str) -> object:
    current = document
    for key in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _json_get(document: object, dotted: str) -> str:
    current = _child(document, dotted)
    if current is None:
        return ""
    if isinstance(current, bool):
        if current:
            return "true"
        return "false"
    if isinstance(current, dict | list):
        return json.dumps(current, ensure_ascii=False)
    return str(current)


def _positive_id(value: str) -> bool:
    return value.isdecimal() and value != "0"
