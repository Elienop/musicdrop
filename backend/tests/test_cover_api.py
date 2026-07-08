from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from tests.conftest import make_test_handle

PNG = Path(__file__).parent / "fixtures" / "cover.png"


@pytest.fixture
def cover_client(edit_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(edit_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _aid(lib: Library) -> int:
    return int(next(iter(lib.albums())).id)


def test_upload_install_sets_cover(cover_client: TestClient, edit_lib: Library) -> None:
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # cover now served
    assert cover_client.get(f"/api/albums/{aid}/cover").status_code == 200


def test_cover_upload_read_is_size_bounded(
    cover_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The endpoint must read the upload with a size cap (never an unbounded
    # read-all) so a spoofed/absent Content-Length can't buffer a huge body.
    import starlette.datastructures as sds

    from app.api.albums import _MAX_COVER_BYTES

    sizes: list[int] = []
    orig = sds.UploadFile.read

    async def spy(self: sds.UploadFile, size: int = -1) -> bytes:
        sizes.append(size)
        return await orig(self, size)

    monkeypatch.setattr(sds.UploadFile, "read", spy)
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
    )
    assert r.status_code == 200
    assert _MAX_COVER_BYTES + 1 in sizes  # bounded read, not read-all (-1)


def test_upload_rejects_non_image(cover_client: TestClient, edit_lib: Library) -> None:
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("x.txt", b"not an image", "text/plain")},
    )
    assert r.status_code == 422


def test_upload_unknown_album_404(cover_client: TestClient) -> None:
    r = cover_client.post(
        "/api/albums/999999/cover", files={"file": ("cover.png", PNG.read_bytes(), "image/png")}
    )
    assert r.status_code == 404


def test_upload_409_while_import_active(
    cover_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover", files={"file": ("cover.png", PNG.read_bytes(), "image/png")}
    )
    assert r.status_code == 409
    assert "in progress" in r.json()["detail"].lower()


def test_fetch_via_filesystem_returns_image(cover_client: TestClient, edit_lib: Library) -> None:
    import os

    aid = _aid(edit_lib)
    album = edit_lib.get_album(aid)
    assert album is not None
    album_dir = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
    Path(album_dir, "cover.png").write_bytes(PNG.read_bytes())
    r = cover_client.post(f"/api/albums/{aid}/cover/fetch")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.headers.get("x-art-source")
    assert r.content == PNG.read_bytes()


def test_upload_rejects_oversize_via_content_length(
    cover_client: TestClient, edit_lib: Library
) -> None:
    """A body whose Content-Length exceeds the cap is rejected 422 before it is read."""
    from app.api.albums import _MAX_COVER_BYTES

    aid = _aid(edit_lib)
    oversize = b"\xff\xd8\xff" + b"\x00" * _MAX_COVER_BYTES
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("big.jpg", oversize, "image/jpeg")},
    )
    assert r.status_code == 422
    assert "too large" in r.json()["detail"].lower()


def test_cross_origin_upload_rejected(cover_client: TestClient, edit_lib: Library) -> None:
    # multipart/form-data is a CORS "simple" content type, so a cross-origin page
    # can POST it WITHOUT a preflight. A CSRF'd browser must not be able to
    # overwrite a cover: an Origin whose authority differs from Host is rejected 403.
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
        headers={"Origin": "http://evil.test"},
    )
    assert r.status_code == 403


def test_same_origin_upload_allowed(cover_client: TestClient, edit_lib: Library) -> None:
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
        headers={"Origin": "http://testserver"},  # authority matches the TestClient Host
    )
    assert r.status_code == 200


def test_no_origin_upload_allowed(cover_client: TestClient, edit_lib: Library) -> None:
    # A non-browser client (curl, LAN tooling) sends no Origin — allowed.
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
    )
    assert r.status_code == 200


def test_dev_frontend_origin_upload_allowed(cover_client: TestClient, edit_lib: Library) -> None:
    # The Vite dev server is served from a different origin than the API but is on
    # the CORS allowlist, so its uploads are allowed.
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code == 200


def test_proxy_forwarded_host_upload_allowed(cover_client: TestClient, edit_lib: Library) -> None:
    # Behind a reverse proxy that rewrites Host to the upstream, the public host
    # the browser used arrives in X-Forwarded-Host. A same-origin upload (Origin
    # authority == X-Forwarded-Host) must be allowed even though it != Host.
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
        headers={"Origin": "http://public.example", "X-Forwarded-Host": "public.example"},
    )
    assert r.status_code == 200


def test_spoofed_forwarded_host_still_rejected(cover_client: TestClient, edit_lib: Library) -> None:
    # A cross-origin attacker can't set X-Forwarded-Host to match its own Origin
    # without making the request non-simple (a preflight the CORS policy rejects),
    # so an Origin that matches neither Host nor X-Forwarded-Host is still 403.
    aid = _aid(edit_lib)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
        headers={"Origin": "http://evil.test", "X-Forwarded-Host": "public.example"},
    )
    assert r.status_code == 403


def test_fetch_404_when_no_art(
    cover_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force fetchart to find nothing (the CI/dev box has live network, so the
    # default sources would otherwise locate real cover art for the album).
    from app.beets import cover as cover_mod

    class _StubPlugin:
        def art_for_album(self, album: object, paths: object, local_only: bool = False) -> None:
            return None

    monkeypatch.setattr(cover_mod, "_make_fetchart_plugin", lambda: _StubPlugin())
    r = cover_client.post(f"/api/albums/{_aid(edit_lib)}/cover/fetch")
    assert r.status_code == 404
