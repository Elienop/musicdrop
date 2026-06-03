"""The lyrics-backfill worker — a sequential library sweep (the `beet lyrics` analog).

``sweep`` is the synchronous, directly-testable loop (snapshot items, fetch each
with skip-existing, record progress, courtesy-sleep, honor Stop). ``start_backfill``
runs it on a daemon thread so the API start endpoint returns immediately. beets
itself does no throttling; the courtesy ``delay`` keeps us polite to LRCLib.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.beets.lyrics import _make_lyrics_plugin, fetch_item_lyrics
from app.lyrics_jobs.registry import LyricsBackfillRegistry
from app.models.lyrics import ItemLyricsOutcome


def _label(item: Any) -> str:
    artist = str(item.albumartist or item.artist or "").strip() or "Unknown"
    return f"{artist} — {item.album} — {item.title}"


def sweep(
    reg: LyricsBackfillRegistry,
    lib: Any,
    *,
    delay: float,
    write: bool,
    album_id: int | None = None,
    fetch_one: Callable[..., ItemLyricsOutcome] = fetch_item_lyrics,
    make_plugin: Callable[[], Any] = _make_lyrics_plugin,
) -> None:
    """Run the (library- or album-scoped) sweep to completion. Never raises."""
    try:
        with lib.music_dir_context():
            plugin = make_plugin()
            if album_id is not None:
                album = lib.get_album(album_id)
                items = list(album.items()) if album is not None else []
            else:
                items = list(lib.items())
            reg.set_total(len(items))
            for item in items:
                if reg.should_stop():
                    reg.finish("stopped")
                    return
                outcome = fetch_one(plugin, item, force=False, write=write)
                reg.record(outcome)
                reg.set_current(_label(item))
                if delay:
                    time.sleep(delay)
            reg.finish("done")
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        reg.fail(str(exc) or exc.__class__.__name__)


def start_backfill(
    reg: LyricsBackfillRegistry, lib: Any, *, delay: float, write: bool, album_id: int | None = None
) -> None:
    """Spawn the (library- or album-scoped) sweep on a daemon thread (non-blocking)."""
    threading.Thread(
        target=lambda: sweep(reg, lib, delay=delay, write=write, album_id=album_id),
        name="musicdrop-lyrics-backfill",
        daemon=True,
    ).start()
