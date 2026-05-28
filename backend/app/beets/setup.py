"""Embedded beets startup — faithful mirror of beets' own _setup.

Reference: beets/ui/__init__.py:807-827 (_setup), :830-864 (_configure),
:879-899 (_open_library), all from beets 2.11.0 in venv.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import beets
from beets import plugins
from beets.library import Library
from beets.ui import get_path_formats, get_replacements

from app.beets.library import LibraryHandle

logger = logging.getLogger(__name__)


def setup_beets(beets_dir: str) -> LibraryHandle:
    """Open a beets Library under ``beets_dir``, honoring its config.yaml.

    Do not reorder the body. Three constraints are load-bearing:

    - ``BEETSDIR`` must be set BEFORE confuse's first resolve. confuse reads
      the env inside ``Configuration.config_dir()``; setting it after the
      first access locks in the platform default and our user file is lost.
    - The file mtime snapshot must be captured BEFORE the first resolve.
      It's the baseline for the Config view's "restart required" check; any
      write during setup would race a freshness comparison taken later.
    - ``plugins.load_plugins()`` must run AFTER the first resolve. It reads
      ``config["plugins"].as_str_seq()`` at call time; running it earlier
      (as the previous implementation did) freezes the bundled defaults and
      the user's ``plugins:`` list is ignored.
    """
    beets_dir_path = Path(beets_dir).resolve()
    beets_dir_path.mkdir(parents=True, exist_ok=True)
    cfg_path = beets_dir_path / "config.yaml"

    if not cfg_path.exists():
        starter = Path(__file__).parent / "config.starter.yaml"
        shutil.copy(starter, cfg_path)
        logger.info("Copied starter config to %s", cfg_path)

    os.environ["BEETSDIR"] = str(beets_dir_path)

    for old in ("MUSICDROP_BEETS_LIBRARY_PATH", "MUSICDROP_BEETS_LIBRARY_DIRECTORY"):
        if os.environ.get(old):
            logger.warning(
                "%s is no longer honored. Move the value into %s "
                "(under `library:` or `directory:`), then remove this env var.",
                old,
                cfg_path,
            )

    file_mtime_at_load = cfg_path.stat().st_mtime

    beets.config["dummy"].exists()  # force confuse's lazy resolve

    plugins.load_plugins()

    lib_path = beets.config["library"].as_filename()
    directory = beets.config["directory"].as_filename()

    lib = Library(
        lib_path,
        directory=directory,
        path_formats=get_path_formats(),
        replacements=get_replacements(),
    )
    plugins.send("library_opened", lib=lib)

    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir_path,
        config_path=cfg_path,
        loaded_at=datetime.now(UTC),
        file_mtime_at_load=file_mtime_at_load,
    )
