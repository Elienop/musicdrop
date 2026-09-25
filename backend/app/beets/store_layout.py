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
from types import MappingProxyType
from typing import Any, Final, NamedTuple

import beets
import confuse

from app.beets.library import LibraryHandle, _music_dir, require_library_present
from app.beets.protected import ProtectedTrees, protected_trees
from app.beets.trash import resolve_trash_dir, resolve_trash_origins_dir
from app.config import Settings, app_owned_dirs, export_dir
from app.fsutil import BELOW_FLAGS, ROOT_FLAGS, bytes_at_most, open_root
from app.wire import display_path

__all__ = [
    "BEETS_SETTING",
    "LIBRARY_SETTING",
    "MUSIC_SETTING",
    "ORIGINS_SETTING",
    "TRASH_SETTING",
    "StoreLayoutError",
    "check_store_layout",
    "checked_protected_trees",
    "checked_reachable_store_dirs",
    "checked_store_dirs",
    "effective_config_paths",
    "layout_check_for_config",
    "lib_music_and_library",
    "yaml_error_at",
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
    the Validate check (``app/beets/config_check.py``) to drop a row the schema already painted: a
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
#: Python 3.11 and 3.12 (what the image ships and the venv runs), and
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
#: exports or the Plex or slskd store, the bank is one ``bank.db`` (its one-time
#: import reads legacy ``*.json`` rows once), and the playlist store globs
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


def checked_reachable_store_dirs(settings: Settings, handle: LibraryHandle) -> tuple[Path, Path]:
    """The same pair, for a request that READS the Trash and creates nothing.

    The rows :func:`checked_store_dirs` runs are relations between the RESOLVED
    paths, and the anchored walk is what sees a chain that reaches into the
    library through a link. Without it a read route answered 200 and listed the
    entries of the directory the attacker's link named, while every destructive
    route answered 503 on the same layout — measured 2026-09-13 (security seat
    M-1), the Trash page's folder names and the tags of every audio file in them.
    The walk creates nothing here (:func:`_check_trash_is_reachable`), so a
    listing still costs no ``mkdir``.

    Raises:
        StoreLayoutError: what the destructive routes would refuse, in the same
            words; request paths answer 503 with the message.
    """
    trash, origins = checked_store_dirs(settings, handle)
    music, _library = lib_music_and_library(handle.lib)
    _check_trash_is_reachable(music_dir=music, settings=settings, trash_dir=trash)
    return trash, origins


# The five Trash-chain refusals below carry no ``config_key``: the value at
# fault is ``MUSICDROP_TRASH_DIR``, which is env-derived and not a
# ``config.yaml`` key the editor can paint. The Validate check reaches them
# through :func:`_check_trash_is_reachable` and falls back to ``directory:``,
# which is the line the editor can act from — so this is deliberate rather than
# an omission to "fix".
def _refuse_an_unreachable_trash(
    spelled: Path, exc: OSError, cause: str | None = None
) -> StoreLayoutError:
    """The refusal for a Trash path whose own chain below the music root is not
    walkable — a symlinked component, or a file in the way.

    Measured: ``O_DIRECTORY|O_NOFOLLOW`` answers ENOTDIR for a link AND for a
    plain file, so the errno cannot say which; the wording matches
    ``lyrics._album_dir_fd``'s for the same ambiguity.

    ``cause`` is the link of the OPERATOR's that took the walk into the library,
    when the component in the way sits inside a link target
    (:meth:`_Chain._follow`). Without it this sentence named nothing the operator
    can act on for the shape the attacker's half makes (code seat S4, measured
    2026-09-13: the spelled path is theirs, the component in the way is not in
    it).
    """
    reached = f", reached through {cause}" if cause else ""
    return StoreLayoutError(
        f"{TRASH_SETTING} is not reachable below the music library:"
        f" {str(spelled)!r} ({exc.strerror} — a link or a file in the way{reached})."
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


def _refuse_a_trash_around_the_music_root(
    spelled: Path, cause: str | None = None
) -> StoreLayoutError:
    """The refusal for a Trash that lands inside the library without naming it.

    A link the operator owns can point BELOW the music root (``/srv/x ->
    <M>/a``): the walk never meets the root's identity, so it would create the
    Trash inside the library through a followed chain, without the anchoring the
    owner's layout ruling makes necessary there. Refused rather than anchored
    because one spelling of the root is all the app has to support.

    ``cause`` is the link and the target that reached in, when the walk resolved
    one (``'srv-x' -> '/music/a'``): the offending component is the only part of
    this the operator can act on, and it is not always the last one in the
    spelling. The sentence is the same either way, because it is the same fault
    — which is why this is not a second refusal (owner ruling 2026-09-13 closed
    the link shape; the message it already had says what happened).
    """
    detail = f" ({cause})" if cause else ""
    return StoreLayoutError(
        f"{TRASH_SETTING} reaches into the music library without naming it:"
        f" {str(spelled)!r}{detail}. Spell it through beets' `directory:`.",
        headline=f"{TRASH_SETTING} reaches into the music library without naming it",
    )


def _refuse_a_trash_target_that_climbs_out_of_the_library(
    spelled: Path, cause: str | None = None
) -> StoreLayoutError:
    """The refusal for a link target that goes INTO the library and back out.

    Its own sentence because it is its own fault: the chain is not reaching into
    the library (that one lands there) and it is not unreachable (the walk
    finished) — it took a detour THROUGH the library, and what the operator
    edits is the target that spells the detour.

    Refused rather than followed because the directory a ``..`` hop lands in is
    the library root's PARENT, which the layout rule does not treat as the
    operator's: with beets' ``directory:`` a subfolder of a writable share, a
    link planted there was resolved and the Trash created at its target with
    ``require_library_present`` skipped (measured 2026-09-13, security seat
    L-1', probes d1 and d3; refused at the previous tip too, by a ``below`` that
    was stale rather than by a decision). The owner's criterion for this branch
    decided it: *"if its safer … do it"* (``decisions.md`` #46).

    A hop that lands ON the root, or one that stays below it, is untouched — the
    first is the alias spelling reached by a climb (probe p2) and the second is
    refused by the reach-in sentence at the hop.

    What this sentence costs, stated because it is the fault the operator reads:
    the hop is judged before the DESTINATION is, so a target that climbs out and
    comes back in by name (``<M>/../<M>/a``) is refused here, for the detour,
    where it used to earn the reach-in sentence — measured 2026-09-13 (security
    seat L-1, probe e1), refused either way with nothing created, and following
    this sentence's own advice yields ``<M>/a``, which the reach-in arm then
    refuses for the real reason. One round-trip, and the alternative is asking
    whether the landing directory still reaches the root, which is a second
    climb per hop on a path the owner already costed.
    """
    detail = f" ({cause})" if cause else ""
    return StoreLayoutError(
        f"{TRASH_SETTING} climbs out of the music library with '..':"
        f" {str(spelled)!r}{detail}. Spell it without the detour through the library.",
        headline=f"{TRASH_SETTING} climbs out of the music library with '..'",
    )


#: How much of ONE half of a cause is printed. Both halves are paths and the
#: spelled path is printed beside them, so an un-elided pair doubled the 503:
#: measured 2026-09-13 (security seat L-2), 1244 characters for a 900-byte link
#: target, worst case about twice ``PATH_MAX``.
_CAUSE_HALF_MAX: Final = 120


def _elided(text: str) -> str:
    """``text`` with its MIDDLE dropped once it is longer than :data:`_CAUSE_HALF_MAX`.

    The middle and not the tail: the head of a path names the disk and the tail
    names the folder, and both are what the operator re-points. The configured
    spelling is left whole wherever it is printed — it is the value they typed.
    """
    if len(text) <= _CAUSE_HALF_MAX:
        return text
    keep = (_CAUSE_HALF_MAX - 1) // 2
    return f"{text[:keep]}…{text[-keep:]}"


def _refuse_an_uncheckable_trash_chain(spelled: Path, exc: OSError) -> StoreLayoutError:
    """The refusal for a chain the walk cannot climb back out of.

    The climb decides whether the spelling landed inside the music library, so
    answering "not inside" to a question that could not be asked anchors nothing
    (security seat L-1). Reachable at the deepest component the walk OPENED when
    that one is readable but not searchable — a MIDDLE component included: the
    walk needs read to open a part and search only to go deeper, and the climb is
    asked right after the open. Measured 2026-09-13 (code seat W-2), mode
    ``0o400`` on ``<T>/mid`` with the Trash spelled at ``<T>/mid/inner/trash``
    fires at ``mid``; the arm this replaces worded the same shape as "could not
    be created". A mover needs write and search there anyway.
    """
    return StoreLayoutError(
        f"{TRASH_SETTING} could not be checked against the music library:"
        f" {str(spelled)!r} ({exc.strerror}). Fix the permissions on its folders.",
        headline=f"{TRASH_SETTING} could not be checked against the music library",
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
    at a time; a ``..`` is refused rather than collapsed. A NUL in the value
    would reach ``os.open`` as ``ValueError``, which no caller's ``except
    OSError`` catches — unreachable today because every caller resolves first and
    ``_unresolvable`` refuses it there (measured 2026-09-12, code seat suggestion
    5). No count here: the five :func:`checked_protected_trees` sites are not all
    of them, :func:`layout_check_for_config` resolves and then walks too.

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


def _music_root_ident(music_dir: Path, spelled: Path, *, creating: bool) -> tuple[int, int] | None:
    """The music root's identity, or ``None`` when there is none to take.

    ``open_root`` FOLLOWS a link at the root, because an operator's beets
    ``directory:`` may be one — so the identity is the directory beets indexes,
    whichever of its spellings the Trash names.

    A root that will not OPEN may still be there: measured 2026-09-12 (code seat
    W2), mode ``0o111`` answers EACCES to ``open_root``, which needs read, while
    ``os.stat`` needs only search on the parent and returns the same pair an
    ``fstat`` would. The walk only ever compares that pair, so the stat is
    evidence enough — without it a spelling that reaches into the library is
    neither anchored nor refused.

    Raises:
        StoreLayoutError: the root will not open, the Trash is spelled below it,
            and this call is about to CREATE — the dropped share the operator has
            to hear about. With no identity there is no evidence but the
            spelling, which is why this one decision is lexical. Only while
            creating: the report is silent for a path that is not there yet, and
            refusing there made Settings → Beets unsavable while the root was
            missing (code seat W1, measured 2026-09-12 — Save answered 422 for an
            edit that named no path).
    """
    try:
        fd = open_root(music_dir)
    except OSError as exc:
        if creating and _is_spelled_below(spelled, music_dir):
            raise _refuse_an_unopenable_music_root(music_dir, exc) from exc
        return _stat_id(music_dir)
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


#: The flags the ``..`` climb opens each rung with. ``O_PATH`` because the climb
#: only ever ``fstat``s: measured 2026-09-12, it answers the same ``(st_dev,
#: st_ino)`` and is usable as a ``dir_fd`` for the next rung, while needing
#: search alone — a ``0o111`` ancestor above the Trash answers EACCES to
#: :data:`~app.fsutil.ROOT_FLAGS` and climbs fine with this. That matters now
#: that a climb which cannot finish is a refusal rather than a "no".
#:
#: ``os.O_PATH`` is Linux-only, and the only such constant in ``app/``: measured
#: 2026-09-13, the nine ``os.O_*``/``os.*_OK`` constants this package uses are
#: otherwise POSIX. On another platform this line raises ``AttributeError`` at
#: import — the app ships in a Linux container, and BACKLOG's mountinfo option
#: for the same decision is Linux-only too.
_CLIMB_FLAGS: Final = os.O_PATH | os.O_DIRECTORY


def _reaches_the_music_root(fd: int, root_ident: tuple[int, int], *, spelled: Path) -> bool:
    """Whether the directory ``fd`` is open on is the music root or sits below it.

    A ``..`` climb through descriptors, because the question is about the
    directory the walk really reached and not about how it is spelled — with one
    measured exception: ``..`` from a MOUNT root crosses to the mountpoint's
    parent, so a bind mount of a library subdirectory at an outside path reads as
    outside the library (security seat M-1, measured 2026-09-12 under ``unshare
    --map-root-user --mount``; recorded as a residual in ``BACKLOG.md``). A link
    in the chain is not a second such exception since 2026-09-13: the walk
    resolves every one of them itself (:meth:`_Chain._follow`) and asks this
    about the target, so a link that lands inside the library is refused rather
    than followed. ``/`` is its own parent, which is the stop condition.

    Raises:
        StoreLayoutError: the climb could not be finished, so the answer is
            unknown; "no" would anchor nothing (security seat L-1).
    """
    here, here_ident = fd, _fstat_ident(fd)
    if here_ident == root_ident:
        return True
    climbed: int | None = None
    try:
        while True:
            try:
                up = os.open("..", _CLIMB_FLAGS, dir_fd=here)
            except OSError as exc:
                raise _refuse_an_uncheckable_trash_chain(spelled, exc) from exc
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


def _below_the_music_root(
    fd: int, root_ident: tuple[int, int], spelled: Path, *, cause: str | None = None
) -> bool:
    """Whether the walk has reached the music root, refusing if it is INSIDE it.

    Asked about every component of the EXISTING prefix the walk stands on while
    it is still above the root, about the destination of every link the walk
    resolves, and again after a ``..`` hop inside a target wherever the walk then
    stands (:meth:`_Chain._arrive`) — not once about the leaf. That third site is
    the only one that can refuse for LEAVING the library: a hop that turns
    ``below`` off raises there rather than following what it landed in
    (:func:`_refuse_a_trash_target_that_climbs_out_of_the_library`).
    Measured 2026-09-12 (security seat H-1)
    on the arm this replaces, which asked it once: both requests were accepted,
    the movers wrote to the attacker's directory and ``empty_all`` enumerated it.
    ``cause`` names the link when the caller is :meth:`_Chain._follow`.

    The EXISTING prefix and not every part: a component the create loop creates is
    never asked, which :func:`_open_the_trash_chain` describes as the
    operator-chain race. Measured 2026-09-13 (security seat L-2') by tracing the
    question — the request that creates ``srv``, ``x`` and ``.trash`` asks 9 times
    and stops at their parent; the next request, with all three there, asks 12.

    Raises:
        StoreLayoutError: this component is below the music root, or the climb
            could not be finished.
    """
    if _fstat_ident(fd) == root_ident:
        return True
    if _reaches_the_music_root(fd, root_ident, spelled=spelled):
        raise _refuse_a_trash_around_the_music_root(spelled, cause)
    return False


#: The flags each component of a link TARGET is opened with. ``O_PATH`` because
#: the kernel needed SEARCH alone on the directories it resolved a link through,
#: and a walk that resolves the same link itself may not need more: measured
#: 2026-09-13, :data:`~app.fsutil.BELOW_FLAGS` answers EACCES on a ``0o111``
#: component where these open it, and the two supported layouts that have one —
#: the search-only ancestor above an outside Trash, a ``0o111`` music root under
#: an alias — would otherwise be refused. Such a descriptor answers the same
#: ``(st_dev, st_ino)`` and serves as the ``dir_fd`` of the next ``openat``,
#: ``readlinkat`` and ``mkdirat`` (measured the same day). ``O_NOFOLLOW`` still
#: answers ENOTDIR for a link, so a link inside a target is resolved here too.
#:
#: ``O_PATH`` is Linux-only, and the walk's ``..`` climb reads the same constant
#: through :data:`_CLIMB_FLAGS`; see the note there.
_HOP_FLAGS: Final = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW

#: How many links one spelling may pass through. 40 is the kernel's own budget:
#: measured 2026-09-13 on Linux 7.2.4, a chain of 40 links resolves and the 41st
#: answers ELOOP. Shared across the whole spelling, siblings included, the way
#: the kernel's is — a chain of 41 is what reaches it, since ``Path.resolve``
#: follows a long chain happily and refuses only a true LOOP, which the row
#: layer then words as "could not be resolved" (measured 2026-09-13:
#: ``RuntimeError('Symlink loop from …')`` from ``resolve_trash_dir``, pinned by
#: ``test_a_trash_dir_that_stops_resolving_is_a_503_not_a_500``). It is also this walk's
#: termination, which the kernel's ELOOP used to be.
_MAX_LINK_HOPS: Final = 40


class _Chain:
    """One descriptor walking the Trash's spelling, resolving links ITSELF.

    Every component is opened ``O_NOFOLLOW``, so the kernel follows nothing; a
    component that is a link is read with ``os.readlink`` and its target walked
    hop by hop under these same rules, which is what lets the walk see every
    directory the kernel would have passed through. Measured 2026-09-12
    (security seat H-1') on the arm this replaces, which opened the operator's
    chain following links: with ``/srv/x -> <M>/a`` the kernel landed the walk
    on the attacker's ``<M>/a`` and no descriptor the walk held was ever inside
    the library, so the Trash relocated outside it on every request. Owner
    ruling 2026-09-13 chose this over refusing every link in the path, which
    would refuse the default Trash wherever a parent such as ``/home`` is a
    link.

    ``flags`` is what the next component is opened with: :data:`BELOW_FLAGS` for
    the configured spelling, whose parts this walk reads, creates in and hands
    on, and :data:`_HOP_FLAGS` inside a link target, where the kernel needed
    search alone. One descriptor is open per instance, and ``fd`` is always the
    directory the walk stands on — a failed step leaves it where it was, so the
    caller's ENOENT arm still sees the prefix that really exists.

    ``cause`` is the link this walk is the target of, for the two refusals that
    name it; ``None`` in the walk over the configured spelling, which the
    refusals print anyway.

    With ``root_ident`` None — a music root that neither opens nor stats — no
    component and no target is asked, because there is no identity to compare:
    measured 2026-09-13 (security seat L-4), a link is then followed and the
    Trash created at its target. The two adjacent states are not that: a
    reach-in target under an absent root answers ENOENT, and a bare mountpoint
    still HAS an identity, so the rule fires there (pinned by
    ``test_an_empty_music_root_still_refuses_a_link_that_reaches_into_it``).
    """

    def __init__(
        self,
        *,
        fd: int,
        flags: int,
        root_ident: tuple[int, int] | None,
        spelled: Path,
        hops: int = 0,
        cause: str | None = None,
    ) -> None:
        self.fd = fd
        self.flags = flags
        self.root_ident = root_ident
        self.spelled = spelled
        self.below = False
        self.hops = hops
        self.cause = cause

    def close(self) -> None:
        """Close the one descriptor this walk holds."""
        os.close(self.fd)

    def step(self, part: str, *, create: bool = False) -> None:
        """Move onto ``part``, creating it through the descriptor first when asked.

        Raises:
            StoreLayoutError: ``part`` is not a directory below the music root,
                it is a link whose target reaches into the library or climbs back
                out of it, or the climb that decides that could not be finished.
            OSError: any other fault; the caller words it. ENOENT is the caller's
                "this part is not there yet" arm, and reaches it from inside a
                link target too — a dangling link answers it the way the kernel
                did (measured 2026-09-13: ``O_NOFOLLOW`` answers ENOTDIR for a
                dangling link, and its target's own walk then answers ENOENT).
        """
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, dir_fd=self.fd)
        try:
            opened = os.open(part, self.flags, dir_fd=self.fd)
        except OSError as exc:
            if exc.errno in (errno.ENOTDIR, errno.ELOOP):
                if self.below:
                    raise _refuse_an_unreachable_trash(self.spelled, exc, self.cause) from exc
                self._follow(part, exc)
                return
            raise
        self._arrive(opened, ask=not create, climbed=part == "..")

    def _arrive(self, opened: int, *, ask: bool, climbed: bool = False) -> None:
        """Adopt ``opened`` as where the walk stands, asking the jump-in question.

        ``ask`` is False for a part the create loop just made, which carries the
        decision rather than re-taking it — the operator-chain race recorded
        under *Accepted residuals* in ``BACKLOG.md``, and the reason the measured
        question counts in :func:`_below_the_music_root` are what they are. No
        test pins that line: measured 2026-09-13 (code seat Q1f), a mutant that
        always asks passes the whole suite, and what tells the two apart is a
        part that acquires the root's IDENTITY inside the create window.

        ``climbed`` re-asks unconditionally, because ``below`` describes the
        directory the walk stands on NOW and ``..`` moves it: a target that dips
        into the library and climbs back out ends outside it, and was refused
        with the not-reachable sentence while ``below`` stayed true (security
        seat L-1, measured 2026-09-13). Only reachable inside a link target — the
        configured spelling may hold no ``..``.

        An OUTWARD crossing is then refused rather than followed: ``below`` was
        true and the hop made it false, so the walk has just left the library and
        stands where the layout rule makes no promise (security seat L-1', the
        owner's criterion in
        :func:`_refuse_a_trash_target_that_climbs_out_of_the_library`). This is
        the THIRD place the jump-in question is asked, and the only one that can
        refuse for leaving.
        """
        os.close(self.fd)
        self.fd = opened
        if ask and (climbed or not self.below) and self.root_ident is not None:
            was_below = self.below
            self.below = _below_the_music_root(
                self.fd, self.root_ident, self.spelled, cause=self.cause
            )
            if was_below and not self.below:
                raise _refuse_a_trash_target_that_climbs_out_of_the_library(
                    self.spelled, self.cause
                )

    def _follow(self, part: str, refused: OSError) -> None:
        """Resolve the link at ``part`` by walking its target, hop by hop.

        The target is walked in a walk of its own, so a fault anywhere in it
        leaves this one standing where it was. What that walk answers decides
        three ways: a target that IS the music root is the alias spelling and is
        anchored from here (``below``); a target below the root is the link into
        the library this refuses; a target outside stays followed, which is the
        supported "Trash on another disk through the operator's own link" — save
        for one that got outside by climbing there from inside, refused at the
        hop (:meth:`_arrive`).

        Raises:
            StoreLayoutError: the target reaches into the music library, or a
                ``..`` in it climbs back out of the library.
            OSError: ``part`` is not a link at all — a plain file, re-raising the
                ENOTDIR it already answered (measured 2026-09-13: ``readlink``
                answers EINVAL there, and the open's errno cannot tell the two
                apart); or the target passes through more links than the kernel
                itself would follow; or any fault its own walk met.
        """
        try:
            target = os.readlink(part, dir_fd=self.fd)
        except OSError:
            raise refused from None
        self.hops += 1
        if self.hops > _MAX_LINK_HOPS:
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(self.spelled))
        absolute = target.startswith("/")
        hops = Path(target).parts[1:] if absolute else Path(target).parts
        walk = _Chain(
            fd=os.open("/", _HOP_FLAGS) if absolute else os.dup(self.fd),
            flags=_HOP_FLAGS,
            root_ident=self.root_ident,
            spelled=self.spelled,
            hops=self.hops,
            cause=f"{_elided(display_path(part))!r} -> {_elided(display_path(target))!r}",
        )
        try:
            for hop in hops:
                walk.step(hop)
            below = self._ended_below_the_music_root(walk)
            arrived = os.open(".", self.flags, dir_fd=walk.fd)
        finally:
            walk.close()
        os.close(self.fd)
        self.fd, self.below, self.hops = arrived, below, walk.hops

    def _ended_below_the_music_root(self, walk: _Chain) -> bool:
        """Whether a finished target walk ended below the music root.

        Asked only when it met the root: a target that did not is outside the
        library by the same climb every other component is judged by, asked
        already by that walk's own last step. ``..`` inside a target is WALKED
        and not collapsed — ``openat(fd, "..")`` is the kernel's own answer for a
        descriptor the walk holds — and this reads the destination rather than
        the path. A hop that leaves the library never reaches here: it is refused
        at the hop (:meth:`_arrive`), so what this still answers is a target that
        met the root and ended below it or ON it.

        Raises:
            StoreLayoutError: the target ended below the music root, named with
                the link that reaches in.
        """
        if walk.below and self.root_ident is not None:
            return _below_the_music_root(walk.fd, self.root_ident, self.spelled, cause=walk.cause)
        return walk.below


def _open_the_trash_chain(
    *, music_dir: Path, spelled: Path, before_creating: Callable[[], None] | None
) -> int:
    """A descriptor on the Trash, decided by IDENTITY component by component.

    From ``/`` down, one ``os.open`` per part, none of them following a link:
    above the music root the parts are the OPERATOR's — a symlinked
    ``directory:``, a ``/srv`` that is a link — so a link there is resolved by
    :class:`_Chain` itself and refused only when its target lands inside the
    library (owner ruling 2026-09-13). Each part is asked
    :func:`_below_the_music_root` as the walk stands on it; the moment an opened
    part's ``(st_dev, st_ino)`` IS the music root's, every further part of the
    EXISTING prefix is refused if it is a link at all, and the missing tail is
    created through its parent's descriptor — an existing part is not created.
    That stricter rule below the root because that is the chain the owner's
    layout ruling leaves attacker-writable. The create loop carries the jump-in
    decision rather than re-taking it, so a part that acquires the music root's
    IDENTITY inside that window — the root renamed onto it, bind-mounted there,
    or its parent renamed into the library — is not noticed, and a link the
    attacker planted at a later component is then resolved and followed: measured
    2026-09-13 (code seat Q1f), the Trash landed at that link's target outside
    the library with the library-presence guard skipped. The precondition is
    write access on the OPERATOR's chain above the music root, which is what
    keeps it a residual — recorded under *Accepted residuals* in ``BACKLOG.md``.
    A plain ``mkdir`` by a racer cannot reach it: a directory they create at that
    path has the walk's own descriptor as its parent, so it is outside the
    library. A link swapped in there IS refused, because resolving it is the step
    itself and not a question about it.

    Identity and not spelling, measured 2026-09-12 (security seat H-2, code seat
    W1): the two settings can name one root two ways — a Trash under an ALIAS of
    ``directory:``, or a ``directory:`` that is a link with the Trash spelled
    through its target — and a lexical ``relative_to`` then answered "not below
    the music root" for a Trash that really was inside it, skipping the anchored
    walk entirely and following the attacker's link.

    What already exists is walked BEFORE anything is created, so a spelling that
    reaches into the library through a link the walk never identifies is refused
    with nothing left behind.

    ``before_creating`` is run ONCE, immediately before the first part is created
    below the music root, and never on the arm that creates nothing there — the
    library-presence guard, which has no business refusing a Trash the music
    share cannot reach. ``None`` creates nothing at all: the report's read-only
    form, which stops at the first part that is not there yet, because none of
    the five paths has to exist.

    The returned fd is the caller's to close.

    Raises:
        StoreLayoutError: a part below the music root is not a directory, the
            spelling reaches into the library without naming it — through a link
            the walk resolved or otherwise — or a chain the walk could not climb
            left that question unanswered.
        OSError: any other fault the walk met; the caller words it.
    """
    root_ident = _music_root_ident(music_dir, spelled, creating=before_creating is not None)
    parts = spelled.parts[1:]
    chain = _Chain(
        fd=os.open("/", ROOT_FLAGS), flags=BELOW_FLAGS, root_ident=root_ident, spelled=spelled
    )
    try:
        walked = 0
        for part in parts:
            try:
                chain.step(part)
            except FileNotFoundError:
                break
            walked += 1
        if before_creating is not None:
            missing = parts[walked:]
            if missing and chain.below:
                before_creating()
            for part in missing:
                chain.step(part, create=True)
        return chain.fd
    except BaseException:
        chain.close()
        raise


def _ensure_trash_root(
    settings: Settings, *, music_dir: Path, trash_dir: Path, lib: Any
) -> tuple[int, int]:
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

    Nothing is created INSIDE a library that is not there: the anchored arm
    writes into the music root, and a directory on a bare mountpoint defeats
    ``require_library_root``'s "an empty root is not mounted" half for every
    later caller — measured 2026-09-12 (security seat H-1), one destructive
    request on a dropped share supplied that entry and the next disk sync dropped
    3 of 3 rows. ``require_library_present`` and not the cheap guard, because the
    cheap one is itself defeated by any stray entry on the mountpoint (measured:
    a ``.stfolder`` and it passes, while the DB sample still refuses). It runs
    only when a part below the root really has to be created, so a Trash that is
    already there costs nothing and a Trash outside the library is not refused
    for a fault it cannot reach.

    Raises:
        StoreLayoutError: the spelling climbs or reaches into the library without
            naming it, the Trash is not reachable below the music root, or it
            could not be created.
        LibraryRootUnavailableError: a part below the music root would have to be
            created while the library's own music is not there.
    """
    spelled = _checked_trash_spelling(settings.trash_dir, trash_dir)
    try:
        fd = _open_the_trash_chain(
            music_dir=music_dir,
            spelled=spelled,
            before_creating=lambda: require_library_present(lib),
        )
    except OSError as exc:
        raise _refuse_an_uncreatable_trash(spelled, exc) from exc
    try:
        return _fstat_ident(fd)
    finally:
        os.close(fd)


def _check_trash_is_reachable(*, music_dir: Path, settings: Settings, trash_dir: Path) -> None:
    """Raise the refusal a destructive request would, without creating anything.

    The Validate check painted Settings from the rows alone and the
    reachability walk lives at the destructive call sites, so a Trash below the
    music root through a symlinked component read HEALTHY where the operator
    configures it while every delete, restore and Empty-Trash answered 503
    (security seat L-3). Same walk and the same messages, no ``mkdir``.

    Silent for a part that is simply not there yet — none of the five paths has
    to exist, and the walk stops at the first missing one. Any other fault is
    worded exactly as the destructive routes word it, because that is the answer
    the operator's next delete will get.

    Which faults actually arrive here, measured 2026-09-13 through this report's
    own path: an EACCES the ROW layer's ``stat`` meets does not — no search bit on
    an ancestor answers "could not be examined" there, and a symlink loop answers
    "could not be resolved" — but an EACCES the ``..`` climb meets DOES, and is
    painted as "could not be checked against the music library (Permission
    denied)". A ``0o400`` Trash outside the library is that arm; the same mode
    inside the library is accepted here, because the identity check settles the
    question before the climb is asked. What also reached here and painted NOTHING
    before this arm existed was a FILE in the operator's chain ABOVE the music
    root — the report read healthy while every destructive request answered "could
    not be created (Not a directory)". A mutant that returned here instead of
    raising survived all 3590 tests before this arm was added.

    Raises:
        StoreLayoutError: the spelling climbs or reaches into the library without
            naming it, a part below the music root is not a directory, or the
            chain cannot be walked at all.
    """
    spelled = _checked_trash_spelling(settings.trash_dir, trash_dir)
    try:
        fd = _open_the_trash_chain(music_dir=music_dir, spelled=spelled, before_creating=None)
    except OSError as exc:
        raise _refuse_an_uncreatable_trash(spelled, exc) from exc
    os.close(fd)


def checked_protected_trees(
    settings: Settings, handle: LibraryHandle, *, trash_dir: Path, origins_dir: Path
) -> ProtectedTrees:
    """The identities the movers and the remover refuse, for THIS request.

    Taken beside :func:`checked_store_dirs`, from the pair it returned, by every
    request site that needs an identity set: the Trash page's restore and DELETE
    routes, the artist-art store and the reorganize orphan sweep
    (``reorganize_jobs/runner.py``) hand it to a mover or the remover; the delete
    ops read it themselves, to tell an album folder that IS one of ours from a
    stranger's (``delete._trash_one``) — they pass no set to ``trash_album``,
    which takes none. Duplicates' resolve calls it too and DISCARDS the set for
    that reason (a recorded residual) — what it wants is the creation.
    ``api/reorganize.py``
    reads ``protected_entries`` directly for the sweep's ignore list, which is a
    list of paths rather than a set of identities. The one destructive path that
    does NOT come through here is the import session's post-import cleanup
    (``import_session._trash_replaced_albums``), which holds no ``LibraryHandle``; its
    ``trash_album`` still creates the Trash by path (a recorded residual).
    Separate from :func:`checked_store_dirs` because every OTHER caller of that
    only reads the pair and would pay a dozen stats for nothing.

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
    trash_ident = _ensure_trash_root(settings, music_dir=music, trash_dir=trash_dir, lib=handle.lib)
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


def _include_source(target: str, written: str, budget: int) -> tuple[confuse.ConfigSource, int]:
    """One ``include:`` entry, read through ONE descriptor. With its size.

    ``target`` is the entry resolved to a path; ``written`` is how the refusals
    name it, as :func:`_as_written` gives it.

    ``os.stat`` then confuse's ``open`` asked the same NAME twice, and flipping a
    symlink between the two put the FIFO hang back — measured, the read ran until
    a 3-second alarm. ``O_NONBLOCK`` is what makes a writer-less FIFO answer at
    all. No ``O_NOFOLLOW``: measured, a real beets start FOLLOWS a symlinked
    include and merges its ``directory:``.

    ``budget`` is what is left of :data:`_MAX_INCLUDE_BYTES` for this request,
    and the size comes back so the caller can subtract it.

    Raises ``ConfigReadError`` for the shapes beets prints-and-continues on, and
    :func:`_unreadable_include` for the rest: a FIFO, a
    descriptor with nothing to read, a read the budget stops, and a parse error
    that is not a ``YAMLError``.
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
            raise _unreadable_include(f"{written!r} is a FIFO; beets would block on it")
        buf = bytes_at_most(fd, budget)
    except BlockingIOError as exc:
        raise _unreadable_include(f"{written!r} had nothing to read") from exc
    except OSError as exc:
        # EISDIR for a directory: beets' own ``open`` answers the same and its
        # ``ConfigReadError`` arm carries on.
        raise confuse.ConfigReadError(target, exc) from exc
    finally:
        os.close(fd)
    if buf is None:
        raise _unreadable_include(
            f"{written!r} takes the include: list over its {_MAX_INCLUDE_BYTES}-byte budget"
        )
    try:
        data = confuse.yaml_util.load_yaml_string(buf, target) or {}
    except confuse.ConfigReadError:
        raise  # a YAML error: beets prints it and skips the include
    except Exception as exc:
        # Everything else a parse raises escapes beets' include loop and ends the
        # start: measured, ``KeyError`` for ``!!bool ture`` and ``AttributeError``
        # for a ``!!timestamp`` that is not a date.
        # The class alone: its text quotes the value, which can be a secret.
        raise _unreadable_include(f"{written!r} raised {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        # What ``YamlSource.load`` raises for the same document, so a beets start
        # over this file refuses too.
        raise TypeError(f"YAML config must be a mapping, got {type(data)}")
    return confuse.ConfigSource(data, filename=os.path.abspath(target)), len(buf)


class SkippedInclude(NamedTuple):
    """An ``include:`` entry beets would skip, as written, and one clause of why."""

    name: str
    reason: str


def yaml_error_at(exc: object) -> str | None:
    """``YAML error at line N`` from a PyYAML or ruamel ``problem_mark``, else ``None``.

    Not the parser's problem text: for an undefined alias or an unknown tag it
    quotes the token, which can be an unquoted secret.
    """
    mark = getattr(exc, "problem_mark", None)
    return None if mark is None else f"YAML error at line {mark.line + 1}"


def _skip_reason(exc: confuse.ConfigReadError) -> str:
    """A YAML error's 1-based line, else the OS error, else the first line."""
    reason = exc.reason
    at = yaml_error_at(reason)
    if at is not None:
        return at
    if isinstance(reason, OSError) and reason.strerror:
        return reason.strerror
    return str(reason).partition("\n")[0]


def _as_written(view: confuse.Subview) -> str:
    """An ``include:`` entry as written when it is a filename, else ``""``.

    A later entry can come from inside an earlier include, and the repr of a
    mapping there quoted its values (``OrderedDict({'password': ...})``).
    """
    raw = view.get()
    return str(raw) if isinstance(raw, (str, bytes)) else ""


def _loaded_paths(cfg: confuse.Configuration, document_file: str) -> tuple[str | None, str | None]:
    """``directory:`` and ``library:`` as beets would load them from ``cfg``.

    ``(None, None)`` when the document's OWN key resolves to no filename: the
    schema paints that one, and two rows saying the same thing was the
    collateral of reporting it here.
    """
    resolved: dict[str, str] = {}
    for key in ("directory", "library"):
        try:
            resolved[key] = cfg[key].as_filename()
        except confuse.ConfigError as exc:
            source = _winning_source(cfg, key)
            if source is not None and source != document_file:
                raise _include_sets_a_non_path(key, source) from exc
            return (None, None)
    return (resolved["directory"], resolved["library"])


class EffectivePaths(NamedTuple):
    """What beets would load, and the ``include:`` entries the gate skipped."""

    directory: str | None
    library: str | None
    skipped: tuple[SkippedInclude, ...] = ()


class LoadedCandidate(NamedTuple):
    """``document`` layered the way beets layers config.yaml, includes merged.

    ``error`` is the include refusal that stopped the merge, if any; ``config``
    then holds the includes merged before it. ``included`` maps each merged
    include's source ``filename`` to the entry as written in ``include:``.
    """

    config: confuse.Configuration
    skipped: tuple[SkippedInclude, ...] = ()
    error: StoreLayoutError | None = None
    included: Mapping[str, str] = MappingProxyType({})


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
    beets drops and this gate did not merge, as written in ``include:``, with
    why: beets prints them and carries on, and Apply and boot refuse them.
    """
    loaded = load_candidate(document, beets_dir)
    if loaded.error is not None:
        raise loaded.error
    directory, library = _loaded_paths(loaded.config, str(beets_dir / "config.yaml"))
    return EffectivePaths(directory, library, loaded.skipped)


def load_candidate(document: Mapping[str, Any], beets_dir: Path) -> LoadedCandidate:
    """``document`` over beets' defaults, with its ``include:`` files merged on top.

    The include gate of :func:`effective_config_paths`: its refusal comes back
    in ``error`` rather than raised, so a caller can still ask the typed reads
    of what was merged.
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
    skipped: list[SkippedInclude] = []
    # One read per resolved path, so a repeated entry costs one. The entry is
    # still ``set`` again at its own position: the LAST include wins, so dropping
    # the repeat would change which file decides ``directory:``.
    read: dict[str, confuse.ConfigSource] = {}
    included: dict[str, str] = {}
    budget = _MAX_INCLUDE_BYTES
    written = ""
    try:
        entries = list(cfg["include"].sequence())
        if len(entries) > _MAX_INCLUDE_ENTRIES:
            raise _too_many_includes(len(entries))
        for view in entries:
            written = _as_written(view)
            # Resolved HERE rather than up front: each entry resolves against
            # the sources set so far, which is what beets' own loop does.
            target = view.as_filename()
            merged = read.get(target)
            if merged is None:
                merged, used = _include_source(target, written, budget)
                budget -= used
                read[target] = merged
            cfg.set(merged)
            # The filename ``_include_source`` gave the source it read.
            included[os.path.abspath(target)] = written
    except confuse.NotFoundError:
        pass  # no ``include:`` key at all
    except confuse.ConfigReadError as exc:
        # beets writes one stderr line and carries on, with the ``except`` OUTSIDE
        # the loop (``beets/__init__.py:29-38``), so the first unreadable entry ends
        # the merge. Measured with the guard placed per-entry instead, this function
        # reported an overlay's ``directory:`` that a real ``setup_beets`` over the
        # same file did not load. Only ``_include_source`` raises this, inside
        # the loop, so ``written`` holds that entry.
        skipped.append(SkippedInclude(written, _skip_reason(exc)))
    except (confuse.ConfigError, TypeError, ValueError, RecursionError) as exc:
        # The shapes a real start does not survive: a non-list ``include:``, an
        # include whose top level is not a mapping, an entry holding a NUL, an
        # include nested past the recursion limit. Measured, all four escaped the
        # old ``except confuse.ConfigError`` and the three routes answered a bare
        # 500 or reported the document CLEAN.
        error = _unreadable_include(f"{written!r}: {exc}" if written else str(exc))
        error.__cause__ = exc
        return LoadedCandidate(cfg, tuple(skipped), error, included)
    except StoreLayoutError as exc:
        return LoadedCandidate(cfg, tuple(skipped), exc, included)
    return LoadedCandidate(cfg, tuple(skipped), None, included)


class LayoutCheck(NamedTuple):
    """A candidate document's refusal, and the includes the gate skipped."""

    error: StoreLayoutError | None
    skipped_includes: tuple[SkippedInclude, ...] = ()


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
    return layout_check_for_candidate(load_candidate(document, handle.beets_dir), settings, handle)


def layout_check_for_candidate(
    loaded: LoadedCandidate, settings: Settings, handle: LibraryHandle
) -> LayoutCheck:
    """:func:`layout_check_for_config` over a candidate :func:`load_candidate` built."""
    skipped = loaded.skipped
    try:
        if loaded.error is not None:
            raise loaded.error
        raw_directory, raw_library = _loaded_paths(
            loaded.config, str(handle.beets_dir / "config.yaml")
        )
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
