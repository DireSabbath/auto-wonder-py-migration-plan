"""把一条待发送评论投到本机假 Aone，并写成已成功。

凭据只留在这次进程里。
"""

import asyncio
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from sqlalchemy import select

from autowonder.config import get_settings
from autowonder.db.session import SessionLocal
from autowonder.integrations.aone_codec import aone_enabled
from autowonder.integrations.aone_outbox import dispatch_pending
from autowonder.integrations.aone_service import crypto
from autowonder.integrations.models import (
    ExternalCommentLink,
    ExternalProjectBinding,
    IntegrationOutbox,
)

_COMMENT = "outbox-live-body"
_EXTERNAL_ID = "88001"
_SECRET = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def outbox_flow(base_url: str) -> dict[str, object]:
    """注册工作空间后插入一条评论写回，并核对假 Aone 收到正文。"""
    os.environ["AUTOWONDER_AONE_ENABLED"] = "true"
    os.environ["AUTOWONDER_SECRET_MASTER_KEY"] = _SECRET
    get_settings.cache_clear()
    return asyncio.run(_run(base_url.rstrip("/")))


class _Capture:
    def __init__(self) -> None:
        self.path = ""
        self.body = ""


class _Handler(BaseHTTPRequestHandler):
    capture = _Capture()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        _Handler.capture.path = self.path
        _Handler.capture.body = raw.decode()
        payload = ('{"success":true,"result":{"id":' + _EXTERNAL_ID + "}}").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


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
        self.client = httpx.Client(timeout=30.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self._access = ""
        self._document: object = None

    def close(self) -> None:
        self.client.close()

    async def walk(self, aone_base: str) -> None:
        self.facts["aoneEnabled"] = aone_enabled()
        self._expect("aone_enabled", aone_enabled(), "Aone switch is on for this process")
        if self.stopped != "":
            return
        username = "aw-outbox-" + str(int(time.time()))
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
        await self._deliver(int(workspace_id), int(workitem_id), aone_base)

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "outbox",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    async def _deliver(self, tenant_id: int, workitem_id: int, aone_base: str) -> None:
        async with SessionLocal() as session:
            binding = ExternalProjectBinding(
                tenant_id=tenant_id,
                provider="AONE",
                external_project_id="proj-outbox",
                external_project_name="outbox",
                base_url=aone_base,
                client_key="aw-outbox",
                credential_ref=crypto().encrypt(_SECRET),
                region_id="1",
                writeback_staff_id="staff-1",
                enabled=1,
                is_deleted=0,
                version=0,
            )
            session.add(binding)
            await session.flush()
            row = IntegrationOutbox(
                tenant_id=tenant_id,
                provider="AONE",
                binding_id=binding.id,
                workitem_id=workitem_id,
                event_type="COMMENT_CREATE",
                payload_json={
                    "externalWorkitemId": "WI-outbox",
                    "content": _COMMENT,
                    "commentId": 1,
                },
                operation_key="outbox-live-" + str(int(time.time())),
                lock_version=0,
                status="PENDING",
                retry_count=0,
            )
            session.add(row)
            await session.flush()
            sent = await dispatch_pending(session, 20)
            await session.commit()
            await session.refresh(row)
            link = await session.scalar(
                select(ExternalCommentLink).where(
                    ExternalCommentLink.binding_id == binding.id,
                    ExternalCommentLink.external_comment_id == _EXTERNAL_ID,
                )
            )
        self.facts["sentCount"] = sent
        self.facts["outboxStatus"] = row.status
        note = "comment writeback succeeded"
        if row.status != "SUCCEEDED":
            note = row.status + " " + (row.last_error or "")
        self._expect("delivered", row.status == "SUCCEEDED" and sent == 1, note)
        received = (
            _Handler.capture.path.endswith("/issue/openapi/IssueTopService/createComment")
            and _COMMENT in _Handler.capture.body
        )
        self._expect("received", received, "fake Aone stored the comment body")
        linked = link is not None and link.direction == "OUTBOUND"
        self._expect("linked", linked, "outbound comment link is stored")

    def _register(self, username: str, password: str) -> None:
        self._api(
            "POST",
            "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": username + "@example.invalid",
                "nickname": "AW Outbox",
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
            {"name": "AW Outbox " + username, "description": "outbox"},
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
            {
                "workType": "REQ",
                "title": "outbox writeback",
                "contentMd": "send the comment",
                "priority": 2,
            },
        )
        workitem_id = _json_get(self._document, "data.id")
        self.facts["workitemId"] = workitem_id
        self._expect("workitem", _positive_id(workitem_id), "work item id")
        return workitem_id

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


async def _run(base_url: str) -> dict[str, object]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    chain = _Chain(base_url)
    try:
        await chain.walk("http://" + str(host) + ":" + str(port))
    finally:
        chain.close()
        server.shutdown()
    return chain.verdict()


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
    return str(current)


def _positive_id(value: str) -> bool:
    return value.isdecimal() and value != "0"
