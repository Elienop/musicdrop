"""Tests for the cover-art adapter (app/beets/cover.py)."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from beets.library import Library
from mediafile import MediaFile

from app.beets.library import _require_id

PNG = Path(__file__).parent / "fixtures" / "cover.png"


def _album_id(lib: Library) -> int:
    return _require_id(next(iter(lib.albums())).id)


def test_make_fetchart_plugin_restores_auto(edit_lib: Library) -> None:
    """Building the throwaway plugin must not leave ``fetchart.auto`` mutated."""
    import beets

    from app.beets import cover as cover_mod

    for user_value in (True, False):
        beets.config["fetchart"]["auto"].set(user_value)
        cover_mod._make_fetchart_plugin()
        assert beets.config["fetchart"]["auto"].get(bool) is user_value


def test_install_cover_sets_artpath(edit_lib: Library) -> None:
    from app.beets.cover import install_cover
    from app.beets.library import get_album_cover

    aid = _album_id(edit_lib)
    result = install_cover(edit_lib, album_id=aid, image_bytes=PNG.read_bytes())
    assert result.ok is True
    # artpath written to a cover file in the album dir, and GET /cover serves it.
    album = edit_lib.get_album(aid)
    assert album is not None
    assert album.artpath is not None
    assert os.path.isfile(os.fsdecode(album.artpath))
    served = get_album_cover(edit_lib, aid)
    assert served is not None
    assert served[1] == "image/png"
    assert served[0] == PNG.read_bytes()


def test_install_cover_rejects_non_image(edit_lib: Library) -> None:
    from app.beets.cover import UnsupportedImageError, install_cover

    aid = _album_id(edit_lib)
    with pytest.raises(UnsupportedImageError):
        install_cover(edit_lib, album_id=aid, image_bytes=b"not an image")


def test_install_cover_unknown_album(edit_lib: Library) -> None:
    from app.beets.cover import AlbumNotFoundError, install_cover

    png = PNG.read_bytes()
    with pytest.raises(AlbumNotFoundError):
        install_cover(edit_lib, album_id=999999, image_bytes=png)


def test_embed_album_gets_embedarts_own_logger(caplog: pytest.LogCaptureFixture) -> None:
    """beets' embedart hands ``embed_album`` its plugin logger, a BeetsLogger that
    formats beets' ``{}``-style messages. A stdlib logger would print
    "--- Logging error ---" in place of the message."""
    from beets import logging as beets_logging

    from app.beets import cover as cover_mod

    assert cover_mod._log is beets_logging.getLogger("beets").getChild("embedart")
    with caplog.at_level(logging.WARNING, logger=cover_mod._log.name):
        cover_mod._log.warning("could not read image file: {}", "x.png")
    assert caplog.records[-1].getMessage() == "could not read image file: x.png"


def test_install_cover_embed_gated_off_by_default(edit_lib: Library) -> None:
    """Our test config has no embedart plugin, so files stay untouched."""
    from app.beets.cover import install_cover

    aid = _album_id(edit_lib)
    result = install_cover(edit_lib, album_id=aid, image_bytes=PNG.read_bytes())
    assert result.embedded is False
    album = edit_lib.get_album(aid)
    assert album is not None
    for item in album.items():
        assert not MediaFile(os.fsdecode(item.path)).images  # no embedded art


def test_install_cover_embeds_when_embedart_enabled(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.beets import cover as cover_mod
    from app.beets.cover import install_cover

    # Force the embed gate on (config gate = embedart in plugins AND should_write).
    monkeypatch.setattr(cover_mod, "_embed_enabled", lambda: True)
    aid = _album_id(edit_lib)
    install_cover(edit_lib, album_id=aid, image_bytes=PNG.read_bytes())
    album = edit_lib.get_album(aid)
    assert album is not None
    for item in album.items():
        images = MediaFile(os.fsdecode(item.path)).images
        assert images
        assert bytes(images[0].data) == PNG.read_bytes()


def test_install_cover_runs_from_a_worker_thread(edit_lib: Library) -> None:
    from app.beets.cover import install_cover

    aid = _album_id(edit_lib)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            install_cover,
            edit_lib,
            album_id=aid,
            image_bytes=PNG.read_bytes(),
        ).result()
    assert result.ok is True
    album = edit_lib.get_album(aid)
    assert album is not None
    assert album.artpath is not None


def test_fetch_via_filesystem_source(edit_lib: Library) -> None:
    """Drop a cover.png in the album dir -> the FileSystem source finds it (no network)."""
    from app.beets.cover import fetch_cover_candidate

    aid = _album_id(edit_lib)
    album = edit_lib.get_album(aid)
    assert album is not None
    album_dir = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
    Path(album_dir, "cover.png").write_bytes(PNG.read_bytes())

    fetched = fetch_cover_candidate(edit_lib, album_id=aid)
    assert fetched is not None
    assert fetched.content_type == "image/png"
    assert fetched.image_bytes == PNG.read_bytes()
    assert fetched.source  # some non-empty source label


def test_fetch_none_when_no_art(edit_lib: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets import cover as cover_mod

    # No filesystem art + force art_for_album to find nothing (no network).
    class _StubPlugin:
        def art_for_album(self, album: object, paths: object, local_only: bool = False) -> None:
            return None

    monkeypatch.setattr(cover_mod, "_make_fetchart_plugin", lambda: _StubPlugin())
    assert cover_mod.fetch_cover_candidate(edit_lib, album_id=_album_id(edit_lib)) is None


def test_fetch_unknown_album(edit_lib: Library) -> None:
    from app.beets.cover import AlbumNotFoundError, fetch_cover_candidate

    with pytest.raises(AlbumNotFoundError):
        fetch_cover_candidate(edit_lib, album_id=999999)


def test_fetch_none_when_candidate_oversize(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An oversize remote download is treated as 'no usable art' (DoS cap), not read."""
    from app.beets import cover as cover_mod

    # A temp file just over the cap; outside the library dir (a remote download).
    big = tmp_path / "huge.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"\x00" * (cover_mod.MAX_COVER_BYTES + 1))

    class _Candidate:
        path = os.fsencode(str(big))
        source_name = "external source"

    class _StubPlugin:
        def art_for_album(self, album: object, paths: object, local_only: bool = False) -> object:
            return _Candidate()

    monkeypatch.setattr(cover_mod, "_make_fetchart_plugin", lambda: _StubPlugin())
    assert cover_mod.fetch_cover_candidate(edit_lib, album_id=_album_id(edit_lib)) is None
    # The oversize temp was cleaned up (treated as a remote download).
    assert not big.exists()
