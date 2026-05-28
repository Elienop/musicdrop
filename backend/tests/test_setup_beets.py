import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from app.beets.setup import setup_beets


@pytest.fixture(autouse=True)
def _clear_beets_globals() -> Iterator[None]:
    """Each test gets a clean beets.config singleton + plugin registry."""
    import beets
    from beets import plugins

    # Snapshot env keys we may mutate
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "BEETSDIR",
            "MUSICDROP_BEETS_LIBRARY_PATH",
            "MUSICDROP_BEETS_LIBRARY_DIRECTORY",
        )
    }
    yield
    # Reset confuse + plugins to defaults so the next test starts fresh.
    # NOTE: beets 2.11 plugins module only exposes _instances (no _classes); the
    # spec's reference to plugins._classes.clear() is stale — load_plugins()
    # guards on `if not _instances`, so clearing the list is sufficient.
    beets.config.clear()
    beets.config.read(user=False, defaults=True)
    plugins._instances.clear()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_setup_copies_starter_when_missing(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    try:
        cfg = tmp_path / "config.yaml"
        assert cfg.exists()
        starter = Path(__file__).parent.parent / "app" / "beets" / "config.starter.yaml"
        assert cfg.read_text() == starter.read_text()
        assert isinstance(handle.loaded_at, datetime)
        assert handle.config_path == cfg
        assert handle.beets_dir == tmp_path.resolve()
    finally:
        # beets' Library exposes _close (single underscore) not close;
        # see close_library() in app/beets/library.py.
        handle.lib._close()  # type: ignore[no-untyped-call]  # beets internals untyped
