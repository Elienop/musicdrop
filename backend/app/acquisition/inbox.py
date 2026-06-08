"""Inbox-dir resolution + the path-containment security boundary.

``resolve_inbox_dir`` mirrors ``app.beets.trash.resolve_trash_dir`` exactly: an
empty setting defaults under the handle's already-absolute ``beets_dir`` (which
sidesteps the cwd-relative gotcha), an override is resolved to absolute.

``contain`` is the single guard that every producer (the slskd webhook in
Chunk 4) routes a folder through before it can reach the importer. It
``realpath``s the candidate AND the inbox root, then admits the path only when it
resolves to the inbox root itself or has the inbox root among its parents. Because
``Path.resolve()`` follows symlinks, a symlink planted under the inbox that points
outside is rejected too — the resolved target is no longer under the inbox.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.beets.library import LibraryHandle
from app.config import Settings

# A leaf folder slskd fires a separate completion for: "CD1", "Disc 2",
# "disk_3", "CD-04". A multi-disc album fragments into one event per disc, so we
# walk up to the shared album parent and enqueue it once.
_DISC_DIR_RE = re.compile(r"(?i)^(cd|disc|disk)[\s_-]*\d+$")


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
