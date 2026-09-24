"""用 Playwright 走查前端路由全集。

路由表对齐 ``frontend/src/app/router.tsx``。未知路径会被前端送到 ``/login``，
已知页面停在自己的地址，响应里不能出现 404，页面也不能画出「无效的 ID」。
"""

import json
import secrets
import time
from dataclasses import dataclass

import httpx
from playwright.sync_api import Page, Response, sync_playwright

_ID_KEYS = ("workitemId", "agentId", "sdlcId", "repoId", "taskId", "runId")


@dataclass(frozen=True)
class PageRoute:
    """一条前端路由：源码路径、实际打开的地址、跳转后的地址。"""

    router_path: str
    visit: str
    final: str


PAGE_ROUTES: tuple[PageRoute, ...] = (
    PageRoute("/help", "/help", "/help"),
    PageRoute("/login", "/login", "/login"),
    PageRoute("/register", "/register", "/register"),
    PageRoute("/workspaces", "/workspaces", "/workspaces"),
    PageRoute("/workspaces/branding", "/workspaces/branding", "/workspaces/branding"),
    PageRoute("/workspaces/recycle-bin", "/workspaces/recycle-bin", "/workspaces/recycle-bin"),
    PageRoute("/profile/settings", "/profile/settings", "/profile/settings"),
    PageRoute("/platform/branding", "/platform/branding", "/workspaces"),
    PageRoute("/open-platform", "/open-platform", "/profile/settings?tab=mcp"),
    PageRoute("", "/", "/workitems"),
    PageRoute("/workitems", "/workitems", "/workitems"),
    PageRoute("/workitems/new", "/workitems/new", "/workitems/new"),
    PageRoute("/workitems/:id", "/workitems/{workitemId}", "/workitems/{workitemId}"),
    PageRoute("/agents", "/agents", "/agents"),
    PageRoute("/agents/reviews", "/agents/reviews", "/agents/reviews"),
    PageRoute("/agents/new", "/agents/new", "/agents/new"),
    PageRoute("/agents/:id", "/agents/{agentId}", "/agents/{agentId}"),
    PageRoute("/agents/:id/edit", "/agents/{agentId}/edit", "/agents/{agentId}/edit"),
    PageRoute("/squads", "/squads", "/squads"),
    PageRoute("/sdlcs", "/sdlcs", "/sdlcs"),
    PageRoute("/sdlcs/generate", "/sdlcs/generate", "/sdlcs/generate"),
    PageRoute("/sdlcs/:id", "/sdlcs/{sdlcId}", "/sdlcs/{sdlcId}"),
    PageRoute("/repos", "/repos", "/repos"),
    PageRoute("/repos/map", "/repos/map", "/repos/map"),
    PageRoute("/repos/:id", "/repos/{repoId}", "/repos/{repoId}"),
    PageRoute("/memories", "/memories", "/memories"),
    PageRoute("/memories/import", "/memories/import", "/memories/import"),
    PageRoute("/memories/reviews", "/memories/reviews", "/memories/reviews"),
    PageRoute("/skills", "/skills", "/skills"),
    PageRoute("/executors", "/executors", "/executors"),
    PageRoute("/scheduled-tasks", "/scheduled-tasks", "/scheduled-tasks"),
    PageRoute("/scheduled-tasks/new", "/scheduled-tasks/new", "/scheduled-tasks/new"),
    PageRoute(
        "/scheduled-tasks/:id/edit",
        "/scheduled-tasks/{taskId}/edit",
        "/scheduled-tasks/{taskId}/edit",
    ),
    PageRoute("/scheduled-tasks/:id", "/scheduled-tasks/{taskId}", "/scheduled-tasks/{taskId}"),
    PageRoute(
        "/scheduled-task-runs/:runId",
        "/scheduled-task-runs/{runId}",
        "/scheduled-task-runs/{runId}",
    ),
    PageRoute("/executions", "/executions", "/executions"),
    PageRoute("/status-templates", "/status-templates", "/status-templates"),
    PageRoute("/integrations", "/integrations", "/integrations"),
    PageRoute("/integrations/aone", "/integrations/aone", "/integrations"),
    PageRoute("/integrations/channels", "/integrations/channels", "/integrations/channels"),
    PageRoute("/evolution", "/evolution", "/evolution"),
    PageRoute("/audit-logs", "/audit-logs", "/audit-logs"),
    PageRoute("/notifications", "/notifications", "/notifications"),
    PageRoute("/settings/members", "/settings/members", "/settings/members"),
    PageRoute("/settings/members-roles", "/settings/members-roles", "/settings/members"),
    PageRoute(
        "/settings/members-roles/:tab",
        "/settings/members-roles/access",
        "/settings/members",
    ),
    PageRoute("/settings/roles", "/settings/roles", "/settings/members"),
    PageRoute("/settings", "/settings", "/settings"),
    PageRoute("/settings/backups", "/settings/backups", "/settings/backups"),
    PageRoute(
        "/settings/environment-variables",
        "/settings/environment-variables",
        "/settings/environment-variables",
    ),
    PageRoute("/insights", "/insights", "/insights"),
    PageRoute("/about", "/about", "/about"),
    PageRoute("*", "/pages-missing-route", "/login"),
)


@dataclass
class _Check:
    name: str
    passed: bool
    note: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "pass": self.passed, "note": self.note}


def pages(base_url: str) -> dict[str, object]:
    """注册并进入工作空间，再逐页打开路由全集。"""
    walk = _Walk(base_url.rstrip("/"))
    walk.run()
    return walk.verdict()


class _Walk:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.client = httpx.Client(timeout=60.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self._token = ""
        self._refresh = ""
        self._workspace_token = ""
        self._user: dict[str, object] = {}
        self._workspace: dict[str, object] = {}
        self._access_level = ""
        self._ids = {key: "" for key in _ID_KEYS}
        self._document: object = None

    def run(self) -> None:
        self._prepare()
        if self.stopped != "":
            return
        self._browse()

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "pages",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    def _prepare(self) -> None:
        suffix = str(int(time.time()))
        username = f"aw-pages-{suffix}"
        password = secrets.token_urlsafe(18)
        self.facts["testUser"] = username
        self._request(
            "POST",
            "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": f"{username}@example.invalid",
                "nickname": "AW Pages",
            },
        )
        self._check("register", _success(self._document) == "true", "register")
        if _success(self._document) != "true":
            self._stop("register failed")
            return
        self._request(
            "POST",
            "/api/auth/login",
            None,
            {"username": username, "password": password},
        )
        self._token = _get(self._document, "data.accessToken")
        self._refresh = _get(self._document, "data.refreshToken")
        user = _child(self._document, "data.user")
        if isinstance(user, dict):
            self._user = user
        self._check("login", self._token != "", "login")
        if self._token == "":
            self._stop("login returned no accessToken")
            return
        self._request(
            "POST",
            "/api/workspaces",
            self._token,
            {
                "name": f"AW Pages Workspace {suffix}",
                "description": "created by verify pages",
                "background": "route walk",
            },
        )
        workspace_id = _get(self._document, "data.id")
        self.facts["workspaceId"] = workspace_id
        self._check("workspace", workspace_id != "", "workspace")
        if workspace_id == "":
            self._stop("workspace creation returned no id")
            return
        self._request("POST", f"/api/workspaces/{workspace_id}/switch", self._token, None)
        self._workspace_token = _get(self._document, "data.accessToken")
        self._access_level = _get(self._document, "data.accessLevel")
        self._check("switch", self._workspace_token != "", "switch")
        if self._workspace_token == "":
            self._stop("switch returned no accessToken")
            return
        self._request("GET", "/api/workspaces/current", self._workspace_token, None)
        current = _child(self._document, "data")
        if isinstance(current, dict):
            self._workspace = current
        self._seed_entities(suffix)

    def _seed_entities(self, suffix: str) -> None:
        token = self._workspace_token
        self._request(
            "POST",
            "/api/sdlcs",
            token,
            {"name": f"AW Pages SDLC {suffix}", "description": "route", "workType": "TASK"},
        )
        self._ids["sdlcId"] = _get(self._document, "data.id")
        self._check("sdlc", self._ids["sdlcId"] != "", "sdlc")
        self._request(
            "POST",
            "/api/agents",
            token,
            {"name": f"AW Pages Agent {suffix}", "roleName": "开发", "roleCode": "dev"},
        )
        self._ids["agentId"] = _get(self._document, "data.id")
        self._check("agent", self._ids["agentId"] != "", "agent")
        if self._ids["agentId"] != "" and self._ids["sdlcId"] != "":
            self._request(
                "PUT",
                f"/api/agents/{self._ids['agentId']}/config",
                token,
                {"sdlcId": int(self._ids["sdlcId"]), "roleName": "开发", "roleCode": "dev"},
            )
            self._request("POST", f"/api/agents/{self._ids['agentId']}/submit", token, None)
            self._request(
                "POST",
                f"/api/agents/{self._ids['agentId']}/approve",
                token,
                {"comment": "pages"},
            )
            self.facts["agentStatus"] = _get(self._document, "data.status")
        self._request(
            "POST",
            "/api/workitems",
            token,
            {
                "workType": "TASK",
                "title": f"AW Pages Workitem {suffix}",
                "contentMd": "route walk",
                "priority": 2,
            },
        )
        self._ids["workitemId"] = _get(self._document, "data.id")
        self._check("workitem", self._ids["workitemId"] != "", "workitem")
        self._request(
            "POST",
            "/api/repos",
            token,
            {
                "name": f"aw-pages-{suffix}",
                "url": "https://example.invalid/aw/pages.git",
                "defaultBranch": "main",
                "description": "route walk",
            },
        )
        self._ids["repoId"] = _get(self._document, "data.id")
        self._check("repo", self._ids["repoId"] != "", "repo")
        self._request(
            "POST",
            "/api/squads",
            token,
            {"name": f"AW Pages Squad {suffix}", "description": "route walk"},
        )
        squad_id = _get(self._document, "data.id")
        self.facts["squadId"] = squad_id
        self._check("squad", squad_id != "", "squad")
        if squad_id != "" and self._ids["agentId"] != "":
            self._request(
                "POST",
                f"/api/squads/{squad_id}/members",
                token,
                {"agentIds": [int(self._ids["agentId"])]},
            )
        if (
            squad_id != ""
            and self._ids["agentId"] != ""
            and self.facts.get("agentStatus") == "ONLINE"
        ):
            self._request(
                "POST",
                "/api/scheduled-tasks",
                token,
                {
                    "name": f"AW Pages Task {suffix}",
                    "instructionMd": "route walk",
                    "squadId": int(squad_id),
                    "initialAgentId": int(self._ids["agentId"]),
                    "scheduleType": "ONCE",
                    "runAt": "2026-12-01T00:00:00Z",
                    "timezone": "Asia/Shanghai",
                },
            )
            self._ids["taskId"] = _get(self._document, "data.id")
            version = _get(self._document, "data.version")
            self._check("scheduled_task", self._ids["taskId"] != "", "scheduled task")
            if self._ids["taskId"] != "" and version != "":
                self._request(
                    "POST",
                    f"/api/scheduled-tasks/{self._ids['taskId']}/run-now",
                    token,
                    {"requestId": f"pages-{suffix}", "version": int(version)},
                )
                self._ids["runId"] = _get(self._document, "data.id")
                self._check("scheduled_run", self._ids["runId"] != "", "scheduled run")

    def _browse(self) -> None:
        state = {
            "accessToken": self._workspace_token,
            "refreshToken": self._refresh,
            "user": self._user,
            "currentWorkspace": self._workspace,
            "accessLevel": self._access_level,
        }
        stored = json.dumps({"state": state, "version": 2}, ensure_ascii=False)
        script = "localStorage.setItem('aw-auth', " + json.dumps(stored) + ");"
        try:
            playwright = sync_playwright().start()
        except Exception as error:
            self._stop("browser launch " + type(error).__name__)
            return
        try:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script(script)
            page = context.new_page()
            page_errors: list[str] = []
            missing: list[str] = []

            def remember_error(error: BaseException) -> None:
                page_errors.append(type(error).__name__)

            def remember_missing(response: Response) -> None:
                if response.status == 404:
                    missing.append(response.url)

            page.on("pageerror", remember_error)
            page.on("response", remember_missing)
            for route in PAGE_ROUTES:
                self._open(page, route, page_errors, missing)
            browser.close()
        except Exception as error:
            self._stop("browser " + type(error).__name__)
        finally:
            playwright.stop()

    def _open(
        self,
        page: Page,
        route: PageRoute,
        page_errors: list[str],
        missing: list[str],
    ) -> None:
        visit = _ready(route.visit, self._ids)
        final = _ready(route.final, self._ids)
        name = route.router_path
        if name == "":
            name = "/"
        if visit == "" or final == "":
            self._record(name, False, "missing id for " + route.router_path)
            return
        page_errors.clear()
        missing.clear()
        try:
            response = page.goto(
                self.base_url + visit,
                wait_until="domcontentloaded",
                timeout=20000,
            )
            page.wait_for_function(
                """(expected) => {
                    const here = location.pathname + location.search;
                    return here === expected || location.pathname === '/login';
                }""",
                arg=final,
                timeout=15000,
            )
        except Exception as error:
            self._record(name, False, type(error).__name__)
            return
        here = page.evaluate("() => location.pathname + location.search")
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        status = 0
        if response is not None:
            status = response.status
        invalid = isinstance(text, str) and ("无效的 ID" in text or "无效的运行 ID" in text)
        landed = here == final
        clean = status < 400 and len(page_errors) == 0 and len(missing) == 0 and not invalid
        self._record(name, landed and clean, f"status={status} at={here}")

    def _request(
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
        try:
            self._document = response.json()
        except json.JSONDecodeError:
            self._document = None

    def _check(self, name: str, passed: bool, note: str) -> None:
        self._record(name, passed, note)

    def _record(self, name: str, passed: bool, note: str) -> None:
        if passed:
            self.pass_count += 1
        else:
            self.fail_count += 1
        self.checks.append(_Check(name, passed, note))

    def _stop(self, detail: str) -> None:
        self.fail_count += 1
        self.stopped = detail
        self.checks.append(_Check("fatal", False, detail))


def _ready(template: str, ids: dict[str, str]) -> str:
    filled = template
    for key in _ID_KEYS:
        token = "{" + key + "}"
        if token not in filled:
            continue
        value = ids[key]
        if value == "":
            return ""
        filled = filled.replace(token, value)
    return filled


def _success(document: object) -> str:
    return _get(document, "success")


def _child(document: object, dotted: str) -> object:
    current = document
    for key in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _get(document: object, dotted: str) -> str:
    current = _child(document, dotted)
    if isinstance(current, bool):
        if current:
            return "true"
        return "false"
    if current is None:
        return ""
    if isinstance(current, dict | list):
        return json.dumps(current, ensure_ascii=False)
    return str(current)
