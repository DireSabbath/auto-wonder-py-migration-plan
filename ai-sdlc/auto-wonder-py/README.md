# auto-wonder-py

AutoWonder 的 Python 实现，与 Java 参考实现并行。迁移范围、目录和验收标准见仓库根目录的 `auto-wonder-py-migration-plan.md`。新代码同时受 `AGENTS.md` 约束。

Java 源码保持只读。`backend/scripts/sync_from_java.py` 把 schema、种子数据、协议文档和前端源码单向拷到本目录。
