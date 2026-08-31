"""Where each trashed folder came from — one JSON file per Trash entry.

A folder MusicDrop moves to Trash is a bare ``shutil.move`` away from being
unrecoverable: the destination name says what the album was called, never where
it lived. This module is the record that closes that, and it is a SIBLING store,
never a sidecar: the origin of ``<trash_dir>/<entry>`` lives in
``<origins_dir>/<entry>.json``, and nothing is written inside the trashed folder
at all.

**That separation is the whole design, and it is freedesktop.org's** (the Trash
spec has ``info/<name>.trashinfo`` beside ``files/<name>``, never inside it).
Two properties fall out of it, both of which the earlier in-folder sidecar had to
buy back with guards:

* **The record is not reachable from the music library.** A trashed folder
  arrives from ``/music``, which this deployment's threat model treats as
  attacker-writable, and ``shutil.move`` carries whatever it holds into Trash —
  so the sidecar's own directory was hostile, and a pre-planted symlink at its
  filename turned an ordinary album delete into a write primitive against
  ``/data`` (measured: beets' ``config.yaml`` overwritten, ``library.db``
  truncated). ``<beets_dir>/trash-origins/`` is a directory this app creates and
  owns; nothing in ``/music`` can plant anything in it, so the symlink refusal,
  the ``mkstemp`` choreography, the size cap, the origin-length cap and the
  hostile-character filter are all gone rather than merely relaxed.
* **beets' source pruning is not blocked.** A file inside the trashed folder
  survives the re-import a failed restore asks for, and beets then refuses to
  prune the directory that still contains it — leaving a husk behind. There is
  no sidecar here, not even a write-only one.

**Writing a record must never make a delete fail.** :func:`write_trash_origin`
swallows everything and logs; a missing record degrades a Trash row to exactly
the pre-feature behaviour (import-restore, or nothing for an audio-free husk),
which is the floor this whole feature has to stay above.

The key is the Trash entry's NAME. Inode keys were rejected: inode numbers are
REUSED, so a stale origin bound to a recycled inode would hand folder A's origin
to folder B, and that origin steers a ``rename()``. The name key has the same
shape of hazard (an entry deleted OUTSIDE the app leaves an origin whose name a
later entry could take), and it is closed at the allocator instead:
``trash._unique_trash_dest`` treats a recorded name as occupied, so a name is
never reused while its origin file is still there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.playlists.atomic import write_atomic_text
from app.wire import display_path

logger = logging.getLogger(__name__)

#: Bumped only for a change a previous reader would misread. An unknown schema
#: reads as "no record", which degrades to import-restore rather than acting on
#: a payload whose meaning has moved. Kept although ``app/bank/store.py`` — the
#: store this one is modelled on — carries no such field, because this is a
#: Docker app shipped on release tags: pinning an older image after an upgrade
#: is a real downgrade path, and the field a stale reader would misread steers a
#: ``rename()`` rather than filling in a listing.
_SCHEMA = 1

#: The longest filename to attempt, in BYTES. ``NAME_MAX`` is 255 on ext4/xfs/
#: btrfs and on every filesystem this app is deployed on; it is not queried per
#: volume (``os.pathconf`` needs the directory to exist and raises on the ones
#: that do not answer) because the fallback for getting it wrong is the same
#: swallow-and-degrade every other write failure takes.
_NAME_MAX = 255

#: Bytes the shared atomic writer adds to the target name WHILE writing, which
#: is the real limit a record filename has to fit under. ``write_atomic_bytes``
#: creates ``.<name>.<pid>.<16 hex>.tmp`` beside the target, so the decoration is
#: ``1 + 1 + len(str(pid)) + 1 + 16 + 4`` = 23 + the pid's digits; Linux caps
#: ``pid_max`` at 4194304 (7 digits), giving 30. Rounded to 32 for slack.
#: Measured the hard way: a 251-byte entry name produced a 256-byte ``.json``
#: (caught) and, once truncated to exactly 255, a 285-byte TEMP name that still
#: raised ENAMETOOLONG — the record was silently lost with the target name
#: legal. Get this wrong and the failure is the documented degradation
#: (swallow, log, import-restore), not a fault.
_MAX_KEY_BYTES = _NAME_MAX - 32


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

    Only ever constructed by :func:`read_trash_origin`, which types every field
    it keeps — so ``origin`` is always an absolute path string and ``moved`` is
    always one of the two shapes. That is TYPING, not a hostility check: the file
    is one this app wrote on the ``/data`` side, and what the parse defends
    against is a truncated write, a hand edit, or a payload from another version.

    The payload's ``trashed_at`` is deliberately NOT a field here. It was: read
    back, type-checked, carried through every caller, and never consulted by
    anything. Surfacing it ("trashed 3 days ago" beside the origin) would be a
    contract change and a UI change; validating a value nobody reads is code that
    only looks like it does something. It is still WRITTEN, because the record is
    a file a human opens and because the file's own mtime is not a dependable
    substitute — a backup restore, a ``cp`` without ``-p`` or an rsync rewrites
    it — so keeping the time in the payload means a later decision to show it has
    no data gap to apologise for.
    """

    origin: str
    moved: MovedShape


def origin_file(origins_dir: Path, entry_name: str) -> Path:
    """The record file for the Trash entry called ``entry_name``.

    Raises ``ValueError`` unless ``entry_name`` is a single path component. Every
    caller in this app passes one by construction (``_trash_container_name``
    replaces both separators, ``_unique_trash_dest``'s collision loop can never
    settle on ``.`` or ``..`` because both always exist), but that guarantee is
    incidental and spread across two functions, and this key names a file on the
    ``/data`` side — the same reason ``app/bank/store.py`` keeps ``_VALID_ID``.

    ``<name>.json`` would be unwritable for an entry whose own name is anywhere
    near ``NAME_MAX``: the Trash entry creates fine and its record cannot, which
    is a silent, permanent loss of the exact restore for exactly the longest
    album folders. Such a name gets a truncated head plus a digest of the WHOLE
    name instead — deterministic, so the reader recomputes the same key. The
    budget is :data:`_MAX_KEY_BYTES`, not ``NAME_MAX``, because the atomic write
    needs room for its own temp name.
    """
    if not entry_name or entry_name in {".", ".."} or os.sep in entry_name or "/" in entry_name:
        raise ValueError(f"{entry_name!r} is not a single path component")
    name = f"{entry_name}.json"
    encoded = os.fsencode(name)
    if len(encoded) > _MAX_KEY_BYTES:
        # ``os.fsencode`` first so the truncation counts BYTES (what the kernel
        # limits) and ``os.fsdecode`` back so a multi-byte character split by the
        # cut survives as surrogates rather than raising — the same round trip
        # every undecodable path in this app takes.
        suffix = f".{hashlib.sha256(os.fsencode(entry_name)).hexdigest()[:16]}.json"
        head = os.fsencode(entry_name)[: _MAX_KEY_BYTES - len(suffix)]
        name = os.fsdecode(head) + suffix
    return origins_dir / name


def origin_recorded(origins_dir: Path, entry_name: str) -> bool:
    """Whether a record already exists for ``entry_name``. Never raises.

    The Trash name allocator asks this so a name whose record is still on disk is
    never handed to a DIFFERENT folder — see the module docstring. False for a
    key this store could not build a path for, which is the same answer the
    allocator would get from a name nothing recorded.
    """
    try:
        return origin_file(origins_dir, entry_name).exists()
    except (OSError, ValueError):
        return False


def write_trash_origin(
    origins_dir: Path, entry_name: str, *, origin: str, moved: MovedShape
) -> None:
    """Record that the Trash entry ``entry_name`` came from ``origin``. NEVER raises.

    Called immediately AFTER the relocation, never before: a record written ahead
    of a move that then fails would describe an entry that never existed, and the
    name key would hand it to whatever takes that name next. Writing it after
    leaves the opposite window — the folder sitting in Trash for a moment with no
    record — which is precisely the pre-feature behaviour.

    Swallows every exception by design: this is a new write on the delete path,
    and a delete that fails because its bookkeeping failed would be a worse
    outcome than the unrecoverable-but-completed delete it replaces. Both callers
    drop library rows on the very next line.
    """
    # ``trashed_at`` has no reader by design — see :class:`TrashOrigin`. Do not
    # "clean it up" as unused; do not add a reader without deciding what shows it.
    payload = {
        "schema": _SCHEMA,
        "trashed_at": datetime.now(UTC).isoformat(),
        "origin": origin,
        "moved": moved,
    }
    try:
        # ``ensure_ascii=True`` is REQUIRED, not a default worth leaving implicit:
        # ``origin`` comes from ``os.fsdecode`` of a real POSIX path, so a
        # non-UTF-8 folder name reaches here as LONE SURROGATES, and
        # ``write_atomic_text`` encodes strict UTF-8 — which raises on those.
        # Escaped to ``\udcXX`` the text is pure ASCII, hence valid UTF-8, and
        # ``json.loads`` restores the identical str that ``os.fsencode`` needs.
        # Exactly the recipe ``app/bank/store.py`` documents at length.
        text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
        # The shared atomic sink, not a hand-rolled temp dance: it creates the
        # parent, writes a uniquely-named ``O_EXCL`` temp, fsyncs it, replaces,
        # and fsyncs the PARENT DIRECTORY — so a reader never sees a half-written
        # record and a power cut cannot lose the rename.
        write_atomic_text(origin_file(origins_dir, entry_name), text)
    except Exception:
        # ``display_path``, not a hand-rolled escape. This handler is the last
        # thing standing between a failed record write and a delete that keeps
        # its library rows, so NOTHING in it may raise -- and the obvious
        # spelling does: ``.encode("utf-8", "backslashreplace")`` escapes
        # nothing, because ``backslashreplace`` on an ENCODE only escapes what
        # the target codec cannot encode and UTF-8 encodes everything, so the
        # following ``.decode("ascii")`` raised ``UnicodeDecodeError`` on every
        # non-ASCII path. ``display_path`` is the shared helper the rest of this
        # package already logs through, and it is documented and tested as never
        # raising. ``%r`` stays: a Trash entry's name comes from the album's own
        # tags, so the quoting is what stops a crafted name forging a log line.
        logger.warning(
            "could not record the Trash origin for %r; the folder is still in Trash but"
            " can only be restored by re-importing it",
            display_path(entry_name),
            exc_info=True,
        )


def read_trash_origin(origins_dir: Path, entry_name: str) -> TrashOrigin | None:
    """The origin recorded for ``entry_name``, or ``None`` when there is none we trust.

    Every rejection collapses to ``None`` — a missing file, an unreadable one, a
    truncated write, a future schema, a hand-edited payload — because the
    caller's answer is the same in all of them: fall back to the import-restore
    that Trash rows have always had. Never a refusal: a record we cannot read
    must never leave a folder worse off than a record that was never written, and
    for an audio-free art/booklet husk — the case the record exists for — Trash
    would then have no exit left at all.

    **The RETURN collapses; the log does not.** An absent record is ordinary —
    every row trashed before this feature shipped has none — while a record that
    is present and unusable means the ``/data`` volume is full, read-only,
    permission-broken or failing, or that a write was cut short. The Trash row
    cannot tell them apart (both get ``trash_manage._NO_RECORD_NOTE``), so the
    WARNING is the only place the difference is visible, and it is MORE
    diagnostic here than it was for a sidecar: on the trusted side, unusable
    means hardware or capacity, not a plant.
    """
    try:
        path = origin_file(origins_dir, entry_name)
    except ValueError:
        return None  # not a key this store can hold; nothing was ever written
    try:
        # ``UnicodeDecodeError`` and ``json.JSONDecodeError`` are both
        # ``ValueError`` subclasses, so the two arms below cover read, decode
        # and parse together. No ``is_file()`` preamble and no size cap: nothing
        # but this module writes into this directory, so there is no planted
        # FIFO to block on and no oversized file to refuse. A directory at the
        # name raises ``IsADirectoryError``, which is an ``OSError``, so the
        # unusable arm below still names it.
        #
        # ``encoding="ascii"`` here is a TRIPWIRE, and NO TEST CAN KILL IT
        # (measured: switching it to "utf-8" leaves the whole suite green).
        # Everything this module writes is pure ASCII by construction, so utf-8
        # would decode it identically; what ascii adds is that a file carrying a
        # non-ASCII byte -- which is therefore not one of ours, or is corrupt --
        # fails loudly into the unusable arm instead of being half-believed. The
        # WRITE side of the same property IS pinned, by
        # ``test_a_non_utf8_folder_name_round_trips_through_the_ascii_record``.
        raw: object = json.loads(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        return None  # the ordinary case: no record was ever written
    except OSError:
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

    ``%r`` rather than ``%s`` on the path, and it is MORE load-bearing here than
    it was for a sidecar: the key is the Trash entry's name, which comes from the
    album's own tags, so a newline or an ANSI escape in an ``albumartist`` now
    reaches this log line through the record's own FILENAME. ``repr`` escapes
    those and lone surrogates too, while leaving ordinary text readable.

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

    Mostly TYPING: ``json.loads`` returns ``object``, and the literal-by-literal
    match below is what narrows ``moved`` to :data:`MovedShape` without an
    annotation that lies about it. Two value checks ride along, both cheap and
    both about a CORRUPT record rather than a hostile one:

    * ``isabs`` — strictly redundant with :func:`move_back_target`, whose
      ``commonpath`` raises ``ValueError`` on mixing absolute and relative paths
      and is already caught there. Kept because it is what makes
      ``TrashOrigin.origin``'s "always absolute" docstring true at the type's own
      boundary rather than two modules away.
    * **NUL** — the one member of the old hostile-character filter with a
      consequence that is not cosmetic, kept for that reason alone. A POSIX path
      cannot contain a NUL (it is the terminator), so no value this app writes
      can carry one and only corruption can produce it — but ``Path.exists()``
      swallows the ``ValueError`` an embedded NUL raises and answers ``False``,
      so ``trash_manage``'s occupancy pre-filter PASSES and the failure surfaces
      at the ``mkdir``/``rename`` as a ``ValueError`` that no ``except OSError``
      catches: a blanket 500 on a row the UI had just labelled "Exact restore".
      One comparison turns that into the documented import-restore.
    """
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        return None
    origin = raw.get("origin")
    if not isinstance(origin, str) or not os.path.isabs(origin) or "\x00" in origin:
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


def delete_trash_origin(origins_dir: Path, entry_name: str) -> None:
    """Drop the record for a Trash entry that is no longer there. NEVER raises.

    Owed by every path that takes an entry OUT of Trash — a landed restore, a
    move-back, a failed undo that stranded the folder in the library, and both
    Empty routes. Not tidiness: the name key is free again the moment the entry
    goes, and a record left behind would be inherited by whatever takes that name
    next, steering a ``rename()`` for the wrong folder. (The allocator refuses to
    reuse a recorded name, so the failure mode of NOT deleting is a permanently
    burnt name rather than a wrong restore — but that is a second line of
    defence, not a reason to skip this one.)

    Failing it is worth a log line, never worth failing a restore that has
    already landed.
    """
    try:
        origin_file(origins_dir, entry_name).unlink(missing_ok=True)
    except (OSError, ValueError):
        logger.warning(
            "could not remove the Trash origin record for %r",
            display_path(entry_name),
            exc_info=True,
        )


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
    library" is the lexical one. So this is NOT a hostile-record guard, and must
    not be described as one: it answers "is this still my library", not "is this
    safe".

    **It survives the move to a trusted store on CONTRACT grounds, not
    threat-model ones.** ``trash_manage._restore_fields`` turns this exact answer
    into ``_OUTSIDE_LIBRARY_NOTE``, so deleting the containment test would start
    offering a ``move_back`` on rows that today say "the folder this came from is
    not inside the current music library" — a change to ``restore_mode`` and
    ``restore_note`` on the wire. The ``origin == root`` refusal is load-bearing
    for a different reason: a corrupt or empty origin resolving to the music root
    would hand ``_move_no_merge`` the library itself.
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
