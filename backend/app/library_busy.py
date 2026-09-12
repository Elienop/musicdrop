"""Single source of truth for the "is a library-mutating job running?" gate.

Five mutually-exclusive job types can hold the library: an import, and the
lyrics / artist-art / reorganize / disk-sync backfills. Many endpoints refuse
(409) while any of them runs; the api-layer sites ALSO refuse while the beets
swap lock is held (a config Apply or duplicate resolve mid-flight). This module
is the ONE place that union lives, so a sixth job type is wired in exactly once.

The job predicates are imported lazily inside the functions so the live binding
is read at call time: tests monkeypatch the source-module attributes, and the
import-job registry is a swappable global (``get_registry()`` reads it fresh).
Kept out of ``app/api`` and ``app/beets`` so the beets adapter and the
acquisition producers can both consume it without crossing either boundary
(``app/import_jobs/gates.py`` must not import the api layer; the beets adapter
must not gain a web dependency at module load).
"""

from __future__ import annotations

import threading
from collections.abc import Container, Iterator
from contextlib import contextmanager

# Names the five single-slot job types accept in ``exclude`` to drop their own
# slot from the union (a job's own start-gate must not see itself as busy).
IMPORT = "import"
LYRICS = "lyrics"
ARTIST_ART = "artist_art"
REORGANIZE = "reorganize"
DISK_SYNC = "disk_sync"

# Public so every registry can name its slot from ONE definition instead of
# repeating a bare literal. A registry whose ``job_type`` does not match a key
# here excludes nothing (it would refuse itself) or — far worse — excludes
# SOMEONE ELSE'S key, silently dropping that pair's mutual exclusion. Bound by
# test_every_registry_job_type_is_a_known_key.
JOB_TYPES: frozenset[str] = frozenset({IMPORT, LYRICS, ARTIST_ART, REORGANIZE, DISK_SYNC})

# Back-compat aliases for the module-private names this file used before the
# keys became part of the registries' contract.
_IMPORT = IMPORT
_LYRICS = LYRICS
_ARTIST_ART = ARTIST_ART
_REORGANIZE = REORGANIZE
_DISK_SYNC = DISK_SYNC


# Serializes "is anyone else running?" + "claim my own slot" across ALL FIVE job
# types. Each registry's own lock only ever guarded its OWN slot, so the union
# check was check-then-act: a daemon producer (bank apply / inbox drain) could
# pass the gate, spend hundreds of ms fingerprinting a folder on a NAS, and then
# claim the import slot while a user-started reorganize had claimed its own in
# that window — a beets import moving files into the library running alongside a
# whole-library reorganize moving those same folders, both writing one SQLite DB.
#
# Lock order is ``_CLAIM_LOCK`` -> a registry's own lock, and NOTHING acquires
# _CLAIM_LOCK while holding a registry lock, so the order can't cycle. Held only
# across the O(1) check+claim — never across a scan, a validate, or a thread
# spawn. A plain Lock (not RLock) on purpose: a nested claim would be a bug, and
# deadlocking loudly beats masking it.
_CLAIM_LOCK = threading.Lock()

# The beets swap lock, registered once at lifespan startup. It is the SIXTH
# mutual-exclusion participant: a config Apply, duplicate resolve, delete, or
# trash restore/empty holds it while mutating beets' process globals and the
# SQLite connection, yet registers no job slot. ``import_gate_clear`` already
# consults it, so a claim that ignored it would be strictly weaker than the gate
# it replaced — a producer could pass its gate, spend seconds fingerprinting a
# folder, and then claim the import slot while an Apply had begun tearing down
# the very Library handle its worker is about to use.
#
# Module-global rather than plumbed through five registries' start() signatures,
# because the registries hold no reference to the FastAPI app.
#
# CAVEAT (deliberate, and the reason this is not a full close): the swap-lock
# HOLDERS do not route their acquisition through _CLAIM_LOCK, so their own
# check-then-act window survives. Consulting the lock here narrows the race to
# the acquire itself — the same best-effort posture every existing gate site
# uses (``Lock.locked()``) — rather than eliminating it. Closing it completely
# means making the swap-lock acquisition itself a claim.
_SWAP_LOCK: object | None = None


def register_swap_lock(lock: object | None) -> None:
    """Register (or clear, with ``None``) the beets swap lock for the claim gate.

    Called once from the app lifespan. Tests that build a registry without an app
    simply never register one, and the claim then checks only the five slots.
    """
    global _SWAP_LOCK
    _SWAP_LOCK = lock


def _swap_in_progress() -> bool:
    """Whether a beets swap (config Apply / duplicate resolve / trash) holds the
    library. Best-effort ``locked()``, never raises if nothing is registered."""
    lock = _SWAP_LOCK
    locked = getattr(lock, "locked", None) if lock is not None else None
    return bool(locked()) if callable(locked) else False


@contextmanager
def claim_slot(job_type: str, *, message: str | None = None) -> Iterator[None]:
    """Atomically refuse-or-let-claim the library for ``job_type``.

    Raises ``RuntimeError`` when another job type holds the library — the same
    exception every caller already handles for the own-slot case (the API maps it
    to 409; the bank-apply and inbox drains defer and retry). The caller claims
    its own slot inside the ``with`` body, so the check and the claim are one
    atomic step rather than two racy ones.
    """
    if job_type not in JOB_TYPES:
        # A typo'd or copy-pasted key would drop the WRONG job from the union —
        # e.g. a reorganize excluding "disk_sync" could claim the library while a
        # disk sync runs. Fail loudly at the claim rather than silently.
        raise ValueError(f"unknown job_type {job_type!r}; expected one of {sorted(JOB_TYPES)}")
    with _CLAIM_LOCK:
        if library_job_active(exclude=(job_type,)) or _swap_in_progress():
            raise RuntimeError(message or "another library operation is already running")
        yield


def library_job_active(*, exclude: Container[str] = ()) -> bool:
    """True while any library-mutating background job is active.

    The union of the five single-slot job types, keyed by name so a caller's own
    start-gate can drop itself via ``exclude``: ``"import"`` (the import slot),
    ``"lyrics"``, ``"artist_art"``, ``"reorganize"``, ``"disk_sync"``.
    Best-effort and read live at call time.
    """
    from app.artist_art_jobs.registry import artist_art_backfill_active
    from app.disk_sync_jobs.registry import disk_sync_active
    from app.import_jobs.registry import get_registry
    from app.lyrics_jobs.registry import lyrics_backfill_active
    from app.reorganize_jobs.registry import reorganize_backfill_active

    return (
        (_IMPORT not in exclude and get_registry().has_active_job())
        or (_LYRICS not in exclude and lyrics_backfill_active())
        or (_ARTIST_ART not in exclude and artist_art_backfill_active())
        or (_REORGANIZE not in exclude and reorganize_backfill_active())
        or (_DISK_SYNC not in exclude and disk_sync_active())
    )


#: The 409 detail every api-layer site refuses with unless it passes its own.
#: One definition so the lock-only gate below says the same thing as the union.
LIBRARY_BUSY_MESSAGE = "A library operation is in progress; try again when it finishes"


def raise_if_swap_lock_held(app: object, *, message: str = LIBRARY_BUSY_MESSAGE) -> None:
    """Raise ``HTTPException(409, message)`` if the beets swap lock is held.

    The LOCK half of :func:`raise_if_library_busy` on its own, for a route that
    must not WAIT on the lock but already has a narrower job gate of its own:
    the union would refuse it for the length of an unrelated import, which only
    ever READS the lock (``app/import_jobs/gates.py``). Best-effort
    ``Lock.locked()`` — the single-user TOCTOU posture every site here uses.
    """
    from fastapi import HTTPException, status

    lock = getattr(getattr(app, "state", None), "beets_swap_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, message)


def raise_if_library_busy(
    app: object,
    *,
    exclude: Container[str] = (),
    message: str = LIBRARY_BUSY_MESSAGE,
) -> None:
    """Raise ``HTTPException(409, message)`` if a library job is active OR the
    beets swap lock is held — the api-layer variant of the gate.

    ``app`` is the FastAPI app (duck-typed ``object`` so tests can pass a stub
    carrying ``state``); ``exclude`` drops the caller's own job from the union;
    ``message`` is the 409 detail.
    """
    from fastapi import HTTPException, status

    if library_job_active(exclude=exclude):
        raise HTTPException(status.HTTP_409_CONFLICT, message)
    raise_if_swap_lock_held(app, message=message)
