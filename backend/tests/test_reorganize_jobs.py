# backend/tests/test_reorganize_jobs.py
from pathlib import Path

import pytest
from beets.library import Library

from app.models.reorganize import ReorganizeOutcome
from app.reorganize_jobs.registry import (
    ReorganizeRegistry,
    reorganize_backfill_active,
    reset_reorganize_backfill,
)
from app.reorganize_jobs.runner import sweep
from tests.conftest import make_test_handle


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
    assert s.failures[0].label == "L0" and s.failures[-1].label == "L9"


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
    assert s.artist == "Radiohead" and s.album_id is None and s.scope_label == "Radiohead"


def test_stop_is_cooperative() -> None:
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    assert reg.should_stop() is False
    reg.request_stop()
    assert reg.should_stop() is True


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
    assert s.total == 4 and s.processed == 4
    assert s.moved == 3 and s.skipped == 1 and s.failed == 0


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
    assert s.phase == "failed" and "kaboom" in (s.error or "")
