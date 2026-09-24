"""对照 Java Controller 清单检查 Python 路由，并在双栈在线时比对响应。

路径参数只保留花括号，名称不同仍算同一条。比对时去掉 id、时间戳、
traceId、token，并把各栈自己的 7001/7002 根地址收成同一占位符。
未登录请求不带正文。这些端点在进入业务写入前就会拒绝。
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

CATALOG = Path(__file__).resolve().parent / "parity" / "cases" / "endpoints.yaml"
_PARAM = re.compile(r"\{[^}/]+\}")
_SKIP_METHODS = frozenset({"HEAD", "OPTIONS"})
_STACK_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1):700[12]")
_VOLATILE_KEYS = frozenset(
    {
        "id",
        "traceId",
        "request_id",
        "token",
        "accessToken",
        "refreshToken",
        "gmtCreate",
        "gmtModified",
        "createdAt",
        "updatedAt",
    }
)


def normalize_path(path: str) -> str:
    """去掉路径参数名。``{id}`` 与 ``{workspace_id}`` 归一成 ``{}``。"""
    return _PARAM.sub("{}", path)


def load_catalog(path: Path = CATALOG) -> list[dict[str, str]]:
    """读出端点清单。"""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def python_route_keys() -> set[tuple[str, str]]:
    """当前应用上的方法与规范化路径。``HEAD`` 与 ``OPTIONS`` 不计入清单。"""
    from autowonder.main import create_app

    app = create_app()
    keys: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods is None or path is None:
            continue
        for method in methods:
            if method in _SKIP_METHODS:
                continue
            keys.add((method, normalize_path(path)))
    return keys


def missing_endpoints(
    catalog: list[dict[str, str]],
    routes: set[tuple[str, str]],
) -> list[dict[str, str]]:
    """清单里有、应用上没有的端点。"""
    missing: list[dict[str, str]] = []
    for row in catalog:
        key = (row["method"], normalize_path(row["path"]))
        if key not in routes:
            missing.append(
                {
                    "method": row["method"],
                    "path": row["path"],
                    "controller": row["controller"],
                }
            )
    return missing


def parity(base_url: str, java_url: str) -> dict[str, object]:
    """清单盖住，并且未登录响应与 Java 一致，才算对拍通过。"""
    catalog = load_catalog()
    missing = missing_endpoints(catalog, python_route_keys())
    catalog_ok = len(missing) == 0
    behavior = java_behavior(base_url, java_url, catalog)
    return {
        "command": "parity",
        "ok": catalog_ok and behavior["ok"] is True,
        "pythonBaseUrl": base_url,
        "javaBaseUrl": java_url,
        "catalog": {
            "ok": catalog_ok,
            "java": len(catalog),
            "covered": len(catalog) - len(missing),
            "missing": missing,
        },
        "behavior": behavior,
    }


def concrete_path(path: str) -> str:
    """把路径参数换成稳定的 ``1``，两边请求同一条具体路径。"""
    return _PARAM.sub("1", path)


def normalize_document(value: object) -> object:
    """去掉会随调用变化、或由数据库分配的字段。"""
    if isinstance(value, dict):
        kept: dict[str, object] = {}
        for key, item in value.items():
            if key in _VOLATILE_KEYS:
                continue
            kept[key] = normalize_document(item)
        return kept
    if isinstance(value, list):
        return [normalize_document(item) for item in value]
    if isinstance(value, str):
        return _STACK_URL.sub("{stack}", value)
    return value


def read_response(base_url: str, method: str, path: str, timeout: float = 8) -> dict[str, Any]:
    """发出一条未登录请求，并收成可比较的状态码与正文。"""
    request = Request(
        base_url.rstrip("/") + path,
        method=method,
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
    except HTTPError as error:
        status = error.code
        content_type = error.headers.get("Content-Type", "")
        raw = error.read()
    return {"status": status, "body": decode_body(raw, content_type)}


def decode_body(raw: bytes, content_type: str) -> object:
    """JSON 走规范化对象；文本保留原文；其余比较摘要。"""
    if raw == b"":
        return None
    media = content_type.split(";")[0].strip().lower()
    if media == "application/json" or media.endswith("+json"):
        text = raw.decode()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return normalize_document(text)
        return normalize_document(parsed)
    if media.startswith("text/"):
        return normalize_document(raw.decode())
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byteLength": len(raw),
    }


def compare_catalog(
    python_url: str,
    java_url: str,
    catalog: list[dict[str, str]],
) -> dict[str, object]:
    """逐条清单比对未登录、无正文的响应。"""
    mismatches: list[dict[str, object]] = []
    compared = 0
    for row in catalog:
        method = row["method"]
        path = concrete_path(row["path"])
        python_response = read_response(python_url, method, path)
        java_response = read_response(java_url, method, path)
        compared += 1
        if python_response != java_response:
            mismatches.append(
                {
                    "method": method,
                    "path": row["path"],
                    "python": python_response,
                    "java": java_response,
                }
            )
    return {
        "ok": len(mismatches) == 0,
        "compared": True,
        "endpoints": compared,
        "mismatches": mismatches,
    }


def java_behavior(
    python_url: str,
    java_url: str,
    catalog: list[dict[str, str]],
) -> dict[str, object]:
    """先确认 Java ``/api/hello`` 可访问，再比对清单响应。"""
    try:
        with urlopen(java_url.rstrip("/") + "/api/hello", timeout=3) as response:
            body = json.loads(response.read().decode())
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        return {
            "ok": False,
            "compared": False,
            "detail": "java stack is offline",
            "error": type(error).__name__,
        }
    if body.get("success") is not True:
        return {
            "ok": False,
            "compared": False,
            "detail": "java hello did not succeed",
        }
    report = compare_catalog(python_url, java_url, catalog)
    if report["ok"] is True:
        report["detail"] = "normalized unauthenticated responses match"
        return report
    report["detail"] = "normalized unauthenticated responses differ"
    return report
