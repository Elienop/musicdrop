from __future__ import annotations

import io
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient
from PIL import Image

from app.api.albums import get_library
from app.artwork.cover_thumbs import CoverThumbCache
from app.beets.library import _require_id
from app.main import app
from tests.conftest import make_test_handle

PNG = Path(__file__).parent / "fixtures" / "cover.png"


@pytest.fixture
def cover_client(edit_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(edit_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    # TestClient(app) skips the lifespan, so app.state.cover_thumb_cache is
    # never built — wire a hermetic cache under its own tmp subdir (separate
    # from the library dir make_test_handle already carved out of tmp_path).
    # Save/restore whatever was there before (mirrors conftest.py's `client`
    # fixture) so this module-global `app` doesn't leak state into tests that
    # never wire their own cache — get_cover_thumb_cache degrades gracefully on
    # a missing attribute, but leaving a stale one set is still cross-test
    # pollution other suites shouldn't have to route around.
    prior_thumb_cache = getattr(app.state, "cover_thumb_cache", None)
    app.state.cover_thumb_cache = CoverThumbCache(tmp_path / "cover-thumbs")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        if prior_thumb_cache is None:
            del app.state.cover_thumb_cache
        else:
            app.state.cover_thumb_cache = prior_thumb_cache


def _aid(lib: Library) -> int:
    return _require_id(next(iter(lib.albums())).id)


def _png(width: int, height: int, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


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


def _install(cover_client: TestClient, aid: int) -> None:
    cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", PNG.read_bytes(), "image/png")},
    )


def test_cover_304_revalidates_without_reading_the_image(
    cover_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conditional GET must 304 off the cheap stat validator WITHOUT re-reading /
    re-hashing the full image — the album grid revalidates every cover on every
    paint, and the old path read the whole image just to produce a 304."""
    import app.api.albums as albums_mod

    aid = _aid(edit_lib)
    _install(cover_client, aid)
    first = cover_client.get(f"/api/albums/{aid}/cover")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert etag

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("get_album_cover must not run on a 304 revalidation")

    monkeypatch.setattr(albums_mod, "get_album_cover", _boom)
    second = cover_client.get(f"/api/albums/{aid}/cover", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag


def test_cover_stale_etag_after_change_returns_200(
    cover_client: TestClient, edit_lib: Library
) -> None:
    """The stat validator is self-correcting: touching the cover file (a re-fetch
    would write a new one) changes mtime, so an old ETag no longer 304s."""
    aid = _aid(edit_lib)
    _install(cover_client, aid)
    etag1 = cover_client.get(f"/api/albums/{aid}/cover").headers["etag"]

    album = edit_lib.get_album(aid)
    assert album is not None and album.artpath is not None
    artpath = os.fsdecode(album.artpath)
    future = time.time() + 10
    os.utime(artpath, (future, future))  # simulate a cover replacement (new mtime)

    resp = cover_client.get(f"/api/albums/{aid}/cover", headers={"If-None-Match": etag1})
    assert resp.status_code == 200  # stale tag, not a false 304
    assert resp.headers["etag"] != etag1


def test_cover_size_thumb_serves_webp_and_304s(cover_client: TestClient, edit_lib: Library) -> None:
    aid = _aid(edit_lib)
    cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", _png(1200, 1200), "image/png")},
    )
    full = cover_client.get(f"/api/albums/{aid}/cover")
    thumb = cover_client.get(f"/api/albums/{aid}/cover", params={"size": "thumb"})
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert len(thumb.content) < len(full.content)
    assert thumb.headers["etag"] != full.headers["etag"]
    again = cover_client.get(
        f"/api/albums/{aid}/cover",
        params={"size": "thumb"},
        headers={"If-None-Match": thumb.headers["etag"]},
    )
    assert again.status_code == 304


@pytest.mark.parametrize(
    "poison",
    ["image/日本語", "image/png\nX-Injected: yes", ""],
    ids=["non-ascii", "response-splitting", "empty"],
)
def test_full_cover_never_serves_an_unsendable_content_type(
    cover_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch, poison: str
) -> None:
    """The full-size cover mime comes from the MEDIA FILE, not a cache sidecar —
    the one content-type sink that reached ``image_response`` unguarded.

    Not reachable today (the artpath extension map is fixed, and mediafile
    re-derives an embedded picture's type from its magic bytes), so this is
    defence in depth — but ``is_header_safe_content_type``'s docstring claims to
    enumerate every sink, and that claim is only true with this guard in place.
    A non-ASCII value 500s here; a newline produces NO RESPONSE AT ALL on a real
    server, which TestClient cannot show.
    """
    import app.api.albums as albums_mod

    aid = _aid(edit_lib)
    _install(cover_client, aid)
    monkeypatch.setattr(
        albums_mod, "get_album_cover", lambda lib, album_id: (PNG.read_bytes(), poison)
    )

    resp = cover_client.get(f"/api/albums/{aid}/cover")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"


def test_cover_size_thumb_ignores_full_etag_on_if_none_match(
    cover_client: TestClient, edit_lib: Library
) -> None:
    """A `size=thumb` request must validate against the THUMB tag only. The full
    and thumb images are different entities (different bytes) sharing one URL
    family — a client that still holds the FULL etag (e.g. it fetched size=full
    before switching to thumbnails) must not get a bodiless 304 that claims the
    thumb is unchanged; it has never even seen the thumb yet."""
    aid = _aid(edit_lib)
    cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("cover.png", _png(1200, 1200), "image/png")},
    )
    full = cover_client.get(f"/api/albums/{aid}/cover")
    full_etag = full.headers["etag"]

    resp = cover_client.get(
        f"/api/albums/{aid}/cover",
        params={"size": "thumb"},
        headers={"If-None-Match": full_etag},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert resp.headers["etag"] != full_etag
    assert (
        resp.content
        == cover_client.get(f"/api/albums/{aid}/cover", params={"size": "thumb"}).content
    )


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


def test_every_image_response_the_cover_routes_build_carries_nosniff(
    cover_client: TestClient, edit_lib: Library
) -> None:
    """The GET inherits nosniff from the two shared http_cache constructors; the
    fetch preview builds its own Response and has to spell it out.

    The preview's mime is already one of four literals from sniff_image_mime's
    magic-byte check, so this header is pure backstop there - but a backstop
    that covers every image response except one is not a backstop, and "all of
    them" is an easier rule to keep true than "all except that one".
    """
    import os

    aid = _aid(edit_lib)
    album = edit_lib.get_album(aid)
    assert album is not None
    album_dir = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
    Path(album_dir, "cover.png").write_bytes(PNG.read_bytes())
    _install(cover_client, aid)

    served = cover_client.get(f"/api/albums/{aid}/cover")
    revalidated = cover_client.get(
        f"/api/albums/{aid}/cover", headers={"If-None-Match": served.headers["etag"]}
    )
    preview = cover_client.post(f"/api/albums/{aid}/cover/fetch")
    # Non-vacuity: each arm must be the response it claims to be, or a 404 would
    # satisfy "carries no sniffable body" for entirely the wrong reason.
    assert [r.status_code for r in (served, revalidated, preview)] == [200, 304, 200]
    for resp in (served, revalidated, preview):
        assert resp.headers["x-content-type-options"] == "nosniff"


def test_the_cover_fetch_declares_every_status_it_can_return() -> None:
    """The generated client only knows what the spec says.

    403 (the Origin guard) and 404 (no album, or no candidate) both render
    ``{"detail": "..."}`` and neither was declared - an undeclared status
    generates ``content?: never``. 422 must stay UNDECLARED so FastAPI's
    ``HTTPValidationError`` survives: the path parameter can fail validation and
    its ``detail`` is a LIST, a different shape.
    """
    operation = app.openapi()["paths"]["/api/albums/{album_id}/cover/fetch"]["post"]
    responses = operation["responses"]
    assert sorted(responses) == ["200", "403", "404", "422"]
    # The 200 is image bytes; before this it offered ONLY a JSON body.
    assert "image/*" in responses["200"]["content"]
    for code in ("403", "404"):
        schema = responses[code]["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/ErrorDetail", code
    assert (
        responses["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/HTTPValidationError"
    )


def test_cross_origin_cover_fetch_is_rejected(cover_client: TestClient, edit_lib: Library) -> None:
    """A body-less POST is a CORS-simple request, so it reaches this route
    without a preflight. It writes nothing, but it still drives an outbound
    cover lookup on this install's behalf - "changes no state" is not "costs
    nothing to trigger". The same guard the install route has carried since it
    was written.
    """
    import os

    aid = _aid(edit_lib)
    album = edit_lib.get_album(aid)
    assert album is not None
    album_dir = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
    Path(album_dir, "cover.png").write_bytes(PNG.read_bytes())
    # Non-vacuity: this exact request WITHOUT the header is the 200 pinned by
    # test_fetch_via_filesystem_returns_image, so the 403 is the Origin guard
    # and not a missing cover.
    r = cover_client.post(f"/api/albums/{aid}/cover/fetch", headers={"Origin": "http://evil.test"})
    assert r.status_code == 403
    # This route uploads NOTHING, so the message must not claim an upload was
    # refused - three of the six routes behind the shared guard are body-less.
    detail = r.json()["detail"]
    assert "upload" not in detail
    assert detail.isascii()
    assert cover_client.post(f"/api/albums/{aid}/cover/fetch").status_code == 200


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
