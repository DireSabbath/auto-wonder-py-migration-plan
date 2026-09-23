"""从只读 Java 树单向拷贝契约资产。不写回 Java 子树。"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = ROOT.parent


def java_root() -> Path:
    """Java 工程根目录。"""
    env = os.environ.get("AUTOWONDER_JAVA_ROOT")
    if env:
        return Path(env)
    return PY_ROOT.parent / "auto-wonder"


def copy_file(source: Path, target: Path) -> None:
    """覆盖拷贝单个文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_tree(source: Path, target: Path) -> None:
    """覆盖目录，保留目标里的 vite.py-backend.config.ts。"""
    preserved = target / "vite.py-backend.config.ts"
    preserved_text = preserved.read_text(encoding="utf-8") if preserved.exists() else None
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("node_modules", "dist", ".git"),
    )
    if preserved_text is not None:
        preserved.write_text(preserved_text, encoding="utf-8")


def write_tenant_tables(java: Path) -> None:
    """把 TenantTables.java 的表名写成 Python frozenset。"""
    text = (java / "src/main/java/com/aliyun/autowonder/tenant/TenantTables.java").read_text(
        encoding="utf-8"
    )
    names = re.findall(r'"([a-z0-9_]+)"', text)
    body = "\n".join(f'        "{name}",' for name in names)
    module = ROOT / "src" / "autowonder" / "db" / "tenant_tables.py"
    module.write_text(
        '"""38 张 tenant 表。由 sync_from_java.py 按 TenantTables.java 覆写。"""\n\n'
        "TENANT_TABLES = frozenset(\n"
        "    {\n"
        f"{body}\n"
        "    }\n"
        ")\n",
        encoding="utf-8",
    )
    fixture = ROOT / "src" / "autowonder" / "db" / "tenant_tables.json"
    import json

    fixture.write_text(json.dumps(names, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """拷贝 schema、种子、协议、前端源码和 tenant 清单。"""
    java = java_root()
    copy_file(
        java / "docs" / "autowonder-schema.sql",
        ROOT / "verify" / "initdb" / "001-schema.sql",
    )
    copy_file(
        java / "docs" / "autowonder-community-templates.sql",
        ROOT / "verify" / "initdb" / "002-templates.sql",
    )
    copy_file(
        java / "docs" / "scheduler-executor-protocol.md",
        ROOT / "docs" / "scheduler-executor-protocol.md",
    )
    copy_file(
        java / "docs" / "openapi-reference.md",
        ROOT / "docs" / "openapi-reference.md",
    )
    copy_tree(java / "frontend", PY_ROOT / "frontend")
    write_tenant_tables(java)
    print(f"synced from {java}")


if __name__ == "__main__":
    main()
