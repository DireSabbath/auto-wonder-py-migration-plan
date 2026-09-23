# auto-wonder

Python 服务端。契约来自 Java 版 `ai-sdlc/auto-wonder`，实现落在本目录。

```bash
export AUTOWONDER_JAVA_ROOT=/path/to/ai-sdlc/auto-wonder
uv run python scripts/sync_from_java.py
uv run python scripts/generate_models.py
uv run python scripts/extract_endpoints.py
uv run autowonder-serve
```
