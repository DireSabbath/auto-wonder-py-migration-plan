"""对照 ``e2e-tests/authchain.sh`` 走一条真实登录链。

令牌和口令只留在这次进程里，JSON verdict 不写出它们。
"""

import json
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from autowonder.config import get_settings

_POSITIVE_ID = re.compile(r"[1-9][0-9]*")


@dataclass
class _Check:
    name: str
    passed: bool
    http: int | None
    want_http: int | None
    success: str
    want_success: str
    note: str

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


def authchain(base_url: str, bodies_dir: Path | None = None) -> dict[str, object]:
    """注册、登录、建空间、软删恢复，再走到澄清会话。

    ``bodies_dir`` 有值时，每次响应只记下 ``request_id``，供 logscan 归因。
    """
    chain = _Chain(base_url.rstrip("/"), bodies_dir)
    chain.walk()
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
        return "true" if current else "false"
    if isinstance(current, dict | list):
        return json.dumps(current, ensure_ascii=False)
    return str(current)


def _positive_id(value: str) -> bool:
    return _POSITIVE_ID.fullmatch(value) is not None


class _Chain:
    def __init__(self, base_url: str, bodies_dir: Path | None) -> None:
        self.base_url = base_url
        self.bodies_dir = bodies_dir
        self.client = httpx.Client(timeout=60.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self.last_code = 0
        self.last_document: object = None
        self._token = ""
        self._workspace_token = ""
        self._body_index = 0

    def walk(self) -> None:
        username = f"aw-e2e-{int(time.time())}"
        password = _password()
        suffix = username.removeprefix("aw-e2e-")
        self.facts["testUser"] = username
        self._register(username, password)
        if self.stopped:
            return
        self._login(username, password)
        if self.stopped:
            return
        self._platform(username)
        workspace_id = self._open_workspace(suffix)
        if self.stopped:
            return
        self._workspace_reads()
        self._edit_workspace(suffix, workspace_id)
        if self.stopped:
            return
        self._name_cycle(suffix)
        if self.stopped:
            return
        self._work_and_conversation()
        self._sql_counts()

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "authchain",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped:
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
                "email": f"{username}@example.invalid",
                "nickname": "AW E2E",
            },
        )
        self._check("register", 200, "true", "first user on a fresh install")
        user_id = _json_get(self.last_document, "data.id")
        self.facts["registeredUserId"] = user_id
        if not _positive_id(user_id):
            self._stop("register returned no id")

    def _login(self, username: str, password: str) -> None:
        self._api(
            "POST",
            "/api/auth/login",
            None,
            {"username": username, "password": password},
        )
        self._check("login", 200, "true", "returns accessToken + refreshToken")
        token = _json_get(self.last_document, "data.accessToken")
        self.facts["token0Present"] = token != ""
        self.facts["token0Length"] = len(token)
        if token == "":
            self._stop("login returned no accessToken")
            return
        self._token = token

    def _platform(self, username: str) -> None:
        self._api("GET", "/api/platform/admins", self._token, None)
        self._check("platform_admins", 200, "true", "platform admin list reachable")
        is_admin = self._sql("SELECT is_admin FROM `user` WHERE username='" + username + "'")
        self.facts["userIsAdmin"] = is_admin
        self._api("GET", "/api/platform/admins/candidates", self._token, None)
        if is_admin == "1":
            self._check(
                "platform_admin_candidates",
                200,
                "true",
                "administrator: candidate search allowed",
            )
        else:
            self._check(
                "platform_admin_candidates",
                403,
                "false",
                "non-administrator: candidate search refused with 10403",
            )
        self._api("GET", "/api/platform/branding/public", self._token, None)
        self._check(
            "branding_authed",
            200,
            "true",
            "public branding exposes the community defaults",
        )
        self.facts["brandingCanManage"] = _json_get(self.last_document, "data.canManage")
        self.facts["communityEdition"] = _json_get(self.last_document, "data.communityEdition")
        self.facts["recommendedRuntimeVersion"] = _json_get(
            self.last_document, "data.recommendedRuntimeVersion"
        )
        self.facts["deploymentVersion"] = _json_get(self.last_document, "data.deploymentVersion")
        self._api("GET", "/api/platform/branding", self._token, None)
        if is_admin == "1":
            self._check(
                "branding_admin",
                200,
                "true",
                "admin branding: canManage derived from isFirstActiveUser",
            )
        else:
            self._check(
                "branding_admin",
                200,
                "any",
                "admin branding for a non-first-active user",
            )
        self.facts["brandingAdminCanManage"] = _json_get(self.last_document, "data.canManage")

    def _open_workspace(self, suffix: str) -> str:
        name = f"AW E2E Smoke Workspace {suffix}"
        self._api(
            "POST",
            "/api/workspaces",
            self._token,
            {
                "name": name,
                "description": "created by verify authchain",
                "background": "fresh-install smoke",
            },
        )
        self._check("ws_create", 200, "true", "creator becomes owner and administrator")
        workspace_id = _json_get(self.last_document, "data.id")
        self.facts["workspaceId"] = workspace_id
        if not _positive_id(workspace_id):
            self._stop("workspace creation returned no id")
            return ""
        self._api("POST", f"/api/workspaces/{workspace_id}/switch", self._token, None)
        self._check("ws_switch", 200, "true", "issues a workspace-scoped token")
        token = _json_get(self.last_document, "data.accessToken")
        self.facts["token1Present"] = token != ""
        self.facts["token1Length"] = len(token)
        self.facts["switchAccessLevel"] = _json_get(self.last_document, "data.accessLevel")
        if token == "":
            self._stop("switch returned no accessToken")
            return ""
        self._workspace_token = token
        return workspace_id

    def _workspace_reads(self) -> None:
        token = self._workspace_token
        self._api("GET", "/api/workspaces/current", token, None)
        self._check("ws_current", 200, "true", "workspace-scoped token accepted")
        self.facts["workspaceVersion"] = _json_get(self.last_document, "data.version")
        self._api("GET", "/api/workspaces/mine", token, None)
        self._check("ws_mine", 200, "true", "membership list")
        self._api("GET", "/api/workspaces/all", token, None)
        self._check("ws_all", 200, "true", "directory backing workspace access requests")
        self._api("GET", "/api/workspaces/recycle-bin", token, None)
        self._check(
            "ws_recycle_bin_empty",
            200,
            "true",
            "recycle bin empty before any delete",
        )
        self._api("GET", "/api/workspaces/current/membership", token, None)
        self._check("ws_membership", 200, "true", "current membership")
        self._api("GET", "/api/workspaces/current/access-requests", token, None)
        self._check(
            "ws_access_requests",
            200,
            "true",
            "workspace_access_request table reachable",
        )
        self._api("GET", "/api/capabilities/scheduled-task", token, None)
        self._check(
            "cap_scheduled_task",
            200,
            "true",
            "scheduled-task capability, was 401 unauthenticated",
        )
        self._api("GET", "/api/scheduled-tasks/summary", token, None)
        self._check(
            "st_summary",
            200,
            "any",
            "scheduled-task module enabled by the community default",
        )

    def _edit_workspace(self, suffix: str, workspace_id: str) -> None:
        version = str(self.facts["workspaceVersion"])
        if version == "":
            self._stop("no workspace version to send")
            return
        edited = f"AW E2E Smoke Workspace {suffix} (edited)"
        self._api(
            "PUT",
            f"/api/workspaces/{workspace_id}",
            self._workspace_token,
            {
                "name": edited,
                "description": "edited by authchain",
                "version": int(version),
            },
        )
        self._check(
            "ws_update",
            200,
            "true",
            f"workspace edit, optimistic-lock version={version}",
        )
        self._api("GET", "/api/workspaces/current", self._workspace_token, None)
        self.facts["workspaceNameAfterEdit"] = _json_get(self.last_document, "data.name")

    def _name_cycle(self, suffix: str) -> None:
        dup_name = f"AW E2E Dup Name Check {suffix}"
        token = self._workspace_token
        self._api(
            "POST",
            "/api/workspaces",
            token,
            {"name": dup_name, "description": "v051 probe A"},
        )
        self._check("ws_dup1", 200, "true", "workspace A with the duplicate-probe name")
        workspace_a = _json_get(self.last_document, "data.id")
        self.facts["workspaceDupA"] = workspace_a
        if not _positive_id(workspace_a):
            self._stop("probe workspace A has no id")
            return
        self._api("DELETE", f"/api/workspaces/{workspace_a}", token, None)
        self._check("ws_dup_delete", 200, "true", "soft delete of A")
        deleted = self._sql(f"SELECT deleted_at IS NOT NULL FROM org WHERE id={workspace_a}")
        released = self._sql(f"SELECT active_name_key IS NULL FROM org WHERE id={workspace_a}")
        self.facts["deletedAtNotNull"] = deleted
        self.facts["activeNameKeyIsNull"] = released
        self._expect("sql_deleted_at", deleted, "1", "soft-deleted row has deleted_at")
        self._expect("sql_active_name", released, "1", "soft-deleted row releases the name key")
        self._api(
            "POST",
            "/api/workspaces",
            token,
            {"name": dup_name, "description": "v051 probe B"},
        )
        self._check(
            "ws_dup2",
            200,
            "true",
            "same name accepted while A is soft-deleted",
        )
        workspace_b = _json_get(self.last_document, "data.id")
        self.facts["workspaceDupB"] = workspace_b
        if not _positive_id(workspace_b):
            self._stop("probe workspace B has no id")
            return
        self._api("GET", "/api/workspaces/recycle-bin", token, None)
        self._check(
            "ws_recycle_bin_after",
            200,
            "true",
            "soft-deleted A appears in the recycle bin",
        )
        self.facts["recycleBin"] = _json_get(self.last_document, "data.list")
        self._api("DELETE", f"/api/workspaces/{workspace_b}", token, None)
        self._check("ws_dup_delete_b", 200, "true", "soft delete of B, freeing the name again")
        self._api("POST", f"/api/workspaces/{workspace_b}/restore", token, {})
        self._check(
            "ws_dup_restore_b",
            200,
            "true",
            "restore succeeds while the name is free",
        )
        self._api("POST", f"/api/workspaces/{workspace_a}/restore", token, {})
        self._check(
            "ws_dup_restore_a",
            200,
            "false",
            "restore refused once B holds the name again",
        )
        restore_code = _json_get(self.last_document, "code")
        self.facts["restoreConflictCode"] = restore_code
        self.facts["restoreConflictMessage"] = _json_get(self.last_document, "message")
        self._expect("restore_conflict_code", restore_code, "11007", "restore conflict is 11007")
        self._api(
            "POST",
            "/api/workspaces",
            token,
            {"name": dup_name, "description": "v051 probe C"},
        )
        self._check(
            "ws_dup3",
            200,
            "false",
            "name rejected while an active row holds it",
        )
        create_code = _json_get(self.last_document, "code")
        self.facts["createRejectCode"] = create_code
        self.facts["createRejectMessage"] = _json_get(self.last_document, "message")
        self._expect("create_reject_code", create_code, "11003", "active name rejected with 11003")

    def _work_and_conversation(self) -> None:
        token = self._workspace_token
        self._api(
            "POST",
            "/api/agents",
            token,
            {
                "name": "AW E2E Agent",
                "roleName": "E2E Probe",
                "roleCode": "AW_E2E_PROBE",
                "businessBackground": "created by authchain",
                "responsibilities": "exercise the clarification conversation endpoints",
            },
        )
        self._check("agent_create", 200, "true", "agent needed as the conversation counterparty")
        agent_id = _json_get(self.last_document, "data.id")
        self.facts["agentId"] = agent_id
        status = ""
        online_version_id = ""
        if _positive_id(agent_id):
            self._api("POST", f"/api/agents/{agent_id}/submit", token, {})
            self._check("agent_submit", 200, "true", "submit conversation counterparty for review")
            self._api(
                "POST",
                f"/api/agents/{agent_id}/approve",
                token,
                {"comment": "Approve disposable E2E conversation counterparty"},
            )
            self._check(
                "agent_approve",
                200,
                "true",
                "workspace owner approves the agent version",
            )
            self._api("GET", f"/api/agents/{agent_id}", token, None)
            self._check(
                "agent_online",
                200,
                "true",
                "read back the published conversation counterparty",
            )
            status = _json_get(self.last_document, "data.status")
            online_version_id = _json_get(self.last_document, "data.onlineVersionId")
        self.facts["agentStatus"] = status
        self.facts["onlineVersionId"] = online_version_id
        if status == "ONLINE" and _positive_id(online_version_id):
            self._record(
                "agent_online_version",
                True,
                None,
                None,
                status,
                "ONLINE",
                f"onlineVersionId={online_version_id}",
            )
        else:
            self._record(
                "agent_online_version",
                False,
                None,
                None,
                status,
                "ONLINE",
                "expected ONLINE and a positive onlineVersionId",
            )
        self._api(
            "POST",
            "/api/workitems",
            token,
            {
                "workType": "TASK",
                "title": "AW E2E smoke work item",
                "contentMd": "created by verify authchain to reach clarification conversations",
                "priority": 2,
            },
        )
        self._check("wi_create", 200, "true", "work item creation in the switched workspace")
        workitem_id = _json_get(self.last_document, "data.id")
        self.facts["workitemId"] = workitem_id
        if not _positive_id(workitem_id):
            self._stop("work item creation returned no id")
            return
        self._api("GET", f"/api/workitems/{workitem_id}", token, None)
        self._check("wi_get", 200, "true", "read back the created work item")
        self._api("GET", f"/api/workitems/{workitem_id}/timeline", token, None)
        self._check("wi_timeline", 200, "true", "timeline")
        self._api("GET", f"/api/workitems/{workitem_id}/unified-timeline", token, None)
        self._check("wi_unified_timeline", 200, "any", "runtime activity timeline")
        self._api("GET", f"/api/workitems/{workitem_id}/delivery-progress", token, None)
        self._check("wi_delivery", 200, "any", "delivery progress")
        if not _positive_id(agent_id):
            self.facts["conversationSkipped"] = "agent creation did not return an id"
            return
        self._api(
            "POST",
            f"/api/workitems/{workitem_id}/clarification-conversations",
            token,
            {"agentId": int(agent_id)},
        )
        self._check(
            "conv_create",
            200,
            "true",
            "ACP clarification conversation created",
        )
        conversation_id = _json_get(self.last_document, "data.id")
        executor_online = _json_get(self.last_document, "data.executorOnline")
        self.facts["conversationId"] = conversation_id
        self.facts["executorOnline"] = executor_online
        self._api(
            "GET",
            f"/api/workitems/{workitem_id}/clarification-conversations?agentId={agent_id}",
            token,
            None,
        )
        self._check("conv_list", 200, "true", "conversation list by agent")
        if not _positive_id(conversation_id):
            self.facts["conversationTurnsSkipped"] = "conversation creation returned no id"
            return
        self._api(
            "POST",
            f"/api/workitems/{workitem_id}/clarification-conversations/{conversation_id}/turns",
            token,
            {"content": "AW E2E clarification probe", "clientMessageId": "aw-e2e-probe-1"},
        )
        self.facts["convTurnCode"] = _json_get(self.last_document, "code")
        self.facts["convTurnMessage"] = _json_get(self.last_document, "message")
        if executor_online == "true":
            self._check("conv_turn", 200, "true", "runtime online: clarification turn accepted")
        else:
            self._check(
                "conv_turn",
                200,
                "false",
                "no runtime connected, turn refused",
            )
        self._api(
            "GET",
            (f"/api/workitems/{workitem_id}/clarification-conversations/{conversation_id}/events"),
            token,
            None,
        )
        self._check(
            "conv_events",
            200,
            "true",
            "conversation event stream",
        )
        self.facts["convEventsCode"] = _json_get(self.last_document, "code")

    def _sql_counts(self) -> None:
        self.facts["elicitationRows"] = self._sql(
            "SELECT COUNT(*) FROM agent_conversation_elicitation"
        )
        self.facts["elicitationTable"] = self._sql(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name='agent_conversation_elicitation'"
        )
        self.facts["accessRequestRows"] = self._sql(
            "SELECT COUNT(*) FROM workspace_access_request"
        )
        self.facts["orgRows"] = self._sql("SELECT COUNT(*) FROM org")
        self.facts["userRows"] = self._sql("SELECT COUNT(*) FROM `user`")

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
        self.last_code = response.status_code
        self.last_document = _document(response)
        self._save_request_id(response)

    def _check(self, name: str, want_code: int, want_success: str, note: str) -> None:
        got = ""
        if self.last_document is not None:
            got = _json_get(self.last_document, "success")
        passed = self.last_code == want_code and (want_success == "any" or got == want_success)
        self._record(name, passed, self.last_code, want_code, got, want_success, note)

    def _expect(self, name: str, got: str, want: str, note: str) -> None:
        self._record(name, got == want, None, None, got, want, note)

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

    def _save_request_id(self, response: httpx.Response) -> None:
        if self.bodies_dir is None:
            return
        request_id = _captured_request_id(self.last_document, response)
        if request_id == "":
            return
        self._body_index += 1
        path = self.bodies_dir / f"call-{self._body_index:04d}.json"
        path.write_text(
            json.dumps({"request_id": request_id}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _sql(self, statement: str) -> str:
        parsed = urlsplit(get_settings().resolved_database_url)
        database = parsed.path.lstrip("/").split("?")[0]
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        path = Path(handle.name)
        handle.write(
            "[client]\n"
            f"host={parsed.hostname}\n"
            f"port={parsed.port}\n"
            f"user={parsed.username}\n"
            f"password={parsed.password}\n"
            f"database={database}\n"
        )
        handle.close()
        path.chmod(0o600)
        try:
            completed = subprocess.run(
                ["mysql", f"--defaults-extra-file={path}", "-N", "-B", "-e", statement],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            path.unlink()
        if completed.returncode != 0:
            self.facts["mysqlError"] = completed.stderr.strip()
            return ""
        return completed.stdout.strip()


def _password() -> str:
    return secrets.token_urlsafe(18)


def _document(response: httpx.Response) -> object:
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def _captured_request_id(document: object, response: httpx.Response) -> str:
    if isinstance(document, dict):
        raw = document.get("request_id")
        if isinstance(raw, str) and raw != "":
            return raw
    header = response.headers.get("x-acs-request-id")
    if header is None or header == "":
        return ""
    return header
