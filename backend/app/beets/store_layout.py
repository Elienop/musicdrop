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

import os
from pathlib import Path
from typing import Final

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
    """

    def __init__(self, message: str, *, config_key: str | None = None) -> None:
        super().__init__(message)
        self.config_key = config_key


def _resolved(path: Path) -> Path:
    """Absolute, symlink-free, ``..``-free — the only form this module compares."""
    return Path(os.path.expanduser(str(path))).resolve()


def _relation(container: Path, inner: Path) -> str | None:
    """``"is"`` when the two are the same directory, ``"contains"`` when ``inner``
    is strictly below ``container``, ``None`` when neither holds.

    Both arguments must already be ``_resolved``; ``is_relative_to`` compares
    path components, so ``/data/trash`` does not read as containing
    ``/data/trash-origins`` the way a string prefix would.
    """
    if container == inner:
        return "is"
    if inner.is_relative_to(container):
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
    music = _resolved(music_dir)
    beets = _resolved(beets_dir)
    trash = _resolved(trash_dir)
    origins = _resolved(origins_dir)
    library = _resolved(library_path)

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


def require_safe_store_layout(settings: Settings, handle: LibraryHandle) -> None:
    """The boot-time and Apply-time check, from the live settings + handle.

    Resolves the same ``trash_dir`` / ``trash_origins_dir`` the delete paths
    themselves resolve (``app.beets.trash``), so the check is asked of the same
    pair of paths those paths use, as they stand at this moment. It says nothing
    about later moments: the delete and sweep call sites re-run it themselves.
    """
    check_store_layout(
        music_dir=Path(_music_dir(handle.lib)),
        beets_dir=handle.beets_dir,
        trash_dir=resolve_trash_dir(settings, handle),
        origins_dir=resolve_trash_origins_dir(settings, handle),
        library_path=_handle_library_path(handle),
    )


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


def layout_error_for_config(
    *,
    raw_directory: str,
    raw_library: str | None,
    settings: Settings,
    handle: LibraryHandle,
) -> StoreLayoutError | None:
    """The refusal a candidate ``directory:`` + ``library:`` pair would cause.

    Returns rather than raises: its callers (Save, Validate, Apply) turn it into
    a response body, and every one of them wants the message rather than a
    traceback. ``B``, ``T`` and ``O`` come from the LIVE settings and handle:
    they are env-derived, and the two values that move through the editor are the
    two this takes.

    ``raw_library`` of ``None`` means the document has no ``library:`` key, which
    is what beets' own bundled default covers — ``library: library.db``, relative
    to ``BEETSDIR`` (``beets/config_default.yaml:3``). That default is what gets
    checked, so a document that drops the key is held to the same rule as one
    that spells it out.
    """
    library_raw = "library.db" if raw_library is None else raw_library
    try:
        check_store_layout(
            music_dir=resolve_configured_path(raw_directory, handle.beets_dir),
            beets_dir=handle.beets_dir,
            trash_dir=resolve_trash_dir(settings, handle),
            origins_dir=resolve_trash_origins_dir(settings, handle),
            library_path=resolve_configured_path(library_raw, handle.beets_dir),
        )
    except StoreLayoutError as exc:
        return exc
    return None
