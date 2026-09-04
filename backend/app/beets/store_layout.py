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
                           music root, and an exclude root at or above the walk
                           root matched every candidate when it was measured
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
=========================  =========================================================

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
from typing import Any, Final

import beets
import confuse

from app.beets.library import LibraryHandle, _music_dir
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
    "checked_store_dirs",
    "effective_config_paths",
    "layout_error_for_config",
    "require_safe_store_layout",
    "resolve_configured_path",
]

#: How each of the five inputs is spelled for the operator who has to change it.
TRASH_SETTING = "MUSICDROP_TRASH_DIR"
ORIGINS_SETTING = "MUSICDROP_TRASH_ORIGINS_DIR"
MUSIC_SETTING = "`directory:` in config.yaml"
LIBRARY_SETTING = "`library:` in config.yaml"
BEETS_SETTING = "MUSICDROP_BEETS_DIR"

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
    """

    def __init__(
        self, message: str, *, config_key: str | None = None, unusable_value: bool = False
    ) -> None:
        super().__init__(message)
        self.config_key = config_key
        self.unusable_value = unusable_value


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
    )


def _resolved(path: Path, setting: str) -> Path:
    """Absolute, symlink-free, ``..``-free — the form this module compares.

    Every path entering :func:`check_store_layout` goes through here, so a call
    site cannot hold one side of a comparison in a lexical spelling.

    It also refuses a path the process cannot ``stat``. Non-strict
    ``Path.resolve()`` re-raises only ELOOP, so a Trash path behind an
    untraversable directory comes back as the string it was handed and every
    inode comparison below silently becomes a string comparison — measured, that
    layout was allowed at boot and then answered 500 (``PermissionError``) at all
    four Trash routes. ENOENT and ENOTDIR are allowed through: none of the five
    has to exist yet.
    """
    try:
        resolved = Path(os.path.expanduser(str(path))).resolve()
    except _UNRESOLVABLE as exc:
        raise _unresolvable(setting, str(path), exc) from exc
    try:
        resolved.stat()
    except OSError as exc:
        if exc.errno not in _ABSENT_ERRNOS:
            raise _unexaminable(setting, str(resolved), exc) from exc
    return resolved


def _guarded(setting: str, raw: str, resolve: Callable[[], Path]) -> Path:
    """Run a resolver that is outside this module and relay its failure as a refusal.

    ``resolve_trash_dir`` and :func:`resolve_configured_path` both call
    ``Path.resolve()`` themselves, so they raise BEFORE
    :func:`check_store_layout` sees anything — widening the ``except`` inside
    the check would not have caught them.
    """
    try:
        return resolve()
    except _UNRESOLVABLE as exc:
        raise _unresolvable(setting, raw, exc) from exc


def _stat_id(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` for a path this process can stat, ``None`` otherwise.

    The one seam :func:`_same_path` uses to ask the filesystem, kept separate so
    a test can answer for it without patching ``os.stat`` for the whole process.

    ``None`` means "no identity to compare", which is not the same as "absent":
    any ``stat`` failure lands here. The five paths the rule is about do not
    reach this in that state — :func:`_resolved` refuses every errno outside
    :data:`_ABSENT_ERRNOS` before a comparison runs — so what a ``None`` here
    covers is a not-yet-created path, an ANCESTOR walked by :func:`_relation`,
    and a candidate spelling a remedy is trying out.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _same_path(a: Path, b: Path) -> bool:
    """Whether two resolved paths name ONE directory or file.

    ``resolve()`` collapses symlinks and ``..``. It does not collapse a bind
    mount, a case-insensitive filesystem or a unicode-normalising one, so two
    different strings can be one directory — measured in the review round with a
    bind mount in an unprivileged user namespace: the two spellings compared as
    unrelated trees, the layout passed, and Empty Trash removed the library.

    So when both paths can be stat'd the filesystem decides, by inode. When
    either cannot, the string comparison is all that is left; that is the
    residual — a Trash directory the operator has not created yet, aliased to the
    library by a mount, is not caught here. It is caught the next time the check
    runs with the directory present, which the delete and sweep call sites do. A
    path that exists but cannot be stat'd does not reach the residual: it is
    refused in :func:`_resolved`.
    """
    if a == b:
        return True
    a_id = _stat_id(a)
    return a_id is not None and a_id == _stat_id(b)


def _relation(container: Path, inner: Path) -> str | None:
    """``"is"`` when the two are the same directory, ``"contains"`` when ``inner``
    is strictly below ``container``, ``None`` when neither holds.

    Both arguments must already be ``_resolved``. Containment walks ``inner``'s
    ancestors and asks :func:`_same_path` about each rather than testing one
    string prefix, because the container can be an ALIAS of an ancestor rather
    than that ancestor's own spelling: a bind mount of ``<M>``'s parent onto the
    Trash path leaves the two endpoints with different inodes, so an
    identity-only fix would still have passed that layout (measured in the
    review round, where Empty Trash then removed both the music dir and the
    beets dir).

    ``is_relative_to`` compares path components, which is what the equality
    branch of :func:`_same_path` reproduces for a not-yet-created path, so
    ``/data/trash`` still does not read as containing ``/data/trash-origins``
    the way a string prefix would.
    """
    if _same_path(container, inner):
        return "is"
    for ancestor in inner.parents:
        if _same_path(container, ancestor):
            return "contains"
    return None


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

    Written for somebody reading ``docker logs`` or the editor's gutter with no
    access to this source: what the layout is, where each side resolved to, what
    it would have cost, and which setting to move.

    Both resolved paths are echoed verbatim, and one of them can be the resolved
    form of a caller-supplied ``directory:``. That is a deliberate trade for a
    single-operator, session-gated app: the operator needs to see where their own
    setting landed, and the same session can already read those paths from
    ``GET /api/config``.
    """
    return StoreLayoutError(
        f"{subject} {relation} {other}. "
        f"{setting} resolves to {str(subject_path)!r}; "
        f"{other_setting} resolves to {str(other_path)!r}. "
        f"{loss} {fix}",
        # Either side of the pair can be the config.yaml key; the subject is
        # asked first because that is the setting the message tells the operator
        # to move, and the gutter should mark the line the sentence is about.
        config_key=_CONFIG_KEY_OF.get(setting) or _CONFIG_KEY_OF.get(other_setting),
    )


def _disjoint(a: Path, b: Path) -> bool:
    """Neither path is the other, and neither contains the other."""
    return _relation(a, b) is None and _relation(b, a) is None


def _trash_fix(*, music: Path, beets: Path, origins: Path) -> str:
    """The remedy sentence for a refused ``MUSICDROP_TRASH_DIR``.

    Each spelling it offers is tested against the layout at hand first, because a
    fix that names a directory this same function would refuse sends the operator
    round the loop again. What is left when a spelling drops out is the PROPERTY
    the directory needs, which is still actionable.
    """
    offers = []
    inside_music = music / ".trash"
    if _disjoint(inside_music, beets) and _disjoint(inside_music, origins):
        offers.append(
            f" A folder INSIDE the music library, such as {str(inside_music)!r},"
            " is allowed and makes deletes same-disk renames."
        )
    default = beets / "trash"
    if _disjoint(default, music) and _disjoint(default, origins):
        offers.append(f" Unsetting {TRASH_SETTING} falls back to {str(default)!r}.")
    return (
        f"Point {TRASH_SETTING} at a directory that neither is nor contains the"
        " music library, the beets data directory, the beets database or the"
        " Trash origin store." + "".join(offers)
    )


def _origins_fix(*, music: Path, beets: Path, trash: Path) -> str:
    """The remedy sentence for a refused ``MUSICDROP_TRASH_ORIGINS_DIR``.

    Same rule as :func:`_trash_fix`: the default sibling is offered only when the
    default itself passes. It used to be offered unconditionally, and with the
    beets dir inside the music library that default WAS the refused path — the
    message named the directory it had just refused as the way out.
    """
    default = beets / "trash-origins"
    offer = ""
    default_ok = (
        _disjoint(default, music)
        and _disjoint(default, trash)
        and _relation(default, beets) is None
    )
    if default_ok:
        offer = f" Unsetting it falls back to {str(default)!r}."
    return (
        f"Point {ORIGINS_SETTING} at a directory of its own, outside the music"
        " library and not overlapping the Trash." + offer
    )


def check_store_layout(
    *,
    music_dir: Path,
    beets_dir: Path,
    trash_dir: Path,
    origins_dir: Path,
    library_path: Path,
) -> None:
    """Raise :class:`StoreLayoutError` on any refused relationship among the five.

    The five are resolved here rather than by the caller, so no call site can
    compare a lexical path against a resolved one. Order of the checks decides
    only WHICH message a layout that breaks several rules gets; every refused
    layout raises whichever comes first. ``B`` versus ``M`` runs first because
    those two are the trees the other three are placed relative to.
    """
    music = _resolved(music_dir, MUSIC_SETTING)
    beets = _resolved(beets_dir, BEETS_SETTING)
    trash = _resolved(trash_dir, TRASH_SETTING)
    origins = _resolved(origins_dir, ORIGINS_SETTING)
    library = _resolved(library_path, LIBRARY_SETTING)

    trash_fix = _trash_fix(music=music, beets=beets, origins=origins)
    origins_fix = _origins_fix(music=music, beets=beets, trash=trash)
    # No spelling is offered for the beets dir unless it passes the rule that
    # just fired: the shipped ``/data/beets`` is a good answer only when the
    # music library is somewhere else, which is the layout this rule is about.
    beets_elsewhere = Path("/data/beets")
    beets_fix = (
        f"Point {BEETS_SETTING} at a directory that neither is, contains, nor sits"
        f" inside the music library — the shipped image uses {str(beets_elsewhere)!r}"
        f" with the library at '/music' — or point {MUSIC_SETTING} at a library"
        " that does not overlap it."
        if _disjoint(beets_elsewhere, music)
        else (
            f"Point {BEETS_SETTING} at a directory that neither is, contains, nor"
            f" sits inside the music library, or point {MUSIC_SETTING} at a library"
            " that does not overlap it."
        )
    )
    library_fix = (
        f"Point {LIBRARY_SETTING} at a file outside the Trash and outside the"
        " Trash origin store — a relative value is taken from the beets data"
        f" directory, so the default 'library.db' resolves to"
        f" {str(beets / 'library.db')!r}."
    )

    if _relation(beets, music) == "is":
        raise _refuse(
            subject="The beets data directory",
            relation="is",
            other="the music library",
            setting=BEETS_SETTING,
            subject_path=beets,
            other_setting=MUSIC_SETTING,
            other_path=music,
            loss=(
                "The beets data directory holds the app's own folders — bank/,"
                " plex/, slskd/, playlists/, inbox/ — and none of them holds"
                " audio, so as the music library it is also the tree a"
                " library-scope Reorganize walks and offers for trashing."
            ),
            fix=beets_fix,
        )

    if _relation(music, beets) == "contains":
        raise _refuse(
            subject="The music library",
            relation="contains",
            other="the beets data directory",
            setting=MUSIC_SETTING,
            subject_path=music,
            other_setting=BEETS_SETTING,
            other_path=beets,
            loss=(
                "A library-scope Reorganize walks the music library and a"
                " whole-folder delete takes a folder's whole subtree, so this"
                " layout puts library.db and config.yaml where both can move"
                " them — measured on d65e635: a plain-named beets dir inside the"
                " library was reported by the orphan sweep."
            ),
            fix=beets_fix,
        )

    if _relation(beets, music) == "contains":
        raise _refuse(
            subject="The beets data directory",
            relation="contains",
            other="the music library",
            setting=BEETS_SETTING,
            subject_path=beets,
            other_setting=MUSIC_SETTING,
            other_path=music,
            loss=(
                "The orphan sweep keeps away from the app's own folders by path"
                " prefix, so a beets data directory above the music library makes"
                " every one of those exclusions an ancestor of the walk root — and"
                " an exclude root at or above the walk root matched every candidate"
                " when it was measured, which reports nothing at all."
            ),
            fix=beets_fix,
        )

    relation = _relation(trash, music)
    if relation is not None:
        raise _refuse(
            subject="The Trash directory",
            relation=relation,
            other="the music library",
            setting=TRASH_SETTING,
            subject_path=trash,
            other_setting=MUSIC_SETTING,
            other_path=music,
            loss=(
                "Emptying the Trash permanently removes every entry under it,"
                " so this layout would delete the music library."
            ),
            fix=trash_fix,
        )

    relation = _relation(trash, beets)
    if relation is not None:
        raise _refuse(
            subject="The Trash directory",
            relation=relation,
            other="the beets data directory",
            setting=TRASH_SETTING,
            subject_path=trash,
            other_setting=BEETS_SETTING,
            other_path=beets,
            loss=(
                "Emptying the Trash permanently removes every entry under it, so"
                " this layout would delete library.db, config.yaml and the Trash"
                " origin records."
            ),
            fix=trash_fix,
        )

    relation = _relation(origins, music)
    if relation is not None:
        raise _refuse(
            subject="The Trash origin store",
            relation=relation,
            other="the music library",
            setting=ORIGINS_SETTING,
            subject_path=origins,
            other_setting=MUSIC_SETTING,
            other_path=music,
            loss=(
                "Emptying the Trash sweeps the store, unlinking every *.json"
                " file directly inside it, so this layout would delete JSON"
                " files from the music library."
            ),
            fix=origins_fix,
        )

    relation = _relation(origins, beets)
    if relation is not None:
        raise _refuse(
            subject="The Trash origin store",
            relation=relation,
            other="the beets data directory",
            setting=ORIGINS_SETTING,
            subject_path=origins,
            other_setting=BEETS_SETTING,
            other_path=beets,
            loss=(
                "Emptying the Trash sweeps the store, unlinking every *.json"
                " file directly inside it, so this layout would delete JSON"
                " files from the beets data directory."
            ),
            fix=origins_fix,
        )

    if _relation(music, origins) == "contains":
        raise _refuse(
            subject="The music library",
            relation="contains",
            other="the Trash origin store",
            setting=MUSIC_SETTING,
            subject_path=music,
            other_setting=ORIGINS_SETTING,
            other_path=origins,
            loss=(
                "Deleting any folder above the store moves the records into"
                " Trash with it, and the record is what Restore reads to put a"
                " trashed folder back where it came from."
            ),
            fix=origins_fix,
        )

    relation = _relation(trash, origins)
    if relation is not None:
        raise _refuse(
            subject="The Trash directory",
            relation=relation,
            other="the Trash origin store",
            setting=TRASH_SETTING,
            subject_path=trash,
            other_setting=ORIGINS_SETTING,
            other_path=origins,
            loss=(
                "Emptying the Trash would delete the records it needs, and each"
                " record file would also be listed as a trashed entry of its own."
            ),
            fix=origins_fix,
        )

    if _relation(origins, trash) == "contains":
        raise _refuse(
            subject="The Trash origin store",
            relation="contains",
            other="the Trash directory",
            setting=ORIGINS_SETTING,
            subject_path=origins,
            other_setting=TRASH_SETTING,
            other_path=trash,
            loss=(
                "Trashed albums would land among the origin records, which both"
                " the store sweep and the Trash listing walk."
            ),
            fix=origins_fix,
        )

    # ``library:`` is its own beets key, so the database file can be moved into
    # Trash while B, T and O stay disjoint. Its own two rows, checked last
    # because the four DIRECTORIES have to be sane before where the DB sits is
    # the interesting question.
    relation = _relation(trash, library)
    if relation is not None:
        raise _refuse(
            subject="The Trash directory",
            relation=relation,
            other="the beets database",
            setting=TRASH_SETTING,
            subject_path=trash,
            other_setting=LIBRARY_SETTING,
            other_path=library,
            loss=(
                "Emptying the Trash permanently removes every entry under it, so"
                " this layout would delete library.db and the migration backups"
                " beets writes beside it."
            ),
            fix=library_fix,
        )

    relation = _relation(origins, library)
    if relation is not None:
        raise _refuse(
            subject="The Trash origin store",
            relation=relation,
            other="the beets database",
            setting=ORIGINS_SETTING,
            subject_path=origins,
            other_setting=LIBRARY_SETTING,
            other_path=library,
            loss=(
                "The store sweep unlinks every *.json directly inside the origin"
                " store. library.db is not a *.json, so that sweep leaves it;"
                " what this refuses is the database sharing a directory the app"
                " prunes on its own schedule, beside records it keys by Trash"
                " entry name."
            ),
            fix=library_fix,
        )


def _handle_library_path(handle: LibraryHandle) -> Path:
    """The beets database file the handle actually opened.

    ``Library.path`` is what ``dbcore.Database.__init__`` stored, which is
    ``Path(os.fsdecode(path))`` on beets 2.13 — but ``os.fsdecode`` is applied
    here too so a bytes path from an older beets still lands as a ``Path``.
    """
    return Path(os.fsdecode(handle.lib.path))


def checked_store_dirs(settings: Settings, handle: LibraryHandle) -> tuple[Path, Path]:
    """The ``(trash_dir, origins_dir)`` pair, checked at the moment of use.

    THE call every destructive path makes instead of the two resolvers, so a
    site cannot take the pair without taking the check with it. The two returns
    are exactly what ``resolve_trash_dir`` / ``resolve_trash_origins_dir`` give,
    so nothing downstream changes shape.

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
    check_store_layout(
        music_dir=Path(_music_dir(handle.lib)),
        beets_dir=handle.beets_dir,
        trash_dir=trash,
        origins_dir=origins,
        library_path=_handle_library_path(handle),
    )
    return trash, origins


def require_safe_store_layout(settings: Settings, handle: LibraryHandle) -> None:
    """The boot-time and Apply-time check, from the live settings + handle.

    :func:`checked_store_dirs` without the pair — the same question, asked where
    there is nothing yet to hand a Trash path to. Startup is the right place for
    it even though every destructive site re-asks: a refused layout should stop
    the process rather than wait for the first delete, and the operator gets one
    ERROR line naming the setting instead of a 503 on a button they pressed.
    """
    checked_store_dirs(settings, handle)


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


def resolve_configured_path(raw: str, beets_dir: Path) -> Path:
    """Resolve a ``directory:`` or ``library:`` value the way beets will.

    Both keys are confuse ``Filename`` templates and share one resolution rule,
    which is why one function serves them: ``directory:`` names ``M`` and
    ``library:`` names ``L``.

    confuse's ``Filename.value`` (``confuse/templates.py``, the ``value`` body):
    ``expanduser`` first, then — for a value that came from a file, with the
    default ``in_source_dir=False`` and beets' source not setting
    ``base_for_paths`` — a RELATIVE path is joined to ``view.root().config_dir()``,
    which is ``BEETSDIR``, and the result goes through ``abspath``. The starter
    config says the same thing in its own words ("Paths are relative to this
    file's directory") and ships ``directory: ../music``.

    ``resolve()`` is ours, not confuse's: this module compares symlink-free
    paths on both sides, and confuse stops at ``abspath``.

    NOTE the pre-existing disagreement this does NOT change: the
    ``WritablePath`` validator in ``app/models/config_editor.py`` resolves the
    same value against the process CWD instead. Both run on a Save; they can
    disagree only for a relative ``directory:``, and only about which directory
    is checked for writability.
    """
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(str(beets_dir), expanded)
    return Path(os.path.abspath(expanded)).resolve()


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
        check_store_layout(
            music_dir=_guarded(
                MUSIC_SETTING,
                raw_directory,
                lambda: resolve_configured_path(raw_directory, handle.beets_dir),
            ),
            beets_dir=handle.beets_dir,
            trash_dir=trash,
            origins_dir=origins,
            library_path=_guarded(
                LIBRARY_SETTING,
                raw_library,
                lambda: resolve_configured_path(raw_library, handle.beets_dir),
            ),
        )
    except StoreLayoutError as exc:
        return exc
    return None
