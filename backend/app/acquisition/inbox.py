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

from pathlib import Path

from app.beets.library import LibraryHandle
from app.config import Settings


def resolve_inbox_dir(settings: Settings, handle: LibraryHandle) -> Path:
    """Where completed downloads land: configured ``inbox_dir`` or ``<beets_dir>/inbox``.

    Empty setting = default under the handle's already-absolute ``beets_dir``; a
    configured override is resolved to absolute. Synchronous (pathlib I/O must
    not run on the event loop). Mirrors ``resolve_trash_dir``.
    """
    if settings.inbox_dir:
        return Path(settings.inbox_dir).resolve()
    return handle.beets_dir / "inbox"


def contain(path: str, inbox_dir: Path) -> Path | None:
    """Return ``realpath(path)`` only if it is the inbox root or strictly under it.

    Rejects ``../`` escapes, absolute paths outside the inbox, and symlink
    escapes (``resolve()`` follows the link, so the resolved target is checked).
    Returns ``None`` on any of those, or on an OS error while resolving.
    """
    try:
        resolved = Path(path).resolve()
        root = inbox_dir.resolve()
    except OSError:
        return None
    if resolved == root or root in resolved.parents:
        return resolved
    return None
