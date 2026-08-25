# backend/tests/test_reorganize_jobs.py
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from beets.library import Library

from app.models.reorganize import ReorganizeOutcome
from app.reorganize_jobs.registry import (
    ReorganizeRegistry,
    reorganize_backfill_active,
    reset_reorganize_backfill,
)
from app.reorganize_jobs.runner import start_backfill, sweep
from tests.conftest import make_test_handle


def test_start_backfill_frees_the_slot_if_the_worker_thread_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Integration guard for M2: start_backfill routes through reg.spawn_worker, so
    # a refused Thread.start() fails the job (releasing the slot) instead of
    # leaving it stuck at "running" — which would wedge every library mutation.
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")

    class _BoomThread:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr("app.jobs.base.threading.Thread", _BoomThread)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        # handle is only captured by the (never-run) worker lambda, so None is safe.
        start_backfill(reg, None, scope="library")  # type: ignore[arg-type]  # handle unused pre-spawn

    assert reg.is_running() is False  # slot released
    assert reg.state().phase == "failed"


def test_start_then_running_then_finish() -> None:
    reg = ReorganizeRegistry()
    assert reg.state().phase == "idle"
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    assert reg.is_running()
    reg.set_total(3)
    reg.record(ReorganizeOutcome(status="moved", label="a"))
    reg.record(ReorganizeOutcome(status="skipped", label="b"))
    reg.record(ReorganizeOutcome(status="failed", label="c", error="x"))
    reg.finish("done")
    s = reg.state()
    assert (s.phase, s.total, s.processed, s.moved, s.skipped, s.failed) == ("done", 3, 3, 1, 1, 1)


def test_records_failure_labels_and_reasons() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record(ReorganizeOutcome(status="moved", label="ok"))
    reg.record(ReorganizeOutcome(status="failed", label="A — B", error="file not found on disk"))
    reg.record(ReorganizeOutcome(status="failed", label="C — D", error="permission denied"))
    reg.finish("failed")
    s = reg.state()
    assert s.failed == 2
    assert [(f.label, f.error) for f in s.failures] == [
        ("A — B", "file not found on disk"),
        ("C — D", "permission denied"),
    ]


def test_failure_reason_defaults_to_empty_string() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record(ReorganizeOutcome(status="failed", label="A — B"))  # error is None
    assert reg.state().failures[0].error == ""


def test_failure_rows_are_capped_at_ten() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    for i in range(11):
        reg.record(ReorganizeOutcome(status="failed", label=f"L{i}", error="boom"))
    s = reg.state()
    assert s.failed == 11  # count is exact
    assert len(s.failures) == 10  # rows are capped
    assert s.failures[0].label == "L0"
    assert s.failures[-1].label == "L9"


def test_state_failures_is_a_copy() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record(ReorganizeOutcome(status="failed", label="A — B", error="boom"))
    snapshot = reg.state().failures
    reg.record(ReorganizeOutcome(status="failed", label="C — D", error="boom2"))
    assert len(snapshot) == 1  # the earlier snapshot must not see later mutations


def test_double_start_raises() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    with pytest.raises(RuntimeError):
        reg.start(scope="library", artist=None, album_id=None, scope_label="library")


def test_scope_fields_surface() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="artist", artist="Radiohead", album_id=None, scope_label="Radiohead")
    s = reg.state()
    assert s.scope == "artist"
    assert s.artist == "Radiohead"
    assert s.album_id is None
    assert s.scope_label == "Radiohead"


def test_stop_is_cooperative() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    assert reg.should_stop() is False
    reg.request_stop()
    assert reg.should_stop() is True


def test_dismiss_clears_a_terminal_job_back_to_idle() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record(ReorganizeOutcome(status="failed", label="A — B", error="boom"))
    reg.finish("done")
    reg.dismiss()
    s = reg.state()
    assert s.phase == "idle"
    assert s.job_id is None
    assert s.failed == 0
    assert s.failures == []


def test_dismiss_refuses_a_running_job_and_leaves_it_intact() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record(ReorganizeOutcome(status="failed", label="A — B", error="boom"))
    with pytest.raises(RuntimeError):
        reg.dismiss()
    s = reg.state()
    assert s.phase == "running"
    assert s.failed == 1
    assert [f.label for f in s.failures] == ["A — B"]


def test_dismiss_on_an_empty_slot_is_a_no_op() -> None:
    reg = ReorganizeRegistry()
    reg.dismiss()
    assert reg.state().phase == "idle"


def test_dismiss_is_idempotent() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.finish("done")
    reg.dismiss()
    reg.dismiss()  # second press must not raise
    assert reg.state().phase == "idle"


def _assert_utc_now(ts: datetime | None, *, not_before: datetime) -> None:
    assert ts is not None
    assert ts.utcoffset() == timedelta(0)  # aware AND UTC
    assert not_before <= ts <= datetime.now(UTC)


def test_finished_at_is_stamped_when_a_job_completes() -> None:
    reg = ReorganizeRegistry()
    before = datetime.now(UTC)
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    assert reg.state().finished_at is None  # a running job has not finished
    reg.finish("done")
    _assert_utc_now(reg.state().finished_at, not_before=before)


def test_finished_at_is_stamped_when_a_job_is_stopped() -> None:
    reg = ReorganizeRegistry()
    before = datetime.now(UTC)
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.request_stop()
    reg.finish("stopped")
    _assert_utc_now(reg.state().finished_at, not_before=before)


def test_finished_at_is_stamped_when_a_job_fails() -> None:
    reg = ReorganizeRegistry()
    before = datetime.now(UTC)
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.fail("kaboom")
    _assert_utc_now(reg.state().finished_at, not_before=before)


def test_idle_invents_no_finished_at() -> None:
    assert ReorganizeRegistry().state().finished_at is None


def test_a_new_job_clears_the_previous_finished_at_until_it_ends() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.finish("done")
    first = reg.state().finished_at
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    assert reg.state().finished_at is None  # the new run has not finished yet
    reg.finish("done")
    second = reg.state().finished_at
    assert first is not None
    assert second is not None
    assert second >= first


def test_module_global_active_and_reset() -> None:
    reg = reset_reorganize_backfill()
    assert reorganize_backfill_active() is False
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    assert reorganize_backfill_active() is True
    reset_reorganize_backfill()
    assert reorganize_backfill_active() is False


def test_sweep_library_moves_three_skips_one(reorganize_lib: Library, tmp_path: Path) -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    handle = make_test_handle(reorganize_lib, tmp_path)
    sweep(reg, handle, scope="library", artist=None, album_id=None, delay=0.0)
    s = reg.state()
    assert s.phase == "done"
    assert s.total == 4
    assert s.processed == 4
    assert s.moved == 3
    assert s.skipped == 1
    assert s.failed == 0


def test_sweep_honors_stop(reorganize_lib: Library, tmp_path: Path) -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.request_stop()
    handle = make_test_handle(reorganize_lib, tmp_path)
    sweep(reg, handle, scope="library", artist=None, album_id=None, delay=0.0)
    s = reg.state()
    assert s.phase == "stopped"
    assert s.processed == 0


def test_sweep_failure_marks_failed(
    reorganize_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    from app.reorganize_jobs import runner

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner, "collect_units", boom)
    handle = make_test_handle(reorganize_lib, tmp_path)
    sweep(reg, handle, scope="library", artist=None, album_id=None, delay=0.0)
    s = reg.state()
    assert s.phase == "failed"
    assert "kaboom" in (s.error or "")
