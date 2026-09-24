"""从 autowonder-schema.sql 生成各域 SQLAlchemy 模型。

表名到域包的映射与迁移方案 §5.3 一致。生成结果覆盖
``src/autowonder/<domain>/models.py`` 与 ``model_imports.py``。
"""

from __future__ import annotations

import keyword
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "autowonder"

DOMAIN_BY_TABLE: dict[str, str] = {
    "user": "users",
    "user_setting": "users",
    "platform_admin_init": "platform",
    "platform_branding_config": "platform",
    "platform_im_selection": "im",
    "platform_im_channel_config": "im",
    "user_im_identity": "im",
    "org": "workspaces",
    "org_member": "workspaces",
    "org_invite": "workspaces",
    "workspace_access_request": "workspaces",
    "audit_log": "audits",
    "status_template": "statemachines",
    "status_node": "statemachines",
    "status_transition": "statemachines",
    "scheduled_task": "scheduledtasks",
    "scheduled_task_run": "scheduledtasks",
    "workitem": "workitems",
    "workitem_comment": "workitems",
    "workitem_comment_mention": "workitems",
    "workitem_event": "workitems",
    "workitem_watcher": "workitems",
    "workitem_execution_control": "workitems",
    "clarification": "clarifications",
    "executor": "executors",
    "executor_update_task": "executors",
    "dispatch": "dispatch",
    "dispatch_recovery_checkpoint": "dispatch",
    "dispatch_runtime_event": "dispatch",
    "dispatch_recovery": "dispatch",
    "debug_log": "debuglogs",
    "artifact": "artifacts",
    "agent": "agents",
    "agent_version": "agents",
    "agent_environment_variable_ref": "agents",
    "agent_repo_perm": "agents",
    "agent_skill": "agents",
    "agent_memory_ref": "agents",
    "environment_variable": "environments",
    "squad": "squads",
    "squad_member": "squads",
    "squad_template": "squads",
    "ai_session": "ai",
    "ai_message": "ai",
    "repo": "repos",
    "repo_conclusion": "repos",
    "repo_relation": "repos",
    "memory": "memories",
    "memory_review": "memories",
    "skill": "skills",
    "evolution_proposal": "evolution",
    "evolution_evidence": "evolution",
    "sdlc": "sdlcs",
    "sdlc_step": "sdlcs",
    "notification": "notifications",
    "notify_pref": "notifications",
    "workitem_comment_delivery": "notifications",
    "ai_usage": "aiusage",
    "dispatch_ai_usage": "aiusage",
    "ai_quota": "aiusage",
    "system_setting": "settings",
    "external_principal": "integrations",
    "external_project_binding": "integrations",
    "external_workitem_link": "integrations",
    "external_workitem_import_record": "integrations",
    "external_comment_link": "integrations",
    "external_status_mapping": "integrations",
    "integration_outbox": "integrations",
    "aone_rate_bucket": "integrations",
    "dingtalk_robot_binding": "integrations",
    "feishu_robot_binding": "integrations",
    "feishu_message_inbox": "integrations",
    "mcp_access_token": "mcp",
    "agent_conversation": "conversations",
    "agent_conversation_turn": "conversations",
    "agent_conversation_turn_event": "conversations",
    "agent_conversation_elicitation": "conversations",
    "conversation_share": "conversations",
    "conversation_turn_artifact": "conversations",
    "conversation_action_plan": "conversations",
    "conversation_action_step": "conversations",
    "project_backup": "backups",
    "asset_category": "categories",
    "asset_category_ref": "categories",
}

EMPTY_PACKAGES = (
    "auth",
    "templates",
    "guidance",
    "taskpackages",
    "insights",
    "dashboards",
    "storage",
    "ws",
    "jobs",
)

TYPE_RE = re.compile(
    r"^(BIGINT UNSIGNED|BIGINT|INT UNSIGNED|INT|TINYINT|DOUBLE|FLOAT|"
    r"JSON|MEDIUMTEXT|LONGTEXT|TEXT|DATETIME\(\d+\)|DATETIME|"
    r"DECIMAL\(\d+,\s*\d+\)|CHAR\(\d+\)|VARCHAR\(\d+\))",
    re.IGNORECASE,
)

TABLE_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS `?(\w+)`? \((.*?)\)\s*ENGINE=InnoDB([^;]*);",
    re.DOTALL,
)


def java_schema_path() -> Path:
    """定位只读 Java 树中的 schema 契约。"""
    env = __import__("os").environ.get("AUTOWONDER_JAVA_ROOT")
    if env:
        return Path(env) / "docs" / "autowonder-schema.sql"
    return ROOT.parents[1] / "auto-wonder" / "docs" / "autowonder-schema.sql"


def split_items(body: str) -> list[str]:
    """按顶层逗号拆分列与索引定义。"""
    items: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in body:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
            current.append(char)
            continue
        if char == ")":
            depth -= 1
            current.append(char)
            continue
        if char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def class_name(table: str) -> str:
    """把物理表名转成模型类名。"""
    return "".join(part.capitalize() for part in table.split("_"))


def attribute_name(column: str) -> str:
    """列名作属性名；与关键字冲突时加尾随下划线。"""
    if keyword.iskeyword(column):
        return f"{column}_"
    return column


def sqlalchemy_type(sql_type: str) -> tuple[str, str]:
    """返回 (导入名, 列类型表达式)。"""
    normalized = sql_type.upper().replace(" ", "")
    if normalized == "BIGINTUNSIGNED" or normalized == "BIGINT":
        return "BigInteger", "BigInteger"
    if normalized in {"INTUNSIGNED", "INT", "TINYINT"}:
        return "Integer", "Integer"
    if normalized == "DOUBLE":
        return "Double", "Double"
    if normalized == "FLOAT":
        return "Float", "Float"
    if normalized == "JSON":
        return "JSON", "JSON"
    if normalized in {"TEXT", "MEDIUMTEXT", "LONGTEXT"}:
        return "Text", "Text"
    if normalized.startswith("DATETIME"):
        return "DateTime", "DateTime"
    if normalized.startswith("DECIMAL"):
        precision, scale = re.findall(r"\d+", normalized)
        return "Numeric", f"Numeric({precision}, {scale})"
    if normalized.startswith("CHAR") or normalized.startswith("VARCHAR"):
        length = re.search(r"\d+", normalized).group(0)
        return "String", f"String({length})"
    raise ValueError(f"unsupported sql type: {sql_type}")


def python_type(sql_type: str, nullable: bool) -> str:
    """列在 Python 侧的标注。"""
    normalized = sql_type.upper()
    if normalized.startswith("BIGINT") or normalized.startswith("INT") or normalized == "TINYINT":
        base = "int"
    elif normalized in {"DOUBLE", "FLOAT"} or normalized.startswith("DECIMAL"):
        base = "float"
    elif normalized == "JSON":
        base = "object"
    elif normalized.startswith("DATETIME"):
        base = "datetime"
    else:
        base = "str"
    if nullable:
        return f"{base} | None"
    return base


def parse_column(item: str) -> dict[str, object] | None:
    """解析一个列定义；索引与表级主键返回 None。"""
    match = re.match(r"^`?(\w+)`?\s+(.*)$", item, re.DOTALL)
    if match is None:
        return None
    name, rest = match.group(1), match.group(2).strip()
    upper_name = name.upper()
    if upper_name in {"PRIMARY", "UNIQUE", "KEY", "CONSTRAINT", "INDEX", "FULLTEXT"}:
        return None
    type_match = TYPE_RE.match(rest)
    if type_match is None:
        raise ValueError(f"unparsed column {name}: {rest}")
    sql_type = type_match.group(1)
    flags = rest[type_match.end() :]
    nullable = "NOT NULL" not in flags.upper()
    primary = "PRIMARY KEY" in flags.upper()
    autoincrement = "AUTO_INCREMENT" in flags.upper()
    comment_match = re.search(r"COMMENT\s+'([^']*)'", flags, re.IGNORECASE)
    default_match = re.search(
        r"DEFAULT\s+((?:CURRENT_TIMESTAMP(?:\(\d+\))?)|'(?:[^']*)'|NULL|-?\d+(?:\.\d+)?)",
        flags,
        re.IGNORECASE,
    )
    default_sql = default_match.group(1) if default_match else None
    generated_match = re.search(
        r"GENERATED ALWAYS AS \((.+)\) STORED",
        flags,
        re.IGNORECASE,
    )
    return {
        "name": name,
        "sql_type": sql_type,
        "nullable": nullable,
        "primary": primary,
        "autoincrement": autoincrement,
        "comment": comment_match.group(1) if comment_match else None,
        "default_sql": default_sql,
        "generated": generated_match.group(1) if generated_match else None,
    }


def parse_tables(sql: str) -> list[dict[str, object]]:
    """解析全部 CREATE TABLE。"""
    tables: list[dict[str, object]] = []
    for match in TABLE_RE.finditer(sql):
        table_name = match.group(1)
        body = match.group(2)
        trailer = match.group(3)
        comment_match = re.search(r"COMMENT='([^']*)'", trailer)
        columns = []
        for item in split_items(body):
            column = parse_column(item)
            if column is not None:
                columns.append(column)
        primary_columns = [column["name"] for column in columns if column["primary"]]
        table_pk = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", body, re.IGNORECASE)
        if table_pk is not None:
            declared = [part.strip().strip("`") for part in table_pk.group(1).split(",")]
            for column in columns:
                if column["name"] in declared:
                    column["primary"] = True
            primary_columns = declared
        tables.append(
            {
                "name": table_name,
                "comment": comment_match.group(1) if comment_match else table_name,
                "columns": columns,
                "primary": primary_columns,
            }
        )
    return tables


def render_column(column: dict[str, object]) -> str:
    """渲染一个 mapped_column。"""
    import_name, type_expr = sqlalchemy_type(str(column["sql_type"]))
    column["_import"] = import_name
    attr = attribute_name(str(column["name"]))
    py_type = python_type(str(column["sql_type"]), bool(column["nullable"]))
    args = [type_expr]
    if attr != column["name"]:
        args.insert(0, repr(column["name"]))
    kwargs: list[str] = []
    if column["primary"]:
        kwargs.append("primary_key=True")
    if column["autoincrement"]:
        kwargs.append("autoincrement=True")
    if column["nullable"]:
        kwargs.append("nullable=True")
    else:
        kwargs.append("nullable=False")
    if column["generated"]:
        kwargs.append(f"Computed({column['generated']!r}, persisted=True)")
        column["_needs_computed"] = True
    default_sql = column["default_sql"]
    if column["generated"]:
        default_sql = None
    if isinstance(default_sql, str) and default_sql.upper() != "NULL":
        if default_sql.upper().startswith("CURRENT_TIMESTAMP"):
            kwargs.append("default=now_local")
            kwargs.append(f'server_default=text("{default_sql}")')
            column["_needs_clock"] = True
            column["_needs_text"] = True
        elif default_sql.startswith("'"):
            literal = default_sql[1:-1]
            kwargs.append(f"default={literal!r}")
            kwargs.append(f"server_default=text({default_sql!r})")
            column["_needs_text"] = True
        else:
            kwargs.append(f"default={default_sql}")
            kwargs.append(f"server_default=text({default_sql!r})")
            column["_needs_text"] = True
    if column["comment"]:
        kwargs.append(f"comment={column['comment']!r}")
    joined = ", ".join(args + kwargs)
    return f"    {attr}: Mapped[{py_type}] = mapped_column({joined})"


def tenant_table_names() -> set[str]:
    """以 TenantTables.java 为 38 张隔离表的唯一真源。"""
    path = (
        java_schema_path().parents[1]
        / "src/main/java/com/aliyun/autowonder/tenant/TenantTables.java"
    )
    return set(re.findall(r'"([a-z0-9_]+)"', path.read_text(encoding="utf-8")))


def render_module(domain: str, tables: list[dict[str, object]], tenant_tables: set[str]) -> str:
    """渲染一个域的 models.py。"""
    imports = {"BigInteger", "Integer", "String"}
    needs_clock = False
    needs_text = False
    needs_datetime = False
    needs_computed = False
    class_blocks: list[str] = []
    class_names: list[str] = []
    tenant_names: list[str] = []
    for table in tables:
        lines = []
        for column in table["columns"]:
            lines.append(render_column(column))
            imports.add(str(column["_import"]))
            if column.get("_needs_clock"):
                needs_clock = True
            if column.get("_needs_text"):
                needs_text = True
            if str(column["sql_type"]).upper().startswith("DATETIME"):
                needs_datetime = True
            if column.get("_needs_computed"):
                needs_computed = True
        name = class_name(str(table["name"]))
        class_names.append(name)
        if table["name"] in tenant_tables:
            tenant_names.append(name)
        docstring = str(table["comment"]).replace('"""', '\\"\\"\\"')
        class_blocks.append(
            f'class {name}(Base):\n    """{docstring}"""\n\n'
            f'    __tablename__ = "{table["name"]}"\n\n' + "\n".join(lines)
        )
    header = ['"""由 scripts/generate_models.py 按 schema 契约生成。"""', ""]
    if needs_datetime:
        header.append("from datetime import datetime")
        header.append("")
    sa_names = sorted(imports)
    if needs_computed:
        sa_names.append("Computed")
    if needs_text:
        sa_names.append("text")
    header.append("from sqlalchemy import " + ", ".join(sa_names))
    header.append("from sqlalchemy.orm import Mapped, mapped_column")
    header.append("")
    header.append("from autowonder.db.base import Base")
    if needs_clock:
        header.append("from autowonder.core.clock import now_local")
    if tenant_names:
        header.append("from autowonder.db.tenant import register_tenant_model")
    header.append("")
    header.append("")
    body = "\n\n\n".join(class_blocks)
    footer = ["", ""]
    for name in tenant_names:
        footer.append(f"register_tenant_model({name})")
    if tenant_names:
        footer.append("")
    return "\n".join(header) + body + "\n".join(footer)


def render_model_imports(domains: list[str]) -> str:
    """渲染聚合导入，保证进程启动时注册全部模型。"""
    lines = [
        '"""导入全部领域模型，使 SQLAlchemy 元数据与 tenant 注册完整。"""',
        "",
    ]
    aliases = []
    for domain in domains:
        alias = domain + "_models"
        aliases.append(alias)
        lines.append(f"from autowonder.{domain} import models as {alias}")
    lines.append("")
    lines.append("MODEL_MODULES = (")
    for alias in aliases:
        lines.append(f"    {alias},")
    lines.append(")")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """读取 schema 并写出模型模块。"""
    schema = java_schema_path()
    tables = parse_tables(schema.read_text(encoding="utf-8"))
    by_domain: dict[str, list[dict[str, object]]] = {}
    for table in tables:
        domain = DOMAIN_BY_TABLE[str(table["name"])]
        by_domain.setdefault(domain, []).append(table)
    for domain in EMPTY_PACKAGES:
        package = SRC / domain
        package.mkdir(parents=True, exist_ok=True)
        init = package / "__init__.py"
        if not init.exists():
            init.write_text(f'"""{domain} 域。"""\n', encoding="utf-8")
    for domain, domain_tables in by_domain.items():
        package = SRC / domain
        package.mkdir(parents=True, exist_ok=True)
        init = package / "__init__.py"
        if not init.exists():
            init.write_text(f'"""{domain} 域。"""\n', encoding="utf-8")
        (package / "models.py").write_text(
            render_module(domain, domain_tables, tenant_table_names()),
            encoding="utf-8",
        )
    domains = sorted(by_domain)
    (SRC / "model_imports.py").write_text(render_model_imports(domains), encoding="utf-8")
    print(f"tables={len(tables)} domains={len(domains)}")


if __name__ == "__main__":
    main()
