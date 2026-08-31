from __future__ import annotations

import pathlib
import tempfile
from collections.abc import Iterator

import pytest
from beets.library import Library

from app.models.reorganize import ReorganizeOutcome
from app.reorganize_jobs.registry import ReorganizeRegistry
from app.reorganize_jobs.runner import sweep


def test_sweep_calls_on_complete(reorganize_lib: Library) -> None:
    from tests.conftest import make_test_handle

    handle = make_test_handle(reorganize_lib, pathlib.Path(tempfile.mkdtemp()))
    reg = ReorganizeRegistry()
    calls: dict[str, int] = {"n": 0}
    sweep(
        reg,
        handle,
        scope="library",
        on_complete=lambda: calls.__setitem__("n", calls["n"] + 1),
    )
    assert calls["n"] == 1


def test_sweep_on_complete_fires_on_failure(reorganize_lib: Library) -> None:
    """on_complete must fire via finally even when reorg_album raises."""
    from tests.conftest import make_test_handle

    handle = make_test_handle(reorganize_lib, pathlib.Path(tempfile.mkdtemp()))
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    calls: dict[str, int] = {"n": 0}

    def _raiser(_lib: object, _album: object) -> ReorganizeOutcome:
        raise RuntimeError("boom")

    sweep(
        reg,
        handle,
        scope="library",
        reorg_album=_raiser,
        on_complete=lambda: calls.__setitem__("n", calls["n"] + 1),
    )

    assert calls["n"] == 1
    assert reg.state().phase == "failed"


def test_sweep_stop_during_orphan_pass_reports_stopped(
    reorganize_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stop hit during the post-move orphan sweep finishes 'stopped', not 'done'."""
    from app.reorganize_jobs import runner as reorg_runner
    from tests.conftest import make_test_handle

    tmp = pathlib.Path(tempfile.mkdtemp())
    handle = make_test_handle(reorganize_lib, tmp)
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")

    def fake_orphans(*_a: object, **_k: object) -> Iterator[pathlib.Path]:
        reg.request_stop()  # user hits Stop as the orphan pass begins
        yield tmp / "husk"

    monkeypatch.setattr(reorg_runner, "find_orphan_folders", fake_orphans)
    sweep(
        reg,
        handle,
        scope="library",
        trash_dir=tmp / "trash",
        trash_origins_dir=tmp / "trash-origins",
    )

    assert reg.state().phase == "stopped"
