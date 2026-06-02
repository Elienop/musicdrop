"""Tests for the cover-art adapter (app/beets/cover.py)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from beets.library import Library
from mediafile import MediaFile

PNG = Path(__file__).parent / "fixtures" / "cover.png"


def _album_id(lib: Library) -> int:
    return int(next(iter(lib.albums())).id)


def test_install_cover_sets_artpath(edit_lib: Library) -> None:
    from app.beets.cover import install_cover
    from app.beets.library import get_album_cover

    aid = _album_id(edit_lib)
    result = install_cover(
        edit_lib, album_id=aid, image_bytes=PNG.read_bytes(), content_type="image/png"
    )
    assert result.ok is True
    # artpath written to a cover file in the album dir, and GET /cover serves it.
    album = edit_lib.get_album(aid)
    assert album is not None
    assert album.artpath is not None
    assert os.path.isfile(os.fsdecode(album.artpath))
    served = get_album_cover(edit_lib, aid)
    assert served is not None and served[1] == "image/png"
    assert served[0] == PNG.read_bytes()


def test_install_cover_rejects_non_image(edit_lib: Library) -> None:
    from app.beets.cover import UnsupportedImageError, install_cover

    with pytest.raises(UnsupportedImageError):
        install_cover(
            edit_lib,
            album_id=_album_id(edit_lib),
            image_bytes=b"not an image",
            content_type="text/plain",
        )


def test_install_cover_unknown_album(edit_lib: Library) -> None:
    from app.beets.cover import AlbumNotFoundError, install_cover

    with pytest.raises(AlbumNotFoundError):
        install_cover(
            edit_lib, album_id=999999, image_bytes=PNG.read_bytes(), content_type="image/png"
        )


def test_install_cover_embed_gated_off_by_default(edit_lib: Library) -> None:
    """Our test config has no embedart plugin, so files stay untouched."""
    from app.beets.cover import install_cover

    aid = _album_id(edit_lib)
    result = install_cover(
        edit_lib, album_id=aid, image_bytes=PNG.read_bytes(), content_type="image/png"
    )
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
    install_cover(edit_lib, album_id=aid, image_bytes=PNG.read_bytes(), content_type="image/png")
    album = edit_lib.get_album(aid)
    assert album is not None
    for item in album.items():
        images = MediaFile(os.fsdecode(item.path)).images
        assert images and bytes(images[0].data) == PNG.read_bytes()


def test_install_cover_runs_from_a_worker_thread(edit_lib: Library) -> None:
    from app.beets.cover import install_cover

    aid = _album_id(edit_lib)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            install_cover,
            edit_lib,
            album_id=aid,
            image_bytes=PNG.read_bytes(),
            content_type="image/png",
        ).result()
    assert result.ok is True
    album = edit_lib.get_album(aid)
    assert album is not None
    assert album.artpath is not None
