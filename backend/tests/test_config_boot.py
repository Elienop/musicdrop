"""config.yaml at STARTUP: the error beets raised, and the one line that names the file.

A config.yaml beets cannot use fails the boot either way; what these pin is that
the failure is the real one (not a ``NotFoundError`` about an unrelated key) and
that it gets its own ``refusing to start`` line rather than the library-path one.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from confuse import ConfigTypeError
from fastapi.testclient import TestClient

from app.beets.setup import ConfigFileMissing, ConfigUnreadable, read_beets_config, setup_beets
from app.main import app as real_app

_DIGIT_LIMIT = (
    "Exceeds the limit (4300 digits) for integer string conversion: value has 5000"
    " digits; use sys.set_int_max_str_digits() to increase the limit"
)


@pytest.fixture
def beets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway BEETSDIR, so a real lifespan never reaches the dev library."""
    beets = tmp_path / "beets"
    beets.mkdir()
    (tmp_path / "music").mkdir()
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets))
    return beets


def _write(beets_dir: Path, extra: str) -> Path:
    cfg = beets_dir / "config.yaml"
    music = beets_dir.parent / "music"
    cfg.write_text(f"directory: {music}\nlibrary: library.db\n{extra}", encoding="utf-8")
    return cfg


def _boot_refusal(caplog: pytest.LogCaptureFixture) -> str:
    refusals = [r for r in caplog.records if r.name == "uvicorn.error"]
    assert len(refusals) == 1, [(r.name, r.getMessage()) for r in caplog.records]
    assert refusals[0].levelno == logging.ERROR
    return refusals[0].getMessage()


def test_an_over_long_integer_at_boot_raises_the_real_error(beets_dir: Path) -> None:
    """confuse's ``exists()`` turned the read's ``ValueError`` into "not found".

    Measured on the parent commit: the boot died on ``NotFoundError: pluginpath
    not found``, a key the file never mentions.
    """
    cfg = _write(beets_dir, f"foo: {'9' * 5000}\n")

    with pytest.raises(ConfigUnreadable) as info:
        setup_beets(str(beets_dir))

    assert str(info.value) == f"{cfg} could not be read: ValueError: {_DIGIT_LIMIT}"
    assert info.value.line is None
    assert isinstance(info.value.__cause__, ValueError)


def _config_refusal(beets_dir: Path, exc: BaseException) -> str:
    return (
        "refusing to start: config.yaml or one of its includes under"
        f" MUSICDROP_BEETS_DIR={str(beets_dir)!r} cannot be used ({exc!r}). Fix it."
    )


@pytest.mark.parametrize(
    ("extra", "raised"),
    [
        ("plugins:\n  - musicbrainz\nmusicbrainz: no\n", ConfigTypeError),
        # A duplicate key: PyYAML keeps the last, so this ``directory:`` wins.
        ("directory: 5\n", ConfigTypeError),
        ("plugins: 5\n", ConfigTypeError),
        ("foo: [unclosed\n", ConfigUnreadable),
        (f"foo: {'9' * 5000}\n", ConfigUnreadable),
        ("foo: !!bool ture\n", ConfigUnreadable),
    ],
    ids=[
        "a-value-of-the-wrong-type",
        "a-directory-that-is-an-int",
        "plugins-that-is-an-int",
        "a-syntax-error",
        "an-over-long-integer",
        "a-mistyped-bool-tag",
    ],
)
def test_a_config_error_at_boot_gets_its_own_refusal_line(
    beets_dir: Path,
    caplog: pytest.LogCaptureFixture,
    extra: str,
    raised: type[Exception],
) -> None:
    """Not a bare traceback, and not the line about `library:` and `directory:`.

    The three typed values are ones Apply's gate passes and its rebuild rejects;
    boot refuses them, which is what Apply's 500 recovery says. The YAML
    error's own text spans lines, so the exception is repr'd.
    """
    _write(beets_dir, extra)

    with caplog.at_level(logging.ERROR), pytest.raises(raised) as info:
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    assert _boot_refusal(caplog) == _config_refusal(beets_dir, info.value)


def test_a_mistyped_tag_in_config_yaml_names_the_error_at_boot(beets_dir: Path) -> None:
    """PyYAML raises ``KeyError('ture')`` for it, outside confuse's ``ConfigReadError``."""
    cfg = _write(beets_dir, "foo: !!bool ture\n")

    with pytest.raises(ConfigUnreadable) as info:
        setup_beets(str(beets_dir))

    assert str(info.value) == f"{cfg} could not be read: KeyError: 'ture'"


def test_a_library_beets_cannot_open_gets_the_library_line(
    beets_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The non-config arm of Apply's 500: boot refuses this file too."""
    music = beets_dir.parent / "music"
    (beets_dir / "config.yaml").write_text(
        f"directory: {music}\nlibrary: /nonexistent-md/x/lib.db\n", encoding="utf-8"
    )

    with caplog.at_level(logging.ERROR), pytest.raises(sqlite3.OperationalError):
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    assert _boot_refusal(caplog) == (
        "refusing to start: beets could not open the library under"
        f" MUSICDROP_BEETS_DIR={str(beets_dir)!r}. OperationalError: unable to open"
        " database file. Check `library:` and `directory:` in that directory's config.yaml."
    )


def _boot_in_a_thread(deadline: float) -> tuple[BaseException | None, bool]:
    """Run a real lifespan on a thread; ``(what it raised, whether it finished)``."""
    raised: list[BaseException] = []

    def boot() -> None:
        try:
            with TestClient(real_app):
                pass
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=boot, daemon=True)
    thread.start()
    thread.join(deadline)
    return (raised[0] if raised else None), not thread.is_alive()


def test_an_include_beets_would_skip_refuses_the_boot(
    beets_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Owner ruling 2026-09-21: boot refuses it, as Apply does.

    beets prints it to stderr and loads without it (``beets/__init__.py:37-38``).
    """
    _write(beets_dir, "include:\n  - gone.yaml\n")

    with caplog.at_level(logging.ERROR), pytest.raises(ConfigUnreadable) as info:
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    assert str(info.value) == "beets would skip the include gone.yaml: No such file or directory"
    assert _boot_refusal(caplog) == _config_refusal(beets_dir, info.value)


_ROOT_SKIP = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the permission bits this test sets"
)


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ("yaml", "expected ',' or ']', but got '<stream end>' at line 3"),
        pytest.param("permission", "Permission denied", marks=_ROOT_SKIP),
    ],
)
def test_a_skipped_include_names_why_at_boot(
    beets_dir: Path, caplog: pytest.LogCaptureFixture, shape: str, reason: str
) -> None:
    """Owner ruling 2026-09-21: a YAML error in the refusal carries its line number."""
    bad = beets_dir / "bad.yaml"
    bad.write_text("a: 1\nfoo: [unclosed\n", encoding="utf-8")
    if shape == "permission":
        bad.chmod(0)
    _write(beets_dir, "include:\n  - bad.yaml\n")

    with caplog.at_level(logging.ERROR), pytest.raises(ConfigUnreadable) as info:
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    assert str(info.value) == f"beets would skip the include bad.yaml: {reason}"
    assert _boot_refusal(caplog) == _config_refusal(beets_dir, info.value)


def test_a_fifo_include_refuses_the_boot_instead_of_hanging(
    beets_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """beets' own read opens the include by name and blocks on a FIFO."""
    os.mkfifo(beets_dir / "pipe.yaml")
    _write(beets_dir, "include:\n  - pipe.yaml\n")

    with caplog.at_level(logging.ERROR):
        raised, finished = _boot_in_a_thread(5.0)

    assert finished, "the boot is still blocked on the FIFO"
    assert isinstance(raised, ConfigUnreadable)
    assert str(raised) == (
        f"`include:` in config.yaml could not be read: {str(beets_dir / 'pipe.yaml')!r}"
        " is a FIFO; beets would block on it. Fix the include: list."
    )
    assert _boot_refusal(caplog) == _config_refusal(beets_dir, raised)


def test_a_mistyped_tag_in_an_include_refuses_the_boot_and_names_it(beets_dir: Path) -> None:
    """``KeyError`` escapes beets' include loop, which catches only ``ConfigReadError``."""
    (beets_dir / "bad.yaml").write_text("foo: !!bool ture\n", encoding="utf-8")
    _write(beets_dir, "include:\n  - bad.yaml\n")

    with pytest.raises(ConfigUnreadable) as info:
        setup_beets(str(beets_dir))

    assert str(info.value) == (
        f"`include:` in config.yaml could not be read: {str(beets_dir / 'bad.yaml')!r}"
        " raised KeyError: 'ture'. Fix the include: list."
    )


def test_a_readable_include_still_boots(beets_dir: Path) -> None:
    """The control: the include is read, and its ``directory:`` is what loads."""
    overlay_music = beets_dir.parent / "overlay-music"
    overlay_music.mkdir()
    (beets_dir / "overlay.yaml").write_text(f"directory: {overlay_music}\n", encoding="utf-8")
    _write(beets_dir, "include:\n  - overlay.yaml\n")

    with TestClient(real_app):
        assert real_app.state.beets_library.lib.directory == os.fsencode(overlay_music)


def test_the_line_of_a_yaml_error_is_one_based(beets_dir: Path) -> None:
    """PyYAML's ``problem_mark`` is 0-based; the unclosed ``[`` fails on line 4."""
    _write(beets_dir, "foo: [unclosed\nbar: 1\n")

    with pytest.raises(ConfigUnreadable) as info:
        setup_beets(str(beets_dir))

    assert info.value.line == 4


def test_a_reload_read_writes_no_starter(tmp_path: Path) -> None:
    """Only the boot writes the starter; Apply's read refuses a missing file.

    The starter Apply used to write carried ``directory: ../music``, because the
    image's ``/music`` override is a boot argument Apply never passed.
    """
    with pytest.raises(ConfigFileMissing) as info:
        read_beets_config(str(tmp_path))

    assert str(info.value) == f"{tmp_path / 'config.yaml'} is missing or is not a regular file"
    assert list(tmp_path.iterdir()) == []


def _alive_drains() -> set[threading.Thread]:
    names = {"musicdrop-acquisition", "musicdrop-bank-apply"}
    return {t for t in threading.enumerate() if t.name in names and t.is_alive()}


def test_a_boot_that_fails_after_the_bank_leaves_no_drain_running(
    beets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drains start next to the ``try`` whose ``finally`` stops them.

    Started earlier, anything that raised in between (here the fanart source)
    skipped the ``finally``, and both drain threads outlived the failed boot.
    """
    _write(beets_dir, "")

    def _boom(*_args: object) -> object:
        raise RuntimeError("fanart")

    monkeypatch.setattr("app.artwork.factory.build_fanart_background_source", _boom)
    drains_before = _alive_drains()

    with pytest.raises(RuntimeError, match="fanart"):
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    assert _alive_drains() - drains_before == set()


def test_a_bank_drain_that_fails_to_start_leaves_no_inbox_drain_running(
    beets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Thread.start`` can raise; the inbox drain it follows is stopped."""
    _write(beets_dir, "")

    def _refused(_self: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr("app.bank.apply_runner.BankApplyRunner.start", _refused)
    drains_before = _alive_drains()

    with pytest.raises(RuntimeError, match="can't start new thread"):
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    assert _alive_drains() - drains_before == set()
