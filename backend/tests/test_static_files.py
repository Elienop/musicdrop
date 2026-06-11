"""The production static seam: SPA + hashed assets from FastAPI.

Tests build their OWN app (router + mount_static against a tmp dist dir)
so they don't depend on the real app's import-time settings. One test
checks the real app: with static_dir unset (the test default), no SPA
catch-all is registered.
"""

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.static_files import mount_static


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>')
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg></svg>")
    return dist


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    api = APIRouter()

    @api.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "ok"}

    app.include_router(api, prefix="/api")
    mount_static(app, str(_dist(tmp_path)))
    return app


def test_index_served_at_root_with_no_cache(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="root"' in resp.text
    assert resp.headers["cache-control"] == "no-cache"


def test_deep_link_falls_back_to_index(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/artists/Adele")
    assert resp.status_code == 200
    assert 'id="root"' in resp.text


def test_assets_are_immutable(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_root_level_build_file_served_directly(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert "<svg>" in resp.text


def test_api_routes_untouched(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    assert client.get("/api/ping").json() == {"pong": "ok"}
    missing = client.get("/api/nonexistent")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")


def test_missing_dir_is_a_noop(tmp_path: Path) -> None:
    app = FastAPI()
    mount_static(app, str(tmp_path / "absent"))
    client = TestClient(app)
    assert client.get("/").status_code == 404


def test_real_app_has_no_spa_catchall_in_dev() -> None:
    from app.main import app as real_app

    paths = {getattr(r, "path", "") for r in real_app.routes}
    assert "/{path:path}" not in paths


def test_null_byte_path_is_404_not_500(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/foo%00bar")
    assert resp.status_code == 404


def test_literal_index_html_gets_no_cache(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/index.html")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
