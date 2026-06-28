from __future__ import annotations

import pathlib
import tempfile
from typing import Any

from beets.library import Library

from app.lyrics_jobs.registry import LyricsBackfillRegistry
from app.lyrics_jobs.runner import sweep
from app.models.lyrics import ItemLyricsOutcome


def test_sweep_calls_on_complete(edit_lib: Library) -> None:
    from tests.conftest import make_test_handle

    handle = make_test_handle(edit_lib, pathlib.Path(tempfile.mkdtemp()))
    reg = LyricsBackfillRegistry()
    calls: dict[str, int] = {"n": 0}

    def _fetch(*_a: Any, **_k: Any) -> ItemLyricsOutcome:
        return ItemLyricsOutcome(item_id=1, status="not_found", source=None, written=False)

    sweep(
        reg,
        handle,
        delay=0.0,
        write=False,
        fetch_one=_fetch,
        on_complete=lambda: calls.__setitem__("n", calls["n"] + 1),
    )
    assert calls["n"] == 1
