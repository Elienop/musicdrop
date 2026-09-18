"""Bank apply runner — hermetic suite (FakeImportRunner registry, tmp bank).

Follows the acquisition-queue suite's posture: tiny poll intervals, ALWAYS
``runner.stop()`` in a ``finally`` so no daemon thread leaks into teardown.
``directive_for`` is pure and tested directly with constructed models.

The duplicate-ENFORCEMENT tests are the exception to "hermetic": deciding
whether a banked collision still exists is a real library read, so those build
a genuine beets ``Library`` (conftest's ``build_library`` + ``make_test_handle``,
the precedent test_bank_duplicate_detect sets). Everything else keeps the
raising default getter, which is itself an assertion — see ``_no_library``.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest
from beets.library import Item

from app.bank import store
from app.bank.apply_runner import BankApplyRunner, directive_for
from app.bank.fingerprint import folder_fingerprint
from app.beets.library import LibraryHandle, _require_id
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.gates import import_gate_clear
from app.import_jobs.registry import ImportJobRegistry
from app.models.bank import BankApplyDirective, BankDecision, BankItem, BankReason
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    CandidateOption,
    DuplicateAction,
    DuplicatePrompt,
    ExistingAlbum,
    ImportOptions,
    ImportOrigin,
    IncomingAlbum,
    ParkedAlbum,
    Recommendation,
)
from tests.conftest import beets_dir_for, build_library, make_test_handle

T = TypeVar("T")


def _poll(
    get: Callable[[], T], pred: Callable[[T], bool], *, timeout: float = 2.0, interval: float = 0.01
) -> T:
    deadline = time.monotonic() + timeout
    value = get()
    while time.monotonic() < deadline:
        value = get()
        if pred(value):
            return value
        time.sleep(interval)
    return value


def _folder(tmp_path: Path, name: str = "Album") -> Path:
    folder = tmp_path / "swept" / name
    folder.mkdir(parents=True)
    (folder / "01 Track.mp3").write_bytes(b"x" * 64)
    return folder


def _bank(tmp_path: Path) -> Path:
    return tmp_path / "bank"


def _seed_queued(bank_dir: Path, folder: Path, decision: BankDecision | None = None) -> str:
    item = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    )
    store.decide_item(bank_dir, item.id, decision or BankDecision(action="asis"))
    return item.id


def _existing(
    album_id: int = 1, *, album_artist: str | None = "A", album: str | None = "B"
) -> ExistingAlbum:
    return ExistingAlbum(
        album_id=album_id,
        album_artist=album_artist,
        album=album,
        year=None,
        track_count=1,
        format=None,
        bitrate_kbps=None,
        folder="/lib/x",
    )


def _dup_prompt(existing: list[ExistingAlbum] | None = None) -> DuplicatePrompt:
    return DuplicatePrompt(
        album_index=0,
        incoming=IncomingAlbum(
            album_artist="A",
            album="B",
            year=None,
            track_count=1,
            format=None,
            bitrate_kbps=None,
            folder="/x",
            has_current_art=False,
        ),
        existing=[_existing()] if existing is None else existing,
    )


def _library(tmp_path: Path, albums: list[tuple[str, str]]) -> tuple[LibraryHandle, list[int]]:
    """A real beets library holding ``albums`` as (albumartist, album), in order.

    Returns the handle plus the ids beets assigned, so a test can pin the
    id-REUSE case (an id that exists but names something else) instead of
    assuming which rowid an album got.
    """
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    ids: list[int] = []
    for i, (artist, album) in enumerate(albums):
        beets_album = lib.add_album([Item(albumartist=artist, album=album, title=f"t{i}", track=1)])
        beets_album.store()
        ids.append(_require_id(beets_album.id))
    return make_test_handle(lib, beets_dir_for(tmp_path)), ids


def _outcome(status: AlbumOutcomeStatus, album_id: int | None = None) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=0,
        folder="/x",
        artist="A",
        album="B",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=status,
        album_id=album_id,
    )


def _no_library() -> LibraryHandle:
    """The default getter: raises if the runner reads the library at all.

    Only two shapes need a library read — an enforced ``skip_new`` and a
    ``replace`` whose banked prompt listed existing albums — so on every other
    row this is a real assertion rather than a stub: if a change starts reading
    it on the apply/asis/astracks/keep_both/merge paths, that row FAILS (the
    raise lands in _drain's catch-all) instead of passing for a new reason.
    """
    raise RuntimeError("this row must resolve without reading the library")


def _make_runner(
    bank_dir: Path,
    reg: ImportJobRegistry,
    library: Callable[[], LibraryHandle] = _no_library,
) -> BankApplyRunner:
    return BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        library=library,
        poll_interval=0.01,
        busy_backoff=0.02,
        idle_poll=0.05,
    )


def _seed_dup_row(
    bank_dir: Path,
    folder: Path,
    duplicate_action: DuplicateAction,
    *,
    prompt: DuplicatePrompt | None = None,
) -> str:
    """A sweep-banked ``needs_dup_resolution`` row decided ``duplicate_action``."""
    item = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint=folder_fingerprint(folder),
        duplicate=_dup_prompt() if prompt is None else prompt,
    )
    store.decide_item(
        bank_dir, item.id, BankDecision(action="duplicate", duplicate_action=duplicate_action)
    )
    return item.id


# ----- directive_for (pure) -----


def _candidate_with_options(release_ids: list[str | None]) -> Candidate:
    change = AlbumChange(artist="A", album="B", year=None, label=None, country=None, media=None)
    return Candidate(
        confidence=80.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=change,
        album_after=change,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[
            CandidateOption(
                index=i,
                confidence=80.0,
                data_source="MusicBrainz",
                disambiguation=None,
                release_id=rid,
            )
            for i, rid in enumerate(release_ids)
        ],
    )


def _queued_item(
    decision: BankDecision,
    *,
    parked: ParkedAlbum | None = None,
    duplicate: DuplicatePrompt | None = None,
    reason: BankReason | None = None,
) -> BankItem:
    # A sweep-banked duplicate row carries BOTH payloads (the prompt to decide
    # on, the candidate to pin), so the reason cannot be inferred from parked
    # alone — pass it explicitly for that shape.
    if reason is None:
        reason = (
            "needs_review"
            if parked is not None
            else ("needs_dup_resolution" if duplicate is not None else "no_match")
        )
    now = datetime.now(UTC)
    return BankItem(
        id="a" * 32,
        folder="/x/A",
        source="sweep",
        reason=reason,
        parked=parked,
        duplicate=duplicate,
        fingerprint="f" * 64,
        status="queued",
        decided=decision,
        banked_at=now,
        decided_at=now,
    )


def test_directive_for_apply_picks_decided_options_release_id() -> None:
    parked = ParkedAlbum(
        album_index=0, folder="/x/A", candidate=_candidate_with_options(["rel-0", "rel-1"])
    )
    item = _queued_item(BankDecision(action="apply", candidate_index=1), parked=parked)
    assert directive_for(item) == BankApplyDirective(action="apply", search_id="rel-1")
    # None index = the top candidate; out-of-range falls back to the top too
    # (mirrors _apply_choice's defensive fallback).
    item_top = _queued_item(BankDecision(action="apply"), parked=parked)
    assert directive_for(item_top).search_id == "rel-0"
    item_oob = _queued_item(BankDecision(action="apply", candidate_index=9), parked=parked)
    assert directive_for(item_oob).search_id == "rel-0"


def test_directive_for_apply_without_stored_id_is_unpinned() -> None:
    parked = ParkedAlbum(album_index=0, folder="/x/A", candidate=_candidate_with_options([None]))
    item = _queued_item(BankDecision(action="apply"), parked=parked)
    assert directive_for(item) == BankApplyDirective(action="apply", search_id=None)


def test_directive_for_duplicate_and_passthroughs() -> None:
    # Today's sweep-banked dup row: both payloads, so the WHOLE directive is
    # pinned here — action, the banked release id, and the resolution.
    dup = _queued_item(
        BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new),
        parked=ParkedAlbum(
            album_index=0, folder="/x/A", candidate=_candidate_with_options(["rel-0", "rel-1"])
        ),
        duplicate=_dup_prompt(),
        reason="needs_dup_resolution",
    )
    # replace_existing is EMPTY here: the whole-directive equality would pass
    # either way if the field defaulted to the prompt's list, so the skip_new
    # arm is where "only replace carries the collision" is actually pinned.
    assert directive_for(dup) == BankApplyDirective(
        action="duplicate",
        search_id="rel-0",
        duplicate_action=DuplicateAction.skip_new,
        replace_existing=[],
    )
    assert directive_for(_queued_item(BankDecision(action="asis"))).action == "asis"
    assert directive_for(_queued_item(BankDecision(action="astracks"))).action == "astracks"


def test_directive_for_replace_carries_the_banked_existing_albums() -> None:
    # THE replace enforcement seam: beets' own re-detection keys on the chosen
    # release's albumartist+album and can miss entirely, so the directive
    # carries the collision the BANKED prompt recorded — by identity, not by a
    # bare id (ids are reused rowids; the session re-checks at trash time).
    stored = [_existing(7), _existing(9, album_artist="C", album="D")]
    item = _queued_item(
        BankDecision(action="duplicate", duplicate_action=DuplicateAction.replace),
        duplicate=_dup_prompt(stored),
    )
    directive = directive_for(item)
    assert directive.replace_existing == stored


def test_directive_for_replace_without_a_prompt_carries_nothing() -> None:
    # Invariant 4: the up-front-resolver flow decides a collision that was
    # never banked (no prompt on the row), so there is nothing stored to
    # enforce — empty, never "trash whatever beets finds".
    item = _queued_item(
        BankDecision(action="duplicate", duplicate_action=DuplicateAction.replace),
        parked=ParkedAlbum(
            album_index=0, folder="/x/A", candidate=_candidate_with_options(["rel-0"])
        ),
    )
    assert directive_for(item).replace_existing == []


def test_duplicate_directive_pins_selected_release() -> None:
    # "decide once": a duplicate decision on a parked candidate row pins the
    # selected option's release_id so the apply imports the chosen release AND
    # resolves the collision in one pass.
    parked = ParkedAlbum(
        album_index=0, folder="/x/A", candidate=_candidate_with_options(["rel-0", "rel-1"])
    )
    item = _queued_item(
        BankDecision(
            action="duplicate", duplicate_action=DuplicateAction.replace, candidate_index=1
        ),
        parked=parked,
    )
    directive = directive_for(item)
    assert directive.action == "duplicate"
    assert directive.search_id == "rel-1"
    assert directive.duplicate_action == DuplicateAction.replace


def test_duplicate_directive_unpinned_when_no_parked() -> None:
    # A LEGACY dup row — banked before the sweep stored its matched release, or
    # banked from a task that had no match to store — has no id to pin and
    # stays honestly unpinned: its apply re-runs the lookup. Such rows drain by
    # user decision; nothing back-fills them.
    item = _queued_item(
        BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new),
        duplicate=_dup_prompt(),
    )
    directive = directive_for(item)
    assert directive.action == "duplicate"
    assert directive.search_id is None
    assert directive.duplicate_action == DuplicateAction.skip_new
    assert directive.replace_existing == []  # not a replace: nothing to trash


# ----- the runner -----


def test_drains_queued_row_to_done_with_album_id(tmp_path: Path) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=77)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    calls: list[tuple[str | list[str], ImportOptions | None, ImportOrigin]] = []
    real_start = reg.start

    def spy_start(
        source: str | list[str],
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        calls.append((source, options, origin))
        return real_start(source, options=options, origin=origin, directive=directive)

    reg.start = spy_start  # type: ignore[method-assign]  # test spy delegates to the real start

    runner = _make_runner(bank, reg)
    runner.start()  # the startup kick IS the first loop pass: no poke needed
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert item is not None
        assert item.album_id == 77
        assert item.error is None
        assert item.resolved_at is not None
        assert fake.received_directive is not None
        assert fake.received_directive.action == "asis"
        # The apply IS an import: through the slot, origin bank_apply, default
        # operation (the worker's in-library guard force-corrects to move when
        # the folder lives under the library root).
        assert calls == [
            (
                str(_bank(tmp_path).parent / "swept" / "Album"),
                ImportOptions(operation="default"),
                "bank_apply",
            )
        ]
    finally:
        runner.stop()


def test_fifo_order_is_decided_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=1)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder_a = _folder(tmp_path, "A")
    folder_b = _folder(tmp_path, "B")
    item_a = store.create_item(
        bank,
        folder=str(folder_a),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder_a),
    )
    item_b = store.create_item(
        bank,
        folder=str(folder_b),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder_b),
    )
    times = iter(
        [
            datetime(2026, 6, 12, 12, 0, 1, tzinfo=UTC),  # A decided LATER
            datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC),  # B decided EARLIER
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(times))
    store.decide_item(bank, item_a.id, BankDecision(action="asis"))
    store.decide_item(bank, item_b.id, BankDecision(action="asis"))
    monkeypatch.undo()  # the runner's own set_status timestamps need the real clock

    order: list[str | list[str]] = []
    real_start = reg.start

    def spy_start(
        source: str | list[str],
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        order.append(source)
        return real_start(source, options=options, origin=origin, directive=directive)

    reg.start = spy_start  # type: ignore[method-assign]

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        _poll(lambda: len(order), lambda n: n >= 2)
        assert order == [str(folder_b), str(folder_a)]  # decided_at order, not banked
    finally:
        runner.stop()


def test_stale_fingerprint_applies_nothing(tmp_path: Path) -> None:
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item_id = _seed_queued(bank, folder)
    (folder / "02 New.mp3").write_bytes(b"y" * 32)  # the folder changed after banking

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "stale",
        )
        assert item is not None
        assert item.error is not None
        assert "changed" in item.error
        assert fake.validate_calls == []  # the import was never started
    finally:
        runner.stop()


def test_missing_folder_goes_stale(tmp_path: Path) -> None:
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    ghost = tmp_path / "swept" / "Ghost"
    item = store.create_item(
        bank, folder=str(ghost), source="sweep", reason="no_match", fingerprint="f" * 64
    )
    store.decide_item(bank, item.id, BankDecision(action="asis"))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status == "stale",
        )
        assert got is not None
        assert got.error is not None
        assert "no longer exists" in got.error
        assert fake.validate_calls == []
    finally:
        runner.stop()


def test_unanticipated_duplicate_fails_with_guidance(tmp_path: Path) -> None:
    # The run's feed shows needs_dup_resolution (the session SKIPped rather
    # than auto-resolving): the row fails with re-decide guidance, and a
    # duplicate decision IS re-postable on a failed row (store._DECIDABLE).
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.needs_dup_resolution)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None
        assert item.error is not None
        assert "duplicate" in item.error
    finally:
        runner.stop()
    requeued = store.decide_item(
        bank, item_id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new)
    )
    assert requeued is not None
    assert requeued.status == "queued"


def test_skip_new_short_circuits_the_import_when_the_copy_survives(tmp_path: Path) -> None:
    # THE enforcement: "keep my copy, import nothing" must not depend on beets
    # re-finding the collision. The banked prompt named library album 1, that
    # album is still there and still itself, so NO import is started at all —
    # previously the run went ahead and a re-detection miss imported the album
    # the user had just refused, reporting done.
    handle, ids = _library(tmp_path, [("A", "B")])
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=99)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank, _folder(tmp_path), DuplicateAction.skip_new, prompt=_dup_prompt([_existing(ids[0])])
    )

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        # Poll to a TERMINAL status first: asserting "the import never ran"
        # against a row still in flight would pass for the wrong reason.
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert got.album_id is None  # nothing landed, by the user's choice
        assert got.error is None  # invariant 8: done rows carry no message
        assert got.resolved_at is not None
        assert fake.validate_calls == []  # the import was never started
        assert fake.received_directive is None
    finally:
        runner.stop()


def test_skip_new_imports_when_the_stored_id_names_a_different_album(tmp_path: Path) -> None:
    # The id-REUSE case, and why presence alone is not enough: beets album ids
    # are SQLite rowids and get reused after a delete, so id 1 exists but is
    # now somebody else's album. The collision the user decided about is gone,
    # so enforcing "skip" against a stranger would be wrong — the import runs.
    handle, ids = _library(tmp_path, [("Someone", "Else")])
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.needs_dup_resolution)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank,
        _folder(tmp_path),
        DuplicateAction.skip_new,
        # Same id, different album: only the identity check separates these.
        prompt=_dup_prompt([_existing(ids[0])]),
    )

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert len(fake.validate_calls) == 1  # the import DID run
        assert fake.received_directive is not None
        assert fake.received_directive.duplicate_action is DuplicateAction.skip_new
    finally:
        runner.stop()


def test_skip_new_without_a_banked_prompt_keeps_the_hook_path(tmp_path: Path) -> None:
    # Invariant 4: the up-front-resolver flow posts a duplicate decision on a
    # needs_review row whose collision was never banked. There is no stored id
    # to verify, so today's trust-the-hook path stands — and the raising
    # default getter proves the library is not read at all on that path.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.needs_dup_resolution)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item = store.create_item(
        bank,
        folder=str(folder),
        source="sweep",
        reason="needs_review",
        fingerprint=folder_fingerprint(folder),
        parked=ParkedAlbum(
            album_index=0, folder=str(folder), candidate=_candidate_with_options(["rel-0"])
        ),
    )
    store.decide_item(
        bank, item.id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new)
    )

    runner = _make_runner(bank, reg)  # _no_library: any read raises -> failed
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert len(fake.validate_calls) == 1
    finally:
        runner.stop()


def test_skip_new_with_an_empty_existing_list_keeps_the_hook_path(tmp_path: Path) -> None:
    # An empty stored list means "none survive", NEVER "collision confirmed" —
    # reading it the other way would refuse every import on a prompt that
    # listed nothing. Same raising getter: no stored id, no library read.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.needs_dup_resolution)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank, _folder(tmp_path), DuplicateAction.skip_new, prompt=_dup_prompt([])
    )

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert len(fake.validate_calls) == 1
    finally:
        runner.stop()


def test_skip_new_on_a_deleted_folder_goes_stale_not_done(tmp_path: Path) -> None:
    # WHERE the enforcement sits is behavior, not tidiness. The banked copy
    # survives, so the short-circuit would resolve this row "done" — but the
    # folder it was banked from is gone, and "done" on a vanished folder claims
    # a decision was carried out about something that no longer exists. Hoisting
    # the enforcement block above the staleness checks passes every other gate
    # and silently swallows this case; this test is what fails.
    handle, ids = _library(tmp_path, [("A", "B")])
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item_id = _seed_dup_row(
        bank, folder, DuplicateAction.skip_new, prompt=_dup_prompt([_existing(ids[0])])
    )
    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed", "stale"),
        )
        assert got is not None
        assert got.status == "stale"
        assert got.error is not None
        assert "no longer exists" in got.error
        assert fake.validate_calls == []
    finally:
        runner.stop()


def test_skip_new_on_a_changed_folder_goes_stale_not_done(tmp_path: Path) -> None:
    # The second half of the placement pin: the folder is still there but no
    # longer what was banked, so the row is stale — the user decided about
    # different contents. Same hoist, same silent "done" if the block moves up.
    handle, ids = _library(tmp_path, [("A", "B")])
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item_id = _seed_dup_row(
        bank, folder, DuplicateAction.skip_new, prompt=_dup_prompt([_existing(ids[0])])
    )
    (folder / "02 New.mp3").write_bytes(b"y" * 32)

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed", "stale"),
        )
        assert got is not None
        assert got.status == "stale"
        assert got.error is not None
        assert "changed" in got.error
        assert fake.validate_calls == []
    finally:
        runner.stop()


def test_merge_that_never_merged_fails_honestly(tmp_path: Path) -> None:
    # A merge cannot be enforced after the fact: beets performs it INSIDE the
    # duplicate hook, and here the hook never fired (no needs_dup_resolution on
    # the feed) while an album still landed. That is "imported as a second
    # copy, nothing merged" — reporting it done would claim a merge that never
    # happened, and "decide again" would import a third copy.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=55)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(bank, _folder(tmp_path), DuplicateAction.merge)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "failed"
        assert got.album_id is None
        assert got.error is not None
        assert "second copy" in got.error
        assert "third time" in got.error  # steers away from a blind retry
        assert "Duplicates page" in got.error
        # ...and the banner must not contradict that error with its own
        # "decide again to retry" headline. The ONE row that says no.
        assert got.error_retryable is False
    finally:
        runner.stop()


def test_merge_that_imported_nothing_keeps_the_retryable_error(tmp_path: Path) -> None:
    # The honest-failure arm is scoped to "an album LANDED but was not merged".
    # A merge run that imported nothing at all is the ordinary transient
    # failure, and must keep its own retryable wording — widening the merge arm
    # to every un-merged run would tell the user to go delete a copy that does
    # not exist.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.skipped)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(bank, _folder(tmp_path), DuplicateAction.merge)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "failed"
        assert got.error is not None
        assert "imported nothing" in got.error
        # The retryability control: widening the not-retryable flag past the
        # landed-a-second-copy arm would hide "decide again" from a row whose
        # only recovery IS deciding again.
        assert got.error_retryable is True
    finally:
        runner.stop()


def test_a_not_retryable_row_becomes_retryable_again_after_a_redecide(tmp_path: Path) -> None:
    # error_retryable describes THIS failure, not the row forever. A merge that
    # landed a second copy parks False; the user removes a copy and decides
    # again, and the next transition must clear it — a row stuck False would
    # permanently hide the banner's retry guidance from every later failure.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=55)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item_id = _seed_dup_row(bank, folder, DuplicateAction.merge)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        failed = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert failed is not None
        assert failed.error_retryable is False
    finally:
        runner.stop()

    # Re-decide as a plain apply — the row's own copy is now the one to keep.
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    runner = _make_runner(bank, reg)
    runner.start()
    try:
        done = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert done is not None
        assert done.status == "done"
        assert done.error_retryable is True
    finally:
        runner.stop()


def test_merge_is_done_when_the_resolution_hook_ran(tmp_path: Path) -> None:
    # The control arm, and it must carry BOTH halves of a real merge: the
    # hook's evidence on the feed AND a landed album id (beets' merge rebuilds
    # the combined album and imports it, so it lands one). With only the dup
    # outcome the honest-failure arm never even reaches its
    # ``not dup_resolution_ran`` clause — the id check short-circuits first, and
    # deleting that clause would go unnoticed.
    fake = FakeImportRunner(
        applied=[
            _outcome(AlbumOutcomeStatus.needs_dup_resolution),
            _outcome(AlbumOutcomeStatus.applied, album_id=55),
        ]
    )
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(bank, _folder(tmp_path), DuplicateAction.merge)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert got.error is None
    finally:
        runner.stop()


def test_replace_that_replaced_nothing_fails_honestly(tmp_path: Path) -> None:
    # The mirror of the merge arm, for the one replace shape that trashes
    # nothing: every banked copy has gone (here the stored id was REUSED and now
    # names a stranger), so the session's seed logs "left in place" and moves
    # nothing, while beets' own re-detection missed too (no needs_dup_resolution
    # on the feed) and an album still landed. Reporting done would render
    # "Replaced - the old copy was moved to Trash": a false claim about a
    # destructive step that never happened.
    handle, ids = _library(tmp_path, [("Someone", "Else")])
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=55)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank, _folder(tmp_path), DuplicateAction.replace, prompt=_dup_prompt([_existing(ids[0])])
    )

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "failed"
        assert got.album_id is None  # not a "the apply landed THIS" success
        assert got.error is not None
        assert "not moved to Trash" in got.error
        assert "another copy" in got.error  # steers away from a blind retry
        assert "Duplicates page" in got.error
        # The album really is in the library, so the banner must not offer
        # "decide again to retry" — the second row that says no.
        assert got.error_retryable is False
    finally:
        runner.stop()


def test_a_replace_that_could_not_reach_trash_fails_the_row_with_its_own_sentence(
    tmp_path: Path,
) -> None:
    # The session answered beets SKIP because the old copy was not disposed of,
    # so nothing was imported. Without the note this row reads DONE —
    # ``_classify_duplicate``'s "the resolution ran" arm cannot tell a Replace
    # that happened from one that refused, and the note is the only thing that
    # can. Retryable: the recovery IS deciding again once Trash works.
    note = "Replace failed while moving the old copy to Trash. Nothing was imported."
    handle, ids = _library(tmp_path, [("A", "B")])
    fake = FakeImportRunner(
        applied=[
            _outcome(AlbumOutcomeStatus.needs_dup_resolution),
            _outcome(AlbumOutcomeStatus.needs_dup_resolution).model_copy(update={"note": note}),
        ]
    )
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank, _folder(tmp_path), DuplicateAction.replace, prompt=_dup_prompt([_existing(ids[0])])
    )

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "failed"
        assert got.error == note
        assert got.album_id is None
        assert got.error_retryable is True
    finally:
        runner.stop()


def test_replace_is_done_when_a_banked_copy_still_survives(tmp_path: Path) -> None:
    # THE control for the arm above, identical in every other respect: an album
    # landed, the hook never ran, the prompt listed a stored id — the ONLY
    # difference is that the copy is still there, so the session's seed trashes
    # it and "Replaced" is the truth. Deleting the pre-check conjunct from
    # _classify_duplicate would fail this row instead.
    handle, ids = _library(tmp_path, [("A", "B")])
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=55)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank, _folder(tmp_path), DuplicateAction.replace, prompt=_dup_prompt([_existing(ids[0])])
    )

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert got.album_id == 55
        assert got.error is None
    finally:
        runner.stop()


def test_replace_without_stored_entries_keeps_todays_path(tmp_path: Path) -> None:
    # No stored evidence, no verdict: a prompt that listed no existing album has
    # nothing to be "gone", so the row resolves exactly as it did before —
    # reading an empty list as "the copies vanished" would fail every such
    # replace. The raising getter proves no library read happens on that shape.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=55)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank, _folder(tmp_path), DuplicateAction.replace, prompt=_dup_prompt([])
    )

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert got.album_id == 55
        assert got.error is None
    finally:
        runner.stop()


def test_replace_is_done_when_the_resolution_hook_ran(tmp_path: Path) -> None:
    # The other control: beets DID find the collision and answered it with the
    # banked action, so the replacement is beets' own and the vanished stored id
    # says nothing about it. Scopes the honest-failure arm to the runs where
    # nothing resolved the duplicate at all.
    handle, ids = _library(tmp_path, [("Someone", "Else")])
    fake = FakeImportRunner(
        applied=[
            _outcome(AlbumOutcomeStatus.needs_dup_resolution),
            _outcome(AlbumOutcomeStatus.applied, album_id=55),
        ]
    )
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(
        bank, _folder(tmp_path), DuplicateAction.replace, prompt=_dup_prompt([_existing(ids[0])])
    )

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "done"
        assert got.error is None
    finally:
        runner.stop()


def test_duplicate_decision_pins_banked_release_end_to_end(tmp_path: Path) -> None:
    # The whole seam, store -> directive_for -> registry -> runner: a dup row
    # banked WITH its matched release drives the import run's search_id to that
    # release. The dup screen posts no candidate_index, so this is the
    # index-less resolution (options[0]) the sweep actually stores.
    # A REAL library, because a replace row with stored entries now reads it
    # before the import (``_replace_targets_are_gone``); the copy survives here,
    # so the pre-check answers False and classification is unaffected.
    handle, ids = _library(tmp_path, [("A", "B")])
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.needs_dup_resolution)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item = store.create_item(
        bank,
        folder=str(folder),
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint=folder_fingerprint(folder),
        parked=ParkedAlbum(
            album_index=0,
            folder=str(folder),
            candidate=_candidate_with_options(["mbid-banked", "mbid-other"]),
        ),
        duplicate=_dup_prompt([_existing(ids[0])]),
    )
    store.decide_item(
        bank, item.id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.replace)
    )

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status == "done",
        )
        assert got is not None
        assert fake.received_directive is not None
        assert fake.received_directive.action == "duplicate"
        assert fake.received_directive.duplicate_action is DuplicateAction.replace
        # THE pin: the banked release, not a re-run lookup's top candidate.
        assert fake.received_directive.search_id == "mbid-banked"
        # ...and the collision the prompt recorded rides along, so the session
        # can trash the old copy even when beets' re-detection never fires.
        assert fake.received_directive.replace_existing == [_existing(ids[0])]
    finally:
        runner.stop()


def test_failed_import_is_recorded_retryable(tmp_path: Path) -> None:
    fake = FakeImportRunner(fail_with="boom")
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None
        assert item.error == "boom"
    finally:
        runner.stop()
    # Retryable per the as-built store: a failed row accepts a new decision.
    requeued = store.decide_item(bank, item_id, BankDecision(action="asis"))
    assert requeued is not None
    assert requeued.status == "queued"


def test_a_crashing_row_records_a_retryable_failure(tmp_path: Path) -> None:
    # The drain's per-row catch-all writes only `error=str(exc)` and LEANS on
    # `set_status(error_retryable=True)`'s default — as does the _UNCONFIRMED
    # write. Every other asserting test passes the value explicitly, so nothing
    # observed that default and flipping it to False was invisible. A row that
    # crashed mid-apply is precisely one whose recovery IS deciding again, so
    # the banner must keep its retry headline.
    def exploding_library() -> LibraryHandle:
        raise RuntimeError("the library could not be opened")

    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    # A skip_new row with a banked collision is the shape that reads the
    # library, so the stubbed getter raises INSIDE _apply_one.
    item_id = _seed_dup_row(bank, _folder(tmp_path), DuplicateAction.skip_new)

    runner = _make_runner(bank, reg, exploding_library)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert got is not None
        assert got.error == "the library could not be opened"
        assert got.error_retryable is True
        assert fake.validate_calls == []  # it crashed before starting anything
    finally:
        runner.stop()


def test_apply_without_album_id_fails_honestly(tmp_path: Path) -> None:
    # The job finished but nothing landed (e.g. a pinned id resolved nothing,
    # the session SKIPped): apply/asis rows must FAIL, never claim done.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.skipped)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None
        assert item.error is not None
        assert "no library album" in item.error
    finally:
        runner.stop()


def test_duplicate_decision_without_dup_evidence_fails(tmp_path: Path) -> None:
    # A transient lookup failure: the re-lookup returned zero candidates, the
    # session SKIPped (skipped outcome, no dup prompt ever surfaced), the job
    # still finished phase=done. NOTHING was imported and the duplicate
    # resolution never ran - the row must fail retryably, never claim done.
    #
    # Reached through the NONE-SURVIVE seam on purpose: the library is real but
    # empty, so the banked album 1 is gone and the enforced skip_new declines to
    # short-circuit. That is what lets the run happen at all — with a surviving
    # copy this row would resolve done without importing.
    handle, _ = _library(tmp_path, [])
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.skipped)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_dup_row(bank, _folder(tmp_path), DuplicateAction.skip_new)

    runner = _make_runner(bank, reg, lambda: handle)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert got is not None
        assert got.status == "failed"
        assert got.error is not None
        assert "imported nothing" in got.error
        assert len(fake.validate_calls) == 1  # the import really did run
    finally:
        runner.stop()
    # Retryable: a failed row accepts a fresh duplicate decision.
    requeued = store.decide_item(
        bank, item_id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new)
    )
    assert requeued is not None
    assert requeued.status == "queued"


def test_duplicate_decision_done_when_album_landed_without_prompt(tmp_path: Path) -> None:
    # The library copy vanished between banking and apply: no dup prompt
    # surfaces, the album just imports. The landed album id IS positive
    # evidence - done, carrying the id.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=33)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item = store.create_item(
        bank,
        folder=str(folder),
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint=folder_fingerprint(folder),
        duplicate=_dup_prompt(),
    )
    store.decide_item(
        bank, item.id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.keep_both)
    )

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status == "done",
        )
        assert got is not None
        assert got.status == "done"
        assert got.album_id == 33
    finally:
        runner.stop()


def test_astracks_without_applied_outcome_fails(tmp_path: Path) -> None:
    # The astracks re-lookup hit network trouble: the session emitted a
    # skipped outcome and the job finished done with zero applied outcomes.
    # No singleton ever imported - failed with re-decide guidance, not done.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.skipped)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path), BankDecision(action="astracks"))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None
        assert item.status == "failed"
        assert item.error is not None
        assert "imported nothing" in item.error
    finally:
        runner.stop()


def test_astracks_done_without_album_id(tmp_path: Path) -> None:
    # Singleton imports create no album entity: done with album_id None.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path), BankDecision(action="astracks"))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert item is not None
        assert item.album_id is None
    finally:
        runner.stop()


def test_defers_while_gate_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=5)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: True)
    runner = _make_runner(bank, reg)
    runner.start()
    try:
        time.sleep(0.2)
        assert fake.validate_calls == []  # never started while the gate is shut
        item = store.get_item(bank, item_id)
        assert item is not None
        assert item.status == "queued"  # not even claimed
        monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: False)
        done = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert done is not None
        assert done.album_id == 5
    finally:
        runner.stop()


def test_slot_toctou_requeues_and_retries(tmp_path: Path) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=5)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    real_start = reg.start
    calls = {"n": 0}

    def flaky_start(
        source: str | list[str],
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("an import is already running")
        return real_start(source, options=options, origin=origin, directive=directive)

    reg.start = flaky_start  # type: ignore[method-assign]

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert item is not None
        assert calls["n"] == 2  # reverted to queued, backed off, retried
    finally:
        runner.stop()


def test_claim_race_skips_rebanked_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The history-loss race: between the FIFO pick and the queued->applying
    # claim, the folder is re-banked (upsert resets the row to needs_review,
    # decided=None). The CAS claim must refuse - a blind overwrite would
    # persist applying+decided=None, a row the BankItem validator rejects on
    # every later read (silent permanent loss) - and the drain must move on
    # to the next queued row, never crash.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=9)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder_a = _folder(tmp_path, "A")
    folder_b = _folder(tmp_path, "B")
    raced_id = _seed_queued(bank, folder_a)  # decided first: the FIFO head
    other_id = _seed_queued(bank, folder_b)

    raced = {"done": False}

    def racing_gate(import_registry: ImportJobRegistry, swap_lock: asyncio.Lock | None) -> bool:
        # Fires between next_queued (the pick) and the claim: re-bank row A
        # exactly like a sweep upsert would (changed fingerprint -> reset).
        if not raced["done"]:
            raced["done"] = True
            store.upsert_by_folder(
                bank,
                folder=str(folder_a),
                source="sweep",
                reason="no_match",
                fingerprint="0" * 64,
            )
        return import_gate_clear(import_registry, swap_lock)

    monkeypatch.setattr("app.bank.apply_runner.import_gate_clear", racing_gate)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        done = _poll(
            lambda: store.get_item(bank, other_id),
            lambda i: i is not None and i.status == "done",
        )
        assert done is not None
        assert done.status == "done"  # the drain continued
        raced_row = store.get_item(bank, raced_id)
        assert raced_row is not None  # still readable - the row was never lost
        assert raced_row.status == "needs_review"  # the re-bank reset survived
        assert raced_row.decided is None
    finally:
        runner.stop()


def test_drain_survives_next_queued_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A raise from the FIFO pick itself (next_queued) must not kill the drain
    # daemon. There is no row to attach an error to, so the drain logs and keeps
    # going; a queued row still applies on a later pass. Before the fix the raise
    # escaped _drain and every queued row was stranded until a process restart.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=42)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    real_next_queued = store.next_queued
    raised = {"done": False}

    def flaky_next_queued(bank_dir: Path) -> BankItem | None:
        if not raised["done"]:
            raised["done"] = True
            raise RuntimeError("transient store read failure")
        return real_next_queued(bank_dir)

    monkeypatch.setattr(store, "next_queued", flaky_next_queued)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert raised["done"]  # the faulty pick actually fired
        assert item is not None
        assert item.status == "done"  # the drain survived it
    finally:
        runner.stop()


def test_stop_is_idempotent(tmp_path: Path) -> None:
    runner = _make_runner(_bank(tmp_path), ImportJobRegistry(runner=FakeImportRunner()))
    runner.start()
    runner.stop()
    runner.stop()  # second stop must not raise


def test_drain_moves_past_a_corrupt_queued_row(tmp_path: Path) -> None:
    """Invariant 4 at the runner level: a corrupt FIFO head must not stall
    _drain (today: log-and-retry the same id forever) — the healthy row
    behind it still drains to done."""
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=5)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    dead = _seed_queued(bank, _folder(tmp_path, "Dead"))
    live_id = _seed_queued(bank, _folder(tmp_path, "Live"))
    (bank / f"{dead}.json").write_bytes(b"\x00\xe9\xff")  # corrupt the FIFO head

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, live_id),
            lambda i: i is not None and i.status in ("done", "failed"),
        )
        assert item is not None
        assert item.status == "done"
        assert item.album_id == 5
    finally:
        runner.stop()
