"""Faithful beets process setup, mirroring beets' own startup (``beets.ui._setup``).

At startup the beets CLI runs: load the configured plugins -> open the Library
(with path formats + replacements) -> send the ``library_opened`` event. The
embedded app must do the same once, so:

- the matcher has its metadata-source plugins. As of beets 2.11 the metadata
  sources (MusicBrainz included) ARE plugins; without ``plugins.load_plugins()``
  the matcher finds zero candidates and every album is skipped.
- imports place/name files per the configured ``paths:`` (via ``open_library``).
- plugins that initialize on ``library_opened`` get their hook.

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import os

from beets import plugins

from app.beets.library import LibraryHandle, open_library


def setup_beets(library_path: str | None, directory: str | None) -> LibraryHandle | None:
    """Run beets' startup sequence; return the opened library, or None.

    Mirrors ``beets.ui._setup`` (minus CLI subcommand wiring), in beets' order:
    load plugins, open the library, fire ``library_opened``. ``plugins.load_plugins``
    is idempotent (beets guards it), so calling this once at app startup is safe;
    the import worker thread reuses the globally-registered plugins.
    """
    # 1. Load configured plugins (config["plugins"], default [musicbrainz]).
    #    In beets 2.11 the metadata sources live in plugins, so until this runs
    #    the matcher has no candidate source -> "every album skipped".
    plugins.load_plugins()

    # 2. Open the library. None when unconfigured/missing -> the API degrades to
    #    empty pages and no import can run.
    if not library_path or not os.path.exists(library_path):
        return None
    lib = open_library(library_path, directory)

    # 3. Let plugins initialize against the opened library (beets' own order).
    plugins.send("library_opened", lib=lib)
    return lib
