from __future__ import annotations

import pathlib
import tempfile

from beets.library import Library

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
