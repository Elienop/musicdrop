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
from collections import Counter
from typing import Any

import beets
import requests
from beets.library import Library
from beets.util.lyrics import Lyrics
from beetsplug._utils.requests import HTTPNotFoundError

from app.models.lyrics import (
    AlbumLyricsResult,
    ItemLyricsOutcome,
    ItemLyricsStatus,
    LyricsCoverage,
)

_log = logging.getLogger(__name__)


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


def _make_lyrics_plugin() -> Any:
    """Throwaway LyricsPlugin with the import stage disabled (auto=False)."""
    beets.config["lyrics"].set({"auto": False})  # overlay before construct
    from beetsplug.lyrics import LyricsPlugin

    return LyricsPlugin()


def _store_lyrics(item: Any, lyrics: Lyrics, *, write: bool) -> None:
    """Persist beets-style: item.lyrics + flex fields, DB store, gated file write."""
    item.lyrics = lyrics.text
    for key in ("backend", "url", "language"):
        value = getattr(lyrics, key, None)
        if value:
            item[f"lyrics_{key}"] = value
    item.store()
    if write:
        item.try_write()


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
                    _store_lyrics(item, result, write=write)
                    return ItemLyricsOutcome(
                        item_id=item_id, status="found", source=result.backend, written=write
                    )
    status: ItemLyricsStatus = "fetch_failed" if failed else "not_found"
    return ItemLyricsOutcome(item_id=item_id, status=status, source=None, written=False)


def _aggregate(
    album_id: int, outcomes: list[ItemLyricsOutcome], *, writes_enabled: bool
) -> AlbumLyricsResult:
    counts: Counter[str] = Counter(o.status for o in outcomes)
    return AlbumLyricsResult(
        album_id=album_id,
        fetched=counts["found"],
        not_found=counts["not_found"],
        failed=counts["fetch_failed"],
        skipped=counts["skipped_existing"] + counts["skipped_no_metadata"],
        items=outcomes,
        writes_enabled=writes_enabled,
    )


def fetch_album_lyrics(
    lib: Library, album_id: int, *, force: bool, write: bool
) -> AlbumLyricsResult:
    """Fetch lyrics for an album's items (skip-existing unless force). 404 -> AlbumNotFoundError."""
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        plugin = _make_lyrics_plugin()
        outcomes = [
            fetch_item_lyrics(plugin, item, force=force, write=write) for item in album.items()
        ]
    return _aggregate(album_id, outcomes, writes_enabled=write)


async def fetch_album_lyrics_op(request_obj: Any, album_id: int) -> AlbumLyricsResult:
    """Per-album fetch: 409 import-gate + shared swap-lock + threadpool (like cover/edit)."""
    from beets.ui import should_write
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    from app.beets.config_editor import _swap_lock
    from app.import_jobs.registry import get_registry

    app = request_obj.app
    if get_registry().has_active_job():
        raise HTTPException(
            status_code=409,
            detail="Import in progress — lyrics fetch available when it finishes",
        )
    async with _swap_lock(app):
        handle = app.state.beets_library
        write = bool(should_write(None))
        try:
            return await run_in_threadpool(
                fetch_album_lyrics, handle.lib, album_id, force=False, write=write
            )
        except AlbumNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # structured 500, like cover/edit
            raise HTTPException(
                status_code=500,
                detail={"message": f"Lyrics fetch failed: {exc}", "recovery": "Reload and retry."},
            ) from exc


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
