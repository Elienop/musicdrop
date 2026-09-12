"""Filesystem predicates, and the one MOVE, shared by callers that must not import
each other.

Three parts, and the second one is here for a structural reason rather than a
thematic one. :func:`occupied` and :func:`move_no_merge` were ``trash_manage``'s
until the delete side needed the same move-back: ``trash_manage`` imports
``import_session``, which imports ``trash``, so ``trash`` cannot import
``trash_manage`` — and a lazy in-function import would hide that cycle rather
than remove it. This module is below both and imports nothing of the app, so it
is where a primitive both ends need can live. There is exactly one definition of
"move a folder onto a path without burying it inside one" and both the restore
and the delete undo call it.

The first part is about paths that may carry a client-supplied name.

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

The third is the anchored descent, :func:`open_root` + :func:`open_below` — the
one spelling of "open the root, following links" and of "walk every part below it
refusing one". Anything that ENUMERATES
through an fd it returns must CLOSE its iterator: ``os.scandir(fd)`` dups the fd
and the dup SHARES the offset, so one partially consumed iterator left open makes
every later ``scandir``/``listdir`` on that fd read ``[]`` (measured 2026-09-12,
twice; two live iterators on one fd interleave and duplicate entries).
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from pathlib import Path
from typing import Final


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


#: What a filesystem answers when it cannot fsync a DIRECTORY at all. Measured
#: errnos for that shape, and the FUSE / network mounts that give them are the
#: ones the cross-device arms exist for.
CANNOT_FSYNC_A_DIR: Final = frozenset({errno.ENOTSUP, errno.EINVAL})


def fsync_dir(dir_fd: int) -> None:
    """Force a directory's own entries durable, unless it cannot be fsynced.

    Both callers run this AFTER the entry they care about is published, so the
    fsync is a durability extra rather than a correctness precondition: a
    filesystem that answers ENOTSUP/EINVAL for it turned a completed write into
    a refusal (security seat L-6). Every other errno is a fault and raises.
    """
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno not in CANNOT_FSYNC_A_DIR:
            raise


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

    ``is_symlink`` is a TRIPWIRE: no CALLER's behaviour changes with it, because
    everything below refuses a symlink anyway — ``os.rename`` answers ENOTDIR,
    the copy branch's ``rmdir`` answers ENOTDIR and ``copytree`` then refuses a
    destination that exists. It is kept because this function's ANSWER is what a
    future caller would act on: read without it, "an empty directory" includes a
    link to one, and acting on that (removing it, or moving into it) leaves the
    library through a link the user never named. That answer IS pinned, directly
    — ``tests/test_fsutil.py::test_a_symlink_to_an_empty_directory_is_occupied``,
    measured True with this clause and False without it. The order matters for
    the same reason: ``is_dir`` and ``scandir`` both FOLLOW links.

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


def _ident(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)``, or ``None`` for a path this process cannot stat."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _holds(src: Path, dest: Path) -> bool:
    """Whether ``dest``'s ancestry reaches ``src`` by identity, not by spelling.

    ``shutil.move`` asks the same question with ``_destinsrc``, which compares
    prefixes: a bind mount or a symlink gives the two ends different spellings
    for one directory and that test answers False.
    """
    ident = _ident(src)
    return ident is not None and any(_ident(rung) == ident for rung in dest.parents)


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
        if exc.errno == errno.EXDEV and _holds(src, dest):
            # ``dest`` sits inside ``src`` by inode, so copy-then-delete would
            # copy the tree into itself and then ``rmtree`` the original, the
            # copy and everything under it. Measured on a Trash that is the host
            # parent of a bind-mounted music library: Restore reported success
            # and both trees were empty afterwards. ``os.rename`` answers EINVAL
            # for the same shape on one filesystem, so both branches now refuse
            # alike and no caller has to know which one ran.
            raise OSError(errno.EINVAL, "the destination is inside the source", str(dest)) from exc
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


#: Every component BELOW the root is opened this way: a link is refused instead of
#: followed, and a FIFO planted mid-path cannot block the open. The ONE definition:
#: the Trash remover's descent and the move-aside's container open import it.
BELOW_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK

#: The ROOT is opened FOLLOWING links: an operator's beets ``directory:`` may be a
#: symlink and refusing it would refuse the library. Same reading as
#: ``store_layout.py:723``. Owner ruling 2026-09-12: below the root a bind mount is
#: the supported spelling for spanning disks, so a link there is refused.
ROOT_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK


def open_root(root: Path) -> int:
    """Open ``root`` itself as a directory fd, FOLLOWING a link at it.

    The one spelling of the root open every anchored caller shares, so the three
    hand-written copies cannot drift. ``O_DIRECTORY`` is mandatory rather than
    tidy: measured 2026-09-12, a FIFO at ``root`` answers ENOTDIR with it and
    BLOCKS the open for as long as no writer appears without it.

    The returned fd is the caller's to close.
    """
    return os.open(root, ROOT_FLAGS)


def open_below(root: Path, rel: Path) -> int:
    """Open ``root/rel`` as a directory fd, refusing a symlink at every part below ``root``.

    The fd-based sibling of ``trash_manage._reaches_through_a_link``
    (``app/beets/trash_manage.py:1029``): both ask every component, not just the
    leaf, and the two must read alike — that docstring's rule is that a second,
    weaker traversal check must not grow. This is the stronger half, because the fd
    the walk returns IS what the caller writes through, so no component can be
    re-resolved between the check and the write.

    Measured 2026-09-12: ``O_DIRECTORY|O_NOFOLLOW`` on a symlink answers
    **ENOTDIR (20)**, not the documented ELOOP — ELOOP (40) needs ``O_NOFOLLOW``
    WITHOUT ``O_DIRECTORY``. A link to a directory, a link to a file, a dangling
    link and a plain regular file all answer ENOTDIR, so the errno cannot tell a
    caller which it met: the answer is "refused", one OSError. Also measured:
    ``open(root/"A"/"link"/"C")`` with ``O_NOFOLLOW`` on that whole path SUCCEEDS,
    which is why the walk is per component.

    An absolute ``rel``, a ``..`` part, and a ``rel`` with no parts are refused
    before any open — ``Path("")`` and ``Path(".")`` both normalise to zero parts,
    and ``Path("a/./b")`` to ``("a", "b")``, so "." never reaches the loop
    (measured). Zero parts is refused rather than answering with the root's own fd:
    the contract is a name BELOW the root. ``ValueError``, not a class of this
    module, because a caller deriving ``rel`` from ``Path.relative_to`` already
    catches one for a directory outside the root and both mean the same thing.

    The returned fd is the caller's to close.
    """
    parts = rel.parts
    if rel.is_absolute() or not parts or ".." in parts:
        raise ValueError(f"not a name below the root: {str(rel)!r}")
    fd = open_root(root)
    try:
        for part in parts:
            below = os.open(part, BELOW_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = below
    except BaseException:
        # Wider than OSError: a NUL in a part makes ``os.open`` raise ValueError
        # ("embedded null character in path", measured), and the fd walked so far
        # would leak. Re-raised unchanged.
        os.close(fd)
        raise
    return fd
