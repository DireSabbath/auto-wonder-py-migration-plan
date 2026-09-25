"""评论写回的摘要、重试边界和通用 HTTP 回写。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx

from autowonder.integrations.aone_api import AoneConfig
from autowonder.integrations.aone_codec import AoneOpenApiError
from autowonder.integrations.aone_outbox import _ambiguous, _retryable
from autowonder.integrations.comment_outbound import format_external_comment
from autowonder.integrations.generic_writeback import update_generic_content
from autowonder.integrations.operation_keys import (
    aone_comment_key,
    operation_marker,
    text_digest,
)


def test_comment_digest_normalizes_newlines() -> None:
    assert text_digest("a\r\nb") == text_digest("a\nb")
    assert text_digest(None) == text_digest("")


def test_comment_key_and_marker_are_stable() -> None:
    key = aone_comment_key(12, 34)
    assert key.startswith("aone.comment:")
    assert operation_marker(key).startswith("<!-- aw-op:")
    assert operation_marker(key).endswith(" -->")


def test_external_comment_names_the_actor() -> None:
    text = format_external_comment("  小明  ", " 用户: 小明（ID: 7） ", "  正文  ")
    assert text == "AutoWonder · 小明\n来源：用户: 小明（ID: 7）\n\n正文"
    blank = format_external_comment(None, None, None)
    assert blank.startswith("AutoWonder · 系统\n来源：AutoWonder 系统")


def test_retry_stops_after_ten_attempts() -> None:
    earlier = SimpleNamespace(retry_count=8)
    last = SimpleNamespace(retry_count=9)
    assert _retryable(earlier, RuntimeError("again")) is True
    assert _retryable(last, RuntimeError("again")) is False
    assert _retryable(earlier, AoneOpenApiError("rejected", True)) is False


def test_network_and_non_json_results_are_ambiguous() -> None:
    try:
        raise AoneOpenApiError("Aone request failed: down") from httpx.ConnectError("down")
    except AoneOpenApiError as error:
        assert _ambiguous(error) is True
    assert _ambiguous(AoneOpenApiError("Aone returned non-JSON response: HTTP 500")) is True
    assert _ambiguous(AoneOpenApiError("business rejection")) is False


class _Capture:
    def __init__(self) -> None:
        self.path = ""
        self.body = ""
        self.authorization = ""
        self.provider = ""


class _Handler(BaseHTTPRequestHandler):
    capture = _Capture()

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _Handler.capture.path = self.path
        _Handler.capture.body = self.rfile.read(length).decode()
        _Handler.capture.authorization = self.headers.get("Authorization", "")
        _Handler.capture.provider = self.headers.get("X-AutoWonder-Provider", "")
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_generic_content_writeback_puts_json() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        update_generic_content(
            "JIRA",
            AoneConfig(
                "http://" + str(host) + ":" + str(port) + "/",
                "client-1",
                "secret-1",
                None,
            ),
            "WI 1/2",
            "标题",
            "正文",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert _Handler.capture.path == "/api/workitems/WI%201%2F2/content"
    assert _Handler.capture.authorization == "Bearer secret-1"
    assert _Handler.capture.provider == "JIRA"
    payload = json.loads(_Handler.capture.body)
    assert payload["provider"] == "JIRA"
    assert payload["externalWorkitemId"] == "WI 1/2"
    assert payload["title"] == "标题"
    assert payload["contentMd"] == "正文"
