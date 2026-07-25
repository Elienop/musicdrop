"""Inbox-dir resolution, the path-containment security boundary + inbox scanning.

``resolve_inbox_dir`` mirrors ``app.beets.trash.resolve_trash_dir`` exactly: an
empty setting defaults under the handle's already-absolute ``beets_dir`` (which
sidesteps the cwd-relative gotcha), an override is resolved to absolute.

``contain`` is the single guard that every producer (the slskd webhook in
Chunk 4) routes a folder through before it can reach the importer. It
``realpath``s the candidate AND the inbox root, then admits the path only when it
resolves to the inbox root itself or has the inbox root among its parents. Because
``Path.resolve()`` follows symlinks, a symlink planted under the inbox that points
outside is rejected too — the resolved target is no longer under the inbox.

``count_pending`` / ``list_inbox`` hold the ONE "what counts as an inbox item"
definition (a top-level non-hidden, non-symlinked dir holding audio) shared by
the nav badge, the Review listing, and the one-click review start.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from app.acquisition.ledger import AcquisitionLedger
from app.beets.library import LibraryHandle
from app.config import Settings
from app.models.acquisition import InboxItem, LedgerOutcome

# A leaf folder slskd fires a separate completion for: "CD1", "Disc 2",
# "disk_3", "CD-04". A multi-disc album fragments into one event per disc, so we
# walk up to the shared album parent and enqueue it once.
_DISC_DIR_RE = re.compile(r"(?i)^(cd|disc|disk)[\s_-]*\d+$")

# The ledger file the queue persists under the inbox — never an album folder, so
# it does not count toward "is there anything to review".
LEDGER_FILENAME = ".musicdrop-ledger.json"

# Extensions we treat as audio when deciding whether an inbox folder holds music.
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma", ".aiff"}


def resolve_inbox_dir(settings: Settings, handle: LibraryHandle) -> Path:
    """Where completed downloads land: configured ``inbox_dir`` or ``<beets_dir>/inbox``.

    Empty setting = default under the handle's already-absolute ``beets_dir``; a
    configured override is resolved to absolute. Synchronous (pathlib I/O must
    not run on the event loop). Mirrors ``resolve_trash_dir``.
    """
    if settings.inbox_dir:
        return Path(settings.inbox_dir).resolve()
    return handle.beets_dir / "inbox"


def contain(path: str, inbox_dir: Path, *, strict: bool = False) -> Path | None:
    """Return ``realpath(path)`` only if it is the inbox root or strictly under it.

    Rejects ``../`` escapes, absolute paths outside the inbox, and symlink
    escapes (``resolve()`` follows the link, so the resolved target is checked).
    Returns ``None`` on any of those, or on an error while resolving — an OS
    error, or a ``ValueError`` from a malformed input such as an embedded null
    byte (so a hostile webhook path can never escape as an unhandled 500).

    With ``strict=True`` the inbox ROOT itself is ALSO rejected (only a strict
    descendant passes). A MOVE-import target must be strict: importing the inbox
    root would sweep in every unrelated/still-downloading sibling and the ledger.
    """
    try:
        resolved = Path(path).resolve()
        root = inbox_dir.resolve()
    except (OSError, ValueError):
        return None
    if root in resolved.parents:
        return resolved
    if resolved == root and not strict:
        return resolved
    return None


def coalesce_album_root(folder: Path, inbox_dir: Path) -> Path:
    """Walk a per-disc leaf up to its album parent; otherwise return ``folder``.

    slskd flattens trees and fires one ``DownloadDirectoryComplete`` per leaf
    dir, so a multi-disc album (``Artist/Album/CD1``, ``.../CD2``) would enqueue
    each disc separately. When ``folder`` is named like a disc dir AND its parent
    is *strictly* under ``inbox_dir`` (a real album dir, never the inbox root
    itself), return that parent so the whole album imports once. Deterministic —
    no timer/debounce: two disc siblings both resolve to the same parent and the
    queue's dedupe collapses them.
    """
    if not _DISC_DIR_RE.match(folder.name):
        return folder
    parent = folder.parent
    # ``strictly under`` = the inbox root is among the parent's ancestors (so the
    # parent is NOT the inbox root). Coalescing up to the inbox root would import
    # the entire inbox, so that case is refused.
    if inbox_dir.resolve() not in parent.resolve().parents:
        return folder
    # Only coalesce a GENUINE multi-disc album: the parent must hold a second
    # disc-named subdir besides ``folder``. Otherwise a lone album whose own
    # folder merely looks like a disc dir (e.g. an album named "CD1") would walk
    # up and MOVE-import the entire parent (artist) directory, merging unrelated
    # albums.
    try:
        disc_siblings = sum(
            1 for child in parent.iterdir() if child.is_dir() and _DISC_DIR_RE.match(child.name)
        )
    except OSError:
        return folder
    return parent if disc_siblings >= 2 else folder


def has_audio(folder: Path) -> bool:
    """True as soon as one audio file is found beneath ``folder`` (early-exit).

    ``os.walk``'s ``followlinks`` default is False, so a symlinked subdir (or a
    symlink loop) inside a real inbox folder is never descended into.
    """
    try:
        for _root, _dirs, files in os.walk(folder):
            if any(os.path.splitext(f)[1].lower() in AUDIO_EXTS for f in files):
                return True
    except OSError:
        return False
    return False


def audio_stats(folder: Path) -> tuple[int, int]:
    """``(track_count, total_bytes)`` of audio files beneath ``folder``."""
    count = 0
    size = 0
    try:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                    continue
                count += 1
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        return count, size
    return count, size


def count_pending(inbox_dir: Path) -> int:
    """Count top-level inbox folders holding audio — the SAME item definition the
    Review listing uses, so the nav badge can't show a phantom count from a loose
    non-audio file, an empty leftover dir, or a symlink. Skips hidden entries, the
    ledger file, and symlinked entries (parity with the import path's symlink
    guard). 0 on any OS error.
    """
    try:
        entries = list(os.scandir(inbox_dir))
    except OSError:
        return 0
    count = 0
    for entry in entries:
        if entry.name.startswith(".") or entry.name == LEDGER_FILENAME:
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        if has_audio(Path(entry.path)):
            count += 1
    return count


def _max_mtime(current: float | None, candidate: float) -> float:
    """The later of two mtimes (``None`` = nothing seen yet)."""
    return candidate if current is None or candidate > current else current


def _newest_mtime(folder: Path) -> float | None:
    """The newest mtime of ANY file under ``folder`` (None when unreadable).

    ANY file, not just audio: a downloader writes partial/temp/sidecar files
    while an album is still arriving, and those are exactly the signal that the
    folder is not finished. DIRECTORY mtimes count too — a tool that preserves
    timestamps (``unzip``, ``rsync -a``, ``cp -p``, a cross-filesystem ``mv``)
    drops files whose own mtimes are ancient, and then the only fresh signal is
    the directory whose entry list just changed. Cheap ``os.scandir`` walk — no
    audio parsing.
    """
    newest: float | None = None
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
            # The dir's own mtime bumps whenever an entry is added or removed —
            # the one signal that stays fresh for preserved-timestamp copies.
            newest = _max_mtime(newest, current.stat().st_mtime)
        except OSError:
            return None  # unreadable mid-walk -> treat as unsettled (caller skips)
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                return None  # vanished mid-walk -> still moving; skip it
            newest = _max_mtime(newest, mtime)
    return newest


def settled_folders(inbox_dir: Path, *, settle_seconds: float, now: float) -> list[Path]:
    """Top-level inbox items that have been QUIET for ``settle_seconds``.

    Same item definition as ``list_inbox``/``count_pending`` (one immediate
    child dir holding audio, no hidden/ledger/symlink entries), minus the ones
    still receiving files. Ledger-seen folders stay eligible: a failed or
    set-aside drop is still sitting there and must remain reviewable.

    Skipping is always the safe direction — a folder we cannot stat, or one that
    vanishes mid-walk, is treated as in-flight rather than swept into an import.
    """
    try:
        entries = list(os.scandir(inbox_dir))
    except OSError:
        return []
    settled: list[Path] = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == LEDGER_FILENAME:
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        folder = Path(entry.path)
        if not has_audio(folder):
            continue
        newest = _newest_mtime(folder)
        if newest is None:
            continue
        # Clamp a FUTURE mtime (NAS/container clock skew) to now: without this a
        # negative age is always < settle_seconds, so the folder would be skipped
        # on every click forever, with no way to import it from here. Clamped, it
        # simply reads as "just touched" and settles once the window elapses.
        age = now - min(newest, now)
        if age < settle_seconds:
            continue
        settled.append(folder)
    settled.sort(key=lambda f: f.name)
    return settled


def list_inbox(
    inbox_dir: Path,
    ledger: AcquisitionLedger | None,
    *,
    settle_seconds: float = 0.0,
    now: float | None = None,
) -> list[InboxItem]:
    """Top-level non-hidden inbox dirs holding audio, ledger-annotated (never filtered).

    An item = one immediate child directory with >=1 audio file beneath it (empty
    leftovers after a successful move-out are skipped). A set-aside item IS in the
    ledger, so the ledger only ANNOTATES (``set_aside``/``failed``) — it never
    removes a row. The ledger keys the (possibly deeper) album path the webhook
    coalesced, so a row is annotated when a ledger entry sits at or under it.
    """
    items: list[InboxItem] = []
    try:
        entries = list(os.scandir(inbox_dir))
    except OSError:
        return items
    ledger_rows = ledger.entries() if ledger is not None else []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == LEDGER_FILENAME:
            continue
        # Skip symlinked entries: the import path's contain() rejects symlink
        # escapes, so the listing must not follow one out of the inbox either.
        if not entry.is_dir(follow_symlinks=False):
            continue
        tracks, size = audio_stats(Path(entry.path))
        if tracks == 0:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        folder = Path(entry.path).resolve()
        in_flight = False
        if settle_seconds > 0:
            newest = _newest_mtime(Path(entry.path))
            reference = time.time() if now is None else now
            # Unreadable (None) reads as in-flight, matching settled_folders:
            # we cannot know it is finished, so we must not imply it is.
            in_flight = newest is None or reference - min(newest, reference) < settle_seconds
        outcome: LedgerOutcome | None = None
        for row in ledger_rows:
            if row.outcome not in ("set_aside", "failed"):
                continue
            try:
                row_path = Path(row.path).resolve()
            except (OSError, ValueError):
                continue  # a corrupt/NUL on-disk ledger path never 500s the list
            if row_path == folder or folder in row_path.parents:
                outcome = row.outcome
                break
        items.append(
            InboxItem(
                name=entry.name,
                mtime=st.st_mtime,
                size=size,
                track_count=tracks,
                outcome=outcome,
                in_flight=in_flight,
            )
        )
    items.sort(key=lambda i: i.mtime, reverse=True)
    return items
