"""用假 CLI 跑澄清、SDLC 生成、仓库扫描和记忆导入。

令牌只留在这次进程里。
"""

import asyncio
import json
import os
import secrets
import stat
import subprocess
import time
from pathlib import Path

import httpx

from autowonder.ai.worker import QUEUE, execute_session
from autowonder.core.redis import redis_client

# keyBusiness、upstreams、downstreams 在库里是 JSON 列。Java 按字符串原样插入，
# 所以这里写成数组，Fastjson getString 得到的文本本身仍是合法 JSON。
_FAKE_CLI = r"""#!/usr/bin/env python3
import json
import sys

prompt = ""
if "-p" in sys.argv:
    prompt = sys.argv[sys.argv.index("-p") + 1]
if "请扫描本地仓库" in prompt:
    body = {
        "purpose": "demo",
        "keyBusiness": ["locks"],
        "upstreams": ["api"],
        "downstreams": ["db"],
        "summaryMd": "scanned",
    }
elif "SDLC workflow" in prompt:
    body = {
        "name": "cli-scene-sdlc",
        "description": "generated",
        "steps": [
            {
                "order": 1,
                "name": "实现",
                "kind": "implementation",
                "instructionMd": "write the change",
                "checklist": ["diff"],
                "gatePolicy": {"passCriteria": "tests"},
                "required": True,
                "timeoutSeconds": 600,
                "retryBudget": 1,
            }
        ],
    }
elif "记忆条目" in prompt:
    body = {"items": [{"type": "工程规则", "title": "锁要过期", "contentMd": "set px"}]}
else:
    body = {"clarificationMd": "登录失败需要复现步骤"}
text = json.dumps(body, ensure_ascii=False)
event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
print(json.dumps(event, ensure_ascii=False), flush=True)
print(json.dumps({"type": "result", "session_id": "cli-scene", "result": text}), flush=True)
"""


def cli_scenes(base_url: str) -> dict[str, object]:
    """创建四条会话，跑完 CLI，再确认落库。"""
    return asyncio.run(_run(base_url.rstrip("/")))


async def _run(base_url: str) -> dict[str, object]:
    chain = _Chain(base_url)
    try:
        await chain.walk()
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
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.client = httpx.Client(timeout=60.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self._access = ""
        self._document: object = None

    def close(self) -> None:
        self.client.close()

    async def walk(self) -> None:
        username = "aw-cli-" + str(int(time.time()))
        password = secrets.token_urlsafe(18)
        self.facts["testUser"] = username
        self._register(username, password)
        if self.stopped != "":
            return
        self._login(username, password)
        if self.stopped != "":
            return
        workspace_id = self._workspace(username)
        if self.stopped != "":
            return
        self.facts["workspaceId"] = workspace_id
        workitem_id = self._workitem()
        if self.stopped != "":
            return
        repo_id = self._repo()
        if self.stopped != "":
            return
        binary = _install_fake_cli()
        os.environ["AUTOWONDER_AI_CLI_BINARY"] = str(binary)
        scenes = {
            "CLARIFICATION": {
                "scene": "CLARIFICATION",
                "bizRefType": "WORKITEM",
                "bizRefId": int(workitem_id),
                "input": "请把这段需求澄清成可执行说明",
            },
            "SDLC_GEN": {"scene": "SDLC_GEN", "input": "直接生成一个内部流程"},
            "MEMORY_IMPORT": {"scene": "MEMORY_IMPORT", "input": "请提炼记忆条目：锁要有过期时间"},
            "REPO_SCAN": {"scene": "REPO_SCAN", "bizRefType": "REPO", "bizRefId": int(repo_id)},
        }
        results: dict[str, str] = {}
        for name, body in scenes.items():
            session_id = self._create_session(name, body)
            if self.stopped != "":
                return
            await redis_client().lrem(QUEUE, 0, session_id)
            await execute_session(int(session_id), None)
            self._api("GET", "/api/ai/sessions/" + session_id, self._access, None)
            status = _json_get(self._document, "data.status")
            self.facts[name + "Status"] = status
            self._expect(name + "_wait", status == "WAIT_USER", "session waits for confirmation")
            if self.stopped != "":
                return
            results[name] = _result_text(self._document)
        self._confirm("CLARIFICATION", results["CLARIFICATION"])
        self._confirm("SDLC_GEN", results["SDLC_GEN"])
        self._confirm("MEMORY_IMPORT", results["MEMORY_IMPORT"])
        self._confirm("REPO_SCAN", results["REPO_SCAN"])
        if self.stopped != "":
            return
        self._api("GET", "/api/workitems/" + workitem_id + "/clarification", self._access, None)
        clarified = _json_get(self._document, "data.contentMd")
        self._expect(
            "clarification_saved",
            clarified == "登录失败需要复现步骤",
            "clarification text is stored",
        )
        self._api("GET", "/api/sdlcs?status=DRAFT", self._access, None)
        self._expect(
            "sdlc_saved",
            _has_name(self._document, "cli-scene-sdlc"),
            "generated SDLC is listed",
        )
        self._api("GET", "/api/memories?status=PENDING", self._access, None)
        self._expect(
            "memory_saved",
            _has_title(self._document, "锁要过期"),
            "imported memory is listed",
        )
        self._api("GET", "/api/repos/" + repo_id, self._access, None)
        self.facts["repoScanStatus"] = _json_get(self._document, "data.scanStatus")
        self._expect(
            "repo_concluded",
            self.facts["repoScanStatus"] == "CONCLUDED",
            "repo scan status is concluded",
        )

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "cli-scenes",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    def _register(self, username: str, password: str) -> None:
        self._api(
            "POST",
            "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": username + "@example.invalid",
                "nickname": "AW CLI",
            },
        )
        self._expect("register", _positive_id(_json_get(self._document, "data.id")), "user id")

    def _login(self, username: str, password: str) -> None:
        self._api(
            "POST",
            "/api/auth/login",
            None,
            {"username": username, "password": password},
        )
        token = _json_get(self._document, "data.accessToken")
        self.facts["accessTokenPresent"] = token != ""
        if token == "":
            self._expect("login", False, "access token")
            return
        self._access = token
        self._expect("login", True, "access token")

    def _workspace(self, username: str) -> str:
        self._api(
            "POST",
            "/api/workspaces",
            self._access,
            {"name": "AW CLI " + username, "description": "cli scenes"},
        )
        workspace_id = _json_get(self._document, "data.id")
        self._expect("workspace", _positive_id(workspace_id), "workspace id")
        if self.stopped != "":
            return ""
        self._api("POST", "/api/workspaces/" + workspace_id + "/switch", self._access, None)
        token = _json_get(self._document, "data.accessToken")
        if token == "":
            self._expect("switch", False, "workspace token")
            return ""
        self._access = token
        self._expect("switch", True, "workspace token")
        return workspace_id

    def _workitem(self) -> str:
        self._api(
            "POST",
            "/api/workitems",
            self._access,
            {"workType": "REQ", "title": "登录失败", "contentMd": "用户无法登录", "priority": 2},
        )
        workitem_id = _json_get(self._document, "data.id")
        self.facts["workitemId"] = workitem_id
        self._expect("workitem", _positive_id(workitem_id), "work item id")
        return workitem_id

    def _repo(self) -> str:
        path = _git_repo()
        self._api(
            "POST",
            "/api/repos",
            self._access,
            {
                "name": "cli-scene-repo",
                "url": path.as_uri(),
                "defaultBranch": "main",
                "description": "local clone source",
            },
        )
        repo_id = _json_get(self._document, "data.id")
        self.facts["repoId"] = repo_id
        self._expect("repo", _positive_id(repo_id), "repo id")
        return repo_id

    def _create_session(self, name: str, body: dict[str, object]) -> str:
        self._api("POST", "/api/ai/sessions", self._access, body)
        session_id = _json_get(self._document, "data")
        self.facts[name + "SessionId"] = session_id
        self._expect(name + "_queued", _positive_id(session_id), "session id")
        return session_id

    def _confirm(self, name: str, result_json: str) -> None:
        session_id = str(self.facts[name + "SessionId"])
        self._api(
            "POST",
            "/api/ai/sessions/" + session_id + "/confirm",
            self._access,
            {"resultJson": result_json},
        )
        confirmed = _json_get(self._document, "success") == "true"
        note = "confirm stores the result"
        if not confirmed:
            note = _json_get(self._document, "code") + " " + _json_get(self._document, "message")
        self._expect(name + "_confirm", confirmed, note)

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
        path: str,
        token: str | None,
        body: dict[str, object] | None,
    ) -> None:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body).encode()
        response = self.client.request(
            method,
            self.base_url + path,
            headers=headers,
            content=content,
        )
        if response.content == b"":
            self._document = None
            return
        self._document = response.json()


def _install_fake_cli() -> Path:
    path = Path("/tmp/aw-fake-claude-" + str(os.getpid()))
    path.write_text(_FAKE_CLI, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _git_repo() -> Path:
    path = Path("/tmp/aw-cli-repo-" + str(int(time.time())))
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
    readme = path / "README.md"
    readme.write_text("locks\n", encoding="utf-8")
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
