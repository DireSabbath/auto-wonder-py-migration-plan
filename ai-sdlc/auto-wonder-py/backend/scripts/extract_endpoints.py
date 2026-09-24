"""从 Java Controller 源码提取全量端点清单，作为 parity 基准。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "verify" / "parity" / "cases" / "endpoints.yaml"

MAPPING_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b",
)
CLASS_MAPPING_RE = re.compile(
    r"@RequestMapping\s*(\((?:[^()]|\([^()]*\))*\))?",
)


def java_root() -> Path:
    """只读 Java 工程根目录。"""
    env = os.environ.get("AUTOWONDER_JAVA_ROOT")
    if env:
        return Path(env)
    return ROOT.parents[1] / "auto-wonder"


def balanced(text: str, start: int) -> str:
    """从 start 指向的左括号读到匹配的右括号。"""
    depth = 0
    quote = None
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if char == quote and text[index - 1] != "\\":
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def string_literals(block: str) -> list[str]:
    """取出注解参数里的字符串字面量。"""
    return re.findall(r'"([^"]*)"', block)


def methods_of(block: str) -> list[str]:
    """RequestMapping 上声明的 HTTP 方法；缺省为 GET。"""
    found = re.findall(r"RequestMethod\.([A-Z]+)", block)
    if found:
        return found
    return ["GET"]


def class_prefix(text: str) -> list[str]:
    """类声明之前的 @RequestMapping 路径。方法上的注解不是前缀。"""
    head = text.partition(" class ")[0]
    match = CLASS_MAPPING_RE.search(head)
    if match is None or match.group(1) is None:
        return [""]
    paths = string_literals(match.group(1))
    return paths or [""]


def join_path(prefix: str, path: str) -> str:
    """拼接类前缀与方法路径。"""
    if path == "":
        return prefix or "/"
    if prefix.endswith("/") and path.startswith("/"):
        return prefix[:-1] + path
    if prefix and not path.startswith("/"):
        return prefix + "/" + path
    return prefix + path


def endpoints_in(text: str, class_name: str) -> list[dict[str, str]]:
    """提取一个 Controller 文件中的端点。"""
    prefixes = class_prefix(text)
    found: list[dict[str, str]] = []
    for match in MAPPING_RE.finditer(text):
        kind = match.group(1)
        if match.start() < text.find(" class "):
            continue
        block = ""
        if match.end() < len(text) and text[match.end()] == "(":
            block = balanced(text, match.end())
        paths = string_literals(block) or [""]
        if kind == "Request":
            verbs = methods_of(block)
        else:
            verbs = [kind.upper()]
        for prefix in prefixes:
            for path in paths:
                for verb in verbs:
                    found.append(
                        {
                            "method": verb,
                            "path": join_path(prefix, path),
                            "controller": class_name,
                        }
                    )
    return found


def extract(root: Path) -> list[dict[str, str]]:
    """扫描全部 Controller。"""
    java = root / "src" / "main" / "java"
    rows: list[dict[str, str]] = []
    for path in sorted(java.rglob("*Controller.java")):
        text = path.read_text(encoding="utf-8")
        rows.extend(endpoints_in(text, path.stem))
    rows.sort(key=lambda row: (row["path"], row["method"], row["controller"]))
    return rows


def main() -> None:
    """写出 YAML 清单。"""
    rows = extract(java_root())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        yaml.safe_dump(rows, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"endpoints={len(rows)} file={OUTPUT}")


if __name__ == "__main__":
    main()
