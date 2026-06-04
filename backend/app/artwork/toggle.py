"""Runtime, persisted on/off toggle for the artist-image feature.

The env var ``MUSICDROP_ARTIST_IMAGES_ENABLED`` seeds the INITIAL default; once
the user flips the toggle in the UI it is persisted to ``<dir>/_enabled.json``
and that persisted value wins on the next start. The value is held in memory
after load, so ``is_enabled()`` is a cheap attribute read (no per-request disk
I/O); a bool flip is GIL-atomic, so no lock is needed for a single-user app.
"""

from __future__ import annotations

import json
from pathlib import Path


class ArtistImageToggle:
    def __init__(self, path: Path | str, *, default: bool) -> None:
        self._path = Path(path)
        self._enabled = self._load(default)

    def _load(self, default: bool) -> bool:
        try:
            raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if isinstance(raw, dict):
            value = raw.get("enabled")
            if isinstance(value, bool):
                return value
        return default

    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> bool:
        """Persist + update the in-memory flag; returns the new value."""
        self._enabled = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # tmp-then-rename: never reuse the YAML-specific atomic_write.
        tmp = self._path.parent / f".{self._path.name}.tmp"
        tmp.write_text(json.dumps({"enabled": value}), encoding="utf-8")
        tmp.replace(self._path)
        return value
