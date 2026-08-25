"""Unit tests for the consolidated library-busy gate (app/library_busy.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.import_jobs.registry import get_registry
from app.library_busy import library_job_active, raise_if_library_busy
from app.models.import_models import ImportOptions

# (source-module attribute to patch, the exclude key that drops that job)
_BACKFILLS = [
    ("app.lyrics_jobs.registry.lyrics_backfill_active", "lyrics"),
    ("app.artist_art_jobs.registry.artist_art_backfill_active", "artist_art"),
    ("app.reorganize_jobs.registry.reorganize_backfill_active", "reorganize"),
    ("app.disk_sync_jobs.registry.disk_sync_active", "disk_sync"),
]


def test_idle_is_not_active() -> None:
    assert library_job_active() is False


@pytest.mark.parametrize(("target", "key"), _BACKFILLS)
def test_each_backfill_makes_it_active(
    monkeypatch: pytest.MonkeyPatch, target: str, key: str
) -> None:
    monkeypatch.setattr(target, lambda: True)
    assert library_job_active() is True
    # ...but excluding that job's own key drops it back to idle.
    assert library_job_active(exclude=(key,)) is False


def test_import_slot_makes_it_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    assert library_job_active() is True
    assert library_job_active(exclude=("import",)) is False


def test_raise_if_busy_passes_when_idle() -> None:
    app = SimpleNamespace(state=SimpleNamespace(beets_swap_lock=None))
    raise_if_library_busy(app)  # no raise


def test_raise_if_busy_raises_on_active_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: True)
    app = SimpleNamespace(state=SimpleNamespace(beets_swap_lock=None))
    with pytest.raises(HTTPException) as exc:
        raise_if_library_busy(app, message="busy now")
    assert exc.value.status_code == 409
    assert exc.value.detail == "busy now"


def test_raise_if_busy_raises_when_swap_lock_held() -> None:
    class _LockedLock:
        def locked(self) -> bool:
            return True

    app = SimpleNamespace(state=SimpleNamespace(beets_swap_lock=_LockedLock()))
    with pytest.raises(HTTPException) as exc:
        raise_if_library_busy(app)
    assert exc.value.status_code == 409


# ----- I2: the union check and the slot claim must be ONE atomic step -----


def test_import_start_refuses_while_a_backfill_holds_the_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate used to live only at the API layer, so a DAEMON producer (bank
    # apply / inbox drain) calling start() directly saw only the import slot.
    # start() itself must now refuse.
    from app.import_jobs.fakes import FakeImportRunner
    from app.import_jobs.registry import ImportJobRegistry

    monkeypatch.setattr("app.reorganize_jobs.registry.reorganize_backfill_active", lambda: True)
    reg = ImportJobRegistry(runner=FakeImportRunner(parked=[]))
    with pytest.raises(RuntimeError, match="another library operation"):
        reg.start("/music/incoming")
    assert reg.has_active_job() is False  # no slot claimed by the refused start


def test_backfill_start_refuses_while_an_import_holds_the_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ...and symmetrically: a user-started sweep must refuse while an import runs.
    from app.reorganize_jobs.registry import ReorganizeRegistry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    reorg = ReorganizeRegistry()
    with pytest.raises(RuntimeError, match="another library operation"):
        reorg.start(scope="library", artist=None, album_id=None, scope_label="library")
    assert reorg.is_running() is False  # no slot claimed by the refused start


def test_a_backfill_does_not_refuse_ITSELF() -> None:
    # The union must drop the caller's own slot, or a registry whose job just
    # finished (or is mid-claim) would refuse to ever start again.
    from app.reorganize_jobs.registry import ReorganizeRegistry

    reorg = ReorganizeRegistry()
    reorg.start(scope="library", artist=None, album_id=None, scope_label="library")
    # its OWN slot is busy -> the own-slot message, not the cross-job one
    with pytest.raises(RuntimeError, match="a reorganize is already running"):
        reorg.start(scope="library", artist=None, album_id=None, scope_label="library")


def test_a_slow_producer_cannot_claim_the_import_slot_after_a_sweep_took_it() -> None:
    """THE RACE: refuse the import that was mid-flight when a sweep claimed.

    The bank-apply / inbox producers pass their gate, then do real work (a folder
    fingerprint walks a NAS — hundreds of ms) before calling start(). With the
    union checked separately from the claim, a reorganize starting in that window
    was invisible and BOTH ran: a beets import moving files into the library while
    a whole-library sweep moved those same folders, two writer threads on one
    SQLite DB. The claim must be atomic, so the late producer is refused.

    Reproduced deterministically by parking the producer inside runner.validate —
    which runs before the claim, exactly where the real fingerprint sits.
    """
    import threading

    from app.import_jobs.fakes import FakeImportRunner
    from app.import_jobs.registry import ImportJobRegistry
    from app.reorganize_jobs.registry import reset_reorganize_backfill

    reorg = reset_reorganize_backfill()
    in_validate = threading.Event()
    release = threading.Event()

    class _SlowRunner(FakeImportRunner):
        def validate(self, paths: list[str], options: ImportOptions | None = None) -> None:
            in_validate.set()
            assert release.wait(timeout=5.0)

    import_reg = ImportJobRegistry(runner=_SlowRunner(parked=[]))
    outcome: list[object] = []

    def producer() -> None:
        try:
            outcome.append(import_reg.start("/inbox/Album"))
        except RuntimeError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    assert in_validate.wait(timeout=5.0)  # the producer is past its gate, pre-claim

    # A user starts a sweep in exactly that window — it must succeed...
    reorg.start(scope="library", artist=None, album_id=None, scope_label="library")
    release.set()
    thread.join(timeout=5.0)

    # ...and the producer, arriving late, must be REFUSED rather than running
    # concurrently with the sweep.
    assert isinstance(outcome[0], RuntimeError)
    assert "another library operation" in str(outcome[0])
    assert import_reg.has_active_job() is False
    reset_reorganize_backfill()


def test_claim_lock_is_released_when_a_claim_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    # A refused claim must not leave the process-wide lock held — that would wedge
    # every future start of every job type.
    from app.library_busy import _CLAIM_LOCK, claim_slot

    monkeypatch.setattr("app.disk_sync_jobs.registry.disk_sync_active", lambda: True)
    slot = claim_slot("import", message="nope")
    with pytest.raises(RuntimeError, match="nope"):
        with slot:
            raise AssertionError("body must not run when the union is busy")
    assert _CLAIM_LOCK.locked() is False


def test_claim_lock_is_released_when_the_body_raises() -> None:
    # ...and an exception from the CLAIM ITSELF (the registry refusing its own
    # slot inside the with-body) must release it too.
    from app.library_busy import _CLAIM_LOCK, claim_slot

    slot = claim_slot("import")
    with pytest.raises(ValueError):
        with slot:
            raise ValueError("own-slot refusal")
    assert _CLAIM_LOCK.locked() is False


def test_claim_slot_SERIALIZES_two_overlapping_claims() -> None:
    """The lock itself, not just the check.

    Every other test here is wall-clock sequential, so they all pass even with
    ``_CLAIM_LOCK`` deleted — they prove "start() consults the union", never
    "the consult and the claim are ONE step". This one overlaps two claims: the
    first parks INSIDE the union check, and the second must be unable to enter
    until the first leaves. Without the lock the second sails straight through,
    which is exactly the interleaving that let two job types run at once.
    """
    import threading

    from app import library_busy as lb

    inside_first = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls = {"n": 0}
    real_active = lb.library_job_active

    def parking_active(*, exclude: object = ()) -> bool:
        # Park only the FIRST claimant, while it holds _CLAIM_LOCK.
        calls["n"] += 1
        if calls["n"] == 1:
            inside_first.set()
            assert release_first.wait(timeout=5.0)
        return False

    lb.library_job_active = parking_active  # test double swaps the module attribute
    try:

        def first() -> None:
            with lb.claim_slot(lb.IMPORT):
                pass

        def second() -> None:
            with lb.claim_slot(lb.LYRICS):
                second_entered.set()

        t1 = threading.Thread(target=first, daemon=True)
        t1.start()
        assert inside_first.wait(timeout=5.0)  # first holds the claim lock

        t2 = threading.Thread(target=second, daemon=True)
        t2.start()
        # THE ASSERTION: the second claim cannot proceed while the first holds
        # the lock. Un-locked, it would enter immediately.
        assert not second_entered.wait(timeout=0.3), (
            "a second claim entered while one was in flight"
        )

        release_first.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        assert second_entered.is_set()  # ...and it proceeds once the lock frees
    finally:
        lb.library_job_active = real_active  # restore
        release_first.set()


@pytest.mark.parametrize(
    ("registry_path", "expected"),
    [
        ("app.reorganize_jobs.registry.ReorganizeRegistry", "reorganize"),
        ("app.disk_sync_jobs.registry.DiskSyncRegistry", "disk_sync"),
        ("app.lyrics_jobs.registry.LyricsBackfillRegistry", "lyrics"),
        ("app.artist_art_jobs.registry.ArtistArtBackfillRegistry", "artist_art"),
    ],
)
def test_every_registry_job_type_is_a_known_key(registry_path: str, expected: str) -> None:
    # A job_type that is not a real union key excludes nothing (the registry
    # refuses itself) or — worse — excludes SOMEONE ELSE'S key, silently dropping
    # that pair's mutual exclusion. Both mutants used to leave the suite green.
    import importlib

    from app.library_busy import JOB_TYPES

    module_name, class_name = registry_path.rsplit(".", 1)
    registry_cls = getattr(importlib.import_module(module_name), class_name)
    assert registry_cls.job_type == expected
    assert registry_cls.job_type in JOB_TYPES


def test_claim_slot_rejects_an_unknown_job_type() -> None:
    from app.library_busy import claim_slot

    slot = claim_slot("reorganise")  # a plausible typo
    with pytest.raises(ValueError, match="unknown job_type"):
        with slot:
            raise AssertionError("must not reach the body")


def test_claim_slot_refuses_while_a_beets_swap_holds_the_library() -> None:
    """The sixth participant: a config Apply / duplicate resolve / trash holds the
    beets swap lock while mutating beets' globals but claims NO job slot. The
    claim must see it, or it would be strictly weaker than the import gate it
    replaced (which already checks the swap lock)."""
    from app.library_busy import claim_slot, register_swap_lock

    class _Held:
        def locked(self) -> bool:
            return True

    register_swap_lock(_Held())
    try:
        slot = claim_slot("import")
        with pytest.raises(RuntimeError, match="another library operation"):
            with slot:
                raise AssertionError("must not claim while a swap is in progress")
    finally:
        register_swap_lock(None)


def test_claim_slot_ignores_an_unregistered_or_free_swap_lock() -> None:
    # No app (the lifespan-less test client) or a free lock must not block claims.
    from app.library_busy import claim_slot, register_swap_lock

    class _Free:
        def locked(self) -> bool:
            return False

    register_swap_lock(None)
    with claim_slot("import"):
        pass
    register_swap_lock(_Free())
    try:
        with claim_slot("import"):
            pass
    finally:
        register_swap_lock(None)
