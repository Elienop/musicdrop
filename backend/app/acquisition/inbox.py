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

``_not_imported_yet`` is the ONE "Not imported yet" rule (a top-level
non-hidden, non-symlinked dir holding audio that no row in "Waiting for review"
holds and that MusicDrop has not imported unchanged since), shared by the count
(``count_pending``, the status route's ``inbox_pending``), the Review listing
(``list_inbox``) and Review all (``settled_folders``).

``record_imported`` is the other half of the last clause: every import that
lands every album of a folder inside slskd's folder records it in the ledger
(decisions #77, remember and hide).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

from app.acquisition.ledger import AcquisitionLedger
from app.bank.store import active_folders_under
from app.beets.library import LibraryHandle
from app.config import INBOX_STORE, Settings, store_dir
from app.models.acquisition import InboxItem, LedgerEntry, LedgerOutcome
from app.wire import display_path

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
    configured override is made absolute for the containment checks below, which
    compare real paths. Synchronous (pathlib I/O must not run on the event loop).
    """
    return store_dir(settings, INBOX_STORE, handle.beets_dir).resolve()


def contain(path: str, inbox_dir: Path, *, strict: bool = False) -> Path | None:
    """Return ``realpath(path)`` only if it is the inbox root or strictly under it.

    Rejects ``../`` escapes, absolute paths outside the inbox, and symlink
    escapes (``resolve()`` follows the link, so the resolved target is checked).
    Returns ``None`` on any of those, or on an error while resolving — an OS
    error, a ``ValueError`` from a malformed input such as an embedded null
    byte, or the ``RuntimeError`` Python 3.12's ``resolve()`` raises for a
    symlink loop (3.13 raises ``OSError``) — so a hostile webhook path can never
    escape as an unhandled 500.

    With ``strict=True`` the inbox ROOT itself is ALSO rejected (only a strict
    descendant passes). An import target must be strict: importing the inbox
    root would sweep in every unrelated/still-downloading sibling and the ledger.
    """
    try:
        resolved = Path(path).resolve()
        root = inbox_dir.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if root in resolved.parents:
        return resolved
    if resolved == root and not strict:
        return resolved
    return None


def record_imported(ledger: AcquisitionLedger, inbox_dir: Path, folders: list[str]) -> None:
    """Record each of ``folders`` strictly inside slskd's folder as ``imported``.

    The import registry's recorder (attached at lifespan): it hands over the
    folders a finished run fully landed, whoever started it, and only the ones
    ``contain(strict=True)`` admits are kept. Each is keyed on its resolved path,
    which is how the list spells an entry, with its identity at this moment, so
    ``_not_imported_yet`` hides it until an entry is added, removed or renamed
    in it. ``mark`` raises ``OSError`` when the ledger cannot be written.
    """
    for folder in folders:
        contained = contain(folder, inbox_dir, strict=True)
        if contained is not None:
            ledger.mark(contained, outcome="imported")


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


def bank_held_names(inbox_dir: Path, bank_dir: Path) -> frozenset[str]:
    """Names of the inbox entries a row in "Waiting for review" holds.

    ONE bank read for the whole inbox, not one per entry. A row holds the
    top-level entry it sits at or below: the drain banks the album folder,
    which can be deeper than the entry (``inbox/X/CD1`` holds ``inbox/X``).

    Matched by spelling, whole names only. ``inbox_dir`` is resolved, and so is
    every folder the inbox routes and the drain hand over (``contain``). A row
    a sweep banked keeps the spelling that sweep walked: beets' ``normpath``
    does not resolve links, so a folder banked through a symlink or a bind
    mount of the inbox does not hide its entry.
    """
    prefix = os.path.join(inbox_dir, "")
    names = (
        folder[len(prefix) :].split("/", 1)[0]
        for folder in active_folders_under(bank_dir, inbox_dir)
    )
    return frozenset(name for name in names if name)


def _entry_is_inbox_item(entry: os.DirEntry[str]) -> bool:
    """True when an inbox entry is a plain top-level dir: not hidden/ledger/symlink."""
    if entry.name.startswith(".") or entry.name == LEDGER_FILENAME:
        return False
    # Skip symlinked entries: the import path's contain() rejects symlink
    # escapes, so the listing must not follow one out of the inbox either.
    return entry.is_dir(follow_symlinks=False)


def _imported_identities(ledger: AcquisitionLedger | None) -> Mapping[str, tuple[float, int]]:
    """Each ``imported`` ledger row's path -> the identity it was recorded with.

    Only ``imported``: a ``set_aside`` or ``failed`` drop is still sitting there
    and must stay reviewable (``settled_folders``), so those rows only annotate.
    """
    if ledger is None:
        return {}
    return {
        row.path: (row.mtime, row.size) for row in ledger.entries() if row.outcome == "imported"
    }


def _imported_unchanged(entry: os.DirEntry[str], imported: Mapping[str, tuple[float, int]]) -> bool:
    """Whether the ledger records ``entry`` itself as imported, unchanged since.

    The entry's own path, spelled as the list spells it (the resolved inbox plus
    the name), so a record for ``X/CD1`` or ``X 2`` never hides ``X``. One stat,
    and only for a path with a record; ``DirEntry`` caches it, so the list's own
    stat of the same entry costs nothing more. A folder that cannot be stat'ed
    stays listed.
    """
    recorded = imported.get(entry.path)
    if recorded is None:
        return False
    try:
        st = entry.stat()
    except OSError:
        return False
    return (st.st_mtime, st.st_size) == recorded


def _not_imported_yet(
    entry: os.DirEntry[str],
    held: frozenset[str],
    imported: Mapping[str, tuple[float, int]],
) -> bool:
    """THE rule for one "Not imported yet" entry, shared by the list, the count
    and Review all so the three can never disagree.

    A plain top-level dir holding audio that no row in "Waiting for review"
    holds (``held``, from ``bank_held_names``): that row is where its album is
    decided. An empty leftover dir or a loose file is never an entry. Nor is a
    folder MusicDrop imported and nobody has added, removed or renamed a file
    in since (``imported``, from ``_imported_identities``), checked before the
    audio walk so a hidden folder costs one stat.
    """
    if not _entry_is_inbox_item(entry) or entry.name in held:
        return False
    if _imported_unchanged(entry, imported):
        return False
    return has_audio(Path(entry.path))


def _not_imported_entries(
    inbox_dir: Path, ledger: AcquisitionLedger | None, held: frozenset[str]
) -> list[os.DirEntry[str]]:
    """Every inbox entry ``_not_imported_yet`` keeps; empty on an OS error."""
    try:
        entries = list(os.scandir(inbox_dir))
    except OSError:
        return []
    imported = _imported_identities(ledger)
    return [entry for entry in entries if _not_imported_yet(entry, held, imported)]


def count_pending(
    inbox_dir: Path, ledger: AcquisitionLedger | None, *, held: frozenset[str]
) -> int:
    """How many entries "Not imported yet" lists: the status route's ``inbox_pending``."""
    return len(_not_imported_entries(inbox_dir, ledger, held))


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


def settled_folders(
    inbox_dir: Path,
    ledger: AcquisitionLedger | None,
    *,
    held: frozenset[str],
    settle_seconds: float,
    now: float,
) -> list[Path]:
    """The "Not imported yet" entries that have been QUIET for ``settle_seconds``.

    What Review all hands over: the listed entries minus the ones still
    receiving files. Only an unchanged ``imported`` record keeps a folder out:
    a failed or set-aside drop is still sitting there and must remain
    reviewable.

    Skipping is always the safe direction — a folder we cannot stat, or one that
    vanishes mid-walk, is treated as in-flight rather than swept into an import.
    """
    settled: list[Path] = []
    for entry in _not_imported_entries(inbox_dir, ledger, held):
        folder = Path(entry.path)
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


def _entry_in_flight(entry: os.DirEntry[str], *, settle_seconds: float, now: float | None) -> bool:
    """True while ``entry``'s folder is still receiving files (or unreadable)."""
    if settle_seconds <= 0:
        return False
    newest = _newest_mtime(Path(entry.path))
    reference = time.time() if now is None else now
    # Unreadable (None) reads as in-flight, matching settled_folders:
    # we cannot know it is finished, so we must not imply it is.
    return newest is None or reference - min(newest, reference) < settle_seconds


def _entry_outcome(folder: Path, rows: list[LedgerEntry]) -> LedgerOutcome | None:
    """The first ``set_aside``/``failed`` row at or under ``folder`` (annotation only)."""
    outcome: LedgerOutcome | None = None
    for row in rows:
        if row.outcome not in ("set_aside", "failed"):
            continue
        try:
            row_path = Path(row.path).resolve()
        except (OSError, ValueError):
            continue  # a corrupt/NUL on-disk ledger path never 500s the list
        if row_path == folder or folder in row_path.parents:
            outcome = row.outcome
            break
    return outcome


def _inbox_item_for_entry(
    entry: os.DirEntry[str],
    ledger_rows: list[LedgerEntry],
    *,
    settle_seconds: float,
    now: float | None,
) -> InboxItem | None:
    """Build the ``InboxItem`` for one listed entry, or ``None`` if it vanished."""
    tracks, size = audio_stats(Path(entry.path))
    try:
        st = entry.stat()
    except OSError:
        return None
    folder = Path(entry.path).resolve()
    return InboxItem(
        # Display-safe: the name is also the import key the client hands
        # back, which ``resolve_display_path`` maps onto the real entry.
        name=display_path(entry.name),
        mtime=st.st_mtime,
        size=size,
        track_count=tracks,
        outcome=_entry_outcome(folder, ledger_rows),
        in_flight=_entry_in_flight(entry, settle_seconds=settle_seconds, now=now),
    )


def list_inbox(
    inbox_dir: Path,
    ledger: AcquisitionLedger | None,
    *,
    held: frozenset[str],
    settle_seconds: float = 0.0,
    now: float | None = None,
) -> list[InboxItem]:
    """The "Not imported yet" entries, ledger-annotated.

    A set-aside item IS in the ledger, so ``set_aside``/``failed`` rows only
    ANNOTATE — they never remove a row. The ledger keys the (possibly deeper)
    album path the webhook coalesced, so a row is annotated when such an entry
    sits at or under it. Only an ``imported`` row for the entry itself, unchanged
    since, leaves it out (the shared rule, ``_not_imported_yet``).
    """
    ledger_rows = ledger.entries() if ledger is not None else []
    items: list[InboxItem] = []
    for entry in _not_imported_entries(inbox_dir, ledger, held):
        item = _inbox_item_for_entry(entry, ledger_rows, settle_seconds=settle_seconds, now=now)
        if item is not None:
            items.append(item)
    items.sort(key=lambda i: i.mtime, reverse=True)
    return items
