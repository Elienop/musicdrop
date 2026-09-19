"""The beets-adapter boundary.

``app/beets/`` is the only PACKAGE in the codebase that imports beets, and the
boundary is the package rather than this module — the adapter outgrew a single
file long ago, and many of its siblings import beets too. What holds is that
nothing OUTSIDE ``app/beets/`` does (CLAUDE.md rule 3). That is a convention
today, not a machine-checked one: no test or lint rule pins it.

Everything beyond the package works in terms of our own Pydantic models, so
beets' untyped surface, global config singletons, and version quirks stay
isolated here.
"""

import os
import unicodedata
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, cast

from beets.dbcore.query import MatchQuery, ParsingError
from beets.dbcore.types import DelimitedString
from beets.library import Album as BeetsAlbum
from beets.library import Item as BeetsItem
from beets.library import Library
from mediafile import MediaFile

from app.artwork.normalize import normalize_artist_name
from app.beets.release_identity import release_identity
from app.etag import stat_etag
from app.models.album import Album, AlbumDetail, OutsideLibrary, Track
from app.models.artist import Artist
from app.models.import_models import ExistingAlbum
from app.models.search import SearchEntity, SearchResults, SearchTrack, TypedSearchPage

# Allowlist of cover-art extensions we serve. `.svg` is deliberately excluded:
# serving user-controlled SVG (even via <img>) is an XSS footgun, not worth it.
_EXTENSION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


@dataclass
class LibraryHandle:
    """Public handle for the opened beets library.

    Callers outside this module annotate with ``LibraryHandle`` so they never
    need to import beets themselves, keeping the adapter the sole beets
    importer (CLAUDE.md rule 3). The handle bundles the opened ``Library`` with
    the metadata the read-only Config view needs: the path of the user-owned
    ``config.yaml``, when ``setup_beets()`` ran, and the file's mtime at load —
    used to flag "restart required" when the file changes on disk.
    """

    lib: Library
    beets_dir: Path
    config_path: Path
    loaded_at: datetime
    file_mtime_at_load: float  # raw stat.st_mtime for direct comparison


def open_library(library_path: str, directory: str | None = None) -> Library:
    """Open a beets library database at ``library_path``.

    ``directory`` is the music root beets indexed. beets 2.12's ``Library`` reads
    the configured path formats + replacements from the global config itself (the
    ``path_formats``/``replacements`` constructor kwargs were removed), so an
    import places and names files exactly as ``beet import`` would — provided the
    config is loaded first (``setup_beets`` does).
    """
    return Library(library_path, directory=directory)


def close_library(lib: Library) -> None:
    """Close the beets library's underlying SQLite connection."""
    lib._close()


class LibraryRootUnavailableError(Exception):
    """The music directory itself is missing — likely an unmounted share.

    Guard against the nightmare scenario: with the root gone EVERY file looks
    deleted, so any "the file is not there, drop the row" rule fires library-wide
    at once. Fail fast instead.

    Lives in the base adapter rather than in one feature module because both
    row-dropping surfaces need the SAME predicate — disk sync's per-item removal
    (``app.beets.disk_sync``) and the Trash primitives' missing-folder handling
    (``app.beets.trash``). A second, looser copy of the check is the failure this
    placement exists to prevent.

    ``empty`` marks the one arm an empty library can legitimately be in — the
    root is there and readable but holds nothing. Only the import side reads it
    (:func:`require_importable_library_root`); every other caller treats all
    three arms alike.
    """

    def __init__(self, message: str, *, empty: bool = False) -> None:
        super().__init__(message)
        self.empty = empty


def _music_dir(lib: Library) -> str:
    """The music root as a normalized str path (beets stores it as bytes)."""
    return os.path.normpath(os.fsdecode(lib.directory))


def require_library_root(lib: Library) -> None:
    """Raise :class:`LibraryRootUnavailableError` unless the music root looks mounted.

    Call this at the DECISION MOMENT — immediately before code would read a
    missing path as a user deletion — not merely once at the start of a job: a
    share can drop mid-run, and every remaining file then looks deleted.
    """
    root = _music_dir(lib)
    if not os.path.isdir(root):
        raise LibraryRootUnavailableError("Library folder unavailable. Is the music share mounted?")
    # A dropped NAS/SMB/NFS mount usually leaves the mountpoint PRESENT but empty
    # (the kernel keeps the directory), so os.path.isdir alone stays True and the
    # caller would read every file as deleted and wipe the DB. Treat an empty or
    # unreadable root as unavailable too — a real library's root always has
    # entries, and a genuine single deletion leaves siblings behind. Deliberately
    # O(1) (one entry, not a recursive audio scan): the residual gaps — a stray
    # file left on the local mountpoint masking a drop, or a genuinely-empty
    # library reading as unavailable — are accepted, since the alternative is the
    # library-wide row loss this guard exists for.
    try:
        with os.scandir(root) as it:
            has_entry = next(it, None) is not None
    except OSError as exc:
        # Fails closed like the empty case, but NOT with the same sentence: a
        # permission drift (PUID/PGID), a stale NFS handle or an I/O error is
        # not an empty folder, and the person reading this string is the one who
        # can fix it. ``strerror`` is the OS's own summary ("Permission denied",
        # "Stale file handle") and carries no path, so it is safe to surface.
        reason = f" ({exc.strerror})" if exc.strerror else ""
        raise LibraryRootUnavailableError(
            f"Library folder is unreadable{reason}. Check its permissions and the mount."
        ) from exc
    if not has_entry:
        raise LibraryRootUnavailableError(
            "Library folder is empty. Is the music share mounted?", empty=True
        )


def require_importable_library_root(lib: object) -> None:
    """:func:`require_library_root` for the IMPORT side, which forgives a fresh install.

    Two jobs, both needed here. The registry and runner must not import beets, so
    they cannot name ``Library``: this is the one cast, beside the predicate it
    delegates to. And an EMPTY root means two opposite things — a dropped share,
    or the empty ``/music`` bind mount Docker hands every new install, which the
    app never creates. The database separates them: with no item rows there is
    nothing a write could shadow and nothing to lose, so the first import starts.

    Only this arm is forgiven. A missing or unreadable root still refuses, rows
    or no rows, and Trash, Delete, Restore and disk sync keep the stricter
    :func:`require_library_root` unchanged.
    """
    library = cast(Library, lib)
    try:
        require_library_root(library)
    except LibraryRootUnavailableError as exc:
        if not (exc.empty and not _sampled_library_files(library, 1)):
            raise


#: How many DISTINCT albums :func:`require_library_present` asks about before it
#: concludes the library's music is not there.
#:
#: The number is a trade between two measurable rates, not a round pick:
#:
#: * **False lockout.** Let ``f`` be the fraction of live album rows whose sampled
#:   FILE is legitimately missing — its folder deleted outside MusicDrop, or that
#:   one track removed from a folder that is still there, or never synced back. The
#:   check refuses only when ALL sampled albums are missing — but the rate is
#:   ``f**(K-1)``, not ``f**K``, and the difference is a whole factor of ``f``.
#:   The sample is drawn from a population that still holds the row this call is
#:   about: every caller runs the check BEFORE ``album.remove()``, and the delete
#:   path's two callers reach it precisely because that album's files are already
#:   gone. So one draw is a guaranteed miss whenever the sampler happens to draw
#:   it — which at ``N <= K`` albums it always does, the draw being exhaustive.
#:   Measured in a 2-album library: the sampler returned the ghost's own file
#:   plus the bystander's. At a pathological ``f = 0.5`` — half the library already
#:   ghosts — K=4 gives 12.5%, K=5 gives 6.3%, K=6 gives 3.1%. At a realistic
#:   ``f = 0.05``, K=5 is 0.000625%. The curve has flattened by 5; more samples
#:   buy almost nothing.
#: * **Blocking cost.** Each sample is one ``isfile`` — one ``os.stat``, the same
#:   cost the folder check paid (measured: 1 for ``isdir``, ``isfile`` and
#:   ``exists`` alike). On a *hung* (rather than
#:   dropped) NFS/SMB mount a stat blocks for the mount's timeout, so K is also
#:   the worst-case number of timeouts a user waits through. That caps K low.
#:
#: The check short-circuits on the FIRST file it finds, so a healthy library
#: pays one stat and only the refusal path pays all K.
_PRESENCE_SAMPLE_SIZE = 5

# One item path per sampled album. ``ORDER BY RANDOM()`` rather than the first K
# rowids on purpose: a FIXED sample makes a false refusal permanent (the same
# few long-deleted albums are re-checked forever), while a re-rolled sample lets
# a healthy-but-stale library recover on the next attempt — but ONLY where the
# library has more albums than ``_PRESENCE_SAMPLE_SIZE``. At ``N <= K`` the draw
# is exhaustive, so re-rolling returns the same set forever and a refusal is
# permanent until the library itself changes. That is not a hedge on a rare
# shape: a library whose albums' files were all removed outside MusicDrop is
# refused at every size, and measured at N = 1, 2, 3, 5 and 6 the user then
# cannot clean up a single row through the app. The message says so (see
# ``require_library_present``) rather than blaming a mount that is fine. None of
# that can weaken the true positive — with the share gone EVERY file the library
# names is missing, so every draw refuses.
#
# Cost is O(rows in `albums`), which is the small table (an item scan at 75k rows
# is what this deliberately avoids), and it is paid only on the rare arm that is
# about to drop rows having moved nothing. That cost rests on
# beets' own ``idx_item_album_id`` (``CREATE INDEX idx_item_album_id ON items
# (album_id)``), which turns the outer query into a SEARCH: measured 0.18 ms at
# 5k albums / 75k items with it, 2.1 ms without. Named because it is otherwise
# an invisible dependency -- if beets ever drops that index the claim above
# silently stops being true.
_SAMPLE_ALBUM_PATHS_SQL = """
SELECT MIN(path) FROM items
WHERE album_id IN (SELECT id FROM albums ORDER BY RANDOM() LIMIT ?)
GROUP BY album_id
"""

# Fallback for a library the query above grouped NOTHING out of, and "no album to
# sample" must not read as "no music". Two shapes reach it: zero album rows (a
# singleton-only library — ``beet import -s``, or a folder of loose tracks), and
# a library whose drawn albums all happen to hold no items. It asks the `items`
# table directly and does NOT narrow to ``album_id IS NULL`` the way it first
# did: the question here is only whether ANYTHING the library names is on disk,
# and an item pointing at an album row that no longer exists names a folder just
# as well as a singleton does. Narrowed, those rows produced an empty sample and
# were waved through — see the empty-sample arm in ``require_library_present``.
#
# ``ORDER BY RANDOM()`` for the same reason as the album query, against a
# measured lockout rather than a hypothetical one. On a singleton-only library —
# 200 rows, 195 of them really on disk, only the 5 lowest-rowid ones removed by hand,
# share mounted — the bare ``LIMIT`` this used to be refused 20 of 20 attempts
# with ONE distinct draw over 5 calls, blaming the mount. That user can never
# delete or restore a single row: ``require_library_present`` gates the delete
# path and ``restore_album``'s entry, so the refusal is permanent.
#
# The cost the bare ``LIMIT`` was justified by ("this scan is over the BIG
# table") does not survive being measured. At 75k singletons: bare ``LIMIT``
# 0.004 ms, ``ORDER BY RANDOM()`` 2.3 ms — a scan plus a temp B-tree, since no
# index helps an unordered pick. It is paid once, only on this arm, and only by a
# library with no groupable album. Against the five ``os.path.isfile`` calls it
# precedes it is the larger half on a local disk (0.0006 ms each) and the smaller
# one on the network share this whole predicate exists for, where every stat is a
# round trip and can block for the mount timeout. Either way, 2 ms once per
# delete attempt does not buy a permanent lockout.
_SAMPLE_ITEM_PATHS_SQL = """
SELECT path FROM items ORDER BY RANDOM() LIMIT ?
"""


def _sampled_library_files(lib: Library, size: int) -> list[str]:
    """Up to ``size`` on-disk FILES the library BELIEVES it owns.

    One per album where the library groups into albums — its ``MIN(path)``
    track — and one per item on the fallback arm, which is what a singleton-only
    library has instead. Both draws re-roll — see the comments on the two
    queries for why a fixed sample makes a false refusal permanent.

    The FILE, not ``os.path.dirname`` of it. Taking the folder made the answer
    depend on how deep the path template files a track: under a ``paths.default``
    with no directory component (beets' own ``$title`` is the shortest, and the
    template is editable from Settings -> Naming) every track sits directly in
    the music root, every dirname collapses to that root, and the check ended up
    asking ``os.path.isdir(<music root>)`` — the question ``require_library_root``
    has already answered, and the one a stray entry on a dropped share's
    mountpoint answers wrongly. Measured on a 200-row flat library with the share
    dropped and only a ``.stfolder`` on the mountpoint: the root came back 5 times
    out of 5 and the check ACCEPTED, while the same rows re-filed under
    ``$albumartist/$album/$title`` were refused.

    Raw SQL rather than ``lib.albums()``/``album.items()``: materializing beets
    models to read one path each costs seconds at 75k tracks. Paths are resolved through
    :func:`_abs_path` because the DB stores them relative to ``lib.directory``
    in the normal case.
    """
    with lib.transaction() as tx:
        rows = tx.query(_SAMPLE_ALBUM_PATHS_SQL, (size,))
        if not rows:
            rows = tx.query(_SAMPLE_ITEM_PATHS_SQL, (size,))
    files: list[str] = []
    for row in rows:
        raw = row[0]
        if not raw:
            # A row naming no file is no evidence either way, so it must not
            # spend one of the K slots. Under the folder check this skip was
            # load-bearing in a second way — ``dirname(join(root, ""))`` is the
            # library ROOT, which exists whenever ``require_library_root`` has
            # just passed, so one such row answered "the music is there" for a
            # share that is gone. A file check cannot be fooled that way (the
            # root is not a file), but the slot is still worth keeping.
            continue
        # ``os.fsencode``, never ``bytes(raw)``. SQLite is dynamically typed, so
        # the declared-BLOB ``path`` column hands back whatever was written to
        # it: beets writes bytes, but a row an external tool or a manual
        # ``UPDATE`` wrote comes back as ``str`` and ``bytes(str)`` raises
        # ``TypeError: string argument without an encoding``. That escapes as a
        # 500 from the presence check — the one predicate whose whole job is to
        # answer "is the music really there" before rows are dropped. fsencode
        # takes both types and is the exact inverse of the ``os.fsdecode``
        # ``_abs_path`` applies next, so neither loses a non-UTF-8 path.
        files.append(_abs_path(lib, os.fsencode(raw)))
    return files


def require_library_present(lib: Library) -> None:
    """:func:`require_library_root`, plus POSITIVE proof the music is really there.

    Strictly stronger, and deliberately NOT the shared default. The cheap
    predicate treats a root holding ANY entry as mounted, which is the right
    trade for disk sync (it runs per removal, and its O(1) cost is a design
    property) but leaves one hole: a ``.stfolder``, a ``lost+found`` or an empty
    leftover directory sitting on the LOCAL mountpoint satisfies "has an entry"
    while the share behind it is gone. Every album then reads as deleted, and any
    caller about to drop rows on that reading drops the whole library.

    So this asks the only question that has no false answer in that state:
    *does anything the library says is on disk actually exist?* With the share
    gone the answer is no for every album at once; with it mounted, one surviving
    file is enough. Sampling ``_PRESENCE_SAMPLE_SIZE`` albums and accepting the
    FIRST hit is what keeps a legitimately-deleted album from locking the user
    out of deleting it.

    Each sampled slot is one FILE — the album's ``MIN(path)`` track, or one item
    row on the fallback arm — never the folder holding it, because a flat layout
    collapses every folder to the music root (see ``_sampled_library_files``).
    That is measurably stricter in exactly one way per album: an album whose
    folder survives with its sampled track removed is a miss where the folder
    check was a hit. Measured, sampling 1000 draws each: at 200 albums with 1 or
    5 files or a whole folder removed it refused 0 times, and at 2 and at 5
    albums with one or four files removed 0 times; at N=1 album with its only (or
    its lowest-named) track removed while the folder stays, it refuses 20 of 20
    where the folder check refused 0 of 20. That last shape is the one the note
    above already describes for a removed FOLDER — "refused at every size" below
    ``_PRESENCE_SAMPLE_SIZE`` albums — extended to a removed file.

    What that is NOT is a claim that a single-album library is the only library
    it refuses. The refusal needs all K sampled albums to miss, which is a RATE
    and not a shape wherever some albums are in that state and some are not:
    measured with half of 200 albums missing their sampled file (folders all
    intact, share mounted), 33 refusals in 1000 draws — 24 in an independent run
    of the same probe, against the ``f**K`` = 3.1% the note above models. At
    ``f = 1`` — every album's file removed by hand — it is certain at every size
    measured (50 of 50 at N = 1, 2, 6, 20 and 200), which is the same answer a
    dropped share gets and the reason this predicate exists.

    Why sampling and not a mount check. ``os.path.ismount`` / an ``st_dev``
    comparison against the parent / ``/proc/mounts`` all answer "is a filesystem
    mounted at this exact path", which is not the question — and they have no
    baseline for what SHOULD be mounted. Measured: a healthy library on a plain
    local directory, or in the very common "library is a subdirectory of the
    mount" layout, is ``ismount() == False``, so requiring it would refuse
    healthy libraries; and in the Docker deployment ``/music`` is a bind mount,
    so it is ``True`` whether or not the share behind it is alive. They
    discriminate in neither direction. A stale NFS handle, the other mount-level
    signal, already surfaces as the ``OSError`` arm above.

    Two states pass by design rather than by accident:

    * a library that names no file path at all — no rows to verify, so no stat
      that could answer the question. The arm below lists every shape that
      reaches it and what each one costs;
    * a library whose sampled albums are all legitimately gone — improbable by
      construction (see ``_PRESENCE_SAMPLE_SIZE``), and failing toward a kept 503
      rather than lost rows. It is NOT self-correcting on the next attempt below
      ``_PRESENCE_SAMPLE_SIZE`` albums, where the re-rolled draw is exhaustive
      and returns the same set forever; the refusal then stands until the library
      changes, which is reachable from a ``library.db`` restored onto a
      re-pointed music dir. That is why the message names both causes instead of
      blaming a mount.
    """
    require_library_root(lib)
    sample = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)
    if not sample:
        # Every way into this arm, and what the predicate does with each:
        #
        # * NO item rows — a fresh install, or a ``library.db`` whose `items`
        #   table was emptied while `albums` survived. Indistinguishable from
        #   here, and it does not matter: both name zero file paths, so there is
        #   no stat that could answer "is the music there". Accepted because the
        #   predicate has no evidence, not because it found any.
        # * Every drawn row's `path` is NULL or empty (only an external ``UPDATE``
        #   writes that). Same answer for the same reason — a row naming no file
        #   cannot be confirmed or denied by ``os.path.isfile``.
        # * Items whose `album_id` points at a deleted album row USED to land
        #   here and be accepted, because the fallback query asked only for
        #   ``album_id IS NULL``. It no longer narrows, so that shape is sampled
        #   and answered like any other rather than waved through.
        #
        # The residual, stated because it is real: a share dropped under a
        # mountpoint that still holds an entry (a ``.stfolder``, a
        # ``lost+found``) passes ``require_library_root``, and if the DB also has
        # no usable path it passes here too. That needs a damaged database — a
        # healthy library with rows never reaches this arm — and the only way to
        # fail closed on it is to refuse EVERY pathless library, which refuses
        # every fresh install.
        #
        # Which is the trade the root check above is already making, deliberately.
        # An empty library cannot tell a fresh install from a dropped share, and
        # the two want opposite answers: allowing the write puts the user's files
        # on a bare mountpoint that the real share then shadows, while refusing
        # leaves them safely where they are. A dropped share normally leaves the
        # mountpoint bare and the root check refuses it. Measured cost of
        # refusing here as well: delete your ONLY album and Restore answers "Is
        # the music share mounted?" about a share that is fine, until any other
        # music exists. That is a recoverable annoyance against an unrecoverable
        # loss, so it fails toward the annoyance.
        return
    # ``os.path.isfile`` swallows OSError itself, so a permission fault or a
    # stale handle on one path reads as "not there" and the loop moves on — the
    # same fail-closed posture as the root check's unreadable arm.
    #
    # ``isfile`` rather than ``exists``: the question is whether the FILE the
    # library named is there, and a directory (or a socket, or a device node)
    # sitting at that path is not evidence the music came back. Both follow
    # symlinks and both cost one stat, so the only behavioural difference beyond
    # the type test is none — a symlinked library reads present, a DANGLING link
    # reads absent, which is the fail-closed direction. The type test is pinned
    # behaviourally, not only by name: ``test_library_presence_sampling.py::
    # test_a_directory_where_the_track_should_be_is_not_the_music_coming_back``
    # puts a directory at a single album's sampled path and fails on the swap.
    for path in sample:
        if os.path.isfile(path):
            return
    raise LibraryRootUnavailableError(
        "Library folder is present but none of the music files the library names"
        " are in it. Either the music share is not mounted, or those files have"
        " been removed outside MusicDrop."
    )


def library_paths_context(handle: LibraryHandle) -> AbstractContextManager[Any]:
    """Bind beets' path conversion to this library for the calling thread.

    beets stores DB paths relative to the library directory (2.11 through 2.13.1
    alike) and converts in BOTH directions through a ``ContextVar``
    (``beets.context``) that ``Library.__init__`` arms only in the context that
    opened the library. A worker thread inherits nothing, so job runners bind
    this around their whole sweep. Unbound, reads get a relative path they cannot
    resolve AND writes store an absolute one — the write side is the dangerous
    half, because the row still points at the right file until the music
    directory moves.

    ``Library._fetch`` binds the same var itself, but only around query PARSING
    — the binding closes before ``_get_results`` builds the models — so it does
    not cover model reads or writes. Do not drop a bind here believing it does.

    The quirk stays behind the adapter (rule 3).
    """
    ctx: AbstractContextManager[Any] = handle.lib.music_dir_context()
    return ctx


def album_exists(handle: LibraryHandle, album_id: int) -> bool:
    """Whether ``album_id`` is in the library. A scalar read; no path expansion."""
    return handle.lib.get_album(album_id) is not None


def _fold(value: object) -> str | None:
    """NFC-normalized, case-folded, stripped comparison key. ``None`` when blank.

    NFC before casefold because the two Unicode spellings of an accented name
    (``é`` as U+00E9 vs ``e`` + U+0301) render identically and name the SAME
    album, while comparing unequal byte-for-byte: whichever spelling a re-tag
    happened to write must not read as a stranger.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    return text.casefold() or None


def _album_identity_matches(album: Any, stored: ExistingAlbum) -> bool:
    """Whether the live album at ``stored.album_id`` is still the album banked.

    An id ALONE proves nothing. beets album ids are SQLite rowids on an
    ``id INTEGER PRIMARY KEY`` table, which reuses a deleted row's id (the
    delete-then-reinsert case is documented at
    ``import_session._library_change_signature``) — so after the banked album
    was deleted, that id can name a completely different album. Acting on it
    unverified would skip an import the user wanted, or Trash a copy they never
    decided anything about.

    Only the entry with NOTHING to compare (blank artist AND blank album AND no
    release URL) is rejected up front: an empty key would match every sparsely
    tagged album in the library, which is the id-reuse hazard again with extra
    steps. That guard does NOT cover a blank-named entry that carries a URL —
    the URL is what keeps it out, and the URL is what must then verify it (see
    the last bullet). Compared in order:

    * ``album_artist`` and ``album``, case-folded and stripped — the same pair
      beets' own ``duplicate_keys.album`` uses, so a match here is the identity
      the user was actually shown. Case-folding is deliberately laxer than
      beets' byte-exact query: a case-only re-tag is the SAME album, and reading
      it as a stranger is the wrong answer for both callers.
    * the release URL. It is the strongest discriminator available
      (``ExistingAlbum`` stores the release identity, not the raw id, and the
      URL embeds it), yet a copy re-tagged to a different release since banking
      must not be read as a match — so a live URL that DISAGREES rejects, while
      a live copy carrying none leaves the name match standing.
    * ...unless the stored name key is INCOMPLETE — either folded name None —
      and a URL was stored. A full name pair is two independent discriminators;
      one name plus a blank is one, and "no name == no name" matches every
      untagged album a reused rowid could now hold. A stored entry that brought
      a URL to that comparison must have it VERIFIED, not merely
      non-contradicting: an absent live URL fails SHUT. Half-named and carrying
      NO URL is unchanged — the single name is all the identity that was ever
      recorded, so it is all that can be asked for.
    """
    stored_url = stored.release.release_url if stored.release is not None else None
    stored_artist = _fold(stored.album_artist)
    stored_album = _fold(stored.album)
    if not (stored_artist or stored_album or stored_url):
        return False
    # Read as plain attributes, not ``getattr(..., None)``: both are FIXED beets
    # Album fields (always present, "" when unset), and a default would quietly
    # accept a ``None`` album from a missing id — making the caller's own
    # not-found skip dead code that no mutation could reach.
    if _fold(album.albumartist) != stored_artist:
        return False
    if _fold(album.album) != stored_album:
        return False
    if not stored_url:
        return True
    # ``getattr`` HERE, unlike the two names above, because this must be read
    # EXACTLY the way the stored side was: ``to_existing_album`` builds the
    # banked URL with ``release_identity(album, getattr(album, "mb_albumid",
    # None))`` (``existing_album.py:70``). Both halves of the comparison have to
    # be computed identically, or a difference in how the id was fetched would
    # read as a difference between the releases.
    live_url = release_identity(album, getattr(album, "mb_albumid", None)).release_url
    if stored_artist is None or stored_album is None:
        # An incomplete name key proved too little on its own: the stored URL is
        # then the identity, and an absent live one verified nothing at all.
        return live_url == stored_url
    return not live_url or live_url == stored_url


def duplicate_albums_still_present(lib: Library, existing: Sequence[ExistingAlbum]) -> list[int]:
    """The banked duplicate ids the library still holds AS THE SAME album.

    The identity-verified answer to "does the collision the user decided about
    still exist?": every id in the result is present AND passes
    ``_album_identity_matches``. A missing id, or one whose album no longer
    matches, is simply absent from the result — callers treat that as "did not
    survive" (import proceeds) or "not trashable" (leave it alone), never as a
    fault.

    ``music_dir_context`` is cheap insurance, the same posture as
    ``completeness.release_missing_report``: callers run on worker threads that
    inherit none of beets' path ContextVar (``library_paths_context``), and
    while the fields compared here are metadata that read fine unbound, an
    unbound path read does not raise — it silently yields ``b""``. Binding costs
    nothing and removes the class of bug rather than the one instance of it.
    """
    present: list[int] = []
    with lib.music_dir_context():
        for stored in existing:
            album = lib.get_album(stored.album_id)
            if album is None:
                continue
            if _album_identity_matches(album, stored):
                present.append(stored.album_id)
    return present


def surviving_duplicate_album_ids(
    handle: LibraryHandle, existing: Sequence[ExistingAlbum]
) -> list[int]:
    """``duplicate_albums_still_present`` for callers outside ``app/beets/``.

    Handle-typed so ``app/bank/`` never touches a beets object (rule 3),
    the same shape ``album_exists`` takes.
    """
    return duplicate_albums_still_present(handle.lib, existing)


def _coerce_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_year(value: object) -> int | None:
    try:
        year = int(value)  # type: ignore[call-overload]  # beets value is untyped
    except (TypeError, ValueError):
        return None
    return year or None


def _play_order(item: Any) -> tuple[int, int, int]:
    """Sort key putting an album's tracks in play order: ``(disc, track, id)``.

    The order every genre fallback resolves in — see :func:`_album_genre`.
    """
    return (
        _coerce_int(item.get("disc")),
        _coerce_int(item.get("track")),
        _coerce_int(item.get("id")),
    )


# beets 2.13 dropped the single-valued ``genre`` field from BOTH ``Item`` and
# ``Album`` in favour of multi-valued ``genres``: a real column holding a
# ``DelimitedString`` — a ``list[str]`` in Python, joined by ``"\␀"`` in the DB
# and by ``"; "`` for display. Reading ``genre`` still "works" (it falls through
# to the flex path) but answers ``None`` for every row in a real library, so
# every genre read in this app goes through the helpers below.
#
# The delimiter and the split rule are taken from beets' OWN field type rather
# than restated here: the browse cache reads the column as raw SQL, bypassing
# beets' type layer, and a second hand-rolled convention is exactly how the two
# paths would drift apart on a beets change.
_GENRES_TYPE = BeetsItem._fields["genres"]
assert isinstance(_GENRES_TYPE, DelimitedString)  # beets 2.13 contract, asserted once
_GENRE_DISPLAY_DELIMITER = _GENRES_TYPE.fmt_delimiter


def _genre_values(value: object) -> list[str]:
    """Every genre in a beets ``genres`` value, whatever shape it arrives in.

    ONE reader, because three layers hand this over differently: beets returns
    the field's ``model_type`` (a ``list``), ``browse.py``'s aggregate SQL pass
    returns the raw delimiter-joined column string, and a row written before the
    multi-genre migration can still hold a single bare genre. Strings are split
    by beets' own ``DelimitedString.parse`` (DB delimiter when present, else
    ``"; "``); blanks are dropped so a trailing delimiter can't invent a genre.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts: list[str] = _GENRES_TYPE.parse(value)
    elif isinstance(value, list | tuple):
        parts = [str(part) for part in value]
    else:
        parts = [str(value)]
    return [text for text in (str(part).strip() for part in parts) if text]


def _genre_join(values: list[str]) -> str | None:
    """Genres as ONE display string — beets' own ``"; "`` join.

    ``None`` when there are none, so ``Album.genre`` stays nullable on the wire
    (a genre-less album must not read as an empty-string genre).
    """
    return _GENRE_DISPLAY_DELIMITER.join(values) or None


def _album_genre(album: BeetsAlbum, items: list[Any]) -> str | None:
    """Read album-level genres, falling back to the album's tracks.

    Heuristic: the first track with any genre wins (not a mode/majority vote),
    and its genres are taken WHOLE. Items are passed in so we don't re-fetch
    them from the database.

    The fallback resolves in PLAY order, not the order ``items`` happens to
    arrive in. Callers pass ``list(album.items())``, which beets returns in the
    user-configurable ``sort_item`` DISPLAY order (``artist+ album+ disc+
    track+`` by default) — so on an album whose tracks carry different artists
    the alphabetically-first artist's genre used to win, and the answer moved
    with a display preference. ``app/beets/browse.py``'s cache reads the same
    fallback straight from SQL in play order; sorting here is what keeps every
    endpoint (Browse rows, album detail, duplicates) on ONE answer per album.
    """
    genres = _genre_values(album.get("genres"))
    if not genres:
        for item in sorted(items, key=_play_order):
            genres = _genre_values(item.get("genres"))
            if genres:
                break
    return _genre_join(genres)


def _coerce_int(value: object) -> int:
    try:
        number: int = int(value)  # type: ignore[call-overload]  # beets value is untyped
    except (TypeError, ValueError):
        return 0
    return number


def _require_id(value: int | None) -> int:
    """Narrow a beets row id: objects loaded from the library always have one."""
    if value is None:  # unreachable for objects fetched from the library
        raise TypeError("beets object has no id (was never stored in the library)")
    return value


def _coerce_duration(value: object) -> float | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]  # beets value is untyped
    except (TypeError, ValueError):
        return None
    return seconds or None


def _instrumental_value(value: object) -> bool:
    """The truth of a ``lyrics_instrumental`` value, however it arrives.

    beets' LyricsPlugin registers the field as BOOLEAN, so a loaded plugin hands
    back a real ``bool``; without it the flex value is the raw string ``"1"`` /
    ``"0"`` — and ``"0"`` is a TRUTHY Python string, so a plain truth test would
    read a track beets explicitly marked NOT instrumental as instrumental. The
    same two shapes come out of a raw ``item_attributes`` SELECT, which is why
    this takes a value rather than an item: ``browse.py``'s aggregate cache
    build reads the flag straight from SQL and must bucket it identically.
    """
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false"}
    return bool(value)


def _is_instrumental(item: Any) -> bool:
    """Whether beets' ``lyrics_instrumental`` flag is set on this track.

    Lives in this module rather than beside the rest of the lyrics adapter
    because ``lyrics.py`` imports THIS file (a top-level import back would
    cycle). ``lyrics.py`` and ``browse.py`` both consume the flag's truth from
    here — one implementation, the way every other shared helper here is used.
    A second copy is how the "0"-is-truthy bug comes back on one path only.
    """
    return _instrumental_value(item.get("lyrics_instrumental"))


def _album_fields(album: BeetsAlbum, *, track_count: int, genre: str | None) -> dict[str, Any]:
    """Map a beets album + precomputed track_count/genre to the ``Album`` fields.

    Factored out so ``_to_album``, ``_to_album_cached`` and ``get_album_detail``
    build the album portion from one source of truth. ``track_count`` and
    ``genre`` are passed in (not derived from ``items`` here) so a caller that
    already has them — the BrowseRow cache — need not re-query ``album.items()``.
    """
    return {
        "id": _require_id(album.id),
        "album_artist": _coerce_str(album.albumartist),
        "title": _coerce_str(album.album),
        "year": _coerce_year(album.year),
        "track_count": track_count,
        "genre": genre,
        "mb_albumid": _coerce_optional_str(album.mb_albumid),
    }


def _to_album(album: BeetsAlbum) -> Album:
    items = list(album.items())
    return Album(**_album_fields(album, track_count=len(items), genre=_album_genre(album, items)))


def _to_album_cached(album: BeetsAlbum, *, track_count: int, genre: str | None) -> Album:
    """Build an ``Album`` from a beets album + a BrowseRow's precomputed
    track_count/genre, WITHOUT a per-album ``items()`` query.

    The browse/list/recent pages already hold these two values from the single
    cache scan that built the BrowseRows; calling ``_to_album`` (which re-runs
    ``album.items()``) once per page row is the avoidable N+1 this replaces.
    """
    return Album(**_album_fields(album, track_count=track_count, genre=genre))


def _to_track(item: Any) -> Track:
    has_lyrics = bool(item.lyrics)
    return Track(
        id=int(item.id),
        title=_coerce_str(item.title),
        track=_coerce_int(item.track),
        disc=_coerce_int(item.disc),
        duration_seconds=_coerce_duration(item.length),
        artist=_coerce_str(item.artist),
        mb_trackid=_coerce_optional_str(item.mb_trackid),
        has_lyrics=has_lyrics,
        # Non-empty lyrics WIN over the flag — the same precedence the coverage
        # SQL's mutually-exclusive buckets use. A sweep that found real lyrics
        # for a previously-instrumental track resets the flag, but a row written
        # before that reset must still read as "has lyrics", not "instrumental".
        instrumental=not has_lyrics and _is_instrumental(item),
        format=_coerce_optional_str(item.format),
    )


def _row_path(item: Any) -> str:
    """The item's file path, normalised — the one spelling judged AND shown."""
    return os.path.abspath(os.fsdecode(item.path))


def _inside_library(lib: Library, item: Any) -> bool:
    """True iff the item's file lives under the library dir; no filesystem read.

    Mirrors beets' guard in ``Item.try_sync`` (``library/models.py:1027``).
    ``commonpath``, not a prefix test: ``<music>`` and ``<music>-inbox`` are
    different folders. No-disk pinned by
    ``test_the_containment_question_records_no_filesystem_read``.
    """
    libdir = os.path.abspath(os.fsdecode(lib.directory))
    return os.path.commonpath([_row_path(item), libdir]) == libdir


def _outside_library(lib: Library, items: list[Any]) -> OutsideLibrary | None:
    """The folder of the album's first outside row, and whether it holds them all.

    ``holds_every_track`` is conservative: a pathless row, a row in the library
    or a row in another folder makes it false. Only that shape re-adds safely
    (``test_the_offered_remedy_finishes_the_album_and_trashes_nothing``).
    """
    outside = next((it for it in items if it.path and not _inside_library(lib, it)), None)
    if outside is None:
        return None
    folder = os.path.dirname(_row_path(outside))
    return OutsideLibrary(
        folder=folder,
        holds_every_track=all(
            bool(it.path) and os.path.dirname(_row_path(it)) == folder for it in items
        ),
    )


def get_album_detail(lib: Library, album_id: int) -> AlbumDetail | None:
    """Return an album with its tracklist, or ``None`` when the album is missing.

    Tracks are sorted by ``(disc, track)`` so the tracklist reads in play order.
    ``music_dir_context`` is bound because the outside-library read below expands
    DB-relative paths; unbound, a whole album reads as outside.
    """
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            return None
        items = list(album.items())
        tracks = sorted(
            (_to_track(item) for item in items),
            key=lambda t: (t.disc, t.track),
        )
        return AlbumDetail(
            **_album_fields(album, track_count=len(items), genre=_album_genre(album, items)),
            tracks=tracks,
            release=release_identity(album, album.mb_albumid),
            # Not display_path: wire.py scrubs at the sink (main.py:459).
            outside_library=_outside_library(lib, items),
        )


def list_artists(lib: Library) -> list[Artist]:
    """Return the artist roster: one entry per distinct ``albumartist``.

    beets has no first-class artist entity, so we derive it by grouping albums on
    ``albumartist`` and counting distinct albums per artist. Sourced from the
    shared ``BrowseRow`` cache (ONE scan, invalidated on every
    ``emit_library_changed``) rather than a fresh ``lib.albums()`` materialization
    — the roster is rebuilt on every debounced search keystroke, and the cache
    already holds ``albumartist`` per album. Sorted diacritic-insensitively
    (``normalize_artist_name``: NFKD accent-fold + casefold), so e.g. "Édith
    Piaf" lands in the "E" run rather than after "Z" — plain ``casefold()``
    breaks ties for determinism when two names normalize identically. The
    frontend A-Z jump strip (``AlphabetIndex``) buckets on this same
    diacritic-folded first letter, so its buckets stay contiguous runs of
    this order.
    """
    # Lazy import: browse.py imports helpers from this module at import time, so a
    # top-level import back would cycle (mirrors list_albums).
    from app.beets.browse import _rows

    counts: dict[str, int] = {}
    for row in _rows(lib):
        name = row.albumartist
        # A null/whitespace albumartist would emit a blank, nameless card; drop
        # those albums from the roster (BrowseRow.albumartist is already _coerce_str'd).
        if not name.strip():
            continue
        counts[name] = counts.get(name, 0) + 1
    artists = [Artist(name=name, album_count=count) for name, count in counts.items()]
    artists.sort(key=lambda a: (normalize_artist_name(a.name), a.name.casefold()))
    return artists


def get_artist_mbid(lib: Library, name: str) -> str | None:
    """Return the MusicBrainz artist MBID for an albumartist name, or None.

    Reads ``mb_albumartistid`` off the artist's albums. Uses a ``MatchQuery``
    (parameterized ``albumartist = ?``) rather than a string-interpolated query,
    so quotes/colons/metacharacters in the name cannot break it. Returns the
    first value that parses as a UUID (rejecting an empty field or junk like
    ``"520"``); ``None`` if the artist is absent or has no valid MBID.
    """
    for album in lib.albums(MatchQuery("albumartist", name)):
        raw = _coerce_str(album.mb_albumartistid)
        try:
            uuid.UUID(raw)
        except ValueError:
            continue
        return raw
    return None


def _to_search_track(item: Any) -> SearchTrack:
    return SearchTrack(
        id=int(item.id),
        title=_coerce_str(item.title),
        artist=_coerce_str(item.artist),
        album=_coerce_str(item.album),
        # Singletons (tracks beets has not grouped into an album) carry a falsy
        # album_id; surface them as None so the FE knows there is no album page.
        album_id=int(item.album_id) if item.album_id else None,
        duration_seconds=_coerce_duration(item.length),
    )


def search(lib: Library, *, query: str, limit: int) -> SearchResults:
    """Search the library across tracks, albums, and artists for a free-text term.

    ``query`` is handed to beets' query parser for tracks and albums (matching
    substrings across fields); artists are filtered with a case-insensitive
    substring match on the derived roster, since beets has no artist entity.

    A blank/whitespace query short-circuits to empty results with no beets call.
    Each beets query is wrapped so a malformed term (parse error) degrades to no
    results for that entity instead of surfacing as a 500.
    """
    if not query.strip():
        return SearchResults(
            artists=[], albums=[], tracks=[], artist_total=0, album_total=0, track_total=0
        )

    # Tracks: beets free-text query. A malformed query raises ParsingError
    # (an InvalidQueryError/ValueError subclass) from the parser; catch it so a
    # bad term yields no tracks rather than a 500. Keep the Results lazy: len()
    # returns the raw row count without constructing any Item (for the
    # SQL-evaluable free-text case), and islice materializes only `limit` — vs
    # list() which built a full Item model for every one of the (up to 75k) rows.
    try:
        track_results = lib.items(query)
        track_total = len(track_results)
        tracks = [_to_search_track(item) for item in islice(track_results, limit)]
    except ParsingError:
        track_total, tracks = 0, []

    # Albums: same free-text query, mapped through the shared _to_album.
    try:
        album_results = lib.albums(query)
        album_total = len(album_results)
        albums = [_to_album(album) for album in islice(album_results, limit)]
    except ParsingError:
        album_total, albums = 0, []

    # Artists: no beets query entity, so filter the derived roster by a
    # case-insensitive substring match on the name.
    needle = query.casefold()
    artist_matches = [a for a in list_artists(lib) if needle in a.name.casefold()]
    artist_total = len(artist_matches)
    artists = artist_matches[:limit]

    return SearchResults(
        artists=artists,
        albums=albums,
        tracks=tracks,
        artist_total=artist_total,
        album_total=album_total,
        track_total=track_total,
    )


def search_typed(
    lib: Library, *, query: str, entity: SearchEntity, limit: int, offset: int
) -> TypedSearchPage:
    """One entity of the free-text search, paged — backs /api/search?type=…

    Per-entity semantics are identical to ``search`` (beets' query parser for
    tracks/albums with the same ParsingError guard; case-insensitive substring
    over the derived roster for artists) and so is the ordering (beets' default
    query sort / the name-sorted roster), so the typed "View all" page lines up
    with the sectioned preview. ``total`` is the full match count BEFORE the
    ``offset:offset+limit`` slice. A blank/whitespace query short-circuits to
    an empty page with no beets call.
    """
    artists: list[Artist] = []
    albums: list[Album] = []
    tracks: list[SearchTrack] = []
    total = 0
    if query.strip():
        if entity == "tracks":
            try:
                results = lib.items(query)
                total = len(results)
                tracks = [
                    _to_search_track(item) for item in islice(results, offset, offset + limit)
                ]
            except ParsingError:
                total, tracks = 0, []
        elif entity == "albums":
            try:
                album_results = lib.albums(query)
                total = len(album_results)
                albums = [_to_album(a) for a in islice(album_results, offset, offset + limit)]
            except ParsingError:
                total, albums = 0, []
        else:
            needle = query.casefold()
            matches = [a for a in list_artists(lib) if needle in a.name.casefold()]
            total = len(matches)
            artists = matches[offset : offset + limit]
    return TypedSearchPage(
        type=entity,
        artists=artists,
        albums=albums,
        tracks=tracks,
        total=total,
        limit=limit,
        offset=offset,
    )


def list_albums(
    lib: Library, *, limit: int, offset: int, artist: str | None = None
) -> tuple[list[Album], int]:
    """Return a page of albums mapped to our Pydantic model plus the total count.

    Albums are sorted stably by album artist then album title (case-insensitive),
    so pagination is deterministic regardless of beets' default sort. When
    ``artist`` is set, albums are filtered to that exact ``albumartist`` BEFORE
    paginating, so ``total`` reflects the filtered count.

    Sorting/filtering/paging run over the shared in-memory ``BrowseRow`` cache
    (ONE full scan, invalidated on every ``emit_library_changed``); only the
    page's albums (<= ``limit``) are loaded from beets, not the whole library.
    """
    # Imported lazily: browse.py imports helpers from this module at import
    # time, so a top-level import back would form a cycle.
    from app.beets.browse import _rows

    rows = sorted(_rows(lib), key=lambda r: (r.artist_key, r.album_key, r.album_id))
    if artist is not None:
        rows = [r for r in rows if r.albumartist == artist]
    total = len(rows)
    albums: list[Album] = []
    for row in rows[offset : offset + limit]:
        album = lib.get_album(row.album_id)
        # Vanished between cache build and load — skip defensively; the cache
        # invalidates on every mutation, so this is belt-and-suspenders.
        if album is not None:
            # track_count + genre come from the cache row that this same scan
            # built — no per-row album.items() query (see _to_album_cached).
            albums.append(_to_album_cached(album, track_count=row.track_count, genre=row.genre_raw))
    return albums, total


def _abs_path(lib: Library, stored: bytes) -> str:
    """Resolve a beets-stored file path to absolute.

    The beets model API returns paths (``item.path``, ``artpath``) already
    resolved to absolute, so on the normal flow the input passes through
    unchanged. The underlying DB rows are stored relative to ``lib.directory``
    (observed with in-place imports); this guard is defense-in-depth for any
    code that reads a raw DB row directly, joining against ``lib.directory``
    only when the path is relative.
    """
    path = os.fsdecode(stored)
    if os.path.isabs(path):
        return path
    return os.path.join(os.fsdecode(lib.directory), path)


def _cover_from_artpath(lib: Library, album: BeetsAlbum) -> tuple[bytes, str] | None:
    raw_path = album.get("artpath")
    if not raw_path:
        return None
    path = _abs_path(lib, raw_path)
    if not os.path.isfile(path):
        return None
    mime = _EXTENSION_MIME.get(os.path.splitext(path)[1].lower())
    if mime is None:
        return None
    with open(path, "rb") as fh:
        return fh.read(), mime


def _cover_from_embedded(lib: Library, album: BeetsAlbum) -> tuple[bytes, str] | None:
    items = list(album.items())
    if not items:
        return None
    track_path = _abs_path(lib, items[0].path)
    if not os.path.isfile(track_path):
        return None
    images = MediaFile(track_path).images
    if not images:
        return None
    image = images[0]
    # Intentional fail-safe: if the embedded image declares no mime, octet-stream
    # makes the browser <img> refuse it, cleanly triggering the FE onError
    # placeholder (serve-or-degrade) rather than rendering garbage.
    mime = _coerce_optional_str(image.mime_type) or "application/octet-stream"
    return bytes(image.data), mime


def get_album_cover(lib: Library, album_id: int) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, mime_type)`` for an album's cover art, or ``None``.

    Resolution order: the album's ``artpath`` file if present, otherwise the
    embedded art on the album's first track. ``None`` when the album is missing
    or no art can be found.
    """
    album = lib.get_album(album_id)
    if album is None:
        return None
    return _cover_from_artpath(lib, album) or _cover_from_embedded(lib, album)


def cover_validator(lib: Library, album_id: int) -> str | None:
    """A CHEAP ETag for an album's cover — derived by ``stat``-ing the cover SOURCE
    file, with NO image read and NO ``MediaFile`` parse.

    Mirrors :func:`get_album_cover`'s source resolution (``artpath`` with a known
    image extension, else the first track's audio file) so the tag identifies the
    same bytes it would serve. Lets the cover endpoint answer a conditional GET
    (``304``) off metadata alone instead of re-reading ~50-200 MB / re-parsing
    audio over a NAS on every album-grid repaint. Self-correcting: a re-fetched
    cover writes a new file, and editing embedded art bumps the audio file's mtime,
    so a stale tag can never yield a false ``304``. ``None`` when the album is
    missing or has no cover source (the endpoint then falls through to the full
    read, which returns the image or a 404).

    Each tag is a ``stat``-only ETag from :func:`app.etag.stat_etag`; it returns
    ``None`` if the file vanished between this function's ``isfile`` check and the
    ``stat`` (a race), and the endpoint then falls through to the full read."""
    album = lib.get_album(album_id)
    if album is None:
        return None
    raw_path = album.get("artpath")
    if raw_path:
        path = _abs_path(lib, raw_path)
        if os.path.isfile(path) and _EXTENSION_MIME.get(os.path.splitext(path)[1].lower()):
            return stat_etag(path)
    items = list(album.items())
    if items:
        track = _abs_path(lib, items[0].path)
        if os.path.isfile(track):
            return stat_etag(track)
    return None
