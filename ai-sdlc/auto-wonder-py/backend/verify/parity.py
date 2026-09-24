"""对照 Java Controller 清单检查 Python 路由是否齐全。

路径参数只保留花括号，名称不同仍算同一条。Java 进程不在时，响应还没有对拍。
"""

import json
import re
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import yaml

CATALOG = Path(__file__).resolve().parent / "parity" / "cases" / "endpoints.yaml"
_PARAM = re.compile(r"\{[^}/]+\}")
_SKIP_METHODS = frozenset({"HEAD", "OPTIONS"})


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
    """清单盖住才算目录通过。Java 的响应比对还要等黄金主在线。"""
    catalog = load_catalog()
    missing = missing_endpoints(catalog, python_route_keys())
    catalog_ok = len(missing) == 0
    behavior = java_behavior(java_url)
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


def java_behavior(java_url: str) -> dict[str, object]:
    """探测 Java ``/api/hello``。连不上就不把响应比对标成完成。"""
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
    return {
        "ok": False,
        "compared": False,
        "detail": "java is reachable and normalized response comparison has not run",
    }
