"""Registry + runner tests for the single-slot disk-sync job."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from beets.library import Library

from app.models.disk_sync import DiskSyncOutcome


def test_registry_single_slot_and_counts() -> None:
    from app.disk_sync_jobs.registry import DiskSyncRegistry

    reg = DiskSyncRegistry()
    reg.start()
    with pytest.raises(RuntimeError):
        reg.start()
    reg.set_total(4)
    reg.record(DiskSyncOutcome(status="removed", label="a"))
    reg.record(DiskSyncOutcome(status="updated", label="b", fields=["title"]))
    reg.record(DiskSyncOutcome(status="unchanged", label="c"))
    reg.record(DiskSyncOutcome(status="read_error", label="d", error="boom"))
    reg.record_emptied(2)
    reg.finish("done")
    s = reg.state()
    assert (s.total, s.processed) == (4, 4)
    assert (s.removed, s.updated, s.unchanged, s.read_errors) == (1, 1, 1, 1)
    assert s.emptied_albums == 2
    assert len(s.failures) == 1
    assert s.failures[0].label == "d"
    assert s.failures[0].error == "boom"
    assert s.phase == "done"


def test_registry_failures_capped_at_10() -> None:
    from app.disk_sync_jobs.registry import FAILURE_ROW_CAP, DiskSyncRegistry

    reg = DiskSyncRegistry()
    reg.start()
    for i in range(FAILURE_ROW_CAP + 3):
        reg.record(DiskSyncOutcome(status="read_error", label=f"x{i}", error="e"))
    s = reg.state()
    assert s.read_errors == FAILURE_ROW_CAP + 3
    assert len(s.failures) == FAILURE_ROW_CAP


def test_sweep_runs_to_done_and_fires_on_complete(edit_lib: Library, tmp_path: Path) -> None:
    from app.disk_sync_jobs.registry import DiskSyncRegistry
    from app.disk_sync_jobs.runner import sweep
    from tests.conftest import make_test_handle

    victim = next(iter(edit_lib.items()))
    os.remove(victim.path)
    handle = make_test_handle(edit_lib, tmp_path)
    reg = DiskSyncRegistry()
    reg.start()
    fired: list[bool] = []
    sweep(reg, handle, on_complete=lambda: fired.append(True))
    s = reg.state()
    assert s.phase == "done"
    assert s.removed == 1
    assert fired == [True]


def test_sweep_missing_root_fails_job(edit_lib: Library, tmp_path: Path) -> None:
    import shutil

    from app.disk_sync_jobs.registry import DiskSyncRegistry
    from app.disk_sync_jobs.runner import sweep
    from tests.conftest import make_test_handle

    shutil.rmtree(os.fsdecode(edit_lib.directory))
    handle = make_test_handle(edit_lib, tmp_path)
    reg = DiskSyncRegistry()
    reg.start()
    sweep(reg, handle)
    s = reg.state()
    assert s.phase == "failed"
    assert s.error is not None
    assert "unavailable" in s.error.lower()


def test_sweep_crash_is_logged_and_fails_job(
    edit_lib: Library,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.disk_sync_jobs import runner
    from app.disk_sync_jobs.registry import DiskSyncRegistry
    from tests.conftest import make_test_handle

    def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner, "run_disk_sync", boom)
    reg = DiskSyncRegistry()
    reg.start()
    with caplog.at_level("ERROR"):
        runner.sweep(reg, make_test_handle(edit_lib, tmp_path))
    assert reg.state().phase == "failed"
    assert reg.state().error == "kaboom"
    assert any(r.exc_info for r in caplog.records)  # traceback reaches the logs


def test_stop_yields_stopped_phase(edit_lib: Library, tmp_path: Path) -> None:
    from app.disk_sync_jobs.registry import DiskSyncRegistry
    from app.disk_sync_jobs.runner import sweep
    from tests.conftest import make_test_handle

    handle = make_test_handle(edit_lib, tmp_path)
    reg = DiskSyncRegistry()
    reg.start()
    reg.request_stop()
    sweep(reg, handle)
    assert reg.state().phase == "stopped"
