"""The Folder sources file, ``<beets_dir>/sources.json``.

A Folder source is a name and a folder, nothing else: no operation, no mapping,
no watcher. slskd is not one (``decisions`` #77: slskd's message is the one way
in), and its settings stay in ``app/slskd/config.py``.

The file sits in the beets dir beside ``password-hash`` (``app/auth/source.py``):
the one directory the operator already mounts and backs up, and one the import
refusal already protects. It holds no secret, so it takes the atomic writer's
default mode.

Every call reads the file, so the file is the only state. A change builds the
new list in a local, writes it, and only then is it the answer: a list that
cannot be serialised or written leaves the file, and so the store, as it was.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from app.playlists.atomic import write_atomic_text

logger = logging.getLogger(__name__)

SOURCES_FILENAME: Final = "sources.json"

#: One change at a time, across every store object: each request builds its own,
#: and two changes that both read the file before either wrote it would lose one.
_CHANGE_LOCK: Final = threading.Lock()


class FolderSource(BaseModel):
    """One stored Folder source. ``folder`` is the display form the client sent."""

    id: str
    name: str
    folder: str


class _SourcesFile(BaseModel):
    folders: list[FolderSource] = []


class SourcesStore:
    """Read, add and remove Folder sources in one JSON file. Blocking I/O."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def folders(self) -> list[FolderSource]:
        """Every Folder source, in the order added.

        A missing file is none yet. A file that cannot be read or parsed reads
        as empty too, so it never stops the app; it is logged, because the next
        change replaces it.
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning(
                "Sources: %r cannot be read (%s); it reads as empty and the next change"
                " replaces it",
                str(self._path),
                exc.strerror,
            )
            return []
        try:
            return _SourcesFile.model_validate_json(raw).folders
        except ValueError:
            logger.warning(
                "Sources: %r is not a valid sources file; it reads as empty and the next"
                " change replaces it",
                str(self._path),
            )
            return []

    def add(self, name: str, folder: str) -> FolderSource:
        """Append one Folder source and return it. Raises if it cannot be written."""
        added = FolderSource(id=uuid.uuid4().hex, name=name, folder=folder)
        with _CHANGE_LOCK:
            self._write([*self.folders(), added])
        return added

    def remove(self, source_id: str) -> bool:
        """Remove one Folder source; ``False`` when no source has that id.

        Touches the file only: the folder on disk is not read or changed.
        """
        with _CHANGE_LOCK:
            current = self.folders()
            kept = [source for source in current if source.id != source_id]
            if len(kept) == len(current):
                return False
            self._write(kept)
        return True

    def _write(self, folders: list[FolderSource]) -> None:
        # Serialised BEFORE the write, so a value JSON cannot carry (a lone
        # surrogate raises PydanticSerializationError, a ValueError) fails
        # before the file is touched. The route's model already refuses one.
        write_atomic_text(self._path, _SourcesFile(folders=folders).model_dump_json(indent=2))


def sources_path(beets_dir: Path) -> Path:
    """Where the Folder sources live: ``<beets_dir>/sources.json``."""
    return beets_dir / SOURCES_FILENAME
