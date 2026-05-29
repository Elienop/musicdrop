import threading
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.import_session import ImportBridge, WebImportSession
from app.import_jobs.runner import BeetsImportRunner


def test_beets_runner_builds_session_and_invokes_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real (empty) beets library; no audio files, no network. We stub
    # WebImportSession.run so run_import_worker exercises the real construction
    # + daemon-thread + on_finish path WITHOUT a MusicBrainz/audio import.
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))

    captured: dict[str, object] = {}

    def fake_run(self: WebImportSession) -> None:
        captured["paths"] = list(self.paths)
        captured["lib_is_real"] = self.lib is lib
        captured["bridge_is_real"] = isinstance(self.bridge, ImportBridge)

    monkeypatch.setattr(WebImportSession, "run", fake_run)

    finished = threading.Event()
    errored: dict[str, str] = {}

    runner = BeetsImportRunner(lib)
    runner.run(
        str(tmp_path / "incoming"),
        ImportBridge(),
        on_finish=finished.set,
        on_error=lambda message: errored.__setitem__("message", message),
    )

    assert finished.wait(timeout=2.0)
    assert errored == {}
    assert captured["lib_is_real"] is True
    assert captured["bridge_is_real"] is True
    # beets normalizes the path to bytes; the basename survives. captured["paths"]
    # is typed ``object`` (dict[str, object]), so assert against the repr.
    assert b"incoming" in repr(captured["paths"]).encode()


def test_beets_runner_crash_routes_to_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))

    def boom(self: WebImportSession) -> None:
        raise ValueError("kaboom")

    monkeypatch.setattr(WebImportSession, "run", boom)

    finished = threading.Event()
    errored: dict[str, str] = {}

    def on_error(message: str) -> None:
        errored["message"] = message
        finished.set()

    runner = BeetsImportRunner(lib)
    runner.run(
        str(tmp_path / "incoming"),
        ImportBridge(),
        on_finish=finished.set,
        on_error=on_error,
    )
    assert finished.wait(timeout=2.0)
    assert errored["message"] == "kaboom"


def test_runner_passes_trash_dir_to_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    import app.import_jobs.runner as runner_mod
    from app.beets.import_session import ImportBridge
    from app.import_jobs.runner import BeetsImportRunner

    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self, *args: object) -> None:
            captured["trash_dir"] = args[5]

    monkeypatch.setattr(runner_mod, "WebImportSession", _FakeSession)
    monkeypatch.setattr(runner_mod, "run_import_worker", lambda s: None)

    BeetsImportRunner(lib=object(), trash_dir=Path("/tmp/t")).run(
        "/music", ImportBridge(), on_finish=lambda: None, on_error=lambda m: None
    )
    # The daemon thread sets it; poll briefly.
    import time

    for _ in range(200):
        if "trash_dir" in captured:
            break
        time.sleep(0.01)
    assert captured["trash_dir"] == Path("/tmp/t")
