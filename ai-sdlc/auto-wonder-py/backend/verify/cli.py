"""验证命令：smoke、authchain、logscan、dispatch-e2e 与 pages。"""

import argparse
import json
from pathlib import Path
from urllib.request import urlopen

from verify.authchain import authchain
from verify.dispatch_e2e import dispatch_e2e
from verify.logscan import logscan


def smoke(base_url: str) -> dict[str, object]:
    """请求 /api/hello，核对 Result 信封。"""
    with urlopen(base_url + "/api/hello", timeout=10) as response:
        body = json.loads(response.read().decode())
    return {
        "command": "smoke",
        "ok": body.get("success") is True and body.get("code") == "0",
        "body": body,
    }


def main(argv: list[str] | None = None) -> int:
    """解析子命令并打印 JSON verdict。"""
    parser = argparse.ArgumentParser(prog="verify")
    parser.add_argument("command")
    parser.add_argument("--base-url", default="http://127.0.0.1:7002")
    parser.add_argument("--app-log", default="")
    parser.add_argument("--file-log", default="")
    parser.add_argument("--bodies", default="")
    parser.add_argument("--app-start-line", type=int, default=0)
    parser.add_argument("--file-start-line", type=int, default=0)
    args = parser.parse_args(argv)
    if args.command == "smoke":
        verdict = smoke(args.base_url)
    elif args.command == "authchain":
        verdict = authchain(args.base_url, _bodies(args.bodies))
    elif args.command == "logscan":
        verdict = logscan(
            Path(args.app_log),
            _bodies(args.bodies),
            _optional_path(args.file_log),
            args.app_start_line,
            args.file_start_line,
        )
    elif args.command == "dispatch-e2e":
        verdict = dispatch_e2e(args.base_url)
    elif args.command == "pages":
        from verify.pages import pages

        verdict = pages(args.base_url)
    else:
        verdict = {
            "command": args.command,
            "ok": False,
            "detail": "command is registered in the harness and not implemented yet",
        }
    print(json.dumps(verdict, ensure_ascii=False))
    if verdict["ok"] is True:
        return 0
    return 1


def _bodies(path: str) -> Path | None:
    if path == "":
        return None
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _optional_path(path: str) -> Path | None:
    if path == "":
        return None
    return Path(path)
