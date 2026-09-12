"""Where MusicDrop's own stores may sit relative to the music library.

``empty_all`` runs ``rmtree`` on every unprotected child of the Trash and
``clear_trash_origins`` unlinks every ``*.json`` in the origin store, so where
those two resolve decides whether "Empty Trash" removes a hundred albums or the
library.

``M`` music library (``directory:``) · ``B`` beets data dir · ``T`` Trash · ``O``
origin store · ``L`` ``library.db`` · eight app-owned stores (bank, plex, slskd,
playlists, inbox, exports, artist-image cache, cover-thumbnail cache).
:data:`_ROWS` IS the rule, a row per refusal with the loss it causes; its shape:

* ``B`` and ``M`` may not be, or nest with, each other.
* ``T`` and ``O`` may not be or contain ``M`` or ``B``, nor be or contain each
  other or any app-owned store (D2); ``M`` may not hold ``O``; ``L`` may sit in
  neither.
* Allowed on purpose (``decisions.md`` 35, and the shipped defaults): ``T``
  inside ``M``; ``T``, ``O``, ``L`` under ``B``; ``M`` and ``B`` disjoint.

Comparisons run on resolved paths and, where both exist, on ``(st_dev, st_ino)``
— ``resolve()`` collapses symlinks and ``..``, not a bind mount, which is why
``app.beets.protected`` asks again at the mover. What this rule does not catch is
listed in one place, the BACKLOG entry for this slice.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple

import beets
import confuse

from app.beets.library import LibraryHandle, _music_dir
from app.beets.protected import ProtectedTrees, protected_trees
from app.beets.trash import resolve_trash_dir, resolve_trash_origins_dir
from app.config import Settings, app_owned_dirs, export_dir
from app.fsutil import BELOW_FLAGS, ROOT_FLAGS, open_root

__all__ = [
    "BEETS_SETTING",
    "LIBRARY_SETTING",
    "MUSIC_SETTING",
    "ORIGINS_SETTING",
    "TRASH_SETTING",
    "StoreLayoutError",
    "check_store_layout",
    "checked_protected_trees",
    "checked_store_dirs",
    "effective_config_paths",
    "layout_check_for_config",
    "lib_music_and_library",
]

#: How each of the five inputs is spelled for the operator who has to change it.
TRASH_SETTING = "MUSICDROP_TRASH_DIR"
ORIGINS_SETTING = "MUSICDROP_TRASH_ORIGINS_DIR"
MUSIC_SETTING = "`directory:` in config.yaml"
LIBRARY_SETTING = "`library:` in config.yaml"
BEETS_SETTING = "MUSICDROP_BEETS_DIR"
#: The playlist export dir takes part in D2's rows only, so it is not one of
#: the five above; it is spelled for the operator the same way.
EXPORT_SETTING = "MUSICDROP_PLAYLISTS_EXPORT_DIR"

#: The ``config.yaml`` key whose value a refusal is about, when there is one —
#: the editor paints its gutter row against that key. ``None`` for a refusal
#: between two env-derived paths, where no submitted value is at fault.
_CONFIG_KEY_OF: Final[dict[str, str]] = {
    MUSIC_SETTING: "directory",
    LIBRARY_SETTING: "library",
}


class StoreLayoutError(Exception):
    """A refused store layout. One exception for every row: same remedy, so a
    caller branching on WHICH would be branching on prose.

    ``config_key`` — the ``config.yaml`` key the editor paints, ``None`` between
    two env-derived paths. ``unusable_value`` — set on the two ONE-value
    refusals (would not resolve, cannot be stat'd), read by
    ``store_layout_report`` to drop a row the schema already painted: a
    ``directory:`` holding a NUL produced two rows saying the same thing.
    ``headline`` — the pair alone, which Apply's 422 is built from.
    """

    def __init__(
        self,
        message: str,
        *,
        config_key: str | None = None,
        unusable_value: bool = False,
        headline: str = "",
    ) -> None:
        super().__init__(message)
        self.config_key = config_key
        self.unusable_value = unusable_value
        self.headline = headline or message


#: What resolving an operator-supplied path can raise. Measured on this tree:
#: ``RuntimeError("Symlink loop from ...")`` for a self-referencing symlink on
#: Python 3.11 (what the image ships) and 3.12 (what the venv runs), and
#: ``ValueError("embedded null character")`` for a ``directory: "/music/\0evil"``,
#: which ruamel accepts. ``OSError`` covers the strict-mode shape 3.13 uses and
#: any I/O fault under the ``lstat`` chain.
_UNRESOLVABLE: Final = (OSError, RuntimeError, ValueError)

#: The ``stat`` failures that mean "this path is not there yet", which the rule
#: allows — none of the five has to exist. Every other errno leaves a path that
#: IS there in a form this process cannot examine, and :func:`_same_rung` then
#: has no inode to compare and falls back to string equality. Measured on this
#: tree: ``MUSICDROP_TRASH_DIR`` pointing at a symlink to the music library
#: inside a mode-000 directory was ALLOWED by :func:`check_store_layout`, and the
#: same layout with that directory traversable was refused.
_ABSENT_ERRNOS: Final = frozenset({errno.ENOENT, errno.ENOTDIR})


def _unresolvable(setting: str, raw: str, exc: Exception) -> StoreLayoutError:
    """The refusal for a path that will not resolve.

    A refusal rather than a traceback: a symlink loop as ``MUSICDROP_TRASH_DIR``
    reached the lifespan uncaught, so the operator got a stack trace and no
    "refusing to start" line, and the same value answered 500 at Validate/Save.
    """
    return StoreLayoutError(
        f"{setting} could not be resolved: {raw!r} raised {type(exc).__name__}: {exc}."
        " Fix the path.",
        config_key=_CONFIG_KEY_OF.get(setting),
        unusable_value=True,
        headline=f"{setting} could not be resolved",
    )


def _unexaminable(setting: str, resolved: str, exc: OSError) -> StoreLayoutError:
    """The refusal for a path the filesystem will not describe.

    Separate from :func:`_unresolvable` because the value DID resolve; what
    failed is the ``stat`` this module compares by.
    """
    return StoreLayoutError(
        f"{setting} could not be examined: {exc.strerror} at {resolved!r}."
        " Fix the path or its permissions.",
        config_key=_CONFIG_KEY_OF.get(setting),
        unusable_value=True,
        headline=f"{setting} could not be examined",
    )


def _resolved(path: Path, setting: str) -> Path:
    """Absolute, symlink-free, ``..``-free — the form this module compares.

    Also refuses a path that EXISTS and cannot be stat'd: ``Path.resolve()``
    re-raises only ELOOP, so a Trash behind a mode-000 directory came back as
    the string it was handed, was allowed at boot, and answered 500 at all four
    Trash routes. ENOENT and ENOTDIR pass — nothing has to exist yet.

    No ``expanduser``: none of the app's resolvers expands ``~`` and neither does
    pydantic-settings, so expanding it here made the rule check ``$HOME/bank``
    while the app used ``<cwd>/~/bank`` — measured, it allowed a Trash at the
    directory really in use and refused one at a directory nothing lived in.
    """
    resolved = _guarded(setting, str(path), lambda: Path(path).resolve())
    try:
        resolved.stat()
    except OSError as exc:
        if exc.errno not in _ABSENT_ERRNOS:
            raise _unexaminable(setting, str(resolved), exc) from exc
    return resolved


def _resolved_store(path: Path, setting: str) -> Path:
    """:func:`_resolved` for a store the rule PROTECTS rather than acts through.

    A permission bit or a symlink loop on the inbox, the bank, the Plex or slskd
    store, the playlist dirs or either image cache used to refuse startup and
    every destructive route. Those are victim-side directories: nothing here
    empties or sweeps them. Falling back to the spelling gives the same
    identity-less rung a not-yet-created path gets. ``T`` and ``O`` keep the
    fail-closed arm — the rule acts THROUGH those two.
    """
    try:
        return _resolved(path, setting)
    except StoreLayoutError:
        return path


def _guarded(setting: str, raw: str, resolve: Callable[[], Path]) -> Path:
    """Run a resolver and relay its failure as a refusal in the message shape.

    ``resolve_trash_dir`` and ``Path.resolve()`` raise on a symlink loop or an
    embedded NUL, outside every ``except StoreLayoutError`` in the app.
    """
    try:
        return resolve()
    except _UNRESOLVABLE as exc:
        raise _unresolvable(setting, raw, exc) from exc


def _stat_id(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` for a path this process can stat, ``None`` otherwise.

    The module's one seam onto the filesystem, so a test can answer for it
    without patching ``os.stat`` process-wide. ``None`` is "no identity to
    compare", not "absent" — :func:`_resolved` has already refused every errno
    outside :data:`_ABSENT_ERRNOS`, so it covers an absent path and an ancestor.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


#: One rung of a path's ancestry: what the filesystem calls it and how it is
#: spelled. Both, because either can be the only thing available — an inode when
#: two spellings are one directory (a bind mount, a case-insensitive volume), a
#: spelling when the path is not there yet and has no inode at all.
_Rung = tuple[tuple[int, int] | None, str]


def _chain(path: Path) -> tuple[_Rung, ...]:
    """``path`` and every ancestor, each as ``(identity, spelling)``.

    Built ONCE per participant. Every row below is then a comparison of tuples,
    so the cost is one ``stat`` per rung instead of one per row: the review round
    measured 91 stats for 12 rows on the shipped layout.
    """
    return tuple((_stat_id(rung), str(rung)) for rung in (path, *path.parents))


def _same_rung(a: _Rung, b: _Rung) -> bool:
    """Whether two rungs name ONE directory or file.

    ``resolve()`` does not collapse a bind mount, a case-insensitive filesystem
    or a unicode-normalising one, so the filesystem decides whenever both rungs
    have an inode: measured with a real bind mount, the two spellings compared
    as unrelated trees and Empty Trash removed the library. With no inode the
    spelling is all that is left — the residual an absent path leaves, caught on
    the next check and at destruction by ``app.beets.protected``.
    """
    if a[1] == b[1]:
        return True
    return a[0] is not None and a[0] == b[0]


def _relation_of(container: tuple[_Rung, ...], inner: tuple[_Rung, ...]) -> str | None:
    """``"is"``, ``"contains"`` or ``None``, from two chains.

    Containment walks ``inner``'s ancestors rather than testing one string
    prefix, because the container can be an ALIAS of an ancestor rather than that
    ancestor's own spelling: a bind mount of ``<M>``'s parent onto the Trash path
    leaves the two endpoints with different inodes, and Empty Trash then removed
    both the music dir and the beets dir. The spelling half of :func:`_same_rung`
    is what keeps ``/data/trash`` from reading as containing
    ``/data/trash-origins`` the way a prefix test would.
    """
    if _same_rung(container[0], inner[0]):
        return "is"
    return "contains" if any(_same_rung(container[0], rung) for rung in inner[1:]) else None


def _refuse(
    *,
    subject: str,
    relation: str,
    other: str,
    setting: str,
    subject_path: Path,
    other_setting: str,
    other_path: Path,
    loss: str,
    fix: str,
) -> StoreLayoutError:
    """Compose the one message shape, so boot, Save, Validate and Apply agree.

    ``<Subject> <relation> <other> - <loss>. <SETTING>: <path>; <other>: <path>.
    <Fix>`` - one line in ``docker logs``, one paragraph on the Trash page (owner
    ruling 2026-09-04). Both paths go through ``repr``, against one holding a
    newline forging a second log line; echoing them at all is the trade a
    single-operator app makes, and the same session can read them from
    ``GET /api/config``.
    """
    return StoreLayoutError(
        f"{subject} {relation} {other} — {loss}. "
        f"{setting}: {str(subject_path)!r}; {other_setting}: {str(other_path)!r}. {fix}",
        headline=f"{subject} {relation} {other}",
        # Either side of the pair can be the config.yaml key; the subject is
        # asked first because that is the setting the message tells the operator
        # to move, and the gutter should mark the line the sentence is about.
        config_key=_CONFIG_KEY_OF.get(setting) or _CONFIG_KEY_OF.get(other_setting),
    )


#: The remedy each refusal ends with: FIXED strings, one per setting a row tells
#: the operator to move (owner ruling 2026-09-04).
_FIX_TRASH: Final = "Set MUSICDROP_TRASH_DIR to its own folder."
_FIX_ORIGINS: Final = "Set MUSICDROP_TRASH_ORIGINS_DIR to its own folder."
_FIX_BEETS: Final = "Move MUSICDROP_BEETS_DIR out of the music library."
_FIX_MUSIC: Final = "Point `directory:` outside the beets data directory."
_FIX_LIBRARY_TRASH: Final = "Move `library:` out of the Trash."
_FIX_LIBRARY_ORIGINS: Final = "Move `library:` out of the Trash origin store."


class _Participant(NamedTuple):
    """A path the rule is about: where it resolved, what to call it, what spells it."""

    path: Path
    name: str
    setting: str


class _Row(NamedTuple):
    """One refused relationship, as data.

    ``container`` and ``inner`` are keys into the participant map; ``relations``
    is which answers of :func:`_relation_of` the row refuses. Both of them for a
    pair that may not overlap at all, one for a pair whose other direction is
    allowed on purpose (``T`` strictly inside ``M`` is the owner's ruling).
    """

    container: str
    inner: str
    relations: tuple[str, ...]
    loss: str
    fix: str


#: THE rule, in the order it is asked. Order decides only WHICH message a layout
#: breaking several rows gets: ``B`` against ``M`` first, ``L``'s rows last.
_ROWS: Final[tuple[_Row, ...]] = (
    _Row(
        "beets",
        "music",
        ("is",),
        "the sweep would offer the app's own folders for trashing",
        _FIX_BEETS,
    ),
    _Row(
        "music",
        "beets",
        ("contains",),
        "a sweep or a folder delete could move library.db and config.yaml",
        _FIX_BEETS,
    ),
    _Row(
        "beets",
        "music",
        ("contains",),
        "the sweep would run with none of the app-owned exclusions",
        _FIX_MUSIC,
    ),
    _Row(
        "trash",
        "music",
        ("is", "contains"),
        "emptying it would delete the music library",
        _FIX_TRASH,
    ),
    _Row(
        "trash",
        "beets",
        ("is", "contains"),
        "Empty Trash would delete library.db and config.yaml",
        _FIX_TRASH,
    ),
    _Row(
        "origins",
        "music",
        ("is", "contains"),
        "the store sweep would unlink *.json files in the music library",
        _FIX_ORIGINS,
    ),
    _Row(
        "origins",
        "beets",
        ("is", "contains"),
        "the store sweep would unlink *.json files in the beets data directory",
        _FIX_ORIGINS,
    ),
    _Row(
        "music",
        "origins",
        ("contains",),
        "a folder delete above the store would trash the restore records",
        _FIX_ORIGINS,
    ),
    _Row(
        "trash",
        "origins",
        ("is", "contains"),
        "Empty Trash would delete the restore records",
        _FIX_ORIGINS,
    ),
    _Row(
        "origins",
        "trash",
        ("contains",),
        "trashed albums would land among the restore records",
        _FIX_ORIGINS,
    ),
    _Row(
        "trash",
        "library",
        ("is", "contains"),
        "Empty Trash would delete library.db and the backups beside it",
        _FIX_LIBRARY_TRASH,
    ),
    _Row(
        "origins",
        "library",
        ("is", "contains"),
        "library.db would sit in a folder the store sweep prunes",
        _FIX_LIBRARY_ORIGINS,
    ),
)

#: D2 - the Trash and the origin store are DEDICATED directories: neither may BE
#: an app-owned store nor CONTAIN one. Measured in the review round:
#: ``MUSICDROP_TRASH_DIR=<B>/plex`` (or ``playlists``, ``bank``, ``slskd``,
#: ``inbox``) booted clean and Empty Trash wiped that store. Generated per store
#: rather than a hand-written block each, so another store is one entry in
#: ``config.APP_STORES`` and nothing here — the playlist exports and the two
#: image caches take their own resolvers, since neither defaults under ``B``.
#:
#: The other direction — T or O sitting INSIDE a store — is deliberately not a
#: row. It refused layouts the module allows (a store setting naming a directory
#: ABOVE the shipped default Trash turned a clean boot into a refusal), and its
#: loss clause was false for every store but the inbox: nothing enumerates the
#: exports, the Plex or slskd store, and the bank and playlist stores glob
#: ``*.json`` one level deep.
#:
#: ``(participant key, cost of holding a store, fix)``.
_DEDICATED: Final[tuple[tuple[str, str, str], ...]] = (
    ("trash", "Empty Trash would delete it", _FIX_TRASH),
    ("origins", "the store sweep would unlink *.json files in it", _FIX_ORIGINS),
)

#: The participant keys of the five paths the rule started with. Everything else
#: in the map is an app-owned store, and :func:`_store_rows` generates for it.
_FIVE: Final = frozenset({"music", "beets", "trash", "origins", "library"})


def _store_rows(stores: tuple[str, ...]) -> tuple[_Row, ...]:
    """D2's rows: two per app-owned store the settings name."""
    return tuple(
        _Row(subject, key, ("is", "contains"), holds, fix)
        for key in stores
        for subject, holds, fix in _DEDICATED
    )


def _refuse_a_library_that_is_a_directory(library: Path) -> None:
    """``library:`` names the beets DATABASE FILE, so a directory cannot open.

    Measured: a hand-edited ``library:`` naming a directory (or empty, which
    resolves to the beets dir) answered Apply's 500 "unable to open database
    file" with "cold start will load it" — and the cold start died on the same
    file. Here it is a 422 that names the key instead.
    """
    if not library.is_dir():
        return
    raise StoreLayoutError(
        f"{LIBRARY_SETTING} is a directory: {str(library)!r}. It has to name the"
        " beets database file. Fix the path.",
        config_key=_CONFIG_KEY_OF[LIBRARY_SETTING],
        unusable_value=True,
        headline=f"{LIBRARY_SETTING} is a directory",
    )


def check_store_layout(
    *,
    music_dir: Path,
    beets_dir: Path,
    trash_dir: Path,
    origins_dir: Path,
    library_path: Path,
    settings: Settings,
) -> None:
    """Raise :class:`StoreLayoutError` on any refused relationship among the paths.

    The paths are resolved HERE rather than by the caller, which is what keeps a
    caller from comparing a lexical spelling against a resolved one — pinned by
    ``test_a_symlinked_music_root_is_still_refused_when_trash_is_the_real_dir``.
    ``settings`` brings the app-owned stores in as participants (D2), required
    rather than defaulted so a new call site cannot silently lose those rows.

    Raises:
        StoreLayoutError: a refused relation, or a path that would not resolve.
    """
    participants = {
        "music": _Participant(
            _resolved(music_dir, MUSIC_SETTING), "the music library", MUSIC_SETTING
        ),
        "beets": _Participant(
            _resolved(beets_dir, BEETS_SETTING), "the beets data directory", BEETS_SETTING
        ),
        "trash": _Participant(
            _resolved(trash_dir, TRASH_SETTING), "the Trash directory", TRASH_SETTING
        ),
        "origins": _Participant(
            _resolved(origins_dir, ORIGINS_SETTING), "the Trash origin store", ORIGINS_SETTING
        ),
        "library": _Participant(
            _resolved(library_path, LIBRARY_SETTING), "the beets database", LIBRARY_SETTING
        ),
    }
    _refuse_a_library_that_is_a_directory(participants["library"].path)
    music_root, beets_root = participants["music"].path, participants["beets"].path
    stores: list[tuple[Path, str, str]] = [
        (export_dir(settings, music_root), "the playlist exports", EXPORT_SETTING),
        *app_owned_dirs(settings, beets_root),
    ]
    for path, name, setting in stores:
        participants[setting] = _Participant(_resolved_store(path, setting), name, setting)

    # One chain per participant, not one per row.
    chains = {key: _chain(who.path) for key, who in participants.items()}
    store_keys = tuple(key for key in participants if key not in _FIVE)
    for row in (*_ROWS, *_store_rows(store_keys)):
        relation = _relation_of(chains[row.container], chains[row.inner])
        if relation not in row.relations:
            continue
        container, inner = participants[row.container], participants[row.inner]
        raise _refuse(
            subject=container.name[0].upper() + container.name[1:],
            relation=relation,
            other=inner.name,
            setting=container.setting,
            subject_path=container.path,
            other_setting=inner.setting,
            other_path=inner.path,
            loss=row.loss,
            fix=row.fix,
        )


def lib_music_and_library(lib: Any) -> tuple[Path, Path]:
    """``(M, L)`` off an open ``Library``: music root, DB file.

    One owner for the two beets attributes this app reads off a ``Library``, so
    they are named once and stay inside the adapter boundary (CLAUDE.md rule 3).
    ``os.fsdecode`` is applied here so a bytes path from an older beets still
    lands as a ``Path``.
    """
    return Path(_music_dir(lib)), Path(os.fsdecode(lib.path))


def checked_store_dirs(settings: Settings, handle: LibraryHandle) -> tuple[Path, Path]:
    """The ``(trash_dir, origins_dir)`` pair, checked at the moment of use.

    THE call every taker of the pair makes instead of the two resolvers, and it
    returns exactly what they return. Per call, not once at boot: the configured
    STRING is fixed for the process lifetime and what it resolves to is not —
    replacing ``<M>/.trash`` with a symlink to ``<M>`` after startup turned
    ``DELETE /api/trash/all`` into a 200 that emptied the music library.

    Raises:
        StoreLayoutError: refused, or a path would not resolve; request paths
            answer 503 with the message.
    """
    trash, origins = _resolve_store_dirs(settings, handle)
    music, library = lib_music_and_library(handle.lib)
    check_store_layout(
        music_dir=music,
        beets_dir=handle.beets_dir,
        trash_dir=trash,
        origins_dir=origins,
        library_path=library,
        settings=settings,
    )
    return trash, origins


# The four Trash-chain refusals below carry no ``config_key``: the value at
# fault is ``MUSICDROP_TRASH_DIR``, which is env-derived and not a
# ``config.yaml`` key the editor can paint. ``store_layout_report`` reaches them
# through :func:`_check_trash_is_reachable` and falls back to ``directory:``,
# which is the line the editor can act from — so this is deliberate rather than
# an omission to "fix".
def _refuse_an_unreachable_trash(spelled: Path, exc: OSError) -> StoreLayoutError:
    """The refusal for a Trash path whose own chain below the music root is not
    walkable — a symlinked component, or a file in the way.

    Measured: ``O_DIRECTORY|O_NOFOLLOW`` answers ENOTDIR for a link AND for a
    plain file, so the errno cannot say which; the wording matches
    ``lyrics._album_dir_fd``'s for the same ambiguity.
    """
    return StoreLayoutError(
        f"{TRASH_SETTING} is not reachable below the music library:"
        f" {str(spelled)!r} ({exc.strerror} — a link or a file in the way)."
        " Bind mounts are the supported spelling for a folder on another disk.",
        headline=f"{TRASH_SETTING} is not reachable below the music library",
    )


def _refuse_an_uncreatable_trash(spelled: Path, exc: OSError) -> StoreLayoutError:
    """The refusal for a Trash directory that could not be created at all.

    A refusal here rather than an absent identity downstream: every mover would
    then refuse with the remover's wording ("could not be examined"), which does
    not say that the directory is missing or why.
    """
    return StoreLayoutError(
        f"{TRASH_SETTING} could not be created: {str(spelled)!r} ({exc.strerror})."
        " Fix its permissions or its mount.",
        headline=f"{TRASH_SETTING} could not be created",
    )


def _refuse_a_climbing_trash_spelling(configured: str) -> StoreLayoutError:
    """The refusal for a configured Trash holding a ``..`` part.

    ``os.path.normpath`` collapses ``..`` lexically and the kernel does not, so a
    ``..`` that crosses a symlinked component names a different directory than
    the spelling reads as. Measured 2026-09-12 (code seat, probe D) on the arm
    this replaces: a configured ``<M>/a/../b/.trash`` with ``a`` swapped for a
    link created a stray ``<M>/b/.trash`` and reported success while the Trash
    the rest of the app resolves stayed absent. One refusal instead of two
    directories.
    """
    return StoreLayoutError(
        f"{TRASH_SETTING} may not contain '..': {configured!r}. Spell the path without it.",
        headline=f"{TRASH_SETTING} may not contain '..'",
    )


def _refuse_a_trash_around_the_music_root(spelled: Path) -> StoreLayoutError:
    """The refusal for a Trash that lands inside the library without naming it.

    A link the operator owns can point BELOW the music root (``/srv/x ->
    <M>/a``): the walk never meets the root's identity, so it would create the
    Trash inside the library through a followed chain, without the anchoring the
    owner's layout ruling makes necessary there. Refused rather than anchored
    because one spelling of the root is all the app has to support.
    """
    return StoreLayoutError(
        f"{TRASH_SETTING} reaches into the music library without naming it:"
        f" {str(spelled)!r}. Spell it through beets' `directory:`.",
        headline=f"{TRASH_SETTING} reaches into the music library without naming it",
    )


def _refuse_an_unopenable_music_root(music_dir: Path, exc: OSError) -> StoreLayoutError:
    """The refusal for a Trash below a music root that will not open at all.

    The ROOT and not the Trash: a dropped share answers ENOENT here, and naming
    ``MUSICDROP_TRASH_DIR`` sends the operator to the wrong setting. Measured
    2026-09-12 (security seat L-2, code seat W3): on the README's own
    ``<M>/.trash`` layout with the share down, the delete route answered
    "MUSICDROP_TRASH_DIR could not be created" where it used to name the mount.
    """
    return StoreLayoutError(
        f"{MUSIC_SETTING} could not be opened: {str(music_dir)!r} ({exc.strerror})."
        " Is the music share mounted?",
        config_key=_CONFIG_KEY_OF[MUSIC_SETTING],
        headline=f"{MUSIC_SETTING} could not be opened",
    )


def _checked_trash_spelling(configured: str, trash_dir: Path) -> Path:
    """The absolute path the walk descends, one component at a time.

    The CONFIGURED value and not the resolved one: ``resolve_trash_dir``
    collapses links, so a Trash at ``<M>/a/b/.trash`` whose ``a`` was swapped for
    a symlink RESOLVES outside the library and would read as "not below it" —
    exactly the shape the anchored walk exists to refuse (measured, security seat
    M-3). An empty setting is the shipped ``<beets_dir>/trash``, which has no
    configured spelling, so the resolved value is walked instead.

    ``absolute`` because a relative setting is cwd-relative (the gotcha
    ``config.py`` names) and ``normpath`` because ``os.open`` takes one component
    at a time; a ``..`` is refused rather than collapsed.

    Raises:
        StoreLayoutError: the configured spelling holds a ``..`` part.
    """
    if not configured:
        return Path(os.path.normpath(trash_dir.absolute()))
    if ".." in Path(configured).absolute().parts:
        raise _refuse_a_climbing_trash_spelling(configured)
    return Path(os.path.normpath(Path(configured).absolute()))


def _fstat_ident(fd: int) -> tuple[int, int]:
    """The ``(st_dev, st_ino)`` of whatever ``fd`` is open on."""
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _music_root_ident(music_dir: Path, spelled: Path) -> tuple[int, int] | None:
    """The music root's identity, or ``None`` when it will not open.

    ``open_root`` FOLLOWS a link at the root, because an operator's beets
    ``directory:`` may be one — so the identity is the directory beets indexes,
    whichever of its spellings the Trash names.

    Raises:
        StoreLayoutError: the root will not open and the Trash is spelled below
            it, which is the dropped share the operator has to hear about. With
            no identity there is no other evidence than the spelling, which is
            why this one decision is lexical.
    """
    try:
        fd = open_root(music_dir)
    except OSError as exc:
        if _is_spelled_below(spelled, music_dir):
            raise _refuse_an_unopenable_music_root(music_dir, exc) from exc
        return None
    try:
        return _fstat_ident(fd)
    finally:
        os.close(fd)


def _is_spelled_below(spelled: Path, music_dir: Path) -> bool:
    """Whether ``spelled`` is LEXICALLY below ``music_dir``."""
    try:
        return bool(spelled.relative_to(music_dir).parts)
    except ValueError:
        return False


def _reaches_the_music_root(fd: int, root_ident: tuple[int, int]) -> bool:
    """Whether the directory ``fd`` is open on is the music root or sits below it.

    A ``..`` climb through descriptors, because the question is about the
    directory the walk really reached and not about its spelling: the chain above
    the root is followed, so a link there can land inside the library with no
    component of the spelling naming it. ``/`` is its own parent, which is the
    stop condition; a chain this process cannot climb answers "no", the same way
    ``protected._note_walk_error`` treats a directory it cannot read.
    """
    here, here_ident = fd, _fstat_ident(fd)
    if here_ident == root_ident:
        return True
    climbed: int | None = None
    try:
        while True:
            try:
                up = os.open("..", ROOT_FLAGS, dir_fd=here)
            except OSError:
                return False
            up_ident = _fstat_ident(up)
            if climbed is not None:
                os.close(climbed)
            climbed = up
            if up_ident == root_ident:
                return True
            if up_ident == here_ident:
                return False
            here, here_ident = up, up_ident
    finally:
        if climbed is not None:
            os.close(climbed)


def _step_into(fd: int, part: str, *, below: bool, create: bool, spelled: Path) -> int:
    """One component of the walk: created when ``create``, then opened from ``fd``.

    ``BELOW_FLAGS`` once the walk is inside the music library, so a link or a file
    at the part is refused instead of followed; ``ROOT_FLAGS`` above it, which is
    the operator's own chain (``fsutil.ROOT_FLAGS``).
    """
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(part, dir_fd=fd)
    try:
        return os.open(part, BELOW_FLAGS if below else ROOT_FLAGS, dir_fd=fd)
    except OSError as exc:
        if below and exc.errno in (errno.ENOTDIR, errno.ELOOP):
            raise _refuse_an_unreachable_trash(spelled, exc) from exc
        raise


def _open_the_trash_chain(*, music_dir: Path, spelled: Path, create: bool) -> int:
    """A descriptor on the Trash, decided by IDENTITY component by component.

    From ``/`` down, one ``os.open`` per part. Above the music root the parts are
    the OPERATOR's — a symlinked ``directory:``, a ``/srv`` that is a link — so
    they are opened following links; the moment an opened part's ``(st_dev,
    st_ino)`` IS the music root's, every further part is opened ``BELOW_FLAGS``
    and created through its parent's descriptor, because that is the chain the
    owner's layout ruling leaves attacker-writable.

    Identity and not spelling, measured 2026-09-12 (security seat H-2, code seat
    W1): the two settings can name one root two ways — a Trash under an ALIAS of
    ``directory:``, or a ``directory:`` that is a link with the Trash spelled
    through its target — and a lexical ``relative_to`` then answered "not below
    the music root" for a Trash that really was inside it, skipping the anchored
    walk entirely and following the attacker's link.

    What already exists is walked BEFORE anything is created, so a spelling that
    reaches into the library through a link the walk never identifies is refused
    with nothing left behind.

    ``create`` is ``False`` for the report's read-only form, which stops at the
    first part that is not there yet: none of the five paths has to exist.

    The returned fd is the caller's to close.

    Raises:
        StoreLayoutError: a part below the music root is not a directory, or the
            spelling reaches into the library without naming it.
        OSError: any other fault the walk met; the caller words it.
    """
    root_ident = _music_root_ident(music_dir, spelled)
    parts = spelled.parts[1:]
    fd = os.open("/", ROOT_FLAGS)
    try:
        below, walked = False, 0
        for part in parts:
            try:
                opened = _step_into(fd, part, below=below, create=False, spelled=spelled)
            except FileNotFoundError:
                break
            os.close(fd)
            fd = opened
            walked += 1
            if not below and root_ident is not None and _fstat_ident(fd) == root_ident:
                below = True
        if not below and root_ident is not None and _reaches_the_music_root(fd, root_ident):
            raise _refuse_a_trash_around_the_music_root(spelled)
        if create:
            for part in parts[walked:]:
                opened = _step_into(fd, part, below=below, create=True, spelled=spelled)
                os.close(fd)
                fd = opened
        return fd
    except BaseException:
        os.close(fd)
        raise


def _ensure_trash_root(settings: Settings, *, music_dir: Path, trash_dir: Path) -> tuple[int, int]:
    """Create the Trash directory and answer the identity of what was opened.

    The identity comes from ``fstat`` on the descriptor the walk itself reached,
    never from a second resolve by name: measured (security seat M-1), the
    by-name stat ran 18 µs after the creation and a real racer won that window
    537 times in 100 876 requests, and a won window put that request's files
    outside the library and pointed ``empty_all``'s ``rmtree`` at a directory of
    the attacker's choosing. The mover's own open must now land on the directory
    this walk reached or be refused.

    Created here, one line before the identity is taken, because a mover handed
    ``protected.trash is None`` has nothing to compare and the arm that created
    the Trash itself opened it by PATH: measured (security seat M-3),
    ``mkdir(parents=True)`` plus a leaf-only ``O_NOFOLLOW`` followed a symlink at
    an INTERMEDIATE component of ``<M>/a/b/.trash``, the files left the library,
    and every later request stat'd and opened the relocation through the same
    link and agreed with it.

    A real directory a stranger already left at the configured path is accepted:
    that is the attacker owning the Trash's location, which no check here can
    undo.

    Raises:
        StoreLayoutError: the spelling climbs or reaches into the library without
            naming it, the Trash is not reachable below the music root, or it
            could not be created.
    """
    spelled = _checked_trash_spelling(settings.trash_dir, trash_dir)
    try:
        fd = _open_the_trash_chain(music_dir=music_dir, spelled=spelled, create=True)
    except OSError as exc:
        raise _refuse_an_uncreatable_trash(spelled, exc) from exc
    try:
        return _fstat_ident(fd)
    finally:
        os.close(fd)


def _check_trash_is_reachable(*, music_dir: Path, settings: Settings, trash_dir: Path) -> None:
    """Raise the refusal a destructive request would, without creating anything.

    ``store_layout_report`` painted Settings from the rows alone and the
    reachability walk lives at the destructive call sites, so a Trash below the
    music root through a symlinked component read HEALTHY where the operator
    configures it while every delete, restore and Empty-Trash answered 503
    (security seat L-3). Same walk and the same messages, no ``mkdir``.

    Silent for a part that is simply not there yet — none of the five paths has
    to exist — and silent for one it cannot open for any other reason: the
    destructive routes word an EACCES with the errno in hand, and a report that
    refused on a permission bit would paint a row the operator cannot act on.

    Raises:
        StoreLayoutError: the spelling climbs or reaches into the library without
            naming it, or a part below the music root is not a directory.
    """
    spelled = _checked_trash_spelling(settings.trash_dir, trash_dir)
    try:
        fd = _open_the_trash_chain(music_dir=music_dir, spelled=spelled, create=False)
    except OSError:
        return
    os.close(fd)


def checked_protected_trees(
    settings: Settings, handle: LibraryHandle, *, trash_dir: Path, origins_dir: Path
) -> ProtectedTrees:
    """The identities the movers and the remover refuse, for THIS request.

    Taken beside :func:`checked_store_dirs`, from the pair it returned, by the
    request sites that hand a mover or the remover an identity set: the delete
    ops, the Trash page's DELETE routes, and the artist-art store. The orphan
    sweep reads ``protected_entries`` directly for its own ignore list, and
    ``trash_album``'s callers pass no set at all (a recorded residual). Separate
    from that call because every OTHER caller of it only reads the pair and would
    pay a dozen stats for nothing.

    The Trash is CREATED here when it is absent (:func:`_ensure_trash_root`), so
    no mover sees ``protected.trash is None`` and none has to create it by path.
    It is the same directory the movers already ``mkdir`` themselves, and a
    stranger's pre-existing directory at the configured path is accepted either
    way, so what moves is only WHERE the creation happens. The Trash's identity
    is that call's ``fstat`` on the descriptor its walk reached, not a second
    resolve of the same name — the window between the two was winnable (security
    seat M-1).

    Raises:
        StoreLayoutError: the Trash could not be created below the music root.
    """
    music, library = lib_music_and_library(handle.lib)
    trash_ident = _ensure_trash_root(settings, music_dir=music, trash_dir=trash_dir)
    return protected_trees(
        settings=settings,
        music_dir=music,
        beets_dir=handle.beets_dir,
        trash_dir=trash_dir,
        origins_dir=origins_dir,
        trash_ident=trash_ident,
        library_path=library,
    )


def _resolve_store_dirs(settings: Settings, handle: LibraryHandle) -> tuple[Path, Path]:
    """The Trash / origin-store pair, with a resolve failure relayed as a refusal.

    Both resolvers call ``Path.resolve()`` on the configured value, which raises
    on a symlink loop or an embedded NUL — outside every ``except
    StoreLayoutError`` in the app. Wrapping them here means one message shape for
    "this value is unusable" and for "this value is dangerous".
    """
    trash = _guarded(TRASH_SETTING, settings.trash_dir, lambda: resolve_trash_dir(settings, handle))
    origins = _guarded(
        ORIGINS_SETTING,
        settings.trash_origins_dir,
        lambda: resolve_trash_origins_dir(settings, handle),
    )
    return trash, origins


class _CandidateConfig(beets.IncludeLazyConfig):
    """beets' own config class, over a document that is not on disk yet.

    Two departures from ``beets.config``: ``config_dir`` is pinned to the
    handle's beets dir rather than read from ``BEETSDIR``, so a relative
    ``directory:``, ``library:`` or ``include:`` resolves against the directory
    THIS handle uses; and the user source is the SUBMITTED document.

    Constructing one touches no beets global — ``Configuration.__init__`` records
    three strings and calls ``RootView.__init__([])``, so every source it holds
    is its own list (``confuse/core.py:504-537``).
    """

    def __init__(self, beets_dir: Path) -> None:
        super().__init__("beets", "beets")
        self._pinned_config_dir = str(beets_dir)

    def config_dir(self) -> str:
        return self._pinned_config_dir


#: The bytes ONE request may read across the WHOLE ``include:`` list, not per
#: entry: a per-entry cap bounds a factor, and the list is what multiplies it.
#: Measured with the cap per entry: a 2,964-byte body naming one 1 MiB include 25
#: times took 12.5 seconds of threadpool CPU (0.5 s per MiB, which is ruamel
#: parsing); ``max_body_bytes`` is 25 MiB, so the list itself bounds nothing.
#: beets' own starter config is under 4 KiB. Measured at this budget: 32
#: line-dense includes summing to 1 MiB answer in 1.5 s.
_MAX_INCLUDE_BYTES: Final = 1 << 20

#: The entries ONE request may read, counted on the raw list before any of them
#: is resolved. beets' own config carries 0-2 includes and upstream caps nothing,
#: so this is generous by an order of magnitude. A repeated entry is read once
#: (:func:`effective_config_paths` reads a resolved path once) and still counts
#: here, because counting after the resolve means resolving an unbounded list.
_MAX_INCLUDE_ENTRIES: Final = 32


def _unreadable_include(detail: str) -> StoreLayoutError:
    """The refusal for an ``include:`` the gate could not follow.

    Painted against the ``include:`` key, so the editor's gutter marks the line
    the operator has to change.
    """
    return StoreLayoutError(
        f"`include:` in config.yaml could not be read: {detail}. Fix the include: list.",
        config_key="include",
        unusable_value=True,
        headline="`include:` in config.yaml could not be read",
    )


def _too_many_includes(count: int) -> StoreLayoutError:
    """The refusal for an ``include:`` list this gate will not read whole."""
    return StoreLayoutError(
        f"`include:` in config.yaml lists {count} files; the limit is"
        f" {_MAX_INCLUDE_ENTRIES}. Shorten the include: list.",
        config_key="include",
        unusable_value=True,
        headline=f"`include:` in config.yaml lists more than {_MAX_INCLUDE_ENTRIES} files",
    )


def _include_sets_a_non_path(key: str, source: str) -> StoreLayoutError:
    """An include gave ``directory:``/``library:`` a value that is not a filename.

    Painted against ``include:``, because the document's own key is fine and the
    editor never shows the overlay. Without a row, Validate was clean, Save wrote
    the file, and the next cold start died on ``ConfigTypeError``.
    """
    return StoreLayoutError(
        f"`{key}:` in {source!r} is not a path. beets will not start. Fix that include.",
        config_key="include",
        unusable_value=True,
        headline=f"`{key}:` in {source!r} is not a path",
    )


def _winning_source(cfg: confuse.Configuration, key: str) -> str | None:
    """The file whose value for ``key`` beets would use, or ``None``."""
    for _value, source in cfg[key].resolve():
        name = getattr(source, "filename", None)
        return str(name) if name else None
    return None


def _include_bytes(fd: int, budget: int) -> bytes | None:
    """Up to ``budget`` bytes from ``fd``, or ``None`` past it.

    The budget bounds the READ, not ``st_size``: every ``/proc`` file reports
    size 0 and ``/proc/kallsyms`` measured 22 MiB through a 1 MiB stat check.
    """
    buf = b""
    while len(buf) <= budget:
        chunk = os.read(fd, budget + 1 - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return None


def _include_source(target: str, budget: int) -> tuple[confuse.ConfigSource, int]:
    """One ``include:`` entry, read through ONE descriptor. With its size.

    ``os.stat`` then confuse's ``open`` asked the same NAME twice, and flipping a
    symlink between the two put the FIFO hang back — measured, the read ran until
    a 3-second alarm. ``O_NONBLOCK`` is what makes a writer-less FIFO answer at
    all. No ``O_NOFOLLOW``: measured, a real beets start FOLLOWS a symlinked
    include and merges its ``directory:``.

    ``budget`` is what is left of :data:`_MAX_INCLUDE_BYTES` for this request,
    and the size comes back so the caller can subtract it.

    Raises ``ConfigReadError`` for the shapes beets prints-and-continues on, and
    :func:`_unreadable_include` for the three it does not survive: a FIFO, a
    descriptor with nothing to read, and a read the budget stops.
    """
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise confuse.ConfigReadError(target, exc) from exc
    try:
        if stat.S_ISFIFO(os.fstat(fd).st_mode):
            # The one shape ``O_NONBLOCK`` hides rather than fixes: with no
            # writer, ``read`` answers EOF, so this would report an empty
            # overlay for a file beets BLOCKS on at startup. The type test is
            # narrow on purpose — a directory, a socket and ``/dev/null`` are
            # shapes beets survives, and refusing those was the collateral.
            raise _unreadable_include(f"{target!r} is a FIFO; beets would block on it")
        buf = _include_bytes(fd, budget)
    except BlockingIOError as exc:
        raise _unreadable_include(f"{target!r} had nothing to read") from exc
    except OSError as exc:
        # EISDIR for a directory: beets' own ``open`` answers the same and its
        # ``ConfigReadError`` arm carries on.
        raise confuse.ConfigReadError(target, exc) from exc
    finally:
        os.close(fd)
    if buf is None:
        raise _unreadable_include(
            f"{target!r} takes the include: list over its {_MAX_INCLUDE_BYTES}-byte budget"
        )
    data = confuse.yaml_util.load_yaml_string(buf, target) or {}
    if not isinstance(data, dict):
        # What ``YamlSource.load`` raises for the same document, so a beets start
        # over this file refuses too.
        raise TypeError(f"YAML config must be a mapping, got {type(data)}")
    return confuse.ConfigSource(data, filename=os.path.abspath(target)), len(buf)


class EffectivePaths(NamedTuple):
    """What beets would load, and the ``include:`` entries the gate skipped."""

    directory: str | None
    library: str | None
    skipped: tuple[str, ...] = ()


def effective_config_paths(document: Mapping[str, Any], beets_dir: Path) -> EffectivePaths:
    """The ``directory:`` and ``library:`` beets would LOAD from ``document``.

    Not ``document["directory"]``: beets merges every ``include:`` file at
    HIGHEST priority (``beets/__init__.py:29-38``, ``confuse/core.py:617``), so
    an included ``directory:`` overrides the key the editor shows. Measured in
    the review round: a safe document with an include pointing the library at the
    Trash passed all three gates.

    ``directory``/``library`` are ``None`` when the DOCUMENT's own key resolves to
    no filename — a non-string ``directory:``, say — which the schema reports
    instead. When an INCLUDE is what supplied it the schema never sees the value,
    so this raises a row painted on ``include:``. ``skipped`` names the includes
    beets drops and this gate did not merge; an advisory, because beets prints
    them and carries on.
    """
    cfg = _CandidateConfig(beets_dir)
    # Defaults first, so the document sits ABOVE them: `library: library.db` and
    # `directory: ~/Music` are beets' own (`beets/config_default.yaml:3-4`), and
    # a document that drops either key is held to the value beets would then use.
    cfg.read(user=False, defaults=True)
    cfg.set(confuse.ConfigSource(dict(document), filename=str(beets_dir / "config.yaml")))
    # The loop ``IncludeLazyConfig.read`` runs after reading, reproduced here
    # because we are replacing the user source rather than reading it: each entry
    # is ``set_file``'d, which inserts it at the FRONT, so the last include wins.
    #
    # Includes are NOT confined to the beets dir. beets does not confine them, and
    # a gate that refused a config beets loads would be worse than the read this
    # exposes — which is bounded by the session gate, by the read budget, and by
    # the rows below, which narrow it to "this file parses as a mapping" rather
    # than "here is its content".
    skipped: list[str] = []
    # One read per resolved path, so a repeated entry costs one. The entry is
    # still ``set`` again at its own position: the LAST include wins, so dropping
    # the repeat would change which file decides ``directory:``.
    read: dict[str, confuse.ConfigSource] = {}
    budget = _MAX_INCLUDE_BYTES
    try:
        entries = list(cfg["include"].sequence())
        if len(entries) > _MAX_INCLUDE_ENTRIES:
            raise _too_many_includes(len(entries))
        for view in entries:
            # Resolved HERE rather than up front: each entry resolves against
            # the sources set so far, which is what beets' own loop does.
            target = view.as_filename()
            merged = read.get(target)
            if merged is None:
                merged, used = _include_source(target, budget)
                budget -= used
                read[target] = merged
            cfg.set(merged)
    except confuse.NotFoundError:
        pass  # no ``include:`` key at all
    except confuse.ConfigReadError as exc:
        # beets writes one stderr line and carries on, with the ``except`` OUTSIDE
        # the loop (``beets/__init__.py:29-38``), so the first unreadable entry ends
        # the merge. Measured with the guard placed per-entry instead, this function
        # reported an overlay's ``directory:`` that a real ``setup_beets`` over the
        # same file did not load.
        skipped.append(exc.name)
    except (confuse.ConfigError, TypeError, ValueError, RecursionError) as exc:
        # The shapes a real start does not survive: a non-list ``include:``, an
        # include whose top level is not a mapping, an entry holding a NUL, an
        # include nested past the recursion limit. Measured, all four escaped the
        # old ``except confuse.ConfigError`` and the three routes answered a bare
        # 500 or reported the document CLEAN.
        raise _unreadable_include(str(exc)) from exc
    names = tuple(skipped)
    document_file = str(beets_dir / "config.yaml")
    resolved: dict[str, str] = {}
    for key in ("directory", "library"):
        try:
            resolved[key] = cfg[key].as_filename()
        except confuse.ConfigError as exc:
            source = _winning_source(cfg, key)
            if source is not None and source != document_file:
                raise _include_sets_a_non_path(key, source) from exc
            # The document's OWN key: the schema paints that one, and two rows
            # saying the same thing was the collateral of reporting it here.
            return EffectivePaths(None, None, names)
    return EffectivePaths(resolved["directory"], resolved["library"], names)


class LayoutCheck(NamedTuple):
    """A candidate document's refusal, and the includes the gate skipped."""

    error: StoreLayoutError | None
    skipped_includes: tuple[str, ...] = ()


def layout_check_for_config(
    *,
    document: Mapping[str, Any],
    settings: Settings,
    handle: LibraryHandle,
) -> LayoutCheck:
    """The refusal a candidate ``config.yaml`` document would cause, or ``None``.

    Returns rather than raises: its callers (Save, Validate, Apply) turn it into
    a response body, and every one of them wants the message rather than a
    traceback. ``B``, ``T`` and ``O`` come from the LIVE settings and handle:
    they are env-derived, and the two values that move through the editor are the
    two :func:`effective_config_paths` reads back out of the document.
    """
    skipped: tuple[str, ...] = ()
    try:
        paths = effective_config_paths(document, handle.beets_dir)
        skipped = paths.skipped
        raw_directory, raw_library = paths.directory, paths.library
        if raw_directory is None or raw_library is None:
            return LayoutCheck(None, skipped)
        trash, origins = _resolve_store_dirs(settings, handle)
        # Both values arrive ABSOLUTE: ``effective_config_paths`` returns
        # confuse's ``as_filename()``, which has already joined a relative
        # ``directory:`` to the beets dir and run ``abspath``. ``_resolved``
        # does the rest, so the wrapper this used to call was a second
        # ``resolve()`` over an already-resolved path.
        check_store_layout(
            music_dir=Path(raw_directory),
            beets_dir=handle.beets_dir,
            trash_dir=trash,
            origins_dir=origins,
            library_path=Path(raw_library),
            settings=settings,
        )
        # The rows say WHERE the Trash may sit; this says whether the app can
        # reach it. Read-only, so a report still creates nothing.
        _check_trash_is_reachable(music_dir=Path(raw_directory), settings=settings, trash_dir=trash)
    except StoreLayoutError as exc:
        return LayoutCheck(exc, skipped)
    return LayoutCheck(None, skipped)
