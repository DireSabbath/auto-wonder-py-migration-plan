"""SPA 回退不挡住后注册的路由，也不改写 /api 的 404。"""

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from autowonder.api.errors import install_exception_handlers
from autowonder.api.spa import mount_spa
from autowonder.core.errors import IllegalArgumentError


def test_spa_fallback_leaves_later_routes_and_api_404(tmp_path) -> None:
    index = tmp_path / "index.html"
    index.write_text("spa", encoding="utf-8")
    asset = tmp_path / "assets"
    asset.mkdir()
    (asset / "app.js").write_text("js", encoding="utf-8")
    app = FastAPI()
    install_exception_handlers(app)
    mount_spa(app, tmp_path)

    @app.get("/__illegal")
    def boom() -> None:
        raise IllegalArgumentError("invalid context content ref")

    @app.get("/status.taobao")
    def status_taobao() -> Response:
        return Response("missing", status_code=404)

    client = TestClient(app)
    illegal = client.get("/__illegal")
    assert illegal.status_code == 200
    assert illegal.json()["code"] == "10001"
    assert illegal.json()["message"] == "invalid context content ref"
    page = client.get("/workitems")
    assert page.status_code == 200
    assert page.text == "spa"
    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert script.text == "js"
    missing_api = client.get("/api/missing-page")
    assert missing_api.status_code == 404
    marker = client.get("/status.taobao")
    assert marker.status_code == 404
    assert marker.text == "missing"
