import threading
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.import_session import ImportBridge, WebImportSession
from app.import_jobs.runner import BeetsImportRunner, InLibraryCopyError
from app.models.import_models import ImportOptions


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


def test_post_import_trash_failure_still_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed import reports success even if post-run Trash cleanup raises.

    The album is in the library the moment ``session.run()`` returns; a failure
    while moving a Replace-superseded copy to Trash must annotate (log), never
    flip the job to failed (which would re-trigger duplicate detection on retry).
    """
    import app.beets.import_session as import_session_mod

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))

    # run() succeeds (the album is imported); the post-run cleanup then blows up.
    monkeypatch.setattr(WebImportSession, "run", lambda self: None)

    def boom(session: WebImportSession) -> None:
        raise RuntimeError("trash move failed")

    monkeypatch.setattr(import_session_mod, "_trash_replaced_albums", boom)

    finished = threading.Event()
    errored: dict[str, str] = {}

    def on_finish() -> None:
        finished.set()

    def on_error(message: str) -> None:
        errored["message"] = message
        finished.set()

    BeetsImportRunner(lib).run(
        str(tmp_path / "incoming"),
        ImportBridge(),
        on_finish=on_finish,
        on_error=on_error,
    )
    assert finished.wait(timeout=2.0)
    assert errored == {}  # cleanup failure must NOT fail the committed import


def test_runner_passes_trash_dir_to_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    import app.import_jobs.runner as runner_mod
    from app.beets.import_session import ImportBridge
    from app.import_jobs.runner import BeetsImportRunner

    captured: dict[str, object] = {}

    class _FakeSession:
        # ``unattended`` now rides as a keyword arg; absorb **kwargs so the
        # positional capture (trash_dir = args[5]) is unaffected.
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["trash_dir"] = args[5]

    monkeypatch.setattr(runner_mod, "WebImportSession", _FakeSession)
    monkeypatch.setattr(
        runner_mod, "run_import_worker", lambda s, *, move=None, sweep=False, directive=None: None
    )

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


@pytest.mark.parametrize(
    ("options", "expected_move"),
    [
        # The chunk's central new behavior: operation -> the run_import_worker
        # ``move`` kwarg that scopes the file op. An inversion here would ship a
        # wrong file operation while keeping every other test green.
        (ImportOptions(operation="move"), True),
        (ImportOptions(operation="copy"), False),
        (ImportOptions(operation="default"), None),  # falls through to beets config
        (None, None),  # today's manual default
    ],
)
def test_runner_translates_options_operation_to_move(
    options: ImportOptions | None,
    expected_move: bool | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.import_jobs.runner as runner_mod

    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    def _capture_worker(
        session: object,
        *,
        move: bool | None = None,
        sweep: bool = False,
        directive: object = None,
    ) -> None:
        captured["move"] = move

    monkeypatch.setattr(runner_mod, "WebImportSession", _FakeSession)
    monkeypatch.setattr(runner_mod, "run_import_worker", _capture_worker)

    finished = threading.Event()
    BeetsImportRunner(lib=object()).run(
        "/music",
        ImportBridge(),
        on_finish=finished.set,
        on_error=lambda _message: None,
        options=options,
    )
    assert finished.wait(timeout=2.0)
    assert captured["move"] == expected_move


@pytest.mark.parametrize(
    ("options", "expected_unattended"),
    [
        # The runner must thread ImportOptions.unattended into the session's
        # keyword-only param; None options = today's attended manual default.
        (ImportOptions(unattended=True), True),
        (ImportOptions(unattended=False), False),
        (ImportOptions(operation="move", unattended=True), True),
        (None, False),
    ],
)
def test_runner_forwards_unattended_to_session(
    options: ImportOptions | None,
    expected_unattended: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.import_jobs.runner as runner_mod

    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["unattended"] = kwargs.get("unattended")

    monkeypatch.setattr(runner_mod, "WebImportSession", _FakeSession)
    monkeypatch.setattr(
        runner_mod, "run_import_worker", lambda s, *, move=None, sweep=False, directive=None: None
    )

    finished = threading.Event()
    BeetsImportRunner(lib=object()).run(
        "/music",
        ImportBridge(),
        on_finish=finished.set,
        on_error=lambda _message: None,
        options=options,
    )
    assert finished.wait(timeout=2.0)
    assert captured["unattended"] is expected_unattended


@pytest.mark.parametrize(
    ("options", "expected_sweep", "expected_bank"),
    [
        (ImportOptions(sweep=True), True, Path("/tmp/bank")),
        # Inbox: unattended but NOT banking - bank_dir must stay None.
        (ImportOptions(unattended=True), False, None),
        (None, False, None),
    ],
)
def test_runner_forwards_sweep_and_bank_dir(
    options: ImportOptions | None,
    expected_sweep: bool,
    expected_bank: Path | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.import_jobs.runner as runner_mod

    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["sweep"] = kwargs.get("sweep")
            captured["bank_dir"] = kwargs.get("bank_dir")

    def _capture_worker(
        session: object,
        *,
        move: bool | None = None,
        sweep: bool = False,
        directive: object = None,
    ) -> None:
        captured["worker_sweep"] = sweep

    monkeypatch.setattr(runner_mod, "WebImportSession", _FakeSession)
    monkeypatch.setattr(runner_mod, "run_import_worker", _capture_worker)

    finished = threading.Event()
    BeetsImportRunner(lib=object(), bank_dir=Path("/tmp/bank")).run(
        "/music",
        ImportBridge(),
        on_finish=finished.set,
        on_error=lambda _message: None,
        options=options,
    )
    assert finished.wait(timeout=2.0)
    assert captured["sweep"] is expected_sweep
    assert captured["bank_dir"] == expected_bank
    assert captured["worker_sweep"] is expected_sweep


def test_validate_refuses_in_library_copy(tmp_path: Path) -> None:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    runner = BeetsImportRunner(lib)
    with pytest.raises(InLibraryCopyError):
        runner.validate(str(tmp_path / "music" / "incoming"), ImportOptions(operation="copy"))


@pytest.mark.parametrize(
    ("path_suffix", "options"),
    [
        ("music/incoming", ImportOptions(operation="move")),  # in-library move: fine
        ("music/incoming", ImportOptions(operation="default")),  # worker will force move
        ("music/incoming", None),  # manual default
        ("downloads/incoming", ImportOptions(operation="copy")),  # outside: copy fine
    ],
)
def test_validate_passes_safe_combinations(
    tmp_path: Path, path_suffix: str, options: ImportOptions | None
) -> None:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    runner = BeetsImportRunner(lib)
    runner.validate(str(tmp_path / path_suffix), options)  # must not raise


def test_runner_forwards_directive_to_session_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.import_jobs.runner as runner_mod
    from app.models.bank import BankApplyDirective

    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["session_directive"] = kwargs.get("directive")
            captured["sweep"] = kwargs.get("sweep")

    def _capture_worker(
        session: object,
        *,
        move: bool | None = None,
        sweep: bool = False,
        directive: object = None,
    ) -> None:
        captured["worker_directive"] = directive

    monkeypatch.setattr(runner_mod, "WebImportSession", _FakeSession)
    monkeypatch.setattr(runner_mod, "run_import_worker", _capture_worker)

    directive = BankApplyDirective(action="asis")
    finished = threading.Event()
    BeetsImportRunner(lib=object()).run(
        "/library/A",
        ImportBridge(),
        on_finish=finished.set,
        on_error=lambda _message: None,
        directive=directive,
    )
    assert finished.wait(timeout=2.0)
    assert captured["session_directive"] is directive
    assert captured["worker_directive"] is directive
    assert captured["sweep"] is False  # an apply is never a sweep
