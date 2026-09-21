"""config.yaml at STARTUP: the error beets raised, and the one line that names the file.

A config.yaml beets cannot use fails the boot either way; what these pin is that
the failure is the real one (not a ``NotFoundError`` about an unrelated key) and
that it gets its own ``refusing to start`` line rather than the library-path one.
"""

from __future__ import annotations

import logging
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

    assert str(info.value) == f"{cfg} could not be read: {_DIGIT_LIMIT}"
    assert info.value.line is None
    assert isinstance(info.value.__cause__, ValueError)


@pytest.mark.parametrize(
    ("extra", "raised"),
    [
        ("plugins:\n  - musicbrainz\nmusicbrainz: no\n", ConfigTypeError),
        ("foo: [unclosed\n", ConfigUnreadable),
        (f"foo: {'9' * 5000}\n", ConfigUnreadable),
    ],
    ids=["a-value-of-the-wrong-type", "a-syntax-error", "an-over-long-integer"],
)
def test_a_config_error_at_boot_gets_its_own_refusal_line(
    beets_dir: Path,
    caplog: pytest.LogCaptureFixture,
    extra: str,
    raised: type[Exception],
) -> None:
    """Not a bare traceback, and not the line about `library:` and `directory:`.

    The over-long integer is a ``ValueError`` underneath, which the library-path
    arm also catches: the config arm has to come first. The YAML error's own text
    spans lines, so the exception is repr'd.
    """
    _write(beets_dir, extra)

    with caplog.at_level(logging.ERROR), pytest.raises(raised) as info:
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    assert _boot_refusal(caplog) == (
        f"refusing to start: beets rejected config.yaml under"
        f" MUSICDROP_BEETS_DIR={str(beets_dir)!r} ({info.value!r}). Fix that file."
    )


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

    assert str(info.value) == f"{tmp_path / 'config.yaml'} was not found"
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
