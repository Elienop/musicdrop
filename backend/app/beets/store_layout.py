"""Where MusicDrop's own stores may sit relative to the music library.

Two directories in this app delete things wholesale. ``trash_manage.empty_all``
``rmtree``s every child of the Trash dir, and ``trash_origins.clear_trash_origins``
unlinks every ``*.json`` directly inside the origin store. Neither asks what the
directory it was handed actually holds, so where those two resolve decides
whether "Empty Trash" removes a hundred trashed albums or the library.

The rule this module enforces — the owner's ruling of 2026-09-04, plus the
beets-dir clause added after the review round: **a Trash inside the music library
is allowed; one on top of or around it is refused. The origin store stays out of
the music library. The beets data directory and the music library do not nest, in
either direction.** Five paths take part —

``M`` the music library (``directory:`` in ``config.yaml``), ``B`` the beets data
directory (``MUSICDROP_BEETS_DIR``, holding ``library.db`` + ``config.yaml``),
``T`` the Trash (``MUSICDROP_TRASH_DIR``, default ``<B>/trash``), ``O`` the
origin store (``MUSICDROP_TRASH_ORIGINS_DIR``, default ``<B>/trash-origins``)
and ``L`` the beets database FILE (``library:`` in ``config.yaml``, default
``<B>/library.db``).

Refused, with the loss each one would cause:

=========================  =========================================================
``B`` is ``M``             the app's own folders (bank, plex, slskd, playlists,
                           inbox) sit in the tree the orphan sweep walks
``M`` contains ``B``       a library-scope sweep and a whole-folder delete can move
                           ``library.db`` + ``config.yaml``
``B`` contains ``M``       every app-owned exclusion becomes an ancestor of the
                           music root, so each is dropped with a WARNING and the
                           sweep runs with no app-owned exclusion at all
``T`` is / contains ``M``  Empty Trash deletes the music library
``T`` is / contains ``B``  Empty Trash deletes ``library.db`` + ``config.yaml``
``O`` is / contains ``M``  the store sweep unlinks ``*.json`` in the library
``O`` is / contains ``B``  the store sweep unlinks ``*.json`` in the beets dir
``M`` contains ``O``       a folder delete above the store trashes the records
``O`` is / inside ``T``    Empty Trash deletes the records; they also list as Trash
                           entries
``T`` inside ``O``         trashed albums land among the origin records
``L`` is / inside ``T``    Empty Trash deletes the beets database
``L`` is / inside ``O``    the database sits in the directory the store sweep prunes
``T`` overlaps a store     Empty Trash deletes that store, or trashed albums land in it
``O`` overlaps a store     the store sweep unlinks its ``*.json``, or the records land
                           in it
=========================  =========================================================

The last two rows are D2, and "a store" is each of the six directories the app
owns beside these five: the import bank, the Plex settings store, the slskd
settings store, the playlist store, the inbox and the playlist export dir. Four
generated rows each — ``T``/``O`` is one, holds one, sits in one — because
``MUSICDROP_TRASH_DIR=<B>/plex`` booted clean and the first Empty Trash wiped
that store.

Allowed, and each one is a shape somebody really runs: ``T`` strictly inside
``M`` (the ruling — ``/music/.trash`` makes a delete a same-disk rename), ``T``
and ``O`` under ``B`` (the DEFAULT), ``L`` under ``B`` (beets' own default
``library.db``), and disjoint trees for ``M`` and ``B`` (the shipped image:
``/music`` for the library, ``/data`` for everything of ours).

Comparisons run on FULLY RESOLVED absolute paths, both sides. ``is_relative_to``
is lexical — ``<T>/../Sibling`` reads as "under ``<T>``" until ``resolve()``
normalises the ``..`` away, the same trap ``trash_manage.resolve_trash_child``
documents — and ``_music_dir`` is not symlink-resolved, so a library reached
through ``/music -> /mnt/tank/music`` would otherwise compare unequal to a Trash
spelled ``/mnt/tank/music/.trash``. ``Path.resolve()`` runs non-strict here: none
of the five has to exist yet.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple

import beets
import confuse

from app.beets.library import LibraryHandle, _music_dir
from app.beets.protected import (
    ProtectedTrees,
    app_owned_dirs,
    export_dir,
    protected_trees,
)
from app.beets.trash import resolve_trash_dir, resolve_trash_origins_dir
from app.config import Settings

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
    "handle_music_and_library",
    "layout_error_for_config",
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
    """Trash or the origin store sits where using it would destroy data.

    One exception for every refused relationship: each is the same operator
    action (move a directory) and the same remedy (point the setting somewhere
    else), so a caller that wants to branch on WHICH would be branching on
    prose. What varies is the message, which names the setting to change, both
    resolved paths, the loss the layout would cause and the fix.

    ``config_key`` is the one machine-readable field: the ``config.yaml`` key the
    editor should paint, or ``None`` when the refusal is between two env-derived
    paths and no submitted value is at fault.

    ``unusable_value`` marks the two refusals that are about ONE value rather
    than a pair — it would not resolve, or it resolved to something the app
    cannot stat. ``config_editor.store_layout_errors`` reads it to drop a row the
    schema has already painted on the same key: measured, a ``directory:``
    holding a NUL produced two rows saying the same thing, while a
    ``directory: /`` produced a schema row about writability plus the layout row
    that explains the loss, and only the first pair is a duplicate.

    ``headline`` is the refusal's first clause — the pair, without the paths or
    the loss. Apply's 422 is built from it rather than from a sentence of its
    own: that sentence said "config.yaml would move the music library" for every
    refusal, including the ones where the Trash is what moved.
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
#: IS there in a form this process cannot examine, and :func:`_same_path` then
#: has no inode to compare and falls back to string equality. Measured on this
#: tree: ``MUSICDROP_TRASH_DIR`` pointing at a symlink to the music library
#: inside a mode-000 directory was ALLOWED by :func:`check_store_layout`, and the
#: same layout with that directory traversable was refused.
_ABSENT_ERRNOS: Final = frozenset({errno.ENOENT, errno.ENOTDIR})


def _unresolvable(setting: str, raw: str, exc: Exception) -> StoreLayoutError:
    """The refusal for a path that will not resolve, in the same message shape.

    A refusal rather than a traceback: before this, a symlink loop as
    ``MUSICDROP_TRASH_DIR`` reached the lifespan uncaught, so the operator got a
    stack trace and no "refusing to start" line — the one thing the boot gate
    exists to print — and the same value through Validate/Save answered 500.
    """
    return StoreLayoutError(
        f"{setting} could not be resolved. It is set to {raw!r}, and resolving it"
        f" raised {type(exc).__name__}: {exc}. A symbolic-link loop and an embedded"
        " NUL byte are the two inputs measured to do this. Correct the value:"
        " until it resolves there is nothing to compare it against, so MusicDrop"
        " treats it the same way as a directory that sits on top of the library.",
        config_key=_CONFIG_KEY_OF.get(setting),
        unusable_value=True,
        headline=f"{setting} could not be resolved",
    )


def _unexaminable(setting: str, resolved: str, exc: OSError) -> StoreLayoutError:
    """The refusal for a path the filesystem will not describe, same message shape.

    Separate from :func:`_unresolvable` because the value DID resolve; what
    failed is the ``stat`` this module compares by.
    """
    return StoreLayoutError(
        f"{setting} could not be examined. It resolves to {resolved!r}, and asking"
        f" the filesystem about it raised {type(exc).__name__}: {exc}. Two"
        " spellings of one directory are told apart by inode, so until MusicDrop"
        " can stat it there is nothing to compare and it is treated the same way"
        " as a directory that sits on top of the library. Correct the value, or"
        " the permissions on the path it names.",
        config_key=_CONFIG_KEY_OF.get(setting),
        unusable_value=True,
        headline=f"{setting} could not be examined",
    )


def _resolved(path: Path, setting: str) -> Path:
    """Absolute, symlink-free, ``..``-free — the form this module compares.

    Every path entering :func:`check_store_layout` goes through here, so a call
    site cannot hold one side of a comparison in a lexical spelling.

    It also refuses a path that EXISTS and cannot be stat'd. Non-strict
    ``Path.resolve()`` re-raises only ELOOP, so a Trash behind a mode-000
    directory came back as the string it was handed and every inode comparison
    became a string comparison: measured, that layout was allowed at boot and
    then answered 500 at all four Trash routes. ENOENT and ENOTDIR pass — none
    of the paths has to exist yet.
    """
    resolved = _guarded(setting, str(path), lambda: Path(os.path.expanduser(str(path))).resolve())
    try:
        resolved.stat()
    except OSError as exc:
        if exc.errno not in _ABSENT_ERRNOS:
            raise _unexaminable(setting, str(resolved), exc) from exc
    return resolved


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

    The module's one seam onto the filesystem, kept separate so a test can answer
    for it without patching ``os.stat`` for the whole process.

    ``None`` means "no identity to compare", not "absent": any ``stat`` failure
    lands here. The participants do not reach it in that state —
    :func:`_resolved` refuses every errno outside :data:`_ABSENT_ERRNOS` first —
    so a ``None`` covers a not-yet-created path and an ANCESTOR walked by
    :func:`_chain`.
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

    ``resolve()`` collapses symlinks and ``..``. It does not collapse a bind
    mount, a case-insensitive filesystem or a unicode-normalising one, so two
    strings can be one directory — measured with a bind mount in an unprivileged
    user namespace: the spellings compared as unrelated trees, the layout passed,
    and Empty Trash removed the library.

    So the filesystem decides when both rungs have an identity. When either does
    not, the spelling is all that is left; that is the residual — a Trash the
    operator has not created yet, aliased to the library by a mount, is not
    caught here. It is caught the next time the check runs with the directory
    present (every delete, every sweep) and at the moment of destruction by
    ``app.beets.protected``.
    """
    if a[1] == b[1]:
        return True
    return a[0] is not None and a[0] == b[0]


def _same_path(a: Path, b: Path) -> bool:
    """:func:`_same_rung` for two paths no chain has been built for."""
    return _same_rung((_stat_id(a), str(a)), (_stat_id(b), str(b)))


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
    <Fix>`` - one line in ``docker logs``, one paragraph on the Trash page. Owner
    ruling 2026-09-04: "please please simplify the notes and descriptions on the
    app - long paragraphs are just a waste of space."

    Both paths go through ``repr``: two characters against a path holding a
    newline or an ANSI escape forging a second log line, which is the promise the
    shape above makes. Echoing them at all is a deliberate trade for a
    single-operator, session-gated app - the operator needs to see where their
    own setting landed, and the same session can read those paths from
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


#: The remedy each refusal ends with. FIXED strings, one per setting the row
#: tells the operator to move - no computed example spellings. Those were built
#: by testing candidate paths against the rule and dropping the ones it would
#: refuse, which is a second copy of the rule carrying its own guards: the
#: reviewers found three rows it had never been taught about, and no test failed
#: when the guards were removed. What is left is the PROPERTY the directory
#: needs, which is the part an operator can act on either way.
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
#: breaking several rows gets. ``B`` against ``M`` comes first because those two
#: are the trees the other three are placed relative to, and ``L``'s rows come
#: last because the four DIRECTORIES have to be sane before where the database
#: file sits is the interesting question.
#:
#: Twelve ``raise`` blocks used to spell this out at ~14 lines each, and the
#: reviewers found three of them missing from the remedy builders' idea of the
#: same rule. One table cannot disagree with itself.
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

#: D2 - the Trash and the origin store are DEDICATED directories: neither may be,
#: contain, or sit inside any other app-owned store. Measured in the review
#: round: ``MUSICDROP_TRASH_DIR=<B>/plex`` (or ``playlists``, ``bank``,
#: ``slskd``, ``inbox``) booted clean and Empty Trash wiped that store. Four
#: generated rows per store rather than a hand-written block each, so a sixth
#: store is one entry in ``protected._APP_STORES`` and nothing here.
#:
#: ``(participant key, cost of holding a store, cost of sitting in one, fix)``.
_DEDICATED: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("trash", "Empty Trash would delete it", "trashed albums would land in it", _FIX_TRASH),
    (
        "origins",
        "the store sweep would unlink *.json files in it",
        "restore records would land in it",
        _FIX_ORIGINS,
    ),
)

#: The participant keys of the five paths the rule started with. Everything else
#: in the map is an app-owned store, and :func:`_store_rows` generates for it.
_FIVE: Final = frozenset({"music", "beets", "trash", "origins", "library"})


def _store_rows(stores: tuple[str, ...]) -> tuple[_Row, ...]:
    """D2's rows: four per app-owned store the settings name."""
    rows: list[_Row] = []
    for key in stores:
        for subject, holds, sits, fix in _DEDICATED:
            rows.append(_Row(subject, key, ("is", "contains"), holds, fix))
            rows.append(_Row(key, subject, ("contains",), sits, fix))
    return tuple(rows)


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

    The paths are resolved here rather than by the caller, so no call site can
    compare a lexical spelling against a resolved one. ``settings`` brings the
    app-owned stores in as participants (D2); it is required rather than
    defaulted so a new call site cannot silently lose those rows.

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
    music_root, beets_root = participants["music"].path, participants["beets"].path
    stores: list[tuple[Path, str, str]] = [
        (export_dir(settings, music_root), "the playlist exports", EXPORT_SETTING),
        *app_owned_dirs(settings, beets_root),
    ]
    for path, name, setting in stores:
        participants[setting] = _Participant(_resolved(path, setting), name, setting)

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


def handle_music_and_library(handle: LibraryHandle) -> tuple[Path, Path]:
    """``(M, L)`` as the opened library actually has them: music root, DB file.

    One function rather than a reach into ``handle.lib`` per caller, so the two
    beets attributes this app reads off an open ``Library`` are named once and
    stay inside the adapter boundary (CLAUDE.md rule 3).

    ``Library.path`` is what ``dbcore.Database.__init__`` stored, which is
    ``Path(os.fsdecode(path))`` on beets 2.13 — but ``os.fsdecode`` is applied
    here too so a bytes path from an older beets still lands as a ``Path``.
    """
    return Path(_music_dir(handle.lib)), Path(os.fsdecode(handle.lib.path))


def checked_store_dirs(settings: Settings, handle: LibraryHandle) -> tuple[Path, Path]:
    """The ``(trash_dir, origins_dir)`` pair, checked at the moment of use.

    THE call every path that takes the pair makes instead of the two resolvers,
    including the lifespan, which hands what it returns to the import registry.
    The two returns are exactly what ``resolve_trash_dir`` /
    ``resolve_trash_origins_dir`` give, so nothing downstream changes shape.
    (``app/main.py`` used to check here and then resolve the pair AGAIN from the
    bare resolvers; measured, a Trash swapped between the two came up attached
    and unchecked, and only the import path's own re-check caught it.)

    It runs per call rather than once at boot because the configured STRING is
    fixed for the process lifetime and what it resolves to is not: replacing
    ``<M>/.trash`` with a symlink to ``<M>`` after startup was measured to turn
    ``DELETE /api/trash/all`` into a 200 that emptied the music library. The boot
    gate cannot see that; a check here does, because ``resolve()`` follows the
    link at this instant.

    Raises:
        StoreLayoutError: the layout is refused, or one of the paths would not
            resolve. Callers on a request path answer 503 with the message.
    """
    trash, origins = _resolve_store_dirs(settings, handle)
    music, library = handle_music_and_library(handle)
    check_store_layout(
        music_dir=music,
        beets_dir=handle.beets_dir,
        trash_dir=trash,
        origins_dir=origins,
        library_path=library,
        settings=settings,
    )
    return trash, origins


def checked_protected_trees(
    settings: Settings, handle: LibraryHandle, *, trash_dir: Path, origins_dir: Path
) -> ProtectedTrees:
    """The identities the movers and the remover refuse, for THIS request.

    Taken beside :func:`checked_store_dirs`, from the pair it returned, by the
    three sites that destroy or relocate a tree. Separate from that call because
    the other five callers do neither and would pay a dozen stats for nothing.
    """
    music, library = handle_music_and_library(handle)
    return protected_trees(
        settings=settings,
        music_dir=music,
        beets_dir=handle.beets_dir,
        trash_dir=trash_dir,
        origins_dir=origins_dir,
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

    Two departures from ``beets.config``, both required and neither global:

    * ``config_dir`` is pinned to the handle's beets dir instead of read from
      ``BEETSDIR``, so a relative ``directory:``, ``library:`` or ``include:``
      resolves against the directory beets will use for THIS handle;
    * the user source is the SUBMITTED document rather than ``config.yaml`` as it
      sits on disk, which is the whole point at Validate and Save time.

    Constructing one touches no beets global. ``confuse.Configuration.__init__``
    records the appname, the modname's package path and the env-var name and
    calls ``RootView.__init__([])`` — every source it later holds is its own list
    (``confuse/core.py:504-537``). ``beets.config``, confuse's caches and the
    plugin registry are untouched.
    """

    def __init__(self, beets_dir: Path) -> None:
        super().__init__("beets", "beets")
        self._pinned_config_dir = str(beets_dir)

    def config_dir(self) -> str:
        return self._pinned_config_dir


#: The largest ``include:`` file this gate will read. beets' own starter config
#: is under 4 KiB, so a megabyte is generous by more than two orders of
#: magnitude; what it bounds is the work ONE authenticated
#: ``POST /api/config/validate`` can ask a Starlette threadpool worker to do on a
#: route that writes nothing. Measured before the cap: a 195 MiB include took
#: 32.7 seconds inside :func:`effective_config_paths`.
_MAX_INCLUDE_BYTES: Final = 1 << 20


def _unreadable_include(detail: str) -> StoreLayoutError:
    """The refusal for an ``include:`` the gate could not follow.

    Painted against the ``include:`` key, so the editor's gutter marks the line
    the operator has to change.
    """
    return StoreLayoutError(
        f"`include:` in config.yaml could not be read: {detail}. An included"
        " file's `directory:` overrides the one in this document, so until this"
        " resolves there is no saying where the music library would end up."
        " Correct the include: list, or the file it names.",
        config_key="include",
        unusable_value=True,
        headline="`include:` in config.yaml could not be read",
    )


def _refuse_include_the_gate_will_not_open(target: str) -> None:
    """Ask ``stat`` about an ``include:`` entry before ``open`` gets a turn.

    ``os.stat`` returns for a FIFO where ``open`` does not: an include naming one
    was measured to hang Validate, Save and Apply until the process restarted,
    each pinning a threadpool worker, because confuse's ``YamlSource.__init__``
    reads the file eagerly.

    A ``stat`` that FAILS is left to ``set_file``, deliberately: confuse turns the
    same ``OSError`` into ``ConfigReadError``, and reproducing what beets then
    does with it is the caller's job, not this one's.
    """
    try:
        st = os.stat(target)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise _unreadable_include(f"{target!r} is not a regular file")
    if st.st_size > _MAX_INCLUDE_BYTES:
        raise _unreadable_include(
            f"{target!r} is {st.st_size} bytes, over the {_MAX_INCLUDE_BYTES}-byte"
            " limit this check reads"
        )


def effective_config_paths(
    document: Mapping[str, Any], beets_dir: Path
) -> tuple[str | None, str | None]:
    """The ``directory:`` and ``library:`` beets would LOAD from ``document``.

    Not ``document["directory"]``: beets merges every file listed under
    ``include:`` at HIGHEST priority (``beets/__init__.py:29-38``, and
    ``confuse/core.py:617`` documents ``set_file`` as "highest priority"), so an
    included file's ``directory:`` overrides the top-level key the editor shows.
    Measured in the review round: a document whose own ``directory:`` was safe,
    with an include pointing the library at the Trash dir, passed all three gates
    and left the process serving a layout the next boot refuses.

    Returns ``(None, None)`` when the document does not resolve to a filename at
    all — a ``directory:`` that is not a string, say. The caller reports that
    through the schema, which owns the "this key has the wrong type" message.
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
    # exposes — which is bounded by the session gate, and which the size/type
    # guard plus the rows below narrow to "this file exists and parses as a
    # mapping" rather than "here is its content".
    try:
        for view in cfg["include"].sequence():
            target = view.as_filename()
            _refuse_include_the_gate_will_not_open(target)
            cfg.set_file(target)
    except (confuse.NotFoundError, confuse.ConfigReadError):
        # The two shapes beets itself tolerates, so the gate tolerates them and
        # whatever has been merged so far stands: no ``include:`` key at all
        # (``NotFoundError``), and an entry naming a file that will not open or
        # parse (``ConfigReadError`` — ``beets/__init__.py:29-38`` writes to
        # stderr and carries on). Reproducing beets is the whole contract here,
        # and the ``except`` sits OUTSIDE the loop in beets too, so the FIRST
        # unreadable entry ends the merge and the ones after it are never read.
        # That is not a rewrite worth "fixing": measured with the guard placed
        # per-entry instead, this function reported an overlay's ``directory:``
        # that a real ``setup_beets`` over the same file never loaded.
        pass
    except (confuse.ConfigError, TypeError, ValueError) as exc:
        # Everything else. Measured on this tree, all three escaped the old
        # ``except confuse.ConfigError`` or were swallowed by it, and Validate,
        # Save and Apply answered a bare 500 or reported the document CLEAN:
        # ``ConfigTypeError`` for an ``include:`` that is not a list of filenames
        # (beets raises the same at startup, so Save was writing a config the
        # next start refuses), ``TypeError`` for an include file whose top level
        # is not a mapping, and ``ValueError`` for an entry holding a NUL.
        #
        # The NUL is a deliberate divergence: beets boots with it, because
        # ``setup.py``'s ``exists()`` turns the ValueError into a NotFoundError
        # and the entry is dropped. That tolerance is an accident of an
        # ``exists()`` call rather than a decision, and an entry with a NUL in it
        # cannot name a file — so this says so instead of reproducing it.
        raise _unreadable_include(str(exc)) from exc
    try:
        return cfg["directory"].as_filename(), cfg["library"].as_filename()
    except confuse.ConfigError:
        return None, None


def layout_error_for_config(
    *,
    document: Mapping[str, Any],
    settings: Settings,
    handle: LibraryHandle,
) -> StoreLayoutError | None:
    """The refusal a candidate ``config.yaml`` document would cause, or ``None``.

    Returns rather than raises: its callers (Save, Validate, Apply) turn it into
    a response body, and every one of them wants the message rather than a
    traceback. ``B``, ``T`` and ``O`` come from the LIVE settings and handle:
    they are env-derived, and the two values that move through the editor are the
    two :func:`effective_config_paths` reads back out of the document.
    """
    try:
        raw_directory, raw_library = effective_config_paths(document, handle.beets_dir)
        if raw_directory is None or raw_library is None:
            return None
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
    except StoreLayoutError as exc:
        return exc
    return None
