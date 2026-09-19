"""The import-gate union — ONE predicate for "may a new import start now?".

Shared by the acquisition drain and the bank apply runner without importing the
api layer (``api.import_.ensure_import_can_start`` is the HTTP-shaped twin). The
four-backfill union lives in ``app.library_busy.library_job_active``, read at
call time so monkeypatched tests keep working.

The library root is answered here rather than left to ``start`` raising: the bank
runner CAS-claims its row and fingerprints the folder first (two row writes and
one folder walk per refusal), and the acquisition drain has no catch-all, so a
raise killed its daemon thread. Nothing here escapes — :func:`import_gate_clear`
never raises (``test_an_unexpected_raise_inside_the_gate_reads_as_wait``, two
fault classes).
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

#: Latched so the two drains, polling twice a second each, say it once
#: (``test_the_wait_is_logged_once_and_its_end_is_logged_once``). Two threads
#: crossing the transition together can duplicate one record.
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
    # Asked on every poll, ahead of the in-memory checks, so the wait latch
    # cannot go stale behind an early return
    # (test_the_root_question_runs_even_when_another_check_would_close_the_gate).
    # ``None`` = no library attached (fake-runner registries, and the window
    # before lifespan wiring): nothing to ask.
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
