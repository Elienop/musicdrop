"""Atomic, identity-keyed ledger of handled inbox folders.

Records each folder the acquisition queue has drained, keyed on
``(path, st_mtime, st_size)`` so a webhook retry or a restart never re-imports
the same drop, yet a genuine *re-download* (same name, different mtime/size)
reads as new. Persisted through the shared ``write_atomic_text`` recipe (default
``0o644``) — the atomic write is NOT re-implemented here. A corrupt/unreadable
file degrades to an empty ledger rather than crashing the seam.

Mutating methods serialise their read-modify-write under a ``threading.Lock`` so
the drain thread and any reader never tear a write.
"""

from __future__ import annotations

import threading
from pathlib import Path

from pydantic import BaseModel

from app.models.acquisition import LedgerEntry, LedgerOutcome
from app.playlists.atomic import write_atomic_text


class _LedgerFile(BaseModel):
    """On-disk wrapper so the JSON has a stable top-level object."""

    entries: list[LedgerEntry] = []


class AcquisitionLedger:
    """Persistent record of handled inbox folders (thread-safe)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries = self._load()

    def _load(self) -> list[LedgerEntry]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            return _LedgerFile.model_validate_json(text).entries
        except ValueError:
            # Corrupt JSON or a schema mismatch (pydantic ValidationError is a
            # ValueError): start clean rather than crash the seam.
            return []

    @staticmethod
    def _identity(folder: Path) -> tuple[float, int] | None:
        """One ``stat()`` -> ``(st_mtime, st_size)``; ``None`` if the folder is gone.

        A single stat (never ``exists()`` + ``stat()``) so there is no TOCTOU
        window between the existence check and the read.
        """
        try:
            st = folder.stat()
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def seen(self, folder: Path) -> bool:
        """True iff a persisted entry matches this folder's path AND identity."""
        identity = self._identity(folder)
        if identity is None:
            return False
        mtime, size = identity
        key = str(folder)
        with self._lock:
            return any(e.path == key and e.mtime == mtime and e.size == size for e in self._entries)

    def mark(self, folder: Path, *, outcome: LedgerOutcome) -> None:
        """Record ``folder``'s current identity + outcome, replacing any prior entry.

        Raises ``OSError`` when the file cannot be written, and ``ValueError``
        when the row cannot be stored (a folder name that is not valid UTF-8
        fails the JSON encode). Either way nothing changes, in memory too: a
        row kept in memory but never written would fail every later ``mark``.
        """
        identity = self._identity(folder)
        mtime, size = identity if identity is not None else (0.0, 0)
        entry = LedgerEntry(path=str(folder), mtime=mtime, size=size, outcome=outcome)
        with self._lock:
            entries = [e for e in self._entries if e.path != entry.path]
            entries.append(entry)
            self._persist(entries)
            self._entries = entries

    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)

    def _persist(self, entries: list[LedgerEntry]) -> None:
        # Caller holds the lock. Reuse the shared atomic-write recipe (0o644).
        write_atomic_text(self._path, _LedgerFile(entries=entries).model_dump_json(indent=2))
