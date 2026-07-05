"""The shared in-library duplicate-copy mapper (ExistingAlbum + its tracks).

ONE mapper feeds the live duplicate prompt, the bank's up-front check, and the
live up-front check, so every surface carries the same fields — including the
release identity and the per-track view that make Skip/Keep/Replace/Merge
decidable. Lives in its own leaf module on purpose: ``duplicates.py`` imports
the import registry, and the registry imports ``import_session`` — hosting
this mapper in either would cycle.

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

from typing import Any

from beets.library import Library

from app.beets.library import _coerce_int, _coerce_optional_str
from app.beets.release_identity import release_identity
from app.beets.trash import album_folder, album_format_bitrate
from app.models.import_models import ExistingAlbum, ExistingTrack

_UNNUMBERED = 1 << 30  # unnumbered tracks sort last within their disc


def _existing_tracks(items: list[Any]) -> list[ExistingTrack]:
    """Per-track view, disc-then-number ordered."""

    def _key(item: Any) -> tuple[int, int]:
        disc = _coerce_int(getattr(item, "disc", 0)) or 1
        track = _coerce_int(getattr(item, "track", 0)) or _UNNUMBERED
        return disc, track

    out: list[ExistingTrack] = []
    for item in sorted(items, key=_key):
        fmt = getattr(item, "format", None)
        raw_bitrate = _coerce_int(getattr(item, "bitrate", 0))  # beets stores bps
        out.append(
            ExistingTrack(
                track=_coerce_int(getattr(item, "track", 0)) or None,
                disc=_coerce_int(getattr(item, "disc", 0)) or None,
                title=_coerce_optional_str(getattr(item, "title", None)),
                format=str(fmt) if fmt else None,
                bitrate_kbps=raw_bitrate // 1000 if raw_bitrate else None,
            )
        )
    return out


def to_existing_album(lib: Library, album: Any) -> ExistingAlbum:
    """Map one in-library beets Album to the slim ExistingAlbum view.

    ``lib`` is read only when the album has items, so an item-less album
    resolves to folder "" without touching it.
    """
    items = list(album.items())
    fmt, bitrate_kbps = album_format_bitrate(items)
    year = album.get("year")
    return ExistingAlbum(
        album_id=int(album.id),
        album_artist=_coerce_optional_str(album.albumartist),
        album=_coerce_optional_str(album.album),
        year=int(year) if year else None,
        track_count=len(items),
        format=fmt,
        bitrate_kbps=bitrate_kbps,
        folder=album_folder(lib, items) if items else "",
        release=release_identity(album, getattr(album, "mb_albumid", None)),
        tracks=_existing_tracks(items),
    )
