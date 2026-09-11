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

Sidecar files are the one artifact here the user may have curated by hand, and
nothing records authorship, so the file layer is **fill-gaps-only**: a backfill
writes a sidecar where none exists and otherwise leaves the disk alone. The sole
exception is content-proven, and it runs both ways: a sidecar whose whole body is
beets' "[Instrumental]" marker carries no lyric data, so a fresh instrumental
verdict removes it, and a found result may replace it (an all-marker set counts
as absent). Everything else on disk wins over anything we fetched — see
:func:`remove_instrumental_marker_sidecars` and :func:`write_lyric_sidecar`.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beets
import confuse
import requests
from beets.library import Library
from beets.util.lyrics import INSTRUMENTAL_LYRICS, Lyrics
from beetsplug._utils.requests import HTTPNotFoundError

from app.beets.library import LibraryHandle, _is_instrumental
from app.beets.sidecars import PLAIN_EXT, SIDECAR_EXTS, SYNCED_EXT, sidecar_base
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


#: Flags for creating the atomic-write temp file, beside the unpredictable name
#: :func:`_tmp_path` picks. ``O_EXCL`` makes the create fail rather than open
#: whatever is at that path — a FIFO there made a plain ``open(tmp, "w")`` block
#: until a reader appeared, forever, on the single-slot backfill worker.
#: ``O_NOFOLLOW`` refuses a symlink; POSIX makes ``O_CREAT | O_EXCL`` fail EEXIST
#: on one anyway (measured), so it earns its keep only if ``O_EXCL`` is dropped.
#: Both guard a path nothing in the music share can aim at WITHOUT GUESSING the
#: name :func:`_tmp_path` picked; a squatter that did land on it fails the create
#: and is unlinked by the ``finally`` below. The read-side twin is
#: :func:`_is_marker_sidecar`'s stat guard.
_TMP_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _tmp_path(dst: Path) -> Path:
    """A temp sibling of ``dst`` under a name picked per call, not derived.

    Sidecars are written inside the music library, which this deployment's
    threat model treats as attacker-writable, and a derived ``.<name>.tmp`` is a
    path something else can occupy first — measured on the art writer next door:
    a symlink planted there was followed and ``os.replace`` published the link as
    the destination. Here the flags above already refused that; the random name
    is what keeps a squatter from refusing the WRITE instead. A FIFO at the
    derived path failed the create and then self-healed, because the ``finally``
    below unlinked it. A DANGLING symlink was the lockout: ``Path.exists()``
    follows it, so it was never unlinked and every attempt for that track failed
    EEXIST.

    The name does NOT embed ``dst.name``, so its length does not grow with the
    destination's: 33 bytes for this writer's ``.lrc``/``.txt`` and this box's
    7-digit ``pid_max``. Embedding it cost ``len(dst.name) + 30``, which made a
    sidecar name of 226-250 bytes fail ENAMETOOLONG where the 5-byte
    ``.<name>.tmp`` had written it. Same naming rule as ``app.playlists.atomic``
    and ``artist_art._tmp_path``.
    """
    return dst.parent / f".{os.getpid()}.{secrets.token_hex(8)}{dst.suffix}.tmp"


def _atomic_write_text(dst: Path, text: str) -> None:
    """Atomic utf-8 write (text mirror of ``artist_art._atomic_write_bytes``):
    an unpredictable tmp created EXCLUSIVELY in the same dir (:func:`_tmp_path`,
    :data:`_TMP_CREATE_FLAGS`) -> fsync -> dst mode preserved on rewrite (umask
    default on first write) -> os.replace -> fsync parent dir.

    Raises ``OSError`` and the caller decides what that means.
    """
    tmp = _tmp_path(dst)
    try:
        # 0o666 so the first write still takes the umask default, as the mode
        # test pins; a rewrite has its mode restored by the copymode below.
        fd = os.open(tmp, _TMP_CREATE_FLAGS, 0o666)
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:  # pragma: no cover - fdopen fails only on a bad fd
            os.close(fd)
            raise
        with stream as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if dst.exists():
            shutil.copymode(dst, tmp)  # mode preserved on rewrite; umask default on first write
        os.replace(tmp, dst)
        dir_fd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        # Whatever is at the temp path: ours, unless something guessed the name
        # this call picked and got there first — in which case the create above
        # already failed and this unlinks the squatter (a dangling symlink
        # excepted: ``exists()`` follows it and reads absent). That condition is
        # what keeps the line off paths somebody else put in the music folder.
        # Its price is that a process KILLED mid-write leaves one dotfile no
        # later call clears — the residual ``playlists.atomic`` already carries.
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()


def _sidecar_base(item: Any) -> str | None:
    """The track path without its extension, for building a sibling sidecar path.

    Item-level wrapper over :func:`app.beets.sidecars.sidecar_base` (the single
    definition of the stem rule, shared with reorganize's sidecar carry).
    ``item.path`` is beets' bytes path; an absent/empty path yields None (e.g. a
    singleton not yet on disk) so the caller no-ops instead of writing garbage.
    """
    return sidecar_base(getattr(item, "path", None))


def _has_sidecar(item: Any) -> bool:
    """Whether a ``.lrc`` or ``.txt`` lyric sidecar already sits next to the track."""
    base = _sidecar_base(item)
    if base is None:
        return False
    return any(os.path.exists(base + ext) for ext in SIDECAR_EXTS)


#: A sidecar larger than this cannot be a bare "[Instrumental]" marker, so it is
#: read no further and kept. Bounds the read on a per-track path.
_MARKER_READ_CAP = 4096


def _is_marker_sidecar(path: Path) -> bool:
    """Whether this file's ENTIRE body is beets' "[Instrumental]" marker.

    The authorship proxy for the one deletion this module still makes. Nothing
    records who wrote a sidecar, so content decides: a file whose every non-empty
    line is the marker holds no lyric data and is exactly the artifact legacy
    flows left behind, while any real lyric text disqualifies the file.

    Timestamps are stripped line-wise with beets' own ``Lyrics.LRC_TIMESTAMP_PAT``
    (``.venv/lib/python3.12/site-packages/beets/util/lyrics.py:31``) so the
    LRC-timestamped form pre-#122 wrote — ``[00:01.00] [Instrumental]`` — matches
    the constant at that module's line 15 too.

    Everything else is False, i.e. KEEP: real text, a mixed file, an EMPTY file
    (a vacuous "no line differs" match is how a content guard turns back into a
    blanket deleter), a file past ``_MARKER_READ_CAP``, undecodable bytes, a
    non-regular or absent path, or a read error — unknown content may be the
    user's lyrics. Never raises, and never opens anything but a regular file.
    """
    try:
        if not path.is_file():
            # A cheap stat, and a HARD requirement rather than an optimisation:
            # opening a FIFO for reading BLOCKS until a writer appears, and the
            # backfill worker is single-slot — one named pipe at a sidecar path
            # would park it until the process restarts. A device node, directory
            # or dangling symlink is likewise never a sidecar, and the ordinary
            # "no sidecar here" case lands on this line too, silently: it is not
            # a fault, and a warning would fire twice per item on a library with
            # none.
            return False
        with open(path, "rb") as f:
            raw = f.read(_MARKER_READ_CAP + 1)
    except FileNotFoundError:
        return False  # raced away between the stat and the open — not a fault
    except OSError:
        _log.warning("lyric sidecar unreadable, keeping it: %s", path, exc_info=True)
        return False
    if len(raw) > _MARKER_READ_CAP:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lines = [Lyrics.LRC_TIMESTAMP_PAT.sub("", line).strip() for line in text.splitlines()]
    body = [line for line in lines if line]
    return bool(body) and all(line == INSTRUMENTAL_LYRICS for line in body)


def remove_instrumental_marker_sidecars(item: Any) -> list[str]:
    """Delete this track's ``.lrc``/``.txt`` sidecars **that are nothing but the
    "[Instrumental]" marker**; return the paths removed.

    Deliberately not a general deleter. It is scoped twice over: to the two
    siblings :func:`write_lyric_sidecar` could have written (a scope over NAMES),
    and to files whose whole content is the marker (:func:`_is_marker_sidecar` —
    a scope over CONTENT, the only authorship proxy available). A sidecar holding
    real lyrics is the user's until proven otherwise and is always kept, so no
    caller — present or future — can use this to destroy lyric data.

    A path-less item, a missing sidecar, a non-marker sidecar or an unlink error
    is a no-op (read/unlink errors logged; a missing or non-marker sidecar is
    deliberately silent — it is the ordinary case) rather than an error — never
    raises, and never touches the audio file or a neighbouring track's sidecar.
    """
    base = _sidecar_base(item)
    if base is None:
        return []
    removed: list[str] = []
    for ext in SIDECAR_EXTS:
        path = Path(base + ext)
        if not _is_marker_sidecar(path):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            _log.warning("lyric sidecar removal failed: %s", path, exc_info=True)
            continue
        removed.append(str(path))
    return removed


def _sidecars_are_all_markers(item: Any) -> bool:
    """Whether every sidecar beside this track is an "[Instrumental]" marker file.

    False when there are none at all: an empty ``all()`` is vacuously true, and a
    vacuous match is exactly how a content guard turns back into a blanket
    deleter. All-or-nothing on purpose — a curated ``.lrc`` next to a marker
    ``.txt`` is one user's lyric state, and acting on half of it is acting on a
    guess.
    """
    base = _sidecar_base(item)
    if base is None:
        return False
    existing = [Path(base + ext) for ext in SIDECAR_EXTS if os.path.exists(base + ext)]
    return bool(existing) and all(_is_marker_sidecar(path) for path in existing)


def write_lyric_sidecar(item: Any, lyrics: Lyrics) -> str | None:
    """Write a Plex-readable lyric sidecar next to the track; return its path or None.

    **Fill gaps only — never clobber.** A track that already has a ``.lrc`` OR a
    ``.txt`` beside it is left exactly as it is: nothing written, nothing
    unlinked, None returned. Nothing anywhere records who wrote a sidecar, and a
    self-hosted Plex user's curated ``.lrc`` collection lives at precisely these
    paths — so an existing NON-MARKER file always wins, the same posture
    :func:`app.beets.sidecars.move_sidecars` already states ("keeping one
    recoverable beats silently destroying either"). Three consequences worth
    naming: a plain result never REPLACES a synced ``.lrc`` (no downgrade), it is
    not written BESIDE one either (no coexistence — nothing in this repo
    establishes what Plex prefers when both are present, so the case is never
    created), and a same-extension atomic rewrite is refused too, an atomic
    replace being destruction all the same.

    The single exception is content-proven and points the other way: when EVERY
    existing sidecar is an "[Instrumental]" marker
    (:func:`_sidecars_are_all_markers`), the set counts as absent and is cleared
    before the write. A stale marker agrees with "this track has no lyrics", so
    keeping it while real lyrics arrive would leave Plex serving "[Instrumental]"
    forever. Any non-marker file present — including one half of a mixed pair —
    still refuses everything.

    The fetched lyrics still reach ``item.lyrics`` and the embedded tag via
    :func:`_store_lyrics`; only the FILE layer is gap-filling. The tag and a
    user's sidecar can therefore hold different lyrics — accepted: Plex reads
    only the sidecar, MusicDrop never renders lyric text, and honesty beats
    deletion.

    ``.lrc`` (timestamped) when the fetched lyrics are synced, else ``.txt``
    (plain, timestamps stripped). Non-destructive (never touches the audio file)
    and best-effort: a write error is logged and swallowed so a batch keeps
    going; never raises.
    """
    base = _sidecar_base(item)
    if base is None:
        return None
    if _has_sidecar(item):
        if not _sidecars_are_all_markers(item):
            return None
        # Every sidecar here is a stale "[Instrumental]" marker, which AGREES
        # with "no lyrics" — keeping it would leave Plex showing "[Instrumental]"
        # for a track we just found lyrics for, the mirror image of the verdict
        # path that deletes exactly these files. Cleared through the
        # marker-guarded remover, so this cannot reach anything else.
        remove_instrumental_marker_sidecars(item)
    if lyrics.synced:
        ext, body = SYNCED_EXT, lyrics.text
    else:
        ext, body = PLAIN_EXT, "\n".join(lyrics.text_lines)
    body = body.strip()
    if not body:
        return None
    dst = Path(base + ext)
    try:
        _atomic_write_text(dst, body + "\n")
    except OSError:
        _log.warning("lyric sidecar write failed: %s", dst, exc_info=True)
        return None
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
    any stale lyrics text off the DB row, and removes the track's MARKER
    sidecars — Plex reads those, and an old "[Instrumental]" file would otherwise
    outlive the verdict. Marker only: a sidecar holding real lyrics survives this
    verdict (see :func:`remove_instrumental_marker_sidecars`), which does leave
    Plex showing lyrics for a track the DB calls instrumental — the honest
    outcome, since the backend's verdict is no evidence about who wrote that
    file. This is the ONLY deletion left in this module. The audio file's own tag
    is deliberately left alone (no ``try_write``): a stale tag is inert, and
    rewriting tags is not this feature's job.
    """
    item.lyrics = ""
    item["lyrics_instrumental"] = 1
    item["lyrics_checked"] = 1
    _set_source_flex(item, lyrics)
    item.store()
    remove_instrumental_marker_sidecars(item)


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


def _early_skip_outcome(
    item_id: int, item: Any, *, force: bool, recheck_misses: bool
) -> ItemLyricsOutcome | None:
    """The "already answered, don't search" outcome for this item, or None when
    the item must actually be fetched."""
    # Answered already, definitively: beets (or a previous run of ours) flagged
    # this track as having no lyrics by nature. Deliberately NOT gated on
    # recheck_misses, and deliberately independent of lyrics_checked — beets'
    # 2.13 migration flags pre-existing instrumentals without setting it.
    if not force and _is_instrumental(item):
        # A REPORT, never file work. This branch used to delete both sidecars
        # before returning — a "skip" for an operation that had just removed two
        # files, on tracks whose flag beets' 2.13 migration set (a flag MusicDrop
        # never wrote). That one-shot sweep of migrated "[Instrumental]" markers
        # shipped in #123 and has since run in production; a fresh verdict still
        # cleans its own markers via _store_instrumental. Any straggler is now
        # left alone rather than risking a curated sidecar.
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
    return None


def _apply_fetched_result(
    item_id: int, item: Any, result: Any, *, write: bool
) -> ItemLyricsOutcome | None:
    """Apply one backend's result to the item; its outcome, or None when the
    result carried no usable text (so the search keeps going to the next
    pair/backend)."""
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
    return None


def _try_backend(
    backend: Any,
    item: Any,
    *,
    artist: object,
    title: object,
    album: str,
    length: int,
    item_id: int,
    write: bool,
) -> tuple[ItemLyricsOutcome | None, bool]:
    """One backend's attempt for one (artist, title) pair.

    Returns ``(outcome, failed)``: the outcome when this backend stored a
    result (None keeps the search going) and whether a transient network
    error was hit (``fetch_failed`` must NOT be marked ``lyrics_checked``).
    """
    try:
        result = backend.fetch(artist, title, album, length)
    except HTTPNotFoundError:
        return None, False  # this pair/backend simply has nothing
    except requests.exceptions.RequestException as exc:
        # Concise one-liner (str(exc) reads "429 ... Too Many Requests
        # for url: ...") instead of a per-item traceback flood.
        _log.warning(
            "lyrics fetch failed: %s [%s]: %s",
            _item_label(item),
            _backend_name(backend),
            exc,
        )
        return None, True
    if result is None:
        return None, False
    return _apply_fetched_result(item_id, item, result, write=write), False


def _search_for_lyrics(
    plugin: Any, item: Any, *, item_id: int, write: bool
) -> tuple[ItemLyricsOutcome | None, bool]:
    """Try every search pair across the plugin's backends.

    Returns ``(outcome, network_failed)``: the outcome of the first stored
    result (or None when nothing usable was found) and whether any backend
    hit a network error (``fetch_failed`` is transient and must NOT be
    marked ``lyrics_checked``).
    """
    from beetsplug.lyrics import search_pairs

    album = str(item.album or "")
    length = int(item.length or 0)
    failed = False
    for artist, titles in search_pairs(item):
        for title in titles:
            for backend in plugin.backends:
                outcome, backend_failed = _try_backend(
                    backend,
                    item,
                    artist=artist,
                    title=title,
                    album=album,
                    length=length,
                    item_id=item_id,
                    write=write,
                )
                failed = failed or backend_failed
                if outcome is not None:
                    return outcome, failed
    return None, failed


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

    ``force`` bypasses the skip gates and NOTHING else. It is not threaded to the
    sidecar layer, which is fill-gaps-only for every caller
    (:func:`write_lyric_sidecar`), and it has no production caller at any layer:
    ``app/lyrics_jobs/runner.py:73`` hardcodes ``force=False`` and no endpoint or
    UI control exposes it. **If force is ever surfaced**, giving it destructive
    file semantics needs two things first: the trigger must get this repo's
    destructive-action AlertDialog (the convention lives in
    ``frontend/src/pages/albums/DeleteAlbumAction.tsx``; the Backfill button has
    none today), and a replaced sidecar must go to Trash rather than
    ``Path.unlink()``, like every other delete in the app.

    One known caveat, unchanged by the sidecar guards: a track with a curated
    sidecar and an empty tag falls through the skip-existing gate below, so it is
    FETCHED once. A found result fills the tag and it settles into
    ``skipped_existing``; a clean ``not_found`` sets ``lyrics_checked`` and it
    settles into ``skipped_checked`` — except on ``recheck_misses`` runs (the
    panel toggle, and every per-album fetch, which pins it True), where such a
    track is re-fetched each time. Network only: the disk is never touched.
    """
    item_id = int(item.id)
    skip = _early_skip_outcome(item_id, item, force=force, recheck_misses=recheck_misses)
    if skip is not None:
        return skip
    outcome, failed = _search_for_lyrics(plugin, item, item_id=item_id, write=write)
    if outcome is not None:
        return outcome
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


def start_album_lyrics_op(
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
