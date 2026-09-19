"""The import-gate union — ONE predicate for "may a new import start now?".

Extracted from ``AcquisitionQueue._gate_clear`` so every background import
producer (the acquisition drain, the bank apply runner) consumes the same
gate without importing the api layer (``api.import_.ensure_import_can_start``
is the HTTP-shaped twin of this check and stays where it is). The four-backfill
union is delegated to ``app.library_busy.library_job_active`` (the one place it
lives); that helper reads the live binding at call time so tests that
monkeypatch the source modules' attributes keep working.

It also answers the library root, which is not a busy-ness question but has the
same consequence for a drain: while the music share is gone an import would
file the album onto the container's own disk. Asked HERE rather than left to
``start`` raising, because both drains commit work before they call it — the
bank runner CAS-claims its row and fingerprints the folder first, so a refusal
at ``start`` cost two row writes and one folder walk per try, and the
acquisition drain has no catch-all at all and its daemon thread simply died.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from app.beets.library import LibraryRootUnavailableError, require_attached_library_root
from app.import_jobs.registry import ImportJobRegistry

logger = logging.getLogger(__name__)

#: Operator-facing records go to ``uvicorn.error``: under the Dockerfile CMD
#: uvicorn leaves app-namespace loggers at WARNING, so an app-namespace INFO
#: never reaches ``docker logs`` (``bank.apply_runner`` documents the same trap).
operator_logger = logging.getLogger("uvicorn.error")

#: Latched so the two drains polling this twice a second say it once. An Event
#: rather than a module flag so there is no ``global``; two threads crossing the
#: transition together can duplicate one record, which is the whole cost.
_root_wait = threading.Event()


def reset_root_wait_latch() -> None:
    """Forget that a wait was logged (test helper; the latch is process-wide)."""
    _root_wait.clear()


def _library_root_clear(library: object) -> bool:
    """True when the music root is there, logging each transition once."""
    try:
        require_attached_library_root(library)
    except LibraryRootUnavailableError as exc:
        if not _root_wait.is_set():
            _root_wait.set()
            logger.warning("import gate: %s Queued imports wait for it.", exc)
        return False
    if _root_wait.is_set():
        _root_wait.clear()
        operator_logger.info("import gate: library folder is back; queued imports resume")
    return True


def import_gate_clear(import_registry: ImportJobRegistry, swap_lock: asyncio.Lock | None) -> bool:
    """True when the import slot, the beets swap lock, every library backfill
    and the music root are all clear. Best-effort (``Lock.locked()``), the
    established single-user TOCTOU posture — callers still handle ``start()``
    raising."""
    if import_registry.has_active_job():
        return False
    if swap_lock is not None and swap_lock.locked():
        return False
    library = import_registry.library
    # None = no library attached (the fake-runner registries the suites build,
    # and the window before lifespan wiring); nothing to ask, nothing to refuse.
    if library is not None and not _library_root_clear(library):
        return False
    from app.library_busy import library_job_active

    # The injected registry is the import slot, checked above; exclude it so the
    # union covers only the four library backfills.
    if library_job_active(exclude=("import",)):
        return False
    return True
