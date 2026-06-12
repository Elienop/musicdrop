"""The import-gate union — ONE predicate for "may a new import start now?".

Extracted from ``AcquisitionQueue._gate_clear`` so every background import
producer (the acquisition drain, the bank apply runner) consumes the same
gate without importing the api layer (``api.import_.ensure_import_can_start``
is the HTTP-shaped twin of this check and stays where it is). The backfill
predicates are imported lazily so this module can never form an import cycle
with the job packages — and so tests that monkeypatch the source modules'
attributes keep working (the live binding is read at call time).
"""

from __future__ import annotations

import asyncio

from app.import_jobs.registry import ImportJobRegistry


def import_gate_clear(import_registry: ImportJobRegistry, swap_lock: asyncio.Lock | None) -> bool:
    """True when the import slot, the beets swap lock, and every library
    backfill are all free. Best-effort (``Lock.locked()``), the established
    single-user TOCTOU posture — callers still handle ``start()`` raising."""
    if import_registry.has_active_job():
        return False
    if swap_lock is not None and swap_lock.locked():
        return False
    from app.artist_art_jobs.registry import artist_art_backfill_active
    from app.lyrics_jobs.registry import lyrics_backfill_active
    from app.reorganize_jobs.registry import reorganize_backfill_active

    if lyrics_backfill_active() or artist_art_backfill_active() or reorganize_backfill_active():
        return False
    return True
