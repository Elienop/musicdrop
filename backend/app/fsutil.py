"""Filesystem predicates, and the one MOVE, shared by callers that must not import
each other.

Two halves, and the second one is here for a structural reason rather than a
thematic one. :func:`occupied` and :func:`move_no_merge` were ``trash_manage``'s
until the delete side needed the same move-back: ``trash_manage`` imports
``import_session``, which imports ``trash``, so ``trash`` cannot import
``trash_manage`` — and a lazy in-function import would hide that cycle rather
than remove it. This module is below both and imports nothing of the app, so it
is where a primitive both ends need can live. There is exactly one definition of
"move a folder onto a path without burying it inside one" and both the restore
and the delete undo call it.

The first half is about paths that may carry a client-supplied name.

``pathlib``'s ``Path.exists()`` / ``is_dir()`` only absorb ENOENT/ENOTDIR/
EBADF/ELOOP; any OTHER OSError propagates — in particular ENAMETOOLONG
(errno 36) when a path component a client sent exceeds the kernel's
NAME_MAX (255 bytes). Endpoints whose contract is "unknown name -> refuse
(404)" used to 500 from exactly that spot: the not-found check itself was
the thing that blew up, before the ``ValueError``/None refusal could run.

A name the kernel cannot even look up because it is TOO LONG cannot be one
of our entries, so the caller's unknown-name answer applies — these
wrappers answer ``False`` for that one failure and nothing else. Every
other OSError (EACCES, ESTALE, ...) is re-raised: swallowing a permission
or mount failure would convert a real incident into a silent 404. Same
swallow-vs-reraise posture as the ``exists()`` guards in
``app/artwork/cache.py``, scoped to the failure a request can actually
cause.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from pathlib import Path


def exists(path: Path) -> bool:
    """``Path.exists`` that refuses an overlong name the way a missing one does."""
    try:
        return path.exists()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        return False


def is_dir(path: Path) -> bool:
    """``Path.is_dir`` that refuses an overlong name the way a missing one does."""
    try:
        return path.is_dir()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        return False


#: ``rename``'s ways of saying "something is already at the destination". POSIX
#: lets an implementation answer a non-empty destination directory with either
#: EEXIST or ENOTEMPTY (Linux picks ENOTEMPTY), and a destination that is a FILE
#: while the source is a directory answers ENOTDIR. All three mean the same thing
#: here, and none of them moved anything.
DEST_OCCUPIED = frozenset({errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR})


def occupied(path: Path) -> bool:
    """Whether something is at ``path`` that a move-back must refuse.

    An EMPTY DIRECTORY is not, and that is the whole reason this is a function
    rather than a bare ``exists``. ``os.rename`` REPLACES an empty destination
    directory, which is the answer :func:`move_no_merge` documents as the wanted
    one — a pruning beets or a half-finished sync leaves empty folders behind,
    and refusing to restore into one would strand exactly the albums this feature
    exists for. A bare ``exists`` pre-filter refused what the move would have
    performed, so one input had two answers depending on which layer saw it.

    Nothing is buried or merged by replacing an empty directory: there is nothing
    in it. Everything else — a non-empty directory, a file, a symlink that
    RESOLVES — is occupied, and such a symlink is occupied even when it points
    at an empty directory: ``rename`` refuses it (ENOTDIR) and following it would
    move the album somewhere the user never named. A DANGLING link reads as
    absent, because the ``exists`` above follows it and answers False; the move
    then meets whatever ``rename`` makes of it.

    Answers "occupied" for anything it cannot read, which is the conservative
    side: a refusal leaves the files in Trash.

    ``is_symlink`` is a TRIPWIRE and NO TEST CAN KILL IT (measured: dropping it
    leaves the whole suite green). Everything below refuses a symlink anyway —
    ``os.rename`` answers ENOTDIR, the copy branch's ``rmdir`` answers ENOTDIR
    and ``copytree`` then refuses a destination that exists — so today it changes
    no outcome. It is kept because this function's ANSWER is what a future caller
    would act on: read without it, "an empty directory" includes a link to one,
    and acting on that (removing it, or moving into it) leaves the library
    through a link the user never named. The order matters for the same reason:
    ``is_dir`` and ``scandir`` both FOLLOW links.

    That layering is also why the refusals need no per-branch test: this runs
    ABOVE the branch, so a file / a non-empty directory / a symlink is refused
    before either move is chosen. Only the ACCEPT is branch-specific, and it is
    pinned on both.
    """
    if not exists(path):
        return False
    try:
        if path.is_symlink() or not path.is_dir():
            return True
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except OSError:
        return True


def move_no_merge(src: Path, dest: Path) -> None:
    """Move ``src`` onto ``dest``, refusing rather than moving INSIDE it.

    ``shutil.move`` treats an existing DIRECTORY destination as a container: it
    puts the source in there under its own name. Both callers check ``exists``
    first, but a check and a move are two syscalls, so anything that creates the
    destination in the window between them — a sync client, an ``*arr``, the
    user — turns a documented refusal into a silent burial one level down.
    Measured: the album ends up at ``<origin>/<trash entry name>/``, still
    present, still complete, and in a place nothing looks for it.

    ``rename`` closes the window because the kernel makes the check and the move
    one operation, so it goes first and only a cross-filesystem move falls back
    to a copy. The refusal is normalised to ``FileExistsError`` whatever errno
    the kernel chose, so a caller can tell "the destination was taken" apart from
    a move that half-happened.

    An EMPTY DIRECTORY at the destination is REPLACED rather than refused, on
    BOTH branches, because that is what ``os.rename`` does and what the callers
    want: nothing is buried or merged (there is nothing in it), and an empty
    folder is what a pruning beets or a half-finished sync leaves behind. The
    two branches have to agree — the deployment that takes the copy branch has
    ``/data`` and ``/music`` on separate mounts and takes it for EVERY restore,
    so a copy branch that refused would refuse every restore the rename branch
    performs. :func:`occupied` is the same answer one layer up.

    One residual, stated rather than hidden: both callers move a DIRECTORY, and
    the EXDEV branch is written for that. A file source raises
    ``NotADirectoryError`` here instead of quietly taking ``shutil.move``'s file
    path, which would bury it the same way.
    """
    try:
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            # Different filesystems, so no rename can do it and the move has to
            # copy. ``copytree``'s own ``os.makedirs(..., exist_ok=False)`` is
            # the atomic refusal ``shutil.move`` skips: one ``mkdir`` syscall
            # that raises ``FileExistsError`` rather than descending into a
            # destination that appeared. ``symlinks=True`` + ``copy2`` are what
            # ``shutil.move`` itself uses for a directory, so the copy is
            # unchanged — only the container behaviour is dropped.
            #
            # ``rmdir`` first is this branch's half of the empty-directory rule
            # above, and it is exactly one syscall wide: it removes the
            # destination only while it is an EMPTY DIRECTORY and answers
            # ENOTEMPTY / ENOTDIR / ENOENT otherwise, so a non-empty directory, a
            # file and a symlink all fall through untouched to ``copytree``'s own
            # atomic refusal. Suppressed rather than inspected for the same
            # reason: every failure it can have means "not an empty directory",
            # and the refusal one line down is the answer.
            with contextlib.suppress(OSError):
                os.rmdir(dest)
            shutil.copytree(src, dest, symlinks=True)
            shutil.rmtree(src)
            return
        if exc.errno in DEST_OCCUPIED:
            raise FileExistsError(exc.errno, os.strerror(exc.errno), str(dest)) from exc
        raise
