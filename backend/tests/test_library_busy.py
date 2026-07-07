"""Unit tests for the consolidated library-busy gate (app/library_busy.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.import_jobs.registry import get_registry
from app.library_busy import library_job_active, raise_if_library_busy

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
