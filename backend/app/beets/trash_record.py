"""The origin record a trashed folder carries so it can be put back.

A folder MusicDrop moves to Trash is a bare ``shutil.move`` away from being
unrecoverable: the destination name says what the album was called, never where
it lived. This module is the record that closes that — one small JSON sidecar
written INSIDE the trashed folder, holding the absolute folder it came from.

**A sidecar, not a central manifest** (owner's decision, 2026-08-27). It travels
with the folder if the user moves it by hand, it survives manual cleanup,
``Empty`` needs no extra logic because the ``rmtree`` takes the record with it,
and a failure to write one folder's record cannot touch another's. The accepted
cost is that the restore path has to delete it on the way back
(:func:`delete_trash_origin`).

**Writing a record must never make a delete fail.** :func:`write_trash_origin`
swallows everything and logs; a missing record degrades a Trash row to exactly
today's behaviour (import-restore, or nothing for an audio-free husk), which is
the floor this whole feature has to stay above.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.wire import display_path

logger = logging.getLogger(__name__)

#: The sidecar's filename. Dot-prefixed on purpose: beets ships
#: ``ignore: [".*"]`` + ``ignore_hidden: yes`` (``config_default.yaml:19-20``),
#: so the importer skips it and neither the existing import-restore nor the
#: in-place restore import ever offers it to ``Item.from_path``. A user CAN
#: override ``ignore`` in their own config, so that is a default and not an
#: immunity — the fallback is benign (beets tries to read a JSON file as media,
#: fails, logs it and moves on), but nothing here should be written as if the
#: default were guaranteed.
RECORD_NAME = ".musicdrop-trash.json"

#: Bumped only for a change a previous reader would misread. An unknown schema
#: reads as "no record", which degrades to import-restore rather than acting on
#: a payload whose meaning has moved.
_SCHEMA = 1

#: Refuse to parse anything larger. The record is four keys; a big file under
#: this name is not one of ours and must not be read into memory to find out.
_MAX_BYTES = 64 * 1024

#: The longest ``origin`` accepted, in characters. Grounded rather than round:
#: Linux caps a path at ``PATH_MAX`` = 4096 BYTES including the terminating NUL
#: (``linux/limits.h``), and a UTF-8 string never has more characters than bytes
#: — so no folder this app could have moved anything out of has an origin longer
#: than this, and a longer string is padding, not a path. Distinct from
#: :data:`_MAX_BYTES`, which is 16x larger and answers a different question ("do
#: not read a big file into memory"); this one answers "is this a path at all".
#: Without it a 60,000-character origin parses and renders into the Trash row.
_MAX_ORIGIN_CHARS = 4096

#: Characters an ``origin`` may not contain. Two groups, rejected for two
#: different reasons:
#:
#: * **C0/C1 controls and DEL.** NUL is the one that BREAKS something rather
#:   than merely looking wrong: ``Path.exists()`` swallows the ``ValueError`` an
#:   embedded NUL raises and answers ``False``, so ``trash_manage``'s occupancy
#:   guard PASSES and the failure surfaces two lines later at ``os.rename`` as a
#:   ``ValueError`` that no ``except OSError`` catches — a blanket 500 on a row
#:   the UI had just labelled "Exact restore". The rest are legal in a POSIX
#:   path and refused anyway: this string is rendered in the Trash UI and named
#:   in log lines, and a newline or an ANSI escape in it is a forged line rather
#:   than a path.
#: * **Bidi controls** (the Trojan-Source set: U+061C, U+200E/U+200F,
#:   U+202A-U+202E, U+2066-U+2069). They reorder the text around them, so a
#:   U+202E turns the one field whose entire job is telling the user where their
#:   files will go into something that reads as a different path.
#:
#: That second reason is broader than this pattern, and the gap is stated here
#: rather than left for the next reader to discover. This is a DENYLIST of two
#: named groups, not a predicate over "characters that misrepresent a path", and
#: several that plainly do are absent. Measured: U+200B zero-width space, U+00AD
#: soft hyphen, U+034F combining grapheme joiner and U+00A0 no-break space all
#: pass the filter, parse, earn a ``move_back_target`` and render intact into the
#: Trash row -- so a row can promise ``/music/Artist/Album`` and move the folder
#: to a visually identical path that is not it.
#:
#: Left open deliberately, and widening this pattern is the WRONG fix. The
#: consequence is bounded by ``move_back_target``'s containment: the folder
#: lands somewhere inside the user's own library under an unexpected name --
#: confusing, not a capability. Rejection is not free the way it looks: an album
#: folder genuinely named with a no-break space (ordinary in Windows-authored
#: names) would lose its exact restore permanently, which is a real loss traded
#: against a cosmetic one. If it is ever closed, the place is the DISPLAY, where
#: non-printing characters can be escaped without denying anyone a restore --
#: a UI decision, not a validation one.
#:
#: Lone surrogates are deliberately NOT here. U+DC80-U+DCFF is how a non-UTF-8
#: POSIX filename survives ``os.fsdecode``, so rejecting them would deny an exact
#: restore to precisely the paths this app takes the most care over.
#:
#: The cost is stated rather than hidden: a folder genuinely named with a control
#: character loses its move-back and degrades to import-restore. That is the
#: documented fallback for any record we cannot trust, not a failure.
_REJECTED_IN_ORIGIN = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

#: What the mover actually relocated, which is what decides whether a faithful
#: move-back is even possible:
#:
#: * ``"folder"`` — one whole directory moved wholesale
#:   (``trash_album_folder``'s folder branch, ``trash_folder``). The trash entry
#:   IS that directory, so putting it back is a single move.
#: * ``"items"`` — beets relocated the album's tracked FILES individually out of
#:   a folder it shared with other music (``trash_album``). The origin is
#:   recorded for display, but a move-back is not offered: the files would have
#:   to go back among a stranger's, and the re-import that has to follow takes a
#:   DIRECTORY (``ImportTaskFactory.paths`` makes one album task per file when
#:   handed files), so it would sweep the neighbours into the same album.
MovedShape = Literal["folder", "items"]


@dataclass(frozen=True)
class TrashOrigin:
    """Where a trashed folder came from, as read back off disk.

    Only ever constructed by :func:`read_trash_origin`, which validates every
    field — so ``origin`` is always an absolute path string and ``moved`` is
    always one of the two shapes.

    The payload's ``trashed_at`` is deliberately NOT a field here. It was: read
    back, type-checked, carried through every caller, and never consulted by
    anything. Surfacing it ("trashed 3 days ago" beside the origin) would be a
    contract change and a UI change; validating a value nobody reads is code
    that only looks like it does something. It is still WRITTEN, because the
    sidecar is a file a human opens and a rename preserves the folder's own
    mtime — so the record is the only place the trash TIME survives at all, and
    keeping it on disk means a later decision to show it has no data gap to
    apologise for.
    """

    origin: str
    moved: MovedShape


def write_trash_origin(entry: Path, *, origin: str, moved: MovedShape) -> None:
    """Record that ``entry``'s contents came from ``origin``. NEVER raises.

    Called immediately after the relocation, on the DESTINATION side — the
    trashed folder — rather than into the source before the move. Both orders
    have a failure window and only one of them is worse than doing nothing:
    writing into the source mutates a folder in the user's music library and, if
    the move then fails, leaves a stray file there that nothing will clean up
    (and that beets' own source-dir pruning would then refuse to remove). Writing
    into the destination touches only Trash, and its window — the folder sitting
    in Trash for a moment with no record — is precisely today's behaviour.

    Swallows every exception by design: this is a new write on the delete path,
    and a delete that fails because its bookkeeping failed would be a worse
    outcome than the unrecoverable-but-completed delete it replaces.

    **Never writes through a symlink, and that is a security property, not
    tidiness.** ``entry`` holds whatever the source folder held, because
    ``shutil.move`` preserves symlinks — so anyone who can write into the MUSIC
    library can pre-plant ``.musicdrop-trash.json`` as a link to any file this
    process can write, and an ordinary album delete would then truncate it.
    Measured before this was fixed: deleting one album overwrote beets'
    ``config.yaml`` and truncated ``library.db`` from 4112 bytes to 148 — files
    on the ``/data`` side of a deployment whose whole point is that ``/music``
    cannot reach them. The attacker needs no HTTP access and no session; a
    household Samba export or an ``*arr`` container on the same media volume is
    enough, and the trigger is the owner deleting an album.

    ``mkstemp`` + ``os.replace`` closes it: ``mkstemp`` opens ``O_CREAT|O_EXCL``
    on an unpredictable name, so it cannot follow a link, and ``os.replace``
    swaps the DIRECTORY ENTRY rather than writing through whatever sits there.
    The random name is load-bearing — a fixed ``.tmp`` suffix would just move
    the plant one step. It also makes the write atomic, so a reader can no
    longer see a half-written record.
    """
    # ``trashed_at`` has no reader by design — see :class:`TrashOrigin`. It is
    # written for the human who opens the file, and because a rename preserves
    # the folder's own mtime, so nothing else on disk records when the move
    # happened. Do not "clean it up" as unused; do not add a reader without
    # deciding what shows it.
    payload = {
        "schema": _SCHEMA,
        "trashed_at": datetime.now(UTC).isoformat(),
        "origin": origin,
        "moved": moved,
    }
    try:
        # ``ensure_ascii`` (the default) escapes the lone surrogates a non-UTF-8
        # POSIX path carries into ``\udcXX``, so the text is pure ASCII and
        # round-trips through ``json.loads`` back to the same surrogate string
        # that ``os.fsencode`` needs. Writing with encoding="ascii" makes that a
        # checked property rather than an assumption.
        text = json.dumps(payload, indent=2, sort_keys=True)
        # ``mkstemp``'s O_EXCL protects the FINAL component only; ``dir=`` is
        # resolved normally, so a symlinked Trash entry sends both the temp file
        # and the ``os.replace`` straight out of Trash. Measured: an album whose
        # own folder is a symlink lands in Trash still a symlink (``shutil.move``
        # preserves them, and ``_album_root`` is just ``dirname(item.path)``), and
        # the record was written into the linked-to directory beside beets'
        # ``config.yaml``. Benign content at a fixed hidden name, so this is
        # litter rather than the overwrite primitive above -- but "touches only
        # Trash" has to be true, not nearly true. Raising here rather than
        # returning: the arm below is exactly the right degradation (no record,
        # folder still in Trash, restorable by re-import) and this is the one
        # place that decision is written down.
        if entry.is_symlink():
            raise OSError(f"{RECORD_NAME} would be written through a symlinked Trash entry")
        fd, tmp = tempfile.mkstemp(dir=entry, prefix=".musicdrop-trash-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                # ``fchmod`` on the open descriptor, never ``chmod`` on the path:
                # a path would be resolved a second time, which is one more
                # moment for something to be swapped underneath it. mkstemp
                # creates 0600; the record is not secret and sits among the
                # user's own music files, so match what the previous plain write
                # produced under a normal umask.
                os.fchmod(handle.fileno(), 0o644)
                handle.write(text)
            os.replace(tmp, entry / RECORD_NAME)
        except BaseException:
            # Includes KeyboardInterrupt on purpose: clean up the temp file, then
            # let it keep propagating (the ``except Exception`` below will not
            # swallow a BaseException, which is correct).
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception:
        # ``display_path``, not a hand-rolled escape. This handler is the last
        # thing standing between a failed record write and a delete that keeps
        # its library rows (both callers drop rows on the very next line), so
        # NOTHING in it may raise -- and the obvious spelling does:
        # ``.encode("utf-8", "backslashreplace")`` escapes nothing, because
        # ``backslashreplace`` on an ENCODE only escapes what the target codec
        # cannot encode and UTF-8 encodes everything, so the following
        # ``.decode("ascii")`` raised ``UnicodeDecodeError`` on every non-ASCII
        # path. ``display_path`` is the shared helper the rest of this package
        # already logs through, and it is documented and tested as never
        # raising. ``%r`` stays: a Trash folder's name comes from the album's
        # own tags, so the quoting is what stops a crafted name forging a log
        # line (see ``test_a_failed_return_to_trash_cannot_forge_a_log_line``).
        logger.warning(
            "could not record the Trash origin for %r; the folder is still in Trash but"
            " can only be restored by re-importing it",
            display_path(entry),
            exc_info=True,
        )


def read_trash_origin(entry: Path) -> TrashOrigin | None:
    """The origin record inside ``entry``, or ``None`` when there is none we trust.

    Every rejection collapses to ``None`` — a missing file, an unreadable one, a
    truncated write, a future schema, a hand-edited payload — because the caller's
    answer is the same in all of them: fall back to the import-restore that Trash
    rows have always had. A record is a claim about a path we are about to
    ``shutil.move`` a folder onto, so it is validated as untrusted input.

    **The RETURN collapses; the log does not.** An absent record is ordinary —
    every row trashed before this feature shipped has none — while a record that
    is present and unusable is a write that failed half-way, a disk going bad, or
    a planted payload. The Trash row cannot tell them apart (both get
    ``trash_manage._NO_RECORD_NOTE``), so a WARNING here is the only place the
    difference is visible, and without it a write failing on a full or read-only
    volume reads as "trashed before origins were recorded" — blaming a feature
    that shipped today and sending nobody to look at the disk while every later
    delete loses its origin the same way.

    **Do NOT turn an unusable record into a refusal.** The WARNING above makes
    that tempting — a planted or corrupt payload now announces itself, so
    refusing the row feels like the safe answer. It is the opposite. A record we
    cannot read must never leave a folder worse off than a record that was never
    written, and that is the floor this whole feature was built above (see the
    module docstring): refusing would strand the row entirely, and for an
    audio-free art/booklet husk — the case the record exists for — Trash would
    have no exit left at all. It buys nothing against an attacker either, since
    whoever can plant an unusable payload can plant a well-formed one just as
    easily. The refusal that matters is of the MOVE-BACK, not of the row, and it
    already happens: :func:`_parse` declines to build a record it cannot vouch
    for and :func:`move_back_target` declines to act outside the library, so the
    row falls through to the import-restore every Trash row has always had.
    """
    path = entry / RECORD_NAME
    try:
        if not path.is_file():
            if os.path.lexists(path):
                # Something is at the record's name and is not a record — a
                # directory (what a write leaves when one was already sitting
                # there), a socket, a dangling symlink. ``lexists`` rather than
                # ``exists`` so a broken link counts as present: it is a thing
                # someone put there, not an absence.
                _warn_unusable(path, "it is not a regular file")
            return None  # otherwise the ordinary case: no record was ever written
        if path.stat().st_size > _MAX_BYTES:
            _warn_unusable(path, f"it is larger than the {_MAX_BYTES}-byte cap")
            return None
        # ``UnicodeDecodeError`` and ``json.JSONDecodeError`` are both
        # ``ValueError`` subclasses, so the two arms below cover read, decode
        # and parse together.
        raw: object = json.loads(path.read_text(encoding="ascii"))
    except OSError:
        # NOT folded into the absent case above: ``Path.is_file()`` already
        # answered False for ENOENT, so reaching here means a real fault
        # (EACCES, EIO, a stale mount) or the file vanishing mid-read.
        _warn_unusable(path, "it could not be read", exc_info=True)
        return None
    except ValueError:
        _warn_unusable(path, "it is not the ASCII JSON this writes", exc_info=True)
        return None
    record = _parse(raw)
    if record is None:
        _warn_unusable(path, "its contents are not a record this version can trust")
    return record


def _warn_unusable(path: Path, why: str, *, exc_info: bool = False) -> None:
    """Log that a record file is present but cannot be used.

    ``%r`` rather than ``%s`` on the path, for the same reason
    :func:`write_trash_origin` uses it: a Trash folder's name comes from the
    album's own tags and carries whatever they held, so a raw ``%s`` lets a
    newline or an ANSI escape in an ``albumartist`` forge log lines. ``repr``
    escapes those and lone surrogates too, while leaving ordinary text readable.

    Once per listing per bad record, which is the right frequency: the row is
    showing a misleading note for as long as the file sits there.
    """
    logger.warning(
        "the Trash origin record at %r is present but unusable: %s. That folder falls back"
        " to a re-import restore, and its Trash row cannot say which of the two causes it"
        " hit — this line is the difference.",
        os.fsdecode(path),
        why,
        exc_info=exc_info,
    )


def _parse(raw: object) -> TrashOrigin | None:
    """Validate a decoded payload into a :class:`TrashOrigin`, or ``None``.

    ``origin`` is checked for being a PATH, not merely for being an absolute-
    looking string: bounded in length and free of the characters that either
    break a filesystem call or misrepresent the result — see
    :data:`_MAX_ORIGIN_CHARS` and :data:`_REJECTED_IN_ORIGIN` for what each one
    costs when it gets through. Both refusals happen here rather than at the
    sinks because there are three of them (the occupancy guard, the move, and
    the Trash row's own text) and only one place where the value is still known
    to be untrusted.
    """
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        return None
    origin = raw.get("origin")
    if not isinstance(origin, str) or not os.path.isabs(origin):
        return None
    if len(origin) > _MAX_ORIGIN_CHARS or _REJECTED_IN_ORIGIN.search(origin):
        return None
    # Matched against the literals one at a time rather than a membership test:
    # that is what narrows the value from the payload's ``Any`` to ``MovedShape``
    # without an annotation lying about it.
    moved_raw = raw.get("moved")
    moved: MovedShape
    if moved_raw == "folder":
        moved = "folder"
    elif moved_raw == "items":
        moved = "items"
    else:
        return None
    return TrashOrigin(origin=os.path.normpath(origin), moved=moved)


def delete_trash_origin(folder: Path) -> None:
    """Remove the record from a folder that has been put back. NEVER raises.

    The sidecar rides along inside the folder on the way out of Trash, so the
    restore owes the library this deletion. Failing it leaves a hidden file beets
    ignores — worth a log line, not worth failing a restore that has already
    landed.
    """
    try:
        (folder / RECORD_NAME).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove %s after a restore", RECORD_NAME, exc_info=True)


def move_back_target(record: TrashOrigin | None, *, music_dir: str) -> Path | None:
    """The folder a trashed entry can be moved back to, or ``None``.

    ``None`` means "no faithful move-back is on offer" and the caller falls back
    to import-restore. Three ways to get there, and the listing distinguishes
    them for the user (``trash_manage._restore_fields``):

    * no record we can use — a row trashed before origins were recorded, or one
      whose record could not be written or cannot be read back
      (:func:`read_trash_origin` logs which);
    * ``moved="items"`` — see :data:`MovedShape`;
    * an origin outside the CURRENT music library — the honest answer after the
      user re-points ``directory`` at another library, where the recorded folder
      is a real path that this library would never see.

    The containment test is LEXICAL (``normpath`` + ``commonpath``), and that is
    deliberate rather than an oversight. Measured: an origin under a symlink that
    points out of the library passes it, and the folder is moved to wherever the
    link really goes. Resolving both sides with ``realpath`` would close that and
    break a legitimate, common self-hosted layout in the same stroke — a library
    with a symlinked subtree (``music/artists -> /mnt/big/artists``) would have
    every one of its rows refused, because the user's own idea of "inside the
    library" is the lexical one. So this is NOT a hostile-sidecar guard, and must
    not be described as one: it answers "is this still my library", not "is this
    safe".

    **What makes that acceptable is :func:`write_trash_origin`'s atomic replace,
    not this function.** An earlier version of this note claimed a hostile
    sidecar "requires write access to the Trash directory, which is more access
    than this path grants". That was FALSE as written, and a security audit
    disproved it: the record write swallows its errors, so a plant plain
    ``chmod 444``'d in the MUSIC library survived the app's failed overwrite and
    its origin won — measured, with the app running as the file's own owner
    (0444 refuses write even to the owner on Linux). Since the write became
    ``mkstemp`` + ``os.replace``, ``rename`` needs write permission on the
    DIRECTORY rather than the file, so the app's record now always wins and the
    precondition really is Trash-directory write. Do not weaken that write
    without re-reading this paragraph — the lexical check leans on it.
    """
    if record is None or record.moved != "folder":
        return None
    origin = os.path.normpath(record.origin)
    root = os.path.normpath(music_dir)
    try:
        if os.path.commonpath([origin, root]) != root or origin == root:
            return None
    except ValueError:
        return None  # different drives, or one side is relative
    return Path(origin)
