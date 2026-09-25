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

import errno
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

from app.beets.import_session import (
    ImportBridge,
    WebImportSession,
    is_in_library_source,
    run_import_worker,
)
from app.beets.import_session import (
    InLibraryCopyError as InLibraryCopyError,
)

# Re-exported so the two drains name the refusal without reaching into the adapter.
from app.beets.library import (
    LibraryRootUnavailableError as LibraryRootUnavailableError,
)
from app.beets.library import (
    require_importable_library_root,
)
from app.beets.store_layout import import_source_refusal
from app.config import Settings
from app.models.bank import BankApplyDirective
from app.models.import_models import ImportOptions


class SourcePathMissingError(Exception):
    """A start was asked for a source the OS does not hand back.

    Its own type, never a widened ``InLibraryCopyError``: the two refusals carry
    different sentences and different remedies, so the route maps them apart.

    ``unreadable`` splits the two shapes this one type covers — absent, and
    there but refused (the sentence then names the OS reason). Read by the batch
    route, whose plural copy is about folders that VANISHED and would misreport
    a permissions problem, and by the inbox drain, which logs a different
    sentence for each.
    """

    def __init__(self, message: str, *, unreadable: bool = False) -> None:
        super().__init__(message)
        self.unreadable = unreadable


class ImportSourceRefusedError(Exception):
    """A start was asked for the library, one of MusicDrop's own folders, or slskd's.

    Its own type, beside the two above: the sentence and the remedy differ (pick
    another folder), and a bank row refused this way cannot be retried by
    deciding again. The sentence is one of the four in
    ``app.beets.store_layout`` and carries no path.
    """


#: What ``stat`` answers when there is nothing at the path, as opposed to
#: something being there that it would not answer for. ENAMETOOLONG belongs here
#: because a name the filesystem cannot hold names nothing.
#:
#: Public because EVERY site that must tell absence from refusal splits on this
#: one set rather than re-spelling it, and two copies would drift. The live list
#: is whatever ``grep -rn ABSENT_ERRNOS app/`` returns - deliberately not
#: enumerated here, because the last enumeration said three and a grep found
#: five. (``app/beets/store_layout.py`` keeps its own, narrower set for a
#: different question: what a MISSING store looks like.)
ABSENT_ERRNOS: Final = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG})


def unreadable_reason(exc: OSError) -> str:
    """The OS's own one-phrase summary of a refusal. Carries no path.

    The errno keeps it complete if a platform ever leaves ``strerror`` unset.
    Split out so a caller composing its OWN sentence (the bank apply runner's
    row error) words the reason identically.
    """
    return exc.strerror or f"errno {exc.errno}"


def unreadable_source_sentence(exc: OSError) -> str:
    """What every caller says about a path the OS refused to answer for.

    ONE sentence wherever a source path refuses to answer, so the same fault
    cannot be worded differently in each place. The live list is ``grep -rnE
    'unreadable_source_error|refuse_unless_absent' app/`` - measured
    2026-09-21: the narrower recipe this said before matched only THIS file,
    because the route sites reach the sentence through ``refuse_unless_absent``.
    Not enumerated here: the last enumeration named the bank apply runner's
    START arm, which consumes an already-built exception via ``str(exc)`` and
    never calls this, and missed the bank routes, which do.

    ``strerror`` is the OS's own summary and carries no path, which is what
    makes surfacing it safe (the same reasoning as the unreadable-root arm in
    ``app/beets/library.py``) - unlike ``str(exc)`` on the OSError itself, which
    interpolates ``exc.filename``.
    """
    return f"That folder can’t be read. {unreadable_reason(exc)}."


def unreadable_source_error(exc: OSError) -> SourcePathMissingError:
    """``unreadable_source_sentence`` as the refusal the import routes map to 422."""
    return SourcePathMissingError(unreadable_source_sentence(exc), unreadable=True)


def refuse_unless_absent(exc: OSError) -> None:
    """Raise the shared unreadable refusal, unless the errno says absent.

    Returns for an absent errno so the caller does its own "nothing here" thing
    (a 404, or a stale bank row). One helper because three routes split
    identically and three copies could be re-spelled apart.
    """
    if exc.errno not in ABSENT_ERRNOS:
        raise unreadable_source_error(exc) from None


def missing_source_error(paths: list[str]) -> SourcePathMissingError | None:
    """The refusal for a start with nothing to import; ``None`` if one path answers.

    One ``os.stat`` per path, no walk, stopping at the first path that answers.
    An empty list is "nothing to import" too, and refuses.

    ``os.path.exists`` returned False for a folder under a parent chmod'd 0o600
    exactly as for an absent one, while ``os.stat`` reported errno 13, Permission
    denied (measured 2026-09-20) — so a PUID/GID mismatch on a downloads share
    read as a typo. Splitting on errno keeps the absent sentence and gives the
    rest ``unreadable_source_error`` above.

    ANSWERS rather than propagates for a path the OS rejects outright: an
    embedded NUL raises ValueError, and a surrogate outside U+DC80..U+DCFF
    raises UnicodeEncodeError (a ValueError subclass). Neither names anything on
    disk, so both read as absent. That range is exactly what ``surrogateescape``
    produces from a real undecodable filename (``app/wire.py`` holds the
    reasoning), and those stat NORMALLY - so this arm catches malformed input,
    never a real file.
    """
    refused: OSError | None = None
    for path in paths:
        try:
            os.stat(path)
        except ValueError:
            continue
        except OSError as exc:
            # The FIRST non-absent errno decides WHICH SENTENCE a refusal uses;
            # a later one does not overwrite it. It does not promote anything:
            # the loop returns None on the first path that answers, so a list
            # with one readable member is not refused at all, however many of
            # the others were unreadable. This only picks the wording for the
            # case where NOTHING answered - and ENOENT never sets it, so an
            # all-absent list still says absent.
            if refused is None and exc.errno not in ABSENT_ERRNOS:
                refused = exc
            continue
        return None
    if refused is not None:
        return unreadable_source_error(refused)
    return SourcePathMissingError("That folder doesn’t exist.")


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
        paths: list[str],
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
        options: ImportOptions | None = None,
        directive: BankApplyDirective | None = None,
    ) -> None: ...

    def validate(self, paths: list[str], options: ImportOptions | None = None) -> str | None:
        """Refuse an invalid (paths, options) combination by raising.

        Called synchronously BEFORE the registry allocates the single job slot,
        so a refusal becomes a clean 4xx/5xx — but never on the event loop: it
        stats a caller-named path, so the three routes hand ``start`` to
        ``run_in_threadpool``. Raises
        ``LibraryRootUnavailableError`` (root missing or unreadable, or empty while
        the library holds item rows), ``SourcePathMissingError`` (no source could
        be stat'd — absent OR there and refused; one missing member of a list
        does NOT refuse the start),
        ``ImportSourceRefusedError`` (a source that is or holds the library; is,
        holds or sits in one of MusicDrop's own folders; or is or holds slskd's
        whole folder; one bad member refuses the whole start) and
        ``InLibraryCopyError`` (copy-mode source inside the library, checked PER
        path, so one bad member refuses the whole start).

        RETURNS the library root that was forgiven for being empty, or ``None``
        when nothing was forgiven. Reported rather than logged here because this
        runs before the slot claim, and the caller is the one that knows whether
        the start was accepted (``ImportJobRegistry.start``).
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
        self,
        lib: object,
        trash_dir: Path | None = None,
        trash_origins_dir: Path | None = None,
        bank_dir: Path | None = None,
        playlists_dir: Path | None = None,
        *,
        settings: Settings | None = None,
        beets_dir: Path | None = None,
    ) -> None:
        self._lib = lib
        # What the source refusal lists MusicDrop's own folders from. ``None``
        # only in unit tests that build a runner bare; the registry always
        # passes both (``attach_library`` takes them as required keywords).
        self._settings = settings
        self._beets_dir = beets_dir
        self._trash_dir = trash_dir
        # Threaded session-ward as a PAIR with trash_dir (see WebImportSession):
        # a Replace records where each trashed copy came from, for Restore.
        self._trash_origins_dir = trash_origins_dir
        # Where sweep runs write bank rows (<beets_dir>/bank by default),
        # threaded session-ward exactly like trash_dir. Non-sweep runs never
        # receive it (the session's _bank_row would no-op anyway).
        self._bank_dir = bank_dir
        # The owned-playlist store, threaded session-ward like the two above so
        # a Replace can repair the `.m3u8` exports that named the replaced
        # album's files (one re-export per run). INJECTED rather than read from
        # settings on the worker thread: a settings read would make a test import
        # list the developer's real playlist store.
        self._playlists_dir = playlists_dir

    def validate(self, paths: list[str], options: ImportOptions | None = None) -> str | None:
        # With the root missing or a bare mountpoint, beets re-creates the root,
        # files the album onto the container's own disk, and a move EMPTIES the
        # download (measured under move/copy/hardlink). Asked before the slot is
        # claimed, and FIRST: an unmounted share must keep reporting itself
        # rather than calling every inbox folder missing.
        #
        # The forgiven root is REPORTED, not logged here:
        # ``require_importable_library_root`` logs nothing, and its docstring
        # holds that arm's key, the L-1 measurement and the 2 Hz gate poll.
        #
        # Skipped, not early-returned, without a library: both library reads
        # below need one, but the source-existence check does not
        # (test_validate_answers_rather_than_500ing_without_a_library).
        forgiven: str | None = None
        if self._lib is not None:
            forgiven = require_importable_library_root(self._lib)
        # EXISTENCE, not ``is_dir``: beets imports a single FILE as one track, so
        # an is_dir guard would take away something it can do (what a missing
        # path does instead is in ``SourcePathMissingError``).
        #
        # The refusal is "nothing to import", NOT "every member is present". A
        # member that goes missing after the inbox derived the list is a TOCTOU
        # this check cannot close - it can only move it, since the folder is as
        # free to vanish after the stat as before it. beets already answers that
        # one: a missing toppath takes the single-FILE branch in
        # ``ImportTaskFactory.paths``, ``read_item`` returns None, and that
        # toppath contributes nothing while the rest import. What has no answer
        # below is a start where NO source is there - the typed path that was
        # wrong, which otherwise runs to "Import finished - 0 albums imported".
        refusal = missing_source_error(paths)
        if refusal is not None:
            raise refusal
        if self._lib is None:
            return None
        # After the existence check, so a typo keeps saying it does not exist.
        refused = self._source_refusal(paths)
        if refused is not None:
            raise ImportSourceRefusedError(refused)
        # Only explicit copy is a user-facing error here; default/None are
        # silently corrected to move by the worker guard (run_import_worker).
        if options is not None and options.operation == "copy":
            # getattr (not attribute access) because self._lib is typed ``object``;
            # direct access would trip mypy attr-defined. noqa: B009 for the same reason.
            directory: bytes = getattr(self._lib, "directory")  # noqa: B009
            # ANY in-library member refuses the whole start: a partial import
            # would leave the caller believing every path was handled.
            if any(is_in_library_source(directory, path) for path in paths):
                raise InLibraryCopyError(
                    "This folder is inside your music library; a copy-import "
                    "would duplicate its files. Choose move instead."
                )
        return forgiven

    def _source_refusal(self, paths: list[str]) -> str | None:
        """The sentence refusing one of ``paths`` for WHERE it is, or ``None``.

        Skipped only for a runner built without the layout it reads: a bare unit
        test, or the registry after an Apply refused the layout, whose ``start``
        raises that refusal before it gets here.
        """
        if (
            self._settings is None
            or self._beets_dir is None
            or self._trash_dir is None
            or self._trash_origins_dir is None
        ):
            return None
        return import_source_refusal(
            paths,
            settings=self._settings,
            lib=self._lib,
            beets_dir=self._beets_dir,
            trash_dir=self._trash_dir,
            origins_dir=self._trash_origins_dir,
        )

    def run(
        self,
        paths: list[str],
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
        # None = the worker decides from the file operation (hardlink goes
        # incremental); False is beets' own ``-I``.
        incremental = options.incremental if options is not None else None
        session = WebImportSession(
            self._lib,
            None,  # loghandler -> beets installs a NullHandler
            [os.fsencode(path) for path in paths],
            None,  # query -> path import, not a library query
            bridge,
            self._trash_dir,
            trash_origins_dir=self._trash_origins_dir,
            unattended=unattended,
            sweep=sweep,
            bank_dir=self._bank_dir if sweep else None,
            directive=directive,
            playlists_dir=self._playlists_dir,
        )

        def target() -> None:
            try:
                run_import_worker(
                    session,
                    move=move,
                    sweep=sweep,
                    incremental=incremental,
                    directive=directive,
                )
            # Broad by design: any worker crash must become a failed job, never
            # an unhandled thread exception (which the API could not surface).
            except Exception as exc:
                # The message VERBATIM. beets' own exception family interpolates
                # absolute paths and a MusicBrainz URL into ``str(exc)``, and
                # that is the diagnosis - `/import` takes an arbitrary server
                # path by ruling, and the app already shows the operator repr'd
                # absolute paths from ``app/beets/store_layout.py``. The class
                # name is the fallback for an exception with no message at all.
                on_error(str(exc) or exc.__class__.__name__)
            else:
                on_finish()

        threading.Thread(target=target, name="musicdrop-import", daemon=True).start()
