"""The import-runner seam.

``ImportRunner`` is the injectable boundary between the registry (pure
lifecycle) and the actual import engine. Production uses ``BeetsImportRunner``
(verified in Task 6), which builds a real WebImportSession and runs it on a
daemon thread. Tests inject a fake that drives the same ImportBridge with canned
outcomes + a parked album.

This module imports beets only via our adapter (app.beets.import_session); it is
in the mypy disallow_untyped_calls override because it constructs beets objects.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from app.beets.import_session import (
    ImportBridge,
    WebImportSession,
    is_in_library_source,
    run_import_worker,
)
from app.beets.import_session import (
    InLibraryCopyError as InLibraryCopyError,
)
from app.models.bank import BankApplyDirective
from app.models.import_models import ImportOptions


class ImportRunner(Protocol):
    """Starts an import on its own thread, reporting completion via callbacks.

    ``on_finish`` is called (no args) when the import ends normally (incl. a
    clean abort); ``on_error`` is called with the exception message when the
    worker raises. Implementations MUST be non-blocking (spawn a thread and
    return) so the API start endpoint returns immediately.

    ``options`` carries per-import overrides (operation move/copy, unattended);
    ``None`` is today's manual default. ``directive`` carries a bank apply
    run's translated decision; None for every other origin.
    """

    def run(
        self,
        path: str,
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
        options: ImportOptions | None = None,
        directive: BankApplyDirective | None = None,
    ) -> None: ...

    def validate(self, path: str, options: ImportOptions | None = None) -> None:
        """Refuse an invalid (path, options) combination by raising.

        Called synchronously on the API thread BEFORE the registry allocates
        the single job slot, so a refusal becomes a clean 4xx — never a failed
        job or a stuck slot. Raises ``InLibraryCopyError`` for copy-mode
        imports of sources inside the library directory.
        """
        ...


class BeetsImportRunner:
    """Production runner: a real WebImportSession on a daemon thread.

    The beets Library is captured at construction (the process-wide library the
    app opened at startup). ``run`` builds the session for ``path`` and starts a
    daemon thread that calls ``run_import_worker`` (which forces
    config["threaded"]=False and calls session.run()), translating any crash
    into ``on_error`` and a normal return (incl. abort) into ``on_finish``.
    """

    def __init__(
        self, lib: object, trash_dir: Path | None = None, bank_dir: Path | None = None
    ) -> None:
        self._lib = lib
        self._trash_dir = trash_dir
        # Where sweep runs write bank rows (<beets_dir>/bank by default),
        # threaded session-ward exactly like trash_dir. Non-sweep runs never
        # receive it (the session's _bank_row would no-op anyway).
        self._bank_dir = bank_dir

    def validate(self, path: str, options: ImportOptions | None = None) -> None:
        # Only explicit copy is a user-facing error here; default/None are
        # silently corrected to move by the worker guard (run_import_worker).
        if options is not None and options.operation == "copy":
            # getattr (not attribute access) because self._lib is typed ``object``;
            # direct access would trip mypy attr-defined. noqa: B009 for the same reason.
            directory: bytes = getattr(self._lib, "directory")  # noqa: B009
            if is_in_library_source(directory, path):
                raise InLibraryCopyError(
                    "This folder is inside your music library; a copy-import "
                    "would duplicate its files. Choose move instead."
                )

    def run(
        self,
        path: str,
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
        options: ImportOptions | None = None,
        directive: BankApplyDirective | None = None,
    ) -> None:
        # "default" / None falls through to the user's beets config (manual
        # default); "move"/"copy" force that operation for this run only,
        # applied as a snapshot/restore mutation inside run_import_worker.
        move = (
            None
            if options is None or options.operation == "default"
            else (options.operation == "move")
        )
        # Unattended (inbox) imports set uncertain/duplicate albums aside instead
        # of parking for a human; None options = today's attended manual default.
        # A sweep is unattended by definition (the session ORs the flag in) and
        # additionally banks each set-aside, so it gets the bank dir.
        unattended = options.unattended if options is not None else False
        sweep = options.sweep if options is not None else False
        session = WebImportSession(
            self._lib,
            None,  # loghandler -> beets installs a NullHandler
            [os.fsencode(path)],
            None,  # query -> path import, not a library query
            bridge,
            self._trash_dir,
            unattended=unattended,
            sweep=sweep,
            bank_dir=self._bank_dir if sweep else None,
            directive=directive,
        )

        def target() -> None:
            try:
                run_import_worker(session, move=move, sweep=sweep, directive=directive)
            # Broad by design: any worker crash must become a failed job, never
            # an unhandled thread exception (which the API could not surface).
            except Exception as exc:
                on_error(str(exc) or exc.__class__.__name__)
            else:
                on_finish()

        threading.Thread(target=target, name="musicdrop-import", daemon=True).start()
