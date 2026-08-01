"""Lyrics adapter — presence + fetch-into-files, on the beets boundary.

All beets lyrics access lives here (CLAUDE.md rule 3). We construct beets'
``LyricsPlugin`` with ``auto=False`` (so it does NOT register an import stage —
mirrors ``cover._make_fetchart_plugin``) and call the resolved backends DIRECTLY
rather than ``plugin.get_lyrics`` so we can tell a network ``fetch_failed`` apart
from a real ``not_found`` (``get_lyrics`` wraps each call in ``handle_request``,
which swallows both to None — the same gotcha as ``completeness`` and
``metadata_plugins.album_for_id``). LRCLib is the default keyless source; we
store plain lyrics into ``item.lyrics`` + flex fields, write the file tag
(``try_write``) when writes are on, AND write an external ``.lrc``/``.txt``
sidecar next to the track. The sidecar is what **Plex** actually reads — Plex
ignores embedded lyrics tags, so the embed alone never surfaced in Plex.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beets
import confuse
import requests
from beets.library import Library
from beets.util.lyrics import Lyrics
from beetsplug._utils.requests import HTTPNotFoundError

from app.beets.library import LibraryHandle, _is_instrumental
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


def _backend_name(backend: Any) -> str:
    """A lyrics backend's source name (e.g. ``"lrclib"``).

    beets sets ``name`` on the backend CLASS via its metaclass, so it is read off
    the type — ``getattr(instance, "name")`` is absent on a backend instance.
    """
    return str(getattr(type(backend), "name", None) or "?")


def _opt_cfg(view: Any) -> Any | None:
    """A confuse value, or None when the key is unset (it raises NotFoundError)."""
    try:
        return view.get()
    except confuse.NotFoundError:
        return None


def _resolve_lyrics_sources(lyrics_cfg: Any) -> list[str]:
    """User-configured lyrics sources, dropping a keyless google.

    ``["lrclib", "genius"]`` when unset (MusicDrop's default — google needs a
    Custom Search key we don't ship). Read BEFORE LyricsPlugin adds its defaults,
    so both ``sources`` and ``google_API_key`` raise NotFoundError when unset —
    both accesses are guarded.
    """
    try:
        configured = list(lyrics_cfg["sources"].as_str_seq())
    except confuse.NotFoundError:
        configured = []
    if not configured:
        return ["lrclib", "genius"]
    if "google" in configured and not _opt_cfg(lyrics_cfg["google_API_key"]):
        return [s for s in configured if s != "google"]
    return configured


def make_lyrics_plugin(*, lrclib_only: bool = False) -> Any:
    """Throwaway LyricsPlugin with the import stage off, synced lyrics on, and a
    keyless google dropped from sources (so beets' 'Disabling Google source'
    warning never fires). The runtime overlay does NOT touch the user's
    config.yaml; ``synced: True`` makes LRCLib return timestamped text for a real
    ``.lrc``.

    ``lrclib_only`` pins sources to just LRCLib. Library sweeps use it so a big
    bulk run never hammers Genius — Genius 429s under load and beets retries each
    429 slowly, so the run crawls and the misses never resolve. With LRCLib only,
    a miss is a clean ``not_found`` (fast, and gets marked checked). The per-album
    fetch leaves it False to keep Genius for targeted use.
    """
    lyrics_cfg = beets.config["lyrics"]
    sources = ["lrclib"] if lrclib_only else _resolve_lyrics_sources(lyrics_cfg)
    lyrics_cfg.set({"auto": False, "synced": True, "sources": sources})
    from beetsplug.lyrics import LyricsPlugin

    return LyricsPlugin()


def active_source_names(plugin: Any) -> list[str]:
    """Source names of a lyrics plugin's resolved backends (e.g. ``["lrclib",
    "genius"]``). Keeps ``plugin.backends`` access on the adapter side of the
    boundary so the job runner can log active sources without touching beets.
    """
    return [_backend_name(b) for b in getattr(plugin, "backends", [])]


def _atomic_write_text(dst: Path, text: str) -> None:
    """Atomic utf-8 write at 0o644 (text mirror of ``artist_art._atomic_write_bytes``):
    tmp in same dir -> fsync -> chmod 0o644 -> os.replace -> fsync parent dir."""
    tmp = dst.parent / f".{dst.name}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # world-readable so the Plex process (other uid) can read it
        os.replace(tmp, dst)
        dir_fd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()


def _sidecar_base(item: Any) -> str | None:
    """The track path without its extension, for building a sibling sidecar path.

    ``item.path`` is beets' bytes path; an absent/empty path yields None (e.g. a
    singleton not yet on disk) so the caller no-ops instead of writing garbage.
    """
    raw = getattr(item, "path", None)
    if not raw:
        return None
    base, _ext = os.path.splitext(os.fsdecode(raw))
    return base or None


def _has_sidecar(item: Any) -> bool:
    """Whether a ``.lrc`` or ``.txt`` lyric sidecar already sits next to the track."""
    base = _sidecar_base(item)
    if base is None:
        return False
    return os.path.exists(base + ".lrc") or os.path.exists(base + ".txt")


def remove_lyric_sidecars(item: Any) -> list[str]:
    """Delete this track's own ``.lrc``/``.txt`` sidecars; return the paths removed.

    Scoped to exactly the two siblings :func:`write_lyric_sidecar` could have
    written, so an instrumental verdict can't leave Plex serving a stale
    "[Instrumental]" file. A path-less item, a missing sidecar or an unlink
    error is a no-op (logged) rather than an error — never raises, and never
    touches the audio file or a neighbouring track's sidecar.
    """
    base = _sidecar_base(item)
    if base is None:
        return []
    removed: list[str] = []
    for ext in (".lrc", ".txt"):
        path = Path(base + ext)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            _log.warning("lyric sidecar removal failed: %s", path, exc_info=True)
            continue
        removed.append(str(path))
    return removed


def write_lyric_sidecar(item: Any, lyrics: Lyrics) -> str | None:
    """Write a Plex-readable lyric sidecar next to the track; return its path or None.

    ``.lrc`` (timestamped) when the fetched lyrics are synced, else ``.txt``
    (plain, timestamps stripped). Writing one removes the opposite-extension
    sibling so Plex never sees two conflicting files. Non-destructive (never
    touches the audio file) and best-effort: a write error is logged and
    swallowed so a batch keeps going; never raises.
    """
    base = _sidecar_base(item)
    if base is None:
        return None
    if lyrics.synced:
        ext, body = ".lrc", lyrics.text
    else:
        ext, body = ".txt", "\n".join(lyrics.text_lines)
    body = body.strip()
    if not body:
        return None
    dst = Path(base + ext)
    other = Path(base + (".txt" if ext == ".lrc" else ".lrc"))
    try:
        _atomic_write_text(dst, body + "\n")
    except OSError:
        _log.warning("lyric sidecar write failed: %s", dst, exc_info=True)
        return None
    if other.exists():
        with suppress(OSError):
            other.unlink()
    return str(dst)


def _set_source_flex(item: Any, lyrics: Lyrics) -> None:
    """Record which backend answered (and where), skipping keys it left unset."""
    for key in ("backend", "url", "language"):
        value = getattr(lyrics, key, None)
        if value:
            item[f"lyrics_{key}"] = value


def _store_instrumental(item: Any, lyrics: Lyrics) -> None:
    """Record a backend's definitive "this track has no lyrics by nature" verdict.

    Flags the track the way beets does (``lyrics_instrumental``), keeps
    MusicDrop's ``lyrics_checked`` bookkeeping so both sweep gates agree, clears
    any stale lyrics text off the DB row, and deletes the track's sidecars —
    Plex reads those, and an old "[Instrumental]" marker file would otherwise
    outlive the verdict. The audio file's own tag is deliberately left alone
    (no ``try_write``): a stale tag is inert, and rewriting tags is not this
    feature's job.
    """
    item.lyrics = ""
    item["lyrics_instrumental"] = 1
    item["lyrics_checked"] = 1
    _set_source_flex(item, lyrics)
    item.store()
    remove_lyric_sidecars(item)


def _store_lyrics(item: Any, lyrics: Lyrics, *, write: bool) -> bool:
    """Persist beets-style: item.lyrics + flex fields, DB store, gated file write,
    and a Plex-readable ``.lrc``/``.txt`` sidecar.

    The embedded tag stores PLAIN text (timestamps stripped); the synced timing
    lives in the ``.lrc`` sidecar. Returns whether the FILE TAG was written:
    ``item.try_write()``'s bool when writes are on, else ``False``. ``item.store()``
    (DB) and the sidecar are written regardless of the tag-write gate — a sidecar
    is non-destructive and is the whole point for Plex.
    """
    item.lyrics = "\n".join(lyrics.text_lines)
    # A found result overrides a stale instrumental verdict (beets' plugin writes
    # this flag on every found track too) — without the reset, later sweeps would
    # keep reporting skipped_instrumental for a track that now has real lyrics.
    item["lyrics_instrumental"] = 0
    _set_source_flex(item, lyrics)
    # Write the file tag BEFORE the DB store (beets' Item.try_sync order): try_write
    # bumps the file's mtime and sets item.mtime = current_mtime() in memory, so the
    # store AFTER it persists that fresh mtime. Storing first left the DB mtime behind
    # the file, and disk-sync's staleness gate then re-probed every lyric-written
    # track forever. store() runs regardless of the write gate (the DB row + sidecar
    # are non-destructive and are the whole point for Plex).
    written = bool(item.try_write()) if write else False
    item.store()
    write_lyric_sidecar(item, lyrics)
    return written


def fetch_item_lyrics(
    plugin: Any, item: Any, *, force: bool, write: bool, recheck_misses: bool = False
) -> ItemLyricsOutcome:
    """Fetch one item's lyrics directly off the plugin's backends.

    Skip-existing unless ``force``. A track previously searched with no result
    carries a ``lyrics_checked`` flag and is skipped (``skipped_checked``) on bulk
    runs unless ``recheck_misses``/``force`` — so obscure tracks aren't
    re-searched every backfill. A clean ``not_found`` sets the flag; a network
    error stays ``fetch_failed`` (transient) and is NOT marked. A track already
    flagged instrumental is skipped by EVERY sweep (``recheck_misses`` included)
    — an instrumental is an answer, not a miss; only ``force`` re-searches one.
    """
    from beetsplug.lyrics import search_pairs

    item_id = int(item.id)
    # Answered already, definitively: beets (or a previous run of ours) flagged
    # this track as having no lyrics by nature. Deliberately NOT gated on
    # recheck_misses, and deliberately independent of lyrics_checked — beets'
    # 2.13 migration flags pre-existing instrumentals without setting it.
    if not force and _is_instrumental(item):
        # Cleanup, not classification: tracks beets' migration flagged arrive
        # with old "[Instrumental]" sidecars that Plex keeps reading, and since
        # flagged tracks are never re-searched, this skip is the only sweep
        # path that can ever remove them. Idempotent (two stats when clean).
        remove_lyric_sidecars(item)
        return ItemLyricsOutcome(
            item_id=item_id, status="skipped_instrumental", source=None, written=False
        )
    # Already complete: has a lyrics tag AND a Plex sidecar.
    if not force and item.lyrics and _has_sidecar(item):
        return ItemLyricsOutcome(
            item_id=item_id, status="skipped_existing", source=None, written=False
        )
    # Known-empty: searched before, found nothing. Skip on bulk runs.
    if not force and not recheck_misses and not item.lyrics and item.get("lyrics_checked"):
        return ItemLyricsOutcome(
            item_id=item_id, status="skipped_checked", source=None, written=False
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
                except requests.exceptions.RequestException as exc:
                    # Concise one-liner (str(exc) reads "429 ... Too Many Requests
                    # for url: ...") instead of a per-item traceback flood.
                    _log.warning(
                        "lyrics fetch failed: %s [%s]: %s",
                        _item_label(item),
                        _backend_name(backend),
                        exc,
                    )
                    failed = True
                    continue
                if result is None:
                    continue
                # Instrumental is DEFINITIVE: stop here, no further backends and
                # no further search pairs. beets normalises the backend's
                # "[Instrumental]" marker to text="" + instrumental=True, so this
                # must be checked BEFORE the empty-text fall-through below.
                if getattr(result, "instrumental", False):
                    _store_instrumental(item, result)
                    return ItemLyricsOutcome(
                        item_id=item_id,
                        status="instrumental",
                        source=result.backend,
                        written=False,
                    )
                # beets 2.12's LRCLib can return a Lyrics whose ``.text`` is None
                # (a best candidate with null plainLyrics and synced not selected);
                # its own ``Lyrics.text_lines`` then does ``None.splitlines()`` and
                # raises, which would abort the whole backfill on that one track.
                # Treat empty/blank text as no usable match — fall through to the
                # next pair/backend and ultimately ``not_found``.
                if (result.text or "").strip():
                    written = _store_lyrics(item, result, write=write)
                    return ItemLyricsOutcome(
                        item_id=item_id, status="found", source=result.backend, written=written
                    )
    if failed:
        status: ItemLyricsStatus = "fetch_failed"  # transient — do NOT mark
    else:
        status = "not_found"
        item["lyrics_checked"] = 1  # searched, nothing found (DB-only bookkeeping)
        item.store()
    return ItemLyricsOutcome(item_id=item_id, status=status, source=None, written=False)


@dataclass
class LyricsSweepUnit:
    """One backfill unit: the live beets item (opaque) plus its display label."""

    item: Any  # the beets Item itself — fetch_item_lyrics mutates + stores it
    label: str


def _item_label(item: Any) -> str:
    artist = str(item.albumartist or item.artist or "").strip() or "Unknown"
    return f"{artist} - {item.album} - {item.title}"


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
    """'artist - album' for the banner/label, or raise AlbumNotFoundError (404)."""
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        artist = str(album.albumartist or "").strip()
        title = str(album.album or "").strip()
        label = " - ".join(p for p in (artist, title) if p)
        return label or f"album {album_id}"


async def start_album_lyrics_op(
    request_obj: Any, album_id: int, on_complete: Callable[[], None] | None = None
) -> LyricsBackfillStatus:
    """Start an album-scoped lyrics fetch JOB (marching progress); returns its status.

    Mirrors the library backfill start: 409 if an import/backfill/other library op
    is in flight, 404 for an unknown album, then spawns the daemon sweep and returns
    immediately (does NOT hold the swap-lock for the fetch). The FE polls
    GET /api/lyrics/backfill.

    ``on_complete`` is forwarded opaquely to the sweep so the API layer can notify
    open tabs when the fetch finishes; the adapter never imports ``app.events``
    (CLAUDE.md rule 3 — it only FORWARDS the callback).
    """
    from fastapi import HTTPException
    from fastapi import status as http_status

    from app.library_busy import raise_if_library_busy
    from app.lyrics_jobs.registry import get_lyrics_backfill
    from app.lyrics_jobs.runner import start_backfill

    app = request_obj.app
    reg = get_lyrics_backfill()
    raise_if_library_busy(
        app,
        message="A library operation is in progress; lyrics fetch available when it finishes",
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
    start_backfill(
        reg,
        handle,
        delay=delay,
        write=write,
        album_id=album_id,
        recheck_misses=True,
        on_complete=on_complete,
    )
    return reg.state()


# `lyrics_instrumental` is a flex attr, so presence is a correlated EXISTS. The
# value test excludes '0'/'false' because beets writes the flag as FALSE on every
# track it DID find lyrics for — a bare "row exists" test would count those as
# instrumental. Deliberately NOT joined to `lyrics_checked`: beets' 2.13 migration
# flags pre-existing instrumentals without it, and they must count immediately.
_INSTRUMENTAL_EXISTS = """EXISTS (
        SELECT 1 FROM item_attributes a
        WHERE a.entity_id = items.id AND a.key = 'lyrics_instrumental'
          AND a.value NOT IN ('', '0', 'false', 'False')
    )"""

# One aggregate for the four coverage counts. `lyrics` is a column on `items`
# (empty string when unset); `lyrics_checked`/`lyrics_instrumental` are flex attrs
# in `item_attributes`, so both empty-lyrics counts are correlated EXISTS
# subqueries. The buckets are mutually exclusive by construction: non-empty
# lyrics wins, then instrumental, then checked-but-empty. No per-Item
# construction — this runs on every panel mount over 15k-75k tracks.
_LYRICS_COVERAGE_SQL = f"""
SELECT
    COUNT(*),
    COALESCE(SUM(CASE WHEN lyrics IS NOT NULL AND lyrics != '' THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN (lyrics IS NULL OR lyrics = '')
        AND {_INSTRUMENTAL_EXISTS} THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN (lyrics IS NULL OR lyrics = '')
        AND NOT {_INSTRUMENTAL_EXISTS}
        AND EXISTS (
        SELECT 1 FROM item_attributes a
        WHERE a.entity_id = items.id AND a.key = 'lyrics_checked' AND a.value != ''
    ) THEN 1 ELSE 0 END), 0)
FROM items
"""


def lyrics_coverage(lib: Library) -> LyricsCoverage:
    """Count items with lyrics vs. instrumental vs. known-empty vs. total. One SQL
    aggregate; no network, no per-Item construction (see
    :data:`_LYRICS_COVERAGE_SQL`). ``percent`` stays with_lyrics/total —
    instrumentals are reported separately, not folded into coverage."""
    with lib.transaction() as tx:
        row = tx.query(_LYRICS_COVERAGE_SQL)[0]
    total, with_lyrics = int(row[0]), int(row[1])
    instrumental, checked_no_lyrics = int(row[2]), int(row[3])
    percent = round(100.0 * with_lyrics / total, 1) if total else 0.0
    return LyricsCoverage(
        total=total,
        with_lyrics=with_lyrics,
        instrumental=instrumental,
        checked_no_lyrics=checked_no_lyrics,
        percent=percent,
    )
