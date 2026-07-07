"""The import-gate union — ONE predicate for "may a new import start now?".

Extracted from ``AcquisitionQueue._gate_clear`` so every background import
producer (the acquisition drain, the bank apply runner) consumes the same
gate without importing the api layer (``api.import_.ensure_import_can_start``
is the HTTP-shaped twin of this check and stays where it is). The four-backfill
union is delegated to ``app.library_busy.library_job_active`` (the one place it
lives); that helper reads the live binding at call time so tests that
monkeypatch the source modules' attributes keep working.
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
    from app.library_busy import library_job_active

    # The injected registry is the import slot, checked above; exclude it so the
    # union covers only the four library backfills.
    if library_job_active(exclude=("import",)):
        return False
    return True
