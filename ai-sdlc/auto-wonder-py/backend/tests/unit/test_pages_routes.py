"""pages 的路由表覆盖 frontend/src/app/router.tsx 里的 path。"""

import re
from pathlib import Path

from verify.pages import PAGE_ROUTES

ROUTER = Path(__file__).resolve().parents[2].parent / "frontend" / "src" / "app" / "router.tsx"


def test_page_routes_cover_the_frontend_router() -> None:
    text = ROUTER.read_text(encoding="utf-8")
    declared = set(re.findall(r"path: '([^']+)'", text))
    covered = {route.router_path for route in PAGE_ROUTES if route.router_path != ""}
    assert declared == covered
