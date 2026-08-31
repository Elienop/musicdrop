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
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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
    always one of the two shapes. ``trashed_at`` is informational only.
    """

    origin: str
    moved: MovedShape
    trashed_at: str | None


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
        logger.warning(
            "could not record the Trash origin for %r; the folder is still in Trash but"
            " can only be restored by re-importing it",
            os.fsdecode(entry).encode("utf-8", "backslashreplace").decode("ascii"),
            exc_info=True,
        )


def read_trash_origin(entry: Path) -> TrashOrigin | None:
    """The origin record inside ``entry``, or ``None`` when there is none we trust.

    Every rejection collapses to ``None`` — a missing file, an unreadable one, a
    truncated write, a future schema, a hand-edited payload — because the caller's
    answer is the same in all of them: fall back to the import-restore that Trash
    rows have always had. A record is a claim about a path we are about to
    ``shutil.move`` a folder onto, so it is validated as untrusted input.
    """
    path = entry / RECORD_NAME
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        # ``UnicodeDecodeError`` and ``json.JSONDecodeError`` are both
        # ``ValueError`` subclasses, so the two arms below cover read, decode
        # and parse together.
        raw: object = json.loads(path.read_text(encoding="ascii"))
    except OSError:
        return None
    except ValueError:
        return None
    return _parse(raw)


def _parse(raw: object) -> TrashOrigin | None:
    """Validate a decoded payload into a :class:`TrashOrigin`, or ``None``."""
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        return None
    origin = raw.get("origin")
    if not isinstance(origin, str) or not os.path.isabs(origin):
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
    trashed_at = raw.get("trashed_at")
    return TrashOrigin(
        origin=os.path.normpath(origin),
        moved=moved,
        trashed_at=trashed_at if isinstance(trashed_at, str) else None,
    )


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

    * no record at all — a row trashed before origins were recorded;
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
