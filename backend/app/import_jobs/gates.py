"""The import-gate union — ONE predicate for "may a new import start now?".

Extracted from ``AcquisitionQueue._gate_clear`` so every background import
producer (the acquisition drain, the bank apply runner) consumes the same
gate without importing the api layer (``api.import_.ensure_import_can_start``
is the HTTP-shaped twin of this check and stays where it is). The four-backfill
union is delegated to ``app.library_busy.library_job_active`` (the one place it
lives); that helper reads the live binding at call time so tests that
monkeypatch the source modules' attributes keep working.

It also answers the library root, which is not a busy-ness question but has the
same consequence for a drain: while the music share is gone an import would file
the album onto the container's own disk. Asked HERE rather than left to ``start``
raising, because the bank runner CAS-claims its row and fingerprints the folder
before it calls ``start``, so a refusal there cost two row writes and one folder
walk per try; the acquisition drain commits nothing first, but has no catch-all
on this path, so a raise killed its daemon thread outright.

Which is why nothing this module asks may escape: :func:`import_gate_clear`
never raises.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from app.beets.library import LibraryRootUnavailableError, require_importable_library_root
from app.import_jobs.registry import ImportJobRegistry

#: Operator-facing records go to ``uvicorn.error``: under the Dockerfile CMD
#: uvicorn leaves app-namespace loggers at WARNING, so an app-namespace INFO is
#: dropped and a WARNING prints as a bare untagged line (both measured).
operator_logger = logging.getLogger("uvicorn.error")

#: Latched so the two drains polling twice a second each say it once. An Event
#: rather than a module flag so there is no ``global``; two threads crossing the
#: transition together can duplicate one record, which is the whole cost.
_root_wait = threading.Event()
#: The same, for an unexpected failure of any question the gate asks.
_gate_fault = threading.Event()


def _library_root_clear(library: object) -> bool:
    """True when the music root is importable, logging each transition once."""
    try:
        require_importable_library_root(library)
    except LibraryRootUnavailableError as exc:
        if not _root_wait.is_set():
            _root_wait.set()
            operator_logger.warning("import gate: %s Queued imports wait for it.", exc)
        return False
    if _root_wait.is_set():
        _root_wait.clear()
        operator_logger.info("import gate: library folder is back; queued imports resume")
    return True


def import_gate_clear(import_registry: ImportJobRegistry, swap_lock: asyncio.Lock | None) -> bool:
    """True when the import slot, the beets swap lock, every library backfill
    and the music root are all clear. Best-effort (``Lock.locked()``), the
    established single-user TOCTOU posture — callers still handle ``start()``
    raising. Never raises (see the module docstring); an unexpected failure
    reads as "not now", logged once per episode with its traceback.
    """
    try:
        clear = _gate_answer(import_registry, swap_lock)
    except Exception:
        if not _gate_fault.is_set():
            _gate_fault.set()
            # ``.exception``, not ``.error``: the message is fixed text, so the
            # traceback is the only thing that names the defect.
            operator_logger.exception("import gate: a check failed; queued imports wait for it")
        return False
    if _gate_fault.is_set():
        _gate_fault.clear()
        operator_logger.info("import gate: the failing check answered again; queued imports resume")
    return clear


def _gate_answer(import_registry: ImportJobRegistry, swap_lock: asyncio.Lock | None) -> bool:
    # The root question runs on EVERY poll, ahead of the in-memory ones, so the
    # wait latch cannot go stale behind an early return — a second outage that
    # began while a backfill held the gate would otherwise never be logged.
    # ``None`` = no library attached (the fake-runner registries the suites
    # build, and the window before lifespan wiring): nothing to ask.
    library = import_registry.library
    if library is not None and not _library_root_clear(library):
        return False
    if import_registry.has_active_job():
        return False
    if swap_lock is not None and swap_lock.locked():
        return False
    from app.library_busy import library_job_active

    # The injected registry is the import slot, checked above; exclude it so the
    # union covers only the four library backfills.
    return not library_job_active(exclude=("import",))
