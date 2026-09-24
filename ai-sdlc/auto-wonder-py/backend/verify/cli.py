"""验证命令：smoke、authchain 与 dispatch-e2e 检查正在运行的 Python 服务。"""

import argparse
import json
from urllib.request import urlopen

from verify.authchain import authchain
from verify.dispatch_e2e import dispatch_e2e


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
    args = parser.parse_args(argv)
    if args.command == "smoke":
        verdict = smoke(args.base_url)
    elif args.command == "authchain":
        verdict = authchain(args.base_url)
    elif args.command == "dispatch-e2e":
        verdict = dispatch_e2e(args.base_url)
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
