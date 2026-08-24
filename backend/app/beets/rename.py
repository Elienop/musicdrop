"""Artist-rename adapter: the album edit fanned across every album of an artist.

One new orchestration over machinery that already ships: per album it calls
``preview_album_edit`` / ``apply_album_edit`` (no second mover, no second
tag-writer, no second collision predicate), selected by the exact
``albumartist`` equality the artist page's album list uses. Only
``album_artist`` is edited; per-track ``artist`` tags are never touched
(owner decision — they must keep matching source metadata for lyrics lookups).

Batch shape mirrors duplicates resolve-all: the op takes the swap lock ONCE,
each album applies in its own transaction, a drifted or failing album is
recorded and never aborts the rest.
"""

from __future__ import annotations

from beets.library import Library

from app.beets.edit import preview_album_edit
from app.beets.library import _coerce_str, _require_id
from app.models.edit import AlbumEditRequest, AlbumFieldEdits
from app.models.rename import (
    ArtistRenameAlbumPreview,
    ArtistRenameMergeInfo,
    ArtistRenamePreview,
    ArtistRenameRequest,
)


class ArtistNotFoundError(Exception):
    """The named artist has no albums. Maps to 404."""


def _artist_albums(lib: Library, name: str) -> list[tuple[int, str]]:
    """(album_id, title) for every album whose albumartist is EXACTLY ``name``.

    Raw, case-sensitive equality — the same comparison ``list_albums``'
    ``?artist=`` filter applies — so the fan-out set is precisely the album
    list the artist page shows. (``delete_artist`` strips both sides; the
    rename must not, or the preview could cover albums the page does not.)
    """
    return [
        (_require_id(a.id), _coerce_str(a.album))
        for a in lib.albums()
        if _coerce_str(a.albumartist) == name
    ]


def _edit_request(new_name: str) -> AlbumEditRequest:
    return AlbumEditRequest(album=AlbumFieldEdits(album_artist=new_name))


def preview_artist_rename(
    lib: Library, *, request: ArtistRenameRequest, move_enabled: bool
) -> ArtistRenamePreview:
    """Fan the single-album preview across the artist. Persists nothing."""
    with lib.music_dir_context():
        targets = _artist_albums(lib, request.name)
        if not targets:
            raise ArtistNotFoundError(f"artist {request.name!r} not found")
        existing = len(_artist_albums(lib, request.new_name))
        edit = _edit_request(request.new_name)
        rows: list[ArtistRenameAlbumPreview] = []
        for album_id, title in targets:
            p = preview_album_edit(lib, album_id=album_id, request=edit, move_enabled=move_enabled)
            rows.append(
                ArtistRenameAlbumPreview(
                    album_id=album_id,
                    title=title,
                    move_count=len(p.move_plan),
                    refusals=p.move_refusals,
                )
            )
        return ArtistRenamePreview(
            name=request.name,
            new_name=request.new_name,
            move_enabled=move_enabled,
            albums=rows,
            merge=(ArtistRenameMergeInfo(existing_album_count=existing) if existing else None),
        )
