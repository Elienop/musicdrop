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
shape of hazard — an entry that leaves Trash without this app noticing (a file
manager, an SMB client, ``docker volume rm``) leaves its record behind for
whatever takes that name next — and two mechanisms narrow it:

* **The allocator**, for the names MusicDrop hands out.
  ``trash._unique_trash_dest`` treats a recorded name as occupied, so this app
  does not give a second folder a name whose record is still on disk. A store it
  cannot reach used to read as empty, and the name was handed on anyway; a
  delete is now REFUSED instead — every mover asks
  :func:`require_usable_store` before it allocates or moves, and
  :func:`origin_recorded` refuses the same fault class met in the window after
  that check. That is the whole of what it covers. An entry that reaches
  ``trash_dir`` by ANOTHER route — a hand copy, a restored backup, a sync
  client writing into the volume — asks the allocator nothing, so it can land
  on a name whose record outlived its entry and inherit it: the listing offers
  "Exact restore" to a stranger's origin and Restore renames the folder there.
  Nothing here detects that today; it is a stated residual, not a closed hazard.
* **The record names its own entry.** ``name`` is in the payload and
  :func:`read_trash_origin` refuses a record whose ``name`` is not the entry it
  was asked about. That does nothing for the adoption case above (an adopted
  folder carries the very same name), and everything for the TRUNCATED key,
  where two DIFFERENT entry names share one record file — see
  :func:`origin_file`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import tempfile
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
#:
#: Adding ``name`` did NOT earn a bump, and the test is the definition above: an
#: older reader handed a record WITH a name reads ``origin``/``moved`` exactly as
#: it always did — it simply keeps the collision this version refuses, which is
#: its own pre-existing behaviour rather than a misreading of new data. The other
#: direction needs no version at all: a record with no ``name`` is not the record
#: for any entry, so it already reads as "no record" and degrades to
#: import-restore. Only this branch's own earlier commits ever wrote one, and the
#: branch is unmerged, so no such file exists outside a developer's ``/data``.
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

    **The truncated key is not injective, and not by 2^64 either.** The recipe is
    in BYTES, exactly as the cut above is: for any long name ``N2`` the SHORT
    name ``N1 = fsdecode(fsencode(N2)[:201]) + "." + sha256(fsencode(N2))[:16]``
    is 218 bytes, so it is NOT truncated, and ``N1 + ".json"`` is
    character-for-character the key ``N2`` is given — one construction, no
    search. Spelled as a CHARACTER slice the recipe only works while ``N2`` is
    ASCII: for ``N2 = "é" * 120`` it yields 257 bytes, which is not even a legal
    filename and collides with nothing, while the byte slice is 218 and does
    (measured). Both are legal Trash entry names (they fit ``NAME_MAX``), and the
    loser's record would be read
    back for the winner: a listing row offering "Exact restore" to a stranger's
    folder. Closed at the READ, which compares the ``name`` stored in the payload
    (see :func:`read_trash_origin`); the allocator cannot close it, because it
    only sees the names it is asked to hand out.
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


class TrashOriginsStoreUnusableError(Exception):
    """The origin-records store cannot be used AS A STORE, so a delete is refused.

    Raised by :func:`require_usable_store` before a mover touches anything, and
    by :func:`origin_recorded` for the same fault class met a moment later (the
    store can drop between the two — a ``/data`` mount going, a chmod landing).
    Both refuse rather than degrade, which is the opposite posture from the rest
    of this module: every OTHER failure here swallows and logs, because it runs
    AFTER irreversible work and a record we cannot write must never make a delete
    fail. This one runs BEFORE any of it.

    The message is USER-facing — the delete routes put it straight into a 503's
    flat ``detail`` — so it names the store the way the README names it and NOT
    by absolute path. The absolute path is logged at WARNING beside every raise,
    which is where an operator reading ``docker logs`` needs it.
    """


#: How the store is named to the USER. Deliberately not its absolute path: the
#: presence refusal this sits beside leaks none either
#: (``test_refusal_message_leaks_no_path``), and the sentence reaches a browser.
_STORE_WHERE = "The Trash origin-records folder (trash-origins under the beets data folder)"

#: What to do about it. "Nothing has been deleted" is a promise the CALLERS keep
#: by asking before they move or drop anything — see :func:`require_usable_store`
#: for where each mover asks.
_STORE_FIX = "Nothing has been deleted. Fix its permissions or its mount, then retry."

#: A name no record can collide with: :func:`origin_file` always appends
#: ``.json`` (the truncating branch too), and neither this key nor the
#: ``mkstemp`` name built from it ends in ``.json``. Used to ask the store the
#: SEARCH question without depending on any record being there.
_PROBE_KEY = ".musicdrop-store-check"

#: Errnos that mean the STORE is the problem rather than the key, met at the one
#: lookup :func:`origin_recorded` makes. Measured on 3.12.13: ``Path.exists()``
#: absorbs ENOENT/ENOTDIR/EBADF/ELOOP itself, so a FILE at the store path and a
#: symlink loop at the key never reach that arm at all — ENOTDIR here would be a
#: line with no reader, and the FILE shape is caught by
#: :func:`require_usable_store` instead. What is left, and what a dropped mount
#: or a bad ``PUID``/``PGID`` really produces, is EACCES/EPERM. Everything else
#: (ENAMETOOLONG from a path over ``PATH_MAX``, a filesystem whose own
#: ``NAME_MAX`` is smaller) is a KEY-level oddity: it says nothing about the
#: store, so it keeps the old answer of "free" and its warning.
_STORE_CLASS_ERRNOS = frozenset({errno.EACCES, errno.EPERM})


def require_usable_store(origins_dir: Path) -> None:
    """Refuse the caller unless a delete could both LOOK UP and WRITE a record here.

    A delete that cannot use this store is a delete that hands out a Trash name
    whose record is still on disk and then loses the new record — measured at
    tip: with the store at mode 0000 the folder lands under the ALREADY-RECORDED
    name, the write fails and is swallowed, and once the mode is repaired the
    stranger's row offers an exact restore to the first folder's origin. So the
    movers ask this first and refuse, rather than completing a delete whose only
    trace of the fault is a WARNING.

    The three probes are the three things a delete does to this directory, in
    the order it does them, and each is a real syscall rather than an
    ``os.access`` guess — ``os.access`` answers for the REAL uid and cannot see
    a read-only mount the way the operation can:

    * ``mkdir(parents=True, exist_ok=True)`` — an ABSENT store is healthy and
      creating it is what the first delete on a fresh install already does (via
      the atomic writer). This is also the probe that catches the shapes that
      are not a directory at all: measured, a regular FILE at the store path
      raises EEXIST here, a file at a PARENT component raises ENOTDIR, and an
      absent store under an unwritable parent raises EACCES. The FILE shape is
      invisible everywhere else — ``Path.exists()`` absorbs the ENOTDIR its
      children raise, so the allocator reads "no record" in silence.
    * ``scandir`` + a ``stat`` of :data:`_PROBE_KEY` — the store must be
      readable AND searchable, and those are two different bits. Measured as a
      non-root user: at mode 0600 the ``scandir`` SUCCEEDS and the ``stat``
      raises EACCES, which is exactly the fault ``origin_recorded`` meets, so
      the ``stat`` is the probe that matters and the ``scandir`` is the one the
      Empty-all sweep needs.
    * ``mkstemp`` — the store must be writable. A read-only store (mode 0500)
      reads perfectly and loses every origin silently, which is the same class
      of setup fault one permission bit away from the unreadable one. ``mkstemp``
      rather than a fixed probe name so two movers running at once cannot refuse
      each other with EEXIST, and it reports the real errno (EACCES here, EROFS
      on a read-only mount, ENOSPC on a full one) instead of a guess.

    Every probe leaves the store as it found it: the ``stat`` target is never
    created and the ``mkstemp`` file is unlinked on the way out.
    """
    try:
        origins_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _store_unusable(origins_dir, "is not a usable folder", exc) from exc
    try:
        with os.scandir(origins_dir) as entries:
            next(entries, None)
        # The lookup ``origin_recorded`` makes, asked of a key that is never
        # there: ENOENT is the healthy answer and everything else is the fault.
        with contextlib.suppress(FileNotFoundError):
            os.stat(origins_dir / _PROBE_KEY)
    except OSError as exc:
        raise _store_unusable(origins_dir, "cannot be read", exc) from exc
    try:
        handle, probe = tempfile.mkstemp(dir=origins_dir, prefix=f"{_PROBE_KEY}.")
    except OSError as exc:
        raise _store_unusable(origins_dir, "cannot be written", exc) from exc
    os.close(handle)
    # Best effort: a probe file we could not remove is litter in a directory
    # this app owns, not a reason to refuse a delete that can otherwise proceed.
    with contextlib.suppress(OSError):
        os.unlink(probe)


def _store_unusable(origins_dir: Path, what: str, exc: OSError) -> TrashOriginsStoreUnusableError:
    """The refusal, with the absolute path in the LOG and out of the message.

    ``%r`` on the path for the reason ``_undo_failure`` gives: a store path can
    be configured to anything, and a raw ``%s`` of one carrying a newline or an
    ANSI escape forges log lines.
    """
    logger.warning(
        "%s %s, so a delete was refused; the store is at %r and the error below says why.",
        _STORE_WHERE,
        what,
        display_path(str(origins_dir)),
        exc_info=True,
    )
    return TrashOriginsStoreUnusableError(
        f"{_STORE_WHERE} {what}: {exc.strerror or exc}. {_STORE_FIX}"
    )


def origin_recorded(origins_dir: Path, entry_name: str) -> bool:
    """Whether a record already exists for ``entry_name``.

    Raises :class:`TrashOriginsStoreUnusableError` when the STORE is what
    answered — see the paragraph below — and nothing else.

    The Trash name allocator asks this so a name whose record is still on disk is
    never handed to a DIFFERENT folder — see the module docstring. False for a
    key this store could not build a path for, which is the same answer the
    allocator would get from a name nothing recorded.

    EXISTENCE of the key file, deliberately, and not "would
    :func:`read_trash_origin` answer for this name". The two answers differ
    whenever the key file is REACHABLE and refused, which is more than the
    colliding pair in :func:`origin_file` (whose record belongs to the other
    name): it is also every UNUSABLE one — a pre-feature payload carrying no
    ``name``, corrupt JSON, a future schema, a truncated write, a non-ASCII
    byte. Measured at this tip: read ``None`` and recorded ``True`` for all
    five. Occupied is the safe side in each of them — the allocator moves on to
    ``<name> (1)``, so AS LONG AS THIS ANSWERS the pair does not come to share a
    file and such a record is not handed to a second folder — and the cost is
    the same burnt name a manual deletion already costs. That qualification is
    not decoration: the paragraph below is the case where it does not answer,
    where this returns ``False`` for a record that is really there and the
    allocator hands the name on.

    **When the STORE itself is what cannot be reached, this REFUSES.** An origins
    directory that is present but unsearchable — measured at mode 0600 as a
    non-root user, the shape a bad ``PUID``/``PGID`` or a restored backup
    produces — makes ``exists()`` raise ``EACCES``, and answering either way is
    wrong. ``False`` hands out a name whose record is still on disk, so once the
    permissions are repaired that second folder's row reads the FIRST folder's
    origin and offers to move it there (measured through
    ``trash._unique_trash_dest``: ``Dummy (1)`` with the store readable,
    ``Dummy`` under the fault, and the payload's ``name`` guard cannot catch it
    because the two folders share a name). ``True`` leaves the allocator with no
    exit at all, since every candidate then reads occupied — measured, 111,939
    candidates in one second and still climbing. So it is neither: the delete is
    refused, which is the owner's ruling (``decisions.md`` 28) and what
    :func:`require_usable_store` already did a few statements earlier for every
    mover. This arm is what closes the window BETWEEN the two, where a ``/data``
    mount drops or a chmod lands after the check has passed.

    A key-level oddity is not that, and keeps the old answer. ENAMETOOLONG — a
    store path over ``PATH_MAX``, or a filesystem whose own ``NAME_MAX`` is
    below this module's guess — says nothing about whether the store works, so
    it still reads as free and still logs the warning below, which is the only
    trace that name got handed out with a record possibly sitting on it.
    """
    try:
        return origin_file(origins_dir, entry_name).exists()
    except OSError as exc:
        if exc.errno in _STORE_CLASS_ERRNOS:
            raise _store_unusable(origins_dir, "cannot be read", exc) from exc
        logger.warning(
            # What went wrong is left to ``exc_info`` rather than named in the
            # sentence. This used to end "Check the permissions on the Trash
            # origins directory", which is one cause of several: the suite
            # already drives ENAMETOOLONG through here (a store path over
            # PATH_MAX, and a filesystem whose own NAME_MAX is below this
            # module's guess), where a permissions hunt finds nothing wrong.
            # The errno's own message is in the traceback and says which.
            #
            # STALE CLAUSE, left for a code change rather than a docs pass: the
            # sentence's "an origins directory the app cannot search" can no
            # longer reach this line. That shape is EACCES, and EACCES is in
            # ``_STORE_CLASS_ERRNOS``, so it raises above. ENAMETOOLONG is the
            # only cause left here today.
            "could not tell whether a Trash origin record exists for %r, so the name is"
            " being treated as free: if a record IS there, a second folder can take that"
            " name and inherit it. The error below says why the check could not be made;"
            " an origins directory the app cannot search and a path the filesystem"
            " refuses are both measured causes.",
            display_path(entry_name),
            exc_info=True,
        )
        return False
    except ValueError:
        return False  # not a key this store can hold, so nothing was ever recorded


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
    #
    # ``name`` is the opposite: it is written to be READ BACK and compared. The
    # key is a filename and :func:`origin_file` truncates a long one, so two
    # different entry names can be handed the same file; carrying the name INSIDE
    # the payload is what lets the reader tell whose record it is holding.
    payload = {
        "schema": _SCHEMA,
        "trashed_at": datetime.now(UTC).isoformat(),
        "name": entry_name,
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
    truncated write, a future schema, a hand-edited payload, one nested past the
    JSON parser's own limit — because the
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

    **A record is only ever read back for the entry it was written for.** The
    payload names its own entry, and a record naming a different one — or naming
    none, which is every record written before this field existed — reads as no
    record. Two entry names can share a file (:func:`origin_file` truncates a
    long key, and landing on the same one takes a construction rather than a
    birthday search), and the wrong answer there is not a missing origin but a
    WRONG one: a ``rename()`` of the loser's folder into the winner's origin. The
    refusal is at the READ rather than the write because the write cannot see the
    clash — it would have to read the file it is about to replace, and lose the
    race anyway.
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
    except RecursionError:
        # Its own arm because it is neither of the two above: ``RecursionError``
        # is a ``RuntimeError``, so ``except ValueError`` walks straight past it
        # and a 60k-deep ``[[[...]]]`` at the key 500s the whole Trash listing
        # (measured at the previous tip). The file is legal ASCII and legal JSON;
        # what it is not is a record, so it earns a sentence of its own rather
        # than borrowing the parse arm's.
        _warn_unusable(path, "it nests deeper than the JSON parser will go", exc_info=True)
        return None
    if not _names_entry(raw, entry_name):
        _warn_unusable(path, "it is the record for a different Trash entry")
        return None
    record = _parse(raw)
    if record is None:
        _warn_unusable(path, "its contents are not a record this version can trust")
    return record


def _names_entry(raw: object, entry_name: str) -> bool:
    """Whether the decoded payload says it was written for ``entry_name``.

    Kept out of :func:`_parse` so the two refusals keep their own log sentences:
    "corrupt" and "this is somebody else's record" send an operator to different
    places, and the second is the only signal a truncated key has collided. A
    payload that is not an object at all falls through to :func:`_parse`, which
    owns that sentence — pinned by the ``not-an-object`` arm of
    ``test_every_unusable_record_names_its_own_cause``. Without it the routing
    disjunct is free: ``not isinstance(raw, dict) or`` -> ``isinstance(raw, dict)
    and`` logs a corrupt file as a different entry's record and nothing goes red.
    """
    return not isinstance(raw, dict) or raw.get("name") == entry_name


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
    """Drop the record for a Trash entry that is no longer there. Swallows and logs.

    **What "swallows" covers, since it is a list and not a promise.** Every
    caller runs this AFTER irreversible work — the folder has been moved back
    into the library, emptied, or stranded by a failed undo — so an exception
    escaping here turns an operation that fully SUCCEEDED into a bare 500.
    Caught: ``OSError`` and ``ValueError`` from the key and the unlink, in the
    handler below; and ``OSError``, ``ValueError`` and ``RecursionError`` from
    the payload read, inside :func:`_names_a_different_entry`. That last one is
    in the list because it was NOT: at the previous tip
    a 60k-deep ``[[[...]]]`` at the key escaped ``empty_one`` with the folder
    already rmtree'd, and aborted ``empty_all`` part-way (measured: 1 of 2
    entries destroyed, the sweep abandoned past its own ``except OSError``).
    The file shapes this has been measured against are listed in
    ``tests/test_trash_origins_store.py``; a shape nobody has tried is a shape
    nobody has measured.

    Owed by every path that takes an entry OUT of Trash — a landed restore, a
    move-back, a failed undo that stranded the folder in the library, and both
    Empty routes. Not tidiness: the name key is free again the moment the entry
    goes, and a record left behind would be inherited by whatever takes that name
    next, steering a ``rename()`` for the wrong folder. (The allocator usually
    refuses to reuse a recorded name, so the failure mode of NOT deleting is
    normally a burnt name rather than a wrong restore — but it is a second line
    of defence that has holes of its own, described at
    :func:`origin_recorded`, and not a reason to skip this one.)

    **The one file this must not take is another entry's**, which the truncated
    key makes possible: two names can share a record file (:func:`origin_file`),
    and the loser's ``read_trash_origin`` already answers ``None``, so BOTH of
    the callers that run this after a read collapsed to ``None`` — the import
    arm of ``trash_manage.restore_album`` and ``trash_manage.empty_one`` — would
    otherwise unlink the WINNER's record while its folder is still sitting in
    Trash, downgrading a row that exists to an import-restore for good.
    Measured at this tip before the guard below: ``delete_trash_origin(short)``
    left ``read_trash_origin(long)`` answering ``None``. So the payload's own
    ``name`` is read first, and a record that names a different entry is kept.

    Everything else still goes: absent, unreadable, unparseable, not an object,
    or carrying no ``name`` at all. That is deliberate and is what keeps the
    import arm's "an unreadable record must not be left to be adopted" true —
    an unreadable record steers nothing but burns its name forever.

    Failing it is worth a log line, never worth failing a restore that has
    already landed.
    """
    try:
        path = origin_file(origins_dir, entry_name)
        if _names_a_different_entry(path, entry_name):
            logger.warning(
                # What this line knows is the PAYLOAD, and nothing else. It used
                # to say the other entry "is still in Trash and still needs it";
                # this function is not given ``trash_dir`` and asks nothing about
                # it, so that was a guess about a directory it cannot see — and
                # the wrong guess (the other entry gone too) is exactly the
                # leftover-record case ``trash_manage.empty_all`` now sweeps.
                "kept the Trash origin record at %r instead of dropping it with %r: the"
                " record names a different Trash entry, so it is not this entry's to"
                " remove. Whether that entry is still in Trash is not checked here — this"
                " function is not told the Trash directory. The two names share one record"
                " file — see origin_file.",
                os.fsdecode(path),
                display_path(entry_name),
            )
            return
        path.unlink(missing_ok=True)
    except (OSError, ValueError):
        logger.warning(
            "could not remove the Trash origin record for %r",
            display_path(entry_name),
            exc_info=True,
        )


def _names_a_different_entry(path: Path, entry_name: str) -> bool:
    """Whether the file at ``path`` is positively SOME OTHER entry's record.

    ``True`` only for a payload that reads back, is an object, and names an
    entry that is not this one. Every "we cannot say whose this is" — no file,
    unreadable, not ASCII JSON, not an object, no ``name``, a ``name`` that is
    not a string — answers ``False``, because the caller's alternative is to
    leave a file the store can never explain on the ``/data`` side, where the
    allocator burns its name for good and a later entry could adopt it.

    Not :func:`_names_entry`, which answers the READ's question ("may I use this
    for this entry?") and so treats a nameless payload as usable-by-nobody.
    Here a nameless payload has to be unlinked, so the two predicates are not
    each other's negation and are deliberately kept apart. It also does no
    schema or origin validation: a record for another entry is that entry's to
    lose whether or not THIS version can parse the rest of it.

    The two statements that can fail here are the read and the parse, and the
    arm below names all three of the exception types they have been measured to
    raise. It is not a proof that nothing else can: ``json.loads`` reached this
    module with a fourth (``RecursionError``, on a deeply nested file) that
    ``except (OSError, ValueError)`` walked straight past, and that escaped out
    of :func:`delete_trash_origin` after the entry was already gone. The shapes
    this arm is measured against are listed at that function.
    """
    try:
        raw: object = json.loads(path.read_text(encoding="ascii"))
    except (OSError, ValueError, RecursionError):
        return False
    if not isinstance(raw, dict):
        return False
    name = raw.get("name")
    return isinstance(name, str) and name != entry_name


def clear_trash_origins(origins_dir: Path) -> None:
    """Drop EVERY record, for a caller that knows none of them describes anything.

    One caller: ``trash_manage.empty_all``, once it has removed at least one
    entry AND found ``trash_dir`` empty afterwards. Both halves of that are
    load-bearing, and the caller owns them because both are questions about the
    Trash directory rather than about this one.

    What the sweep closes is the litter :func:`delete_trash_origin` cannot
    reach. A record whose entry left Trash without this app noticing — a file
    manager, an SMB client, ``docker volume rm`` — is never handed to that
    function at all, so it survived every per-row action for good; the cost of
    one is a burnt name (:func:`origin_recorded`), and, for a folder that
    reaches Trash by another route and adopts it, the residual the module
    docstring states. An emptied Trash is the one moment the whole store can be
    answered at once instead of one key at a time.

    Only the RECORDS go. Every record is written as ``<key>.json``
    (:func:`origin_file`), so the sweep is keyed on that suffix rather than on
    "everything in this directory": owning the store dir is not the same as
    being the only writer, and an Empty all is no reason to delete an operator's
    note or a backup directory left beside the records. The one non-record name
    the suffix does not spare is a file called exactly ``.json`` — measured,
    ``".json".endswith(".json")`` is True — which this store can never have
    written (:func:`origin_file` refuses an empty ``entry_name``) and which the
    sweep removes anyway. Stated, not fixed: it is one reserved name in a
    directory this app owns, and it costs an operator nothing they could not
    have named otherwise.

    Failures are logged per file and the sweep carries on, the same posture as
    :func:`delete_trash_origin` and for the same reason: it runs after the
    entries are already gone, so an exception escaping would 500 an Empty that
    succeeded. Measured against a store that was never created, against a
    directory planted at a record's own name, and against a non-record file and
    a subdirectory beside the records — see ``tests/test_trash_origins_store.py``.
    """
    try:
        # Materialised before the first unlink: removing entries from a
        # directory while iterating it is not a walk this module wants to
        # reason about.
        records = list(origins_dir.iterdir())
    except FileNotFoundError:
        return  # nothing was ever recorded, which is every pre-feature store
    except OSError:
        logger.warning(
            "could not list the Trash origin records to clear them after Trash was"
            " emptied; any that are there will be left behind, and each one holds its"
            " name against a future album",
            exc_info=True,
        )
        return
    for path in records:
        if not path.name.endswith(".json"):
            continue  # not a record this store wrote; not this sweep's to remove
        try:
            path.unlink()
        except OSError:
            # ``%r`` on the path for the same reason :func:`_warn_unusable`
            # uses it: the filename is a Trash entry's name, which comes from
            # the album's own tags.
            logger.warning(
                "could not remove the Trash origin record at %r while clearing the store;"
                " it is left behind and holds its name against a future album",
                os.fsdecode(path),
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
    would hand ``move_no_merge`` the library itself.
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
