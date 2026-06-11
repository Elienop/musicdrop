"""Lyrics adapter — presence + fetch-into-files, on the beets boundary.

All beets lyrics access lives here (CLAUDE.md rule 3). We construct beets'
``LyricsPlugin`` with ``auto=False`` (so it does NOT register an import stage —
mirrors ``cover._make_fetchart_plugin``) and call the resolved backends DIRECTLY
rather than ``plugin.get_lyrics`` so we can tell a network ``fetch_failed`` apart
from a real ``not_found`` (``get_lyrics`` wraps each call in ``handle_request``,
which swallows both to None — the same gotcha as ``completeness`` and
``metadata_plugins.album_for_id``). LRCLib is the default keyless source; we
store plain lyrics into ``item.lyrics`` + flex fields and write the file tag
(``try_write``) only when writes are on, which is what Plex reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import beets
import requests
from beets.library import Library
from beets.util.lyrics import Lyrics
from beetsplug._utils.requests import HTTPNotFoundError

from app.beets.library import LibraryHandle
from app.models.lyrics import (
    ItemLyricsOutcome,
    ItemLyricsStatus,
    LyricsBackfillStatus,
    LyricsCoverage,
)

_log = logging.getLogger(__name__)


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


def writes_enabled() -> bool:
    """Whether beets' config has file-tag writes on (``should_write``).

    Thin wrapper so the API layer never imports beets directly (CLAUDE.md rule 3).
    """
    from beets.ui import should_write

    return bool(should_write(None))


def make_lyrics_plugin() -> Any:
    """Throwaway LyricsPlugin with the import stage disabled (auto=False).

    Returned as an opaque object: callers (the backfill runner) only ferry it
    back into ``fetch_item_lyrics``, never call beets on it themselves.
    """
    beets.config["lyrics"].set({"auto": False})  # overlay before construct
    from beetsplug.lyrics import LyricsPlugin

    return LyricsPlugin()


def _store_lyrics(item: Any, lyrics: Lyrics, *, write: bool) -> bool:
    """Persist beets-style: item.lyrics + flex fields, DB store, gated file write.

    Returns whether the file tag was actually written: ``item.try_write()``'s
    bool result when writes are on, else ``False``. ``item.store()`` (the DB
    write) always runs regardless.
    """
    item.lyrics = lyrics.text
    for key in ("backend", "url", "language"):
        value = getattr(lyrics, key, None)
        if value:
            item[f"lyrics_{key}"] = value
    item.store()
    written = bool(item.try_write()) if write else False
    return written


def fetch_item_lyrics(plugin: Any, item: Any, *, force: bool, write: bool) -> ItemLyricsOutcome:
    """Fetch one item's lyrics directly off the plugin's backends.

    Skip-existing unless ``force`` (beets' default). Returns a typed outcome;
    never raises on a fetch problem — a network error becomes ``fetch_failed``
    so a batch can keep going.
    """
    from beetsplug.lyrics import search_pairs

    item_id = int(item.id)
    if not force and item.lyrics:
        return ItemLyricsOutcome(
            item_id=item_id, status="skipped_existing", source=None, written=False
        )
    if not str(item.title or "").strip() or not str(item.artist or "").strip():
        return ItemLyricsOutcome(
            item_id=item_id, status="skipped_no_metadata", source=None, written=False
        )

    album = str(item.album or "")
    length = int(item.length or 0)
    failed = False
    for artist, titles in search_pairs(item):
        for title in titles:
            for backend in plugin.backends:
                try:
                    result = backend.fetch(artist, title, album, length)
                except HTTPNotFoundError:
                    continue  # this pair/backend simply has nothing
                except requests.exceptions.RequestException:
                    _log.warning("lyrics fetch failed for item %s", item_id, exc_info=True)
                    failed = True
                    continue
                if result is not None:
                    written = _store_lyrics(item, result, write=write)
                    return ItemLyricsOutcome(
                        item_id=item_id, status="found", source=result.backend, written=written
                    )
    status: ItemLyricsStatus = "fetch_failed" if failed else "not_found"
    return ItemLyricsOutcome(item_id=item_id, status=status, source=None, written=False)


@dataclass
class LyricsSweepUnit:
    """One backfill unit: the live beets item (opaque) plus its display label."""

    item: Any  # the beets Item itself — fetch_item_lyrics mutates + stores it
    label: str


def _item_label(item: Any) -> str:
    artist = str(item.albumartist or item.artist or "").strip() or "Unknown"
    return f"{artist} — {item.album} — {item.title}"


def collect_lyrics_units(
    handle: LibraryHandle, album_id: int | None = None
) -> list[LyricsSweepUnit]:
    """Snapshot the items a lyrics sweep will visit (album-scoped or whole library).

    An unknown ``album_id`` yields an empty list, so the runner finishes "done"
    at zero items — same posture as a direct beets lookup. Bound to
    ``music_dir_context`` like every adapter op (cheap insurance; scalar reads).
    """
    lib = handle.lib
    with lib.music_dir_context():
        if album_id is not None:
            album = lib.get_album(album_id)
            items = list(album.items()) if album is not None else []
        else:
            items = list(lib.items())
    return [LyricsSweepUnit(item=item, label=_item_label(item)) for item in items]


def _album_scope_label(lib: Library, album_id: int) -> str:
    """'artist — album' for the banner/label, or raise AlbumNotFoundError (404)."""
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        artist = str(album.albumartist or "").strip()
        title = str(album.album or "").strip()
        label = " — ".join(p for p in (artist, title) if p)
        return label or f"album {album_id}"


async def start_album_lyrics_op(request_obj: Any, album_id: int) -> LyricsBackfillStatus:
    """Start an album-scoped lyrics fetch JOB (marching progress); returns its status.

    Mirrors the library backfill start: 409 if an import/backfill/other library op
    is in flight, 404 for an unknown album, then spawns the daemon sweep and returns
    immediately (does NOT hold the swap-lock for the fetch). The FE polls
    GET /api/lyrics/backfill.
    """
    from fastapi import HTTPException
    from fastapi import status as http_status

    from app.artist_art_jobs.registry import artist_art_backfill_active
    from app.import_jobs.registry import get_registry
    from app.lyrics_jobs.registry import get_lyrics_backfill, lyrics_backfill_active
    from app.lyrics_jobs.runner import start_backfill
    from app.reorganize_jobs.registry import reorganize_backfill_active

    app = request_obj.app
    reg = get_lyrics_backfill()
    if (
        get_registry().has_active_job()
        or lyrics_backfill_active()
        or artist_art_backfill_active()
        or reorganize_backfill_active()
    ):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="A library operation is in progress — lyrics fetch available when it finishes",
        )
    lock = getattr(app.state, "beets_swap_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="A library operation is in progress — lyrics fetch available when it finishes",
        )
    handle = app.state.beets_library
    try:
        label = _album_scope_label(handle.lib, album_id)
    except AlbumNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    write = writes_enabled()
    try:
        reg.start(writes_enabled=write, album_id=album_id, scope_label=label)
    except RuntimeError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail="A lyrics fetch is already running"
        ) from None
    app_settings = getattr(app.state, "settings", None)
    delay = float(getattr(app_settings, "lyrics_backfill_delay_seconds", 0.2))
    start_backfill(reg, handle, delay=delay, write=write, album_id=album_id)
    return reg.state()


def lyrics_coverage(lib: Library) -> LyricsCoverage:
    """Count items with vs. without stored lyrics. One DB scan; no network."""
    with lib.music_dir_context():
        total = 0
        with_lyrics = 0
        for item in lib.items():
            total += 1
            if item.lyrics:
                with_lyrics += 1
    percent = round(100.0 * with_lyrics / total, 1) if total else 0.0
    return LyricsCoverage(total=total, with_lyrics=with_lyrics, percent=percent)
