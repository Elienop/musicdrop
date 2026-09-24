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

import errno
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beets
import confuse
import requests
from beets.library import Library
from beets.util.lyrics import INSTRUMENTAL_LYRICS, Lyrics
from beetsplug._utils.requests import HTTPNotFoundError

from app.beets.library import LibraryHandle, _is_instrumental, _music_dir
from app.beets.sidecars import PLAIN_EXT, SIDECAR_EXTS, SYNCED_EXT, sidecar_base
from app.fsutil import open_below, open_root
from app.models.lyrics import (
    ItemLyricsOutcome,
    ItemLyricsStatus,
    LyricsBackfillStatus,
    LyricsCoverage,
)
from app.playlists.atomic import write_atomic_text
from app.wire import display_path

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


def _sidecar_base(item: Any) -> str | None:
    """The track path without its extension, for building a sibling sidecar path.

    Item-level wrapper over :func:`app.beets.sidecars.sidecar_base` (the single
    definition of the stem rule, shared with reorganize's sidecar carry).
    ``item.path`` is beets' bytes path; an absent/empty path yields None (e.g. a
    singleton not yet on disk) so the caller no-ops instead of writing garbage.
    """
    return sidecar_base(getattr(item, "path", None))


def _sidecar_present(item: Any) -> bool:
    """Whether a lyric sidecar sits next to the track, read through the album
    folder's own descriptor — the same one the write resolves its names against.

    This decides the ``skipped_existing`` early return in
    :func:`_early_skip_outcome`, so it gates the FETCH: read by NAME it could
    disagree with the write, and did. A dangling symlink at a sidecar name is
    absent to ``os.path.exists`` and PRESENT to the descriptor's ``lstat``, so
    such a track was fetched on every run and never written; a sidecar visible
    only THROUGH a symlinked album folder was the mirror image, reported as
    complete on a file the write refuses to touch.

    A folder this cannot open answers False — the fetch then runs (the DB layer
    still benefits) and :func:`write_lyric_sidecar` logs its own refusal, once.
    """
    base = _sidecar_base(item)
    if base is None:
        return False
    try:
        dir_fd = _open_album_dir(Path(base).parent, Path(_music_dir(item._db)))
    except (OSError, ValueError):
        return False
    try:
        return bool(_present_sidecars(base, dir_fd=dir_fd))
    finally:
        os.close(dir_fd)


def _sidecar_names(base: str) -> list[str]:
    """The two sidecar names beside this track — the only strings that travel to a
    syscall once the album folder's descriptor is open."""
    return [Path(base + ext).name for ext in SIDECAR_EXTS]


def _present_sidecars(base: str, *, dir_fd: int) -> list[str]:
    """The sidecar NAMES that exist in ``dir_fd``, in ``SIDECAR_EXTS`` order.

    ``follow_symlinks=False``, so a symlink at a sidecar name counts as PRESENT:
    the gap-fill gate then refuses instead of publishing a regular file over it.
    """
    return [name for name in _sidecar_names(base) if _exists_at(name, dir_fd=dir_fd)]


def _exists_at(name: str, *, dir_fd: int) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return True


#: A sidecar larger than this cannot be a bare "[Instrumental]" marker, so it is
#: read no further and kept. Bounds the read on a per-track path.
_MARKER_READ_CAP = 4096


#: A sidecar is read through the album folder's descriptor: ``O_NOFOLLOW`` so a
#: symlink at the name is refused rather than read through, and ``O_NONBLOCK`` so
#: a FIFO planted there cannot park the single-slot backfill worker on the open.
#: Neither says the file IS regular — the ``fstat`` below decides that.
_SIDECAR_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _is_marker_body(raw: bytes) -> bool:
    """Whether these bytes are nothing but beets' "[Instrumental]" marker.

    Timestamps are stripped line-wise with beets' own ``Lyrics.LRC_TIMESTAMP_PAT``
    (``.venv/lib/python3.12/site-packages/beets/util/lyrics.py:31``) so the
    LRC-timestamped form pre-#122 wrote — ``[00:01.00] [Instrumental]`` — matches
    the constant at that module's line 15 too.

    False for real text, a mixed file, EMPTY bytes (a vacuous "no line differs"
    match is how a content guard turns back into a blanket deleter), more than
    ``_MARKER_READ_CAP`` bytes, and undecodable bytes.
    """
    if len(raw) > _MARKER_READ_CAP:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lines = [Lyrics.LRC_TIMESTAMP_PAT.sub("", line).strip() for line in text.splitlines()]
    body = [line for line in lines if line]
    return bool(body) and all(line == INSTRUMENTAL_LYRICS for line in body)


def _is_marker_sidecar_at(name: str, *, dir_fd: int, shown: str) -> bool:
    """Whether ``name`` in ``dir_fd`` is a file whose ENTIRE body is the marker.

    The authorship proxy for the one deletion this module still makes. Nothing
    records who wrote a sidecar, so content decides: a file whose every non-empty
    line is the marker holds no lyric data and is exactly the artifact legacy
    flows left behind, while any real lyric text disqualifies the file. ``shown``
    is the path to name in a log line; only ``name`` reaches a syscall.

    Everything it cannot read is False, i.e. KEEP — unknown content may be the
    user's lyrics. A non-regular file (FIFO, directory, device) is rejected by
    the ``fstat`` before any read: the open cannot block on it
    (``_SIDECAR_READ_FLAGS``), and a read would. A refusal is silent — an absent
    name is the ordinary case and a warning would fire twice per item on a
    library with no sidecars, and a symlink at the name is a refusal rather than
    a fault. A real read failure is logged once. Never raises.
    """
    try:
        fd = os.open(name, _SIDECAR_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        # ELOOP is what O_NOFOLLOW answers for a symlink at the name, live or
        # dangling (measured 2026-09-12; with O_DIRECTORY it would be ENOTDIR).
        # Silent, the way the stat guard this replaced rejected every
        # non-regular shape.
        if exc.errno != errno.ELOOP:
            _log.warning(
                "lyric sidecar unreadable, keeping it: %r", display_path(shown), exc_info=True
            )
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        raw = os.read(fd, _MARKER_READ_CAP + 1)
    except OSError:
        _log.warning("lyric sidecar unreadable, keeping it: %r", display_path(shown), exc_info=True)
        return False
    finally:
        os.close(fd)
    return _is_marker_body(raw)


def _remove_marker_sidecars_at(base: str, *, dir_fd: int) -> list[str]:
    """The marker-guarded unlink, every name resolved against ``dir_fd``."""
    removed: list[str] = []
    for ext in SIDECAR_EXTS:
        shown = base + ext
        name = Path(shown).name
        if not _is_marker_sidecar_at(name, dir_fd=dir_fd, shown=shown):
            continue
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            continue
        except OSError:
            _log.warning("lyric sidecar removal failed: %r", display_path(shown), exc_info=True)
            continue
        removed.append(shown)
    return removed


def remove_instrumental_marker_sidecars(item: Any, *, root: Path) -> list[str]:
    """Delete this track's ``.lrc``/``.txt`` sidecars **that are nothing but the
    "[Instrumental]" marker**; return the paths removed.

    Deliberately not a general deleter. It is scoped three times over: to the
    album folder's own descriptor (a scope over the FOLDER), to the two siblings
    :func:`write_lyric_sidecar` could have written (a scope over NAMES), and to
    files whose whole content is the marker (:func:`_is_marker_sidecar_at` — a
    scope over CONTENT, the only authorship proxy available). A sidecar holding
    real lyrics is the user's until proven otherwise and is always kept, so no
    caller — present or future — can use this to destroy lyric data.

    ``root`` is the library root. The read and the unlink both resolve their name
    against the descriptor :func:`_album_dir_fd` opened part by part below it, so
    a folder reached through a symlinked component is refused with nothing read
    and nothing unlinked. Measured before this: the unlink followed such a link
    and deleted a file outside the library. Anchoring confines the remaining
    name-level race — a swap between the content read and the unlink — to the
    album folder itself.

    A path-less item, a refused folder, a missing sidecar, a non-marker sidecar
    or an unlink error is a no-op (read/unlink errors logged; a missing or
    non-marker sidecar is deliberately silent — it is the ordinary case) rather
    than an error: never raises, and never touches the audio file or a
    neighbouring track's sidecar.
    """
    base = _sidecar_base(item)
    if base is None:
        return []
    dir_fd = _album_dir_fd(Path(base).parent, root, refused="lyric sidecars left alone")
    if dir_fd is None:
        return []
    try:
        return _remove_marker_sidecars_at(base, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _open_album_dir(directory: Path, root: Path) -> int:
    """A directory fd for ``directory``, refusing a symlinked component below ``root``.

    Owner ruling 2026-09-12: below the library root a bind mount is the supported
    spelling for spanning disks, so a symlinked component is refused; the root
    itself may be reached through a link. :func:`app.fsutil.open_below` opens
    every part below the root with ``O_NOFOLLOW`` — measured, a symlink answers
    ENOTDIR and so does a plain file in the way, so the errno cannot say which.

    A track sitting directly IN the root (a flat ``path_formats``) has no part
    below it, which ``open_below`` refuses by contract; the root is opened here
    instead, following links the way the shared writer's own parent open does.

    Raises ``OSError`` (something in the way) or ``ValueError`` (``directory`` is
    outside ``root``). The fd is the caller's to close.
    """
    rel = directory.relative_to(root)
    if not rel.parts:
        return open_root(root)
    return open_below(root, rel)


def _album_dir_fd(directory: Path, root: Path, *, refused: str) -> int | None:
    """:func:`_open_album_dir`'s fd, or None and ONE warning naming the refusal.

    Two causes, two messages: a row that never was under the root (a legacy
    import, or a ``directory:`` the operator respelled — ``/mnt/music`` vs
    ``/music``) is not a link problem, and a bind mount would not help it.

    ``%r`` + ``display_path``: a folder name carrying a newline or an ANSI escape
    forges log lines (measured; the rule is stated at ``api/artists.py:896``).
    """
    try:
        return _open_album_dir(directory, root)
    except ValueError:
        _log.warning(
            "%s, this folder is not under the library root: %r",
            refused,
            display_path(str(directory)),
        )
    except OSError:
        _log.warning(
            "%s, this folder is not reachable without following a link (bind mounts are"
            " the supported spelling): %r",
            refused,
            display_path(str(directory)),
        )
    return None


def write_lyric_sidecar(item: Any, lyrics: Lyrics, *, root: Path) -> str | None:
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
    (:func:`_is_marker_sidecar_at`), the set counts as absent and is cleared
    before the write. A stale marker agrees with "this track has no lyrics", so
    keeping it while real lyrics arrive would leave Plex serving "[Instrumental]"
    forever. Any non-marker file present — including one half of a mixed pair —
    still refuses everything.

    The fetched lyrics still reach ``item.lyrics`` and the embedded tag via
    :func:`_store_lyrics`; only the FILE layer is gap-filling. The tag and a
    user's sidecar can therefore hold different lyrics — accepted: Plex reads
    only the sidecar, MusicDrop never renders lyric text, and honesty beats
    deletion.

    ``root`` is the library root. The album folder's descriptor is opened FIRST,
    part by part below it (:func:`_album_dir_fd`), and every later syscall —
    the gap-fill gate's stat, the marker read, the clear's unlink, the create and
    the publish — resolves a bare NAME against that one fd, so no component can
    be re-resolved between the check and the act. A folder outside the root, or
    reached through a symlinked component, is refused: None, one log line, and
    nothing read, unlinked or written. Measured before the ruling: the write
    followed such a link and landed outside the library; measured before the
    descriptor moved first, the clear deleted a sidecar through the link the
    write then refused.

    ``.lrc`` (timestamped) when the fetched lyrics are synced, else ``.txt``
    (plain, timestamps stripped). Non-destructive (never touches the audio file)
    and best-effort: a write error is logged and swallowed so a batch keeps
    going; never raises.
    """
    base = _sidecar_base(item)
    if base is None:
        return None
    if lyrics.synced:
        ext, body = SYNCED_EXT, lyrics.text
    else:
        ext, body = PLAIN_EXT, "\n".join(lyrics.text_lines)
    body = body.strip()
    if not body:
        return None
    dst = Path(base + ext)
    dir_fd = _album_dir_fd(dst.parent, root, refused="lyric sidecar skipped")
    if dir_fd is None:
        return None
    try:
        present = _present_sidecars(base, dir_fd=dir_fd)
        if present:
            if not all(
                _is_marker_sidecar_at(name, dir_fd=dir_fd, shown=str(dst.parent / name))
                for name in present
            ):
                # The tag now holds the lyrics and the FILE layer is gap-fill
                # only, so the existing sidecar wins. One line, because "found"
                # with nothing written is otherwise invisible — and it fires once
                # per track, since the filled tag then answers the skip gate.
                _log.warning(
                    "lyric sidecar not written, one is already beside the track: %r",
                    display_path(str(dst)),
                )
                return None
            # Every sidecar here is a stale "[Instrumental]" marker, which AGREES
            # with "no lyrics" — keeping it would leave Plex showing
            # "[Instrumental]" for a track we just found lyrics for, the mirror
            # image of the verdict path that deletes exactly these files. Cleared
            # through the marker-guarded unlink, so this cannot reach anything
            # else.
            _remove_marker_sidecars_at(base, dir_fd=dir_fd)
        try:
            # Only ``dst.name`` travels: every name is resolved against the fd the
            # walk returned. ``mode=None`` takes the umask default, so the sidecar
            # is as readable as the rest of the library (the Plex process is
            # another uid). Its preserve-on-rewrite arm is all but unreachable
            # from here: the gate above only lets a write through when nothing
            # sits at the name or every sidecar was a marker just unlinked — the
            # one way a file is still at the name is an unlink that FAILED and
            # logged (``_remove_marker_sidecars_at``), where preserving that
            # file's mode is the wanted answer anyway.
            write_atomic_text(Path(dst.name), body + "\n", mode=None, dir_fd=dir_fd)
        except (OSError, UnicodeEncodeError):
            # UnicodeEncodeError (a ValueError): the shared writer's encode is
            # STRICT, and fetched lyrics can carry a lone surrogate. This
            # function's contract is that it never raises.
            _log.warning("lyric sidecar write failed: %r", display_path(str(dst)), exc_info=True)
            return None
    finally:
        os.close(dir_fd)
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
    # The root the markers must stay below, read from the item's own library
    # handle — the private surface ``item.store()`` above already requires. Kept
    # off the signature: ``tests/test_browse.py:737`` calls this with an item.
    remove_instrumental_marker_sidecars(item, root=Path(_music_dir(item._db)))


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
    # The root the sidecar must stay below, read from the item's own library
    # handle — the private surface ``item.store()`` above already requires.
    write_lyric_sidecar(item, lyrics, root=Path(_music_dir(item._db)))
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
    if not force and item.lyrics and _sidecar_present(item):
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
    # A backend can hand back blank text (MusiXmatch builds its Lyrics from
    # whatever its page split leaves). Treat blank text as no usable match —
    # fall through to the next pair/backend and ultimately ``not_found``.
    if result.text.strip():
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
        # for url: ...") instead of a per-item traceback flood. The label is raw
        # TAG text, so it goes ``%r``: the same forged-log-line shape a path has
        # (a newline plus an ANSI escape in a title), measured on the folder name.
        _log.warning(
            "lyrics fetch failed: %r [%s]: %s",
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
