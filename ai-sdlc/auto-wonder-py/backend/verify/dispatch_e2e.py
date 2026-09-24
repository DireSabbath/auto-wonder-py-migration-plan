"""mock 执行器走完一条派发，再走远程重启。

令牌和口令只留在这次进程里，JSON verdict 不写出它们。
派发回应使用 duplicate：ACK 发两遍，然后进度和旧式 TASK_RESULT。
重启使用断线重连：RESTARTING 之后用更晚的启动时间重新心跳。
"""

import asyncio
import json
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from verify.harness.mock_executor import MockExecutor

_POSITIVE = re.compile(r"[1-9][0-9]*")
_TERMINAL = {"FAILED", "TIMEOUT", "CANCELED"}


def dispatch_e2e(base_url: str) -> dict[str, object]:
    """注册工作空间，下发一条工单，再远程重启执行器。"""
    return asyncio.run(_run(base_url.rstrip("/")))


async def _run(base_url: str) -> dict[str, object]:
    chain = _Chain(base_url)
    try:
        await chain.walk()
    finally:
        await chain.close()
    return chain.verdict()


def _iso(offset_seconds: float) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return moment.isoformat()


def _json_get(document: object, dotted: str) -> str:
    current = document
    for key in dotted.split("."):
        if isinstance(current, list):
            current = current[int(key)]
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            return ""
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
    return _POSITIVE.fullmatch(value) is not None


def _collection(document: object) -> list[dict[str, Any]]:
    """执行器列表在 data 上，调度分页在 data.list 上。"""
    if not isinstance(document, dict):
        return []
    data = document.get("data")
    rows: object = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("list")
    if not isinstance(rows, list):
        return []
    collected: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict):
            collected.append(item)
    return collected


def _find_id(rows: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("id")) == wanted:
            return row
    return None


class _Check:
    def __init__(
        self,
        name: str,
        passed: bool,
        http: int | None,
        want_http: int | None,
        success: str,
        want_success: str,
        note: str,
    ) -> None:
        self.name = name
        self.passed = passed
        self.http = http
        self.want_http = want_http
        self.success = success
        self.want_success = want_success
        self.note = note

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "pass": self.passed,
            "http": self.http,
            "wantHttp": self.want_http,
            "success": self.success,
            "wantSuccess": self.want_success,
            "note": self.note,
        }


class _Chain:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=60.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self.last_code = 0
        self.last_document: object = None
        self._token = ""
        self._workspace_token = ""
        self.executor: MockExecutor | None = None

    async def close(self) -> None:
        if self.executor is not None:
            await self.executor.close()
        await self.client.aclose()

    def verdict(self) -> dict[str, object]:
        ok = False
        if self.fail_count == 0:
            if self.stopped == "":
                ok = True
        body: dict[str, object] = {
            "command": "dispatch-e2e",
            "ok": ok,
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped:
            body["stopped"] = self.stopped
        return body

    async def walk(self) -> None:
        suffix = str(int(time.time()))
        username = "aw-dispatch-" + suffix
        password = secrets.token_urlsafe(18)
        self.facts["testUser"] = username
        self.facts["dispatchFault"] = "duplicate"
        await self._register(username, password)
        if self.stopped:
            return
        await self._login(username, password)
        if self.stopped:
            return
        await self._open_workspace(suffix)
        if self.stopped:
            return
        sdlc_id = await self._sdlc(suffix)
        if self.stopped:
            return
        agent_id = await self._agent(suffix, sdlc_id)
        if self.stopped:
            return
        executor_id, token = await self._executor(agent_id)
        if self.stopped:
            return
        await self._come_online(agent_id, executor_id, token)
        if self.stopped:
            return
        workitem_id = await self._assign(agent_id, sdlc_id)
        if self.stopped:
            return
        await self._finish_dispatch(workitem_id)
        if self.stopped:
            return
        await self._restart(executor_id)

    async def _register(self, username: str, password: str) -> None:
        await self._api(
            "POST",
            "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": f"{username}@example.invalid",
                "nickname": "AW Dispatch",
            },
        )
        self._check("register", 200, "true", "register a user for the dispatch loop")
        if not _positive_id(_json_get(self.last_document, "data.id")):
            self._stop("register returned no id")

    async def _login(self, username: str, password: str) -> None:
        await self._api(
            "POST",
            "/api/auth/login",
            None,
            {"username": username, "password": password},
        )
        self._check("login", 200, "true", "login returns an access token")
        token = _json_get(self.last_document, "data.accessToken")
        if token == "":
            self._stop("login returned no accessToken")
            return
        self._token = token

    async def _open_workspace(self, suffix: str) -> None:
        await self._api(
            "POST",
            "/api/workspaces",
            self._token,
            {
                "name": f"AW Dispatch {suffix}",
                "description": "created by verify dispatch-e2e",
                "background": "mock executor loop",
            },
        )
        self._check("ws_create", 200, "true", "creator becomes owner")
        workspace_id = _json_get(self.last_document, "data.id")
        self.facts["workspaceId"] = workspace_id
        if not _positive_id(workspace_id):
            self._stop("workspace creation returned no id")
            return
        await self._api("POST", f"/api/workspaces/{workspace_id}/switch", self._token, None)
        self._check("ws_switch", 200, "true", "workspace-scoped token")
        token = _json_get(self.last_document, "data.accessToken")
        if token == "":
            self._stop("switch returned no accessToken")
            return
        self._workspace_token = token

    async def _sdlc(self, suffix: str) -> str:
        await self._api(
            "POST",
            "/api/sdlcs",
            self._workspace_token,
            {
                "name": f"AW Dispatch SDLC {suffix}",
                "description": "one agent step",
                "workType": "TASK",
            },
        )
        self._check("sdlc_create", 200, "true", "draft SDLC")
        sdlc_id = _json_get(self.last_document, "data.id")
        self.facts["sdlcId"] = sdlc_id
        if not _positive_id(sdlc_id):
            self._stop("sdlc creation returned no id")
            return ""
        await self._api(
            "POST",
            f"/api/sdlcs/{sdlc_id}/steps",
            self._workspace_token,
            {
                "stepOrder": 1,
                "name": "实现",
                "kind": "WORK",
                "code": "implement",
                "handlerType": "AGENT",
                "instructionMd": "完成工单",
                "required": True,
                "timeoutSeconds": 600,
                "retryBudget": 0,
            },
        )
        self._check("sdlc_step", 200, "true", "agent work step")
        await self._api("POST", f"/api/sdlcs/{sdlc_id}/enable", self._workspace_token, None)
        self._check("sdlc_enable", 200, "true", "enabled SDLC")
        if _json_get(self.last_document, "success") != "true":
            self._stop("sdlc enable failed")
            return ""
        return sdlc_id

    async def _agent(self, suffix: str, sdlc_id: str) -> str:
        await self._api(
            "POST",
            "/api/agents",
            self._workspace_token,
            {"name": f"AW Dispatch Agent {suffix}", "roleName": "开发", "roleCode": "dev"},
        )
        self._check("agent_create", 200, "true", "draft agent")
        agent_id = _json_get(self.last_document, "data.id")
        self.facts["agentId"] = agent_id
        if not _positive_id(agent_id):
            self._stop("agent creation returned no id")
            return ""
        await self._api(
            "PUT",
            f"/api/agents/{agent_id}/config",
            self._workspace_token,
            {"sdlcId": int(sdlc_id), "roleName": "开发", "roleCode": "dev"},
        )
        self._check("agent_config", 200, "true", "draft version carries the SDLC")
        await self._api("POST", f"/api/agents/{agent_id}/submit", self._workspace_token, None)
        self._check("agent_submit", 200, "true", "submit the draft")
        await self._api(
            "POST",
            f"/api/agents/{agent_id}/approve",
            self._workspace_token,
            {"comment": "dispatch-e2e"},
        )
        self._check("agent_approve", 200, "true", "owner approves the version")
        status = _json_get(self.last_document, "data.status")
        self.facts["agentStatus"] = status
        if status != "ONLINE":
            self._stop("agent did not come online")
            return ""
        return agent_id

    async def _executor(self, agent_id: str) -> tuple[str, str]:
        await self._api(
            "POST",
            f"/api/agents/{agent_id}/executors",
            self._workspace_token,
            {"name": "mock-executor", "clientKind": "QODER_CLI"},
        )
        self._check("executor_create", 200, "true", "issue an executor token")
        executor_id = _json_get(self.last_document, "data.id")
        token = _json_get(self.last_document, "data.token")
        self.facts["executorId"] = executor_id
        self.facts["tokenPresent"] = token != ""
        if not _positive_id(executor_id):
            self._stop("executor creation returned no id")
            return "", ""
        if token == "":
            self._stop("executor creation returned no token")
            return "", ""
        return executor_id, token

    async def _come_online(self, agent_id: str, executor_id: str, token: str) -> None:
        self.executor = MockExecutor(self.base_url, int(executor_id), token)
        try:
            await self.executor.connect()
            await self.executor.heartbeat(_iso(-30))
        except Exception as error:
            self.facts["connectError"] = type(error).__name__
            self._stop("executor websocket rejected")
            return
        ready = await self._wait_executor(agent_id, executor_id, _online)
        self.facts["executorOnline"] = ready
        if not ready:
            self._stop("executor did not publish heartbeat")

    async def _assign(self, agent_id: str, sdlc_id: str) -> str:
        await self._api(
            "POST",
            "/api/workitems",
            self._workspace_token,
            {
                "workType": "TASK",
                "title": "AW dispatch loop",
                "contentMd": "created by verify dispatch-e2e",
                "priority": 2,
            },
        )
        self._check("wi_create", 200, "true", "work item")
        workitem_id = _json_get(self.last_document, "data.id")
        self.facts["workitemId"] = workitem_id
        if not _positive_id(workitem_id):
            self._stop("work item creation returned no id")
            return ""
        await self._api(
            "PUT",
            f"/api/workitems/{workitem_id}/assignee",
            self._workspace_token,
            {"assigneeType": "AGENT", "assigneeRef": int(agent_id), "sdlcId": int(sdlc_id)},
        )
        self._check("wi_assign", 200, "true", "assign the agent and enqueue a dispatch")
        if _json_get(self.last_document, "success") != "true":
            self._stop("assign failed")
            return ""
        return workitem_id

    async def _finish_dispatch(self, workitem_id: str) -> None:
        frame = await self.executor.wait_type("TASK_DISPATCH", 20)
        if frame is None:
            await self._note_dispatch(workitem_id)
            self._stop("no TASK_DISPATCH")
            return
        dispatch_id = str(frame.get("dispatchId"))
        self.facts["dispatchId"] = dispatch_id
        self._record(
            "task_dispatch",
            _positive_id(dispatch_id),
            None,
            None,
            dispatch_id,
            "positive",
            "duplicate ACK, then progress and legacy TASK_RESULT",
        )
        await self.executor.answer_dispatch(frame, "duplicate")
        ack = await self.executor.wait_type("TASK_RESULT_ACK", 20)
        accepted = ""
        if ack is not None:
            accepted = _json_get(ack, "accepted")
        self._record(
            "task_result_ack",
            accepted == "true",
            None,
            None,
            accepted,
            "true",
            "legacy TASK_RESULT accepted",
        )
        status = await self._wait_dispatch(workitem_id, "SUCCEEDED")
        self._record(
            "dispatch_succeeded",
            status == "SUCCEEDED",
            None,
            None,
            status,
            "SUCCEEDED",
            _fact_text(self.facts, "dispatchError"),
        )
        if status != "SUCCEEDED":
            self._stop("dispatch did not succeed")

    async def _restart(self, executor_id: str) -> None:
        await self._api(
            "POST",
            f"/api/executors/{executor_id}/restart",
            self._workspace_token,
            {"update": False},
        )
        self._check("restart_request", 200, "true", "remote restart while the mock is online")
        request_id = _json_get(self.last_document, "data.requestId")
        self.facts["restartRequestId"] = request_id
        if request_id == "":
            self._stop("restart returned no requestId")
            return
        frame = await self.executor.wait_type("EXECUTOR_RESTART", 15)
        framed = ""
        if frame is not None:
            framed = str(frame.get("requestId"))
        self._record(
            "restart_frame",
            framed == request_id,
            None,
            None,
            framed,
            request_id,
            "EXECUTOR_RESTART carries the same requestId",
        )
        if framed != request_id:
            self._stop("missing EXECUTOR_RESTART")
            return
        await self.executor.restart_result(request_id, "RESTARTING")
        phase = await self._wait_restart(executor_id, "RESTARTING")
        self.facts["restartPhase"] = phase
        if phase != "RESTARTING":
            self._stop("restart did not enter RESTARTING")
            return
        await self.executor.reconnect(_iso(0), request_id)
        status = await self._wait_restart(executor_id, "COMPLETED")
        self.facts["restartStatus"] = status
        self._record(
            "restart_completed",
            status == "COMPLETED",
            None,
            None,
            status,
            "COMPLETED",
            _fact_text(self.facts, "restartMessage"),
        )
        if status != "COMPLETED":
            self._stop("restart did not complete after reconnect")

    async def _wait_executor(
        self,
        agent_id: str,
        executor_id: str,
        ready: Callable[[dict[str, Any]], bool],
    ) -> bool:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await self._api(
                "GET",
                f"/api/agents/{agent_id}/executors",
                self._workspace_token,
                None,
            )
            row = _find_id(_collection(self.last_document), executor_id)
            if row is not None:
                if ready(row):
                    return True
            await asyncio.sleep(0.2)
        return False

    async def _wait_dispatch(self, workitem_id: str, want: str) -> str:
        deadline = time.monotonic() + 20
        status = ""
        while time.monotonic() < deadline:
            status = await self._note_dispatch(workitem_id)
            if status == want:
                return status
            if status in _TERMINAL:
                return status
            await asyncio.sleep(0.25)
        return status

    async def _note_dispatch(self, workitem_id: str) -> str:
        await self._api(
            "GET",
            f"/api/dispatches?workitem_id={workitem_id}&page_size=20",
            self._workspace_token,
            None,
        )
        rows = _collection(self.last_document)
        if len(rows) == 0:
            return ""
        row = rows[0]
        status = str(row.get("status"))
        self.facts["dispatchStatus"] = status
        self.facts["dispatchError"] = row.get("error")
        self.facts["dispatchId"] = row.get("id")
        return status

    async def _wait_restart(self, executor_id: str, want: str) -> str:
        deadline = time.monotonic() + 15
        status = ""
        while time.monotonic() < deadline:
            status = await self._note_restart(executor_id)
            if status == want:
                return status
            if status in {"FAILED", "TIMED_OUT", "COMPLETED"}:
                return status
            await asyncio.sleep(0.2)
        return status

    async def _note_restart(self, executor_id: str) -> str:
        await self._api("GET", "/api/executors", self._workspace_token, None)
        row = _find_id(_collection(self.last_document), executor_id)
        if row is None:
            return ""
        restart = row.get("restart")
        if not isinstance(restart, dict):
            return ""
        status = str(restart.get("status"))
        self.facts["restartMessage"] = restart.get("message")
        return status

    async def _api(
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
        response = await self.client.request(
            method,
            self.base_url + path,
            headers=headers,
            content=content,
        )
        self.last_code = response.status_code
        self.last_document = _document(response)

    def _check(self, name: str, want_code: int, want_success: str, note: str) -> None:
        got = ""
        if self.last_document is not None:
            got = _json_get(self.last_document, "success")
        success_ok = got == want_success
        if want_success == "any":
            success_ok = True
        passed = False
        if self.last_code == want_code:
            if success_ok:
                passed = True
        self._record(name, passed, self.last_code, want_code, got, want_success, note)

    def _record(
        self,
        name: str,
        passed: bool,
        http: int | None,
        want_http: int | None,
        success: str,
        want_success: str,
        note: str,
    ) -> None:
        if passed:
            self.pass_count += 1
        else:
            self.fail_count += 1
        self.checks.append(_Check(name, passed, http, want_http, success, want_success, note))

    def _stop(self, detail: str) -> None:
        self.fail_count += 1
        self.stopped = detail
        self.checks.append(_Check("fatal", False, self.last_code, None, "", "", detail))


def _fact_text(facts: dict[str, object], key: str) -> str:
    value = facts.get(key)
    if value is None:
        return ""
    return str(value)


def _online(row: dict[str, Any]) -> bool:
    if row.get("status") != "ONLINE":
        return False
    if row.get("restartSupported") is not True:
        return False
    return row.get("lastStartedAt") is not None


def _document(response: httpx.Response) -> object:
    try:
        return response.json()
    except json.JSONDecodeError:
        return None
