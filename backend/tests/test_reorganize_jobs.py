# backend/tests/test_reorganize_jobs.py
import pytest

from app.models.reorganize import ReorganizeOutcome
from app.reorganize_jobs.registry import (
    ReorganizeRegistry,
    reorganize_backfill_active,
    reset_reorganize_backfill,
)


def test_start_then_running_then_finish() -> None:
    reg = ReorganizeRegistry()
    assert reg.state().phase == "idle"
    reg.start(artist=None, album_id=None, scope_label="library")
    assert reg.is_running()
    reg.set_total(3)
    reg.record(ReorganizeOutcome(status="moved", label="a"))
    reg.record(ReorganizeOutcome(status="skipped", label="b"))
    reg.record(ReorganizeOutcome(status="failed", label="c", error="x"))
    reg.finish("done")
    s = reg.state()
    assert (s.phase, s.total, s.processed, s.moved, s.skipped, s.failed) == ("done", 3, 3, 1, 1, 1)


def test_double_start_raises() -> None:
    reg = ReorganizeRegistry()
    reg.start(artist=None, album_id=None, scope_label="library")
    with pytest.raises(RuntimeError):
        reg.start(artist=None, album_id=None, scope_label="library")


def test_scope_fields_surface() -> None:
    reg = ReorganizeRegistry()
    reg.start(artist="Radiohead", album_id=None, scope_label="Radiohead")
    s = reg.state()
    assert s.artist == "Radiohead" and s.album_id is None and s.scope_label == "Radiohead"


def test_stop_is_cooperative() -> None:
    reg = ReorganizeRegistry()
    reg.start(artist=None, album_id=None, scope_label="library")
    assert reg.should_stop() is False
    reg.request_stop()
    assert reg.should_stop() is True


def test_module_global_active_and_reset() -> None:
    reg = reset_reorganize_backfill()
    assert reorganize_backfill_active() is False
    reg.start(artist=None, album_id=None, scope_label="library")
    assert reorganize_backfill_active() is True
    reset_reorganize_backfill()
    assert reorganize_backfill_active() is False
