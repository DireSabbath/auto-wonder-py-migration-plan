"""注册后套用七角色小队，完成一步 SDLC，再把工单交回真人。

时间线同时从接口和工单详情页核对。令牌和口令只留在这次进程里。
"""

import asyncio
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from playwright.async_api import async_playwright

from verify.harness.mock_executor import MockExecutor

FULL_CYCLE_NAME = "全链路研发协作小队"
ENTRY_ROLE = "REQ_CLARIFIER"
FULL_CYCLE_ROLES = frozenset(
    {
        "FS_DEV",
        "CR",
        "QA",
        "REQ_CLARIFIER",
        "PROJECT_MANAGER",
        "CONFLICT_RESOLVER",
        "DBA",
    }
)
_TERMINAL = {"FAILED", "TIMEOUT", "CANCELED"}


def squad_flow(base_url: str, include_page: bool = True) -> dict[str, object]:
    """走完七角色小队到交真人。页面时间线只在单栈验收时打开。"""
    return asyncio.run(_run(base_url.rstrip("/"), include_page))


def role_codes(agents: object) -> set[str]:
    """从模板或应用结果里取出角色编码。"""
    found: set[str] = set()
    if not isinstance(agents, list):
        return found
    for item in agents:
        if isinstance(item, dict):
            code = item.get("roleCode")
            if isinstance(code, str) and code != "":
                found.add(code)
    return found


async def _run(base_url: str, include_page: bool) -> dict[str, object]:
    chain = _Chain(base_url, include_page)
    try:
        await chain.walk()
    finally:
        await chain.close()
    return chain.verdict()


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
    return value.isdecimal() and value != "0"


def _rows(document: object) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    data = document.get("data")
    if not isinstance(data, list):
        return []
    collected: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            collected.append(item)
    return collected


def _child(document: object, dotted: str) -> object:
    current = document
    for key in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


class _Check:
    def __init__(self, name: str, passed: bool, note: str) -> None:
        self.name = name
        self.passed = passed
        self.note = note

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "pass": self.passed, "note": self.note}


class _Chain:
    def __init__(self, base_url: str, include_page: bool) -> None:
        self.base_url = base_url
        self.include_page = include_page
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
        self._refresh = ""
        self._user: dict[str, object] = {}
        self._workspace: dict[str, object] = {}
        self._access_level = ""
        self._user_id = ""
        self.executor: MockExecutor | None = None

    async def close(self) -> None:
        if self.executor is not None:
            await self.executor.close()
        await self.client.aclose()

    def verdict(self) -> dict[str, object]:
        ok = self.fail_count == 0 and self.stopped == ""
        body: dict[str, object] = {
            "command": "squad-flow",
            "ok": ok,
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    async def walk(self) -> None:
        suffix = str(int(time.time()))
        username = "aw-squad-" + suffix
        password = secrets.token_urlsafe(18)
        self.facts["testUser"] = username
        await self._register(username, password)
        if self.stopped != "":
            return
        await self._login(username, password)
        if self.stopped != "":
            return
        await self._open_workspace(suffix)
        if self.stopped != "":
            return
        template_id = await self._template()
        if self.stopped != "":
            return
        agent_id, squad_id = await self._apply(template_id)
        if self.stopped != "":
            return
        executor_id, token = await self._executor(agent_id)
        if self.stopped != "":
            return
        await self._come_online(agent_id, executor_id, token)
        if self.stopped != "":
            return
        workitem_id = await self._assign(agent_id, squad_id, suffix)
        if self.stopped != "":
            return
        await self._finish_and_hand_off(workitem_id)
        if self.stopped != "":
            return
        await self._timeline(workitem_id)
        if self.stopped != "":
            return
        if self.include_page:
            await self._see_page(workitem_id)

    async def _register(self, username: str, password: str) -> None:
        await self._api(
            "POST",
            "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": f"{username}@example.invalid",
                "nickname": "AW Squad",
            },
        )
        self._check("register", _json_get(self.last_document, "success") == "true")
        if not _positive_id(_json_get(self.last_document, "data.id")):
            self._stop("register returned no id")

    async def _login(self, username: str, password: str) -> None:
        await self._api(
            "POST",
            "/api/auth/login",
            None,
            {"username": username, "password": password},
        )
        self._token = _json_get(self.last_document, "data.accessToken")
        self._refresh = _json_get(self.last_document, "data.refreshToken")
        user = _child(self.last_document, "data.user")
        if isinstance(user, dict):
            self._user = user
        self._user_id = _json_get(self.last_document, "data.user.id")
        self.facts["userId"] = self._user_id
        self._check("login", self._token != "" and _positive_id(self._user_id))
        if self._token == "" or not _positive_id(self._user_id):
            self._stop("login returned no user")

    async def _open_workspace(self, suffix: str) -> None:
        await self._api(
            "POST",
            "/api/workspaces",
            self._token,
            {
                "name": f"AW Squad {suffix}",
                "description": "created by verify squad-flow",
                "background": "seven role squad",
            },
        )
        workspace_id = _json_get(self.last_document, "data.id")
        self.facts["workspaceId"] = workspace_id
        self._check("workspace", _positive_id(workspace_id))
        if not _positive_id(workspace_id):
            self._stop("workspace creation returned no id")
            return
        await self._api("POST", f"/api/workspaces/{workspace_id}/switch", self._token, None)
        self._workspace_token = _json_get(self.last_document, "data.accessToken")
        self._access_level = _json_get(self.last_document, "data.accessLevel")
        self._check("switch", self._workspace_token != "")
        if self._workspace_token == "":
            self._stop("switch returned no accessToken")
            return
        await self._api("GET", "/api/workspaces/current", self._workspace_token, None)
        current = _child(self.last_document, "data")
        if isinstance(current, dict):
            self._workspace = current

    async def _template(self) -> str:
        await self._api("GET", "/api/squad-templates", self._workspace_token, None)
        chosen = None
        for row in _rows(self.last_document):
            if row.get("name") == FULL_CYCLE_NAME:
                chosen = row
        template_id = ""
        squad_size = ""
        if chosen is not None:
            template_id = str(chosen.get("id"))
            squad_size = str(chosen.get("squadSize"))
        self.facts["templateId"] = template_id
        self.facts["templateSquadSize"] = squad_size
        self._check(
            "template_listed",
            _positive_id(template_id) and squad_size == "7",
        )
        if not _positive_id(template_id):
            self._stop("seven-role template is not seeded")
            return ""
        await self._api(
            "GET",
            f"/api/squad-templates/{template_id}",
            self._workspace_token,
            None,
        )
        agents = _child(self.last_document, "data.agents")
        codes = role_codes(agents)
        self.facts["templateRoles"] = sorted(codes)
        self._check("template_roles", codes == set(FULL_CYCLE_ROLES))
        if codes != set(FULL_CYCLE_ROLES):
            self._stop("template does not contain the seven roles")
            return ""
        return template_id

    async def _apply(self, template_id: str) -> tuple[str, str]:
        await self._api(
            "POST",
            f"/api/squad-templates/{template_id}/apply",
            self._workspace_token,
            None,
        )
        squad_id = _json_get(self.last_document, "data.squadId")
        agents = _child(self.last_document, "data.agents")
        codes = role_codes(agents)
        self.facts["squadId"] = squad_id
        self.facts["appliedRoles"] = sorted(codes)
        self._check(
            "apply",
            _json_get(self.last_document, "success") == "true"
            and _positive_id(squad_id)
            and codes == set(FULL_CYCLE_ROLES),
        )
        if not _positive_id(squad_id) or codes != set(FULL_CYCLE_ROLES):
            self._stop("apply did not create the seven roles")
            return "", ""
        agent_id = ""
        if isinstance(agents, list):
            for item in agents:
                if isinstance(item, dict) and item.get("roleCode") == ENTRY_ROLE:
                    agent_id = str(item.get("agentId"))
        self.facts["entryAgentId"] = agent_id
        self.facts["entryRole"] = ENTRY_ROLE
        if not _positive_id(agent_id):
            self._stop("apply did not return the clarifier")
            return "", ""
        return agent_id, squad_id

    async def _executor(self, agent_id: str) -> tuple[str, str]:
        await self._api(
            "POST",
            f"/api/agents/{agent_id}/executors",
            self._workspace_token,
            {"name": "mock-squad", "clientKind": "QODER_CLI"},
        )
        executor_id = _json_get(self.last_document, "data.id")
        token = _json_get(self.last_document, "data.token")
        self.facts["executorId"] = executor_id
        self.facts["tokenPresent"] = token != ""
        self._check(
            "executor",
            _positive_id(executor_id) and token != "",
        )
        if not _positive_id(executor_id) or token == "":
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
        ready = await self._wait_executor(agent_id, executor_id)
        self.facts["executorOnline"] = ready
        self._check("executor_online", ready)
        if not ready:
            self._stop("executor did not publish heartbeat")

    async def _assign(self, agent_id: str, squad_id: str, suffix: str) -> str:
        await self._api(
            "POST",
            "/api/workitems",
            self._workspace_token,
            {
                "workType": "REQ",
                "title": f"AW squad flow {suffix}",
                "contentMd": "七角色小队从需求澄清交回真人",
                "priority": 2,
            },
        )
        workitem_id = _json_get(self.last_document, "data.id")
        self.facts["workitemId"] = workitem_id
        self._check("workitem", _positive_id(workitem_id))
        if not _positive_id(workitem_id):
            self._stop("work item creation returned no id")
            return ""
        await self._api(
            "PUT",
            f"/api/workitems/{workitem_id}/assignee",
            self._workspace_token,
            {
                "assigneeType": "AGENT",
                "assigneeRef": int(agent_id),
                "squadId": int(squad_id),
            },
        )
        sdlc_id = _json_get(self.last_document, "data.sdlcId")
        assignee = _json_get(self.last_document, "data.assigneeType")
        self.facts["sdlcId"] = sdlc_id
        self._check(
            "assign",
            _json_get(self.last_document, "success") == "true"
            and assignee == "AGENT"
            and _positive_id(sdlc_id),
        )
        if assignee != "AGENT" or not _positive_id(sdlc_id):
            self._stop("assign did not bind the clarifier SDLC")
            return ""
        return workitem_id

    async def _finish_and_hand_off(self, workitem_id: str) -> None:
        frame = await self.executor.wait_type("TASK_DISPATCH", 20)
        if frame is None:
            self._stop("no TASK_DISPATCH")
            return
        dispatch_id = str(frame.get("dispatchId"))
        self.facts["dispatchId"] = dispatch_id
        self._check("task_dispatch", _positive_id(dispatch_id))
        await self.executor.answer_dispatch(frame, "duplicate")
        ack = await self.executor.wait_type("TASK_RESULT_ACK", 20)
        accepted = ""
        if ack is not None:
            accepted = _json_get(ack, "accepted")
        self._check("task_result", accepted == "true")
        status = await self._wait_dispatch(workitem_id, "SUCCEEDED")
        self.facts["dispatchStatus"] = status
        self._check("dispatch_succeeded", status == "SUCCEEDED")
        if status != "SUCCEEDED":
            self._stop("dispatch did not succeed")
            return
        await self.executor.handoff_human(int(dispatch_id), int(workitem_id), int(self._user_id))
        result = await self.executor.wait_type("TASK_HANDOFF_RESULT", 20)
        handoff_status = ""
        target_type = ""
        target_ref = ""
        if result is not None:
            handoff_status = str(result.get("status"))
            target_type = str(result.get("targetType"))
            target_ref = str(result.get("targetRef"))
        self.facts["handoffStatus"] = handoff_status
        self.facts["handoffTargetType"] = target_type
        self.facts["handoffTargetRef"] = target_ref
        if result is not None:
            self.facts["handoffReason"] = result.get("reasonCode")
            self.facts["handoffMessage"] = result.get("message")
        self._check(
            "handoff_human",
            handoff_status == "HUMAN_ASSIGNED"
            and target_type == "HUMAN"
            and target_ref == self._user_id,
        )
        if handoff_status != "HUMAN_ASSIGNED":
            self._stop("handoff did not assign the human")
            return
        await self._api(
            "GET",
            f"/api/workitems/{workitem_id}",
            self._workspace_token,
            None,
        )
        assignee = _json_get(self.last_document, "data.assigneeType")
        assignee_ref = _json_get(self.last_document, "data.assigneeRef")
        self.facts["assigneeType"] = assignee
        self.facts["assigneeRef"] = assignee_ref
        self._check(
            "workitem_human",
            assignee == "HUMAN" and assignee_ref == self._user_id,
        )
        if assignee != "HUMAN":
            self._stop("work item is not assigned to the human")

    async def _timeline(self, workitem_id: str) -> None:
        await self._api(
            "GET",
            f"/api/workitems/{workitem_id}/timeline",
            self._workspace_token,
            None,
        )
        events = _rows(self.last_document)
        types = {str(row.get("eventType")) for row in events}
        self.facts["timelineEvents"] = sorted(types)
        self._check("timeline_api", "CREATE" in types and "ASSIGN" in types)
        await self._api(
            "GET",
            f"/api/workitems/{workitem_id}/unified-timeline",
            self._workspace_token,
            None,
        )
        text = "\n".join(str(row.get("content")) for row in _rows(self.last_document))
        phrases = "工单已创建" in text and "交付负责人已变更" in text
        self.facts["unifiedPhrases"] = phrases
        self._check("unified_timeline", phrases)

    async def _see_page(self, workitem_id: str) -> None:
        state = {
            "accessToken": self._workspace_token,
            "refreshToken": self._refresh,
            "user": self._user,
            "currentWorkspace": self._workspace,
            "accessLevel": self._access_level,
        }
        stored = json.dumps({"state": state, "version": 2}, ensure_ascii=False)
        script = "localStorage.setItem('aw-auth', " + json.dumps(stored) + ");"
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.add_init_script(script)
            await page.goto(
                self.base_url + "/workitems/" + workitem_id,
                wait_until="domcontentloaded",
            )
            section = page.get_by_test_id("workitem-timeline-section")
            await section.wait_for(timeout=20000)
            body = await section.inner_text()
            location = page.url
            self.facts["pagePath"] = location.removeprefix(self.base_url)
            visible = "工单已创建" in body and "交付负责人已变更" in body
            stayed = location.endswith("/workitems/" + workitem_id)
            self._check("timeline_page", visible and stayed)
            await browser.close()
        except Exception as error:
            self.facts["pageError"] = type(error).__name__
            self._stop("timeline page " + type(error).__name__)
        finally:
            await playwright.stop()

    async def _wait_executor(self, agent_id: str, executor_id: str) -> bool:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await self._api(
                "GET",
                f"/api/agents/{agent_id}/executors",
                self._workspace_token,
                None,
            )
            for row in _rows(self.last_document):
                if str(row.get("id")) == executor_id and row.get("status") == "ONLINE":
                    return True
            await asyncio.sleep(0.2)
        return False

    async def _wait_dispatch(self, workitem_id: str, want: str) -> str:
        deadline = time.monotonic() + 20
        status = ""
        while time.monotonic() < deadline:
            await self._api(
                "GET",
                f"/api/dispatches?workitem_id={workitem_id}&page_size=20",
                self._workspace_token,
                None,
            )
            data = _child(self.last_document, "data.list")
            rows: list[dict[str, Any]] = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        rows.append(item)
            if len(rows) > 0:
                status = str(rows[0].get("status"))
                self.facts["dispatchError"] = rows[0].get("error")
            if status == want or status in _TERMINAL:
                return status
            await asyncio.sleep(0.25)
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
        try:
            self.last_document = response.json()
        except json.JSONDecodeError:
            self.last_document = None

    def _check(self, name: str, passed: bool) -> None:
        if passed:
            self.pass_count += 1
        else:
            self.fail_count += 1
        self.checks.append(_Check(name, passed, name))

    def _stop(self, detail: str) -> None:
        self.fail_count += 1
        self.stopped = detail
        self.checks.append(_Check("fatal", False, detail))


def _iso(offset_seconds: float) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return moment.isoformat()
