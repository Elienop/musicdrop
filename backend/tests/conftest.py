"""Shared pytest fixtures for the backend test suite.

The two beets fixtures here are load-bearing for every test that touches the
adapter:

* ``_clear_beets_globals`` (autouse) resets the global ``beets.config`` confuse
  singleton and the global plugin registry between tests. beets exposes these
  as module globals, so without this any test that calls ``setup_beets()`` (or
  any code path that calls ``plugins.load_plugins()``) leaks state into the
  next test — and the leak is silent because confuse's ``LazyConfig.clear()``
  doesn't reset ``_materialized``, so a subsequent force-resolve short-circuits
  on stale state and the user file is silently ignored.

* ``beets_library`` writes a minimal ``config.yaml`` into a tmp BEETSDIR and
  calls ``setup_beets()`` against it, yielding a fully-formed
  :class:`LibraryHandle`. This is the canonical way to get a hermetic beets
  library in tests now that the old ``MUSICDROP_BEETS_LIBRARY_*`` settings
  are gone.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle, close_library
from app.beets.setup import setup_beets

if TYPE_CHECKING:
    from beets.library import Library


def make_test_handle(lib: "Library", beets_dir: Path) -> LibraryHandle:
    """Snapshot fields are SENTINELS — use a real ``setup_beets()`` handle to assert on them.

    For endpoints that only touch ``handle.lib``: this wraps a raw ``Library``
    in a ``LibraryHandle`` so dependency-override fixtures (the per-file
    ``temp_library`` fixtures in test_albums / test_artists / test_search that
    build their own hermetic ``Library`` without going through ``setup_beets``)
    still satisfy the ``LibraryHandle`` shape that the API endpoints now
    expect.

    The snapshot fields use **sentinel values** rather than realistic-looking
    placeholders so a future Task 5/6 ``BeetsConfigSnapshot`` test can't
    silently assert against them and pass for the wrong reason:

    * ``config_path`` points at the literal ``Path("__placeholder__")`` — no
      file backs it, so any ``.stat()`` / ``.read_text()`` against it raises.
    * ``loaded_at`` is the Unix epoch.
    * ``file_mtime_at_load`` is ``0.0``.
    """
    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir.resolve(),
        config_path=Path("__placeholder__"),
        loaded_at=datetime(1970, 1, 1, tzinfo=UTC),
        file_mtime_at_load=0.0,
    )


def build_library(
    path: str,
    directory: str,
    *,
    path_format: str = "$albumartist/$album/$track $title",
) -> "Library":
    """Build a hermetic beets ``Library`` with a path format set through config.

    beets 2.12 removed the ``path_formats``/``replacements`` constructor kwargs;
    ``Library.path_formats`` is now a ``cached_property`` over ``config['paths']``.
    So the format is set in config BEFORE construction (before that property is
    first read), mirroring how the production adapter relies on the loaded config.
    Replacements fall through to beets' defaults (no test customises them). The
    autouse ``_clear_beets_globals`` fixture resets config between tests, so this
    write never leaks. Tests that need a different layout pass ``path_format``.
    """
    from beets import config
    from beets.library import Library

    config["paths"]["default"] = path_format
    return Library(path, directory=directory)


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-marked async tests on asyncio only (no trio dependency)."""
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_import_registry() -> Iterator[None]:
    """Reset the global single-slot import registry around every test.

    The registry is module-global mutable state (one active job); without this a
    job started in one test would block ``start`` in the next with a 409.
    """
    from app.import_jobs.registry import reset_registry

    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def reset_lyrics_backfill_registry() -> Iterator[None]:
    """Reset the global single-slot lyrics backfill registry around every test.

    Mirrors ``reset_import_registry``: the backfill registry is module-global
    mutable state (one active job). Without this, a test that leaves a backfill
    ``running`` would leak a 409 into the next test's per-album fetch / start.
    """
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill()
    yield
    reset_lyrics_backfill()


@pytest.fixture(autouse=True)
def reset_artist_art_backfill_registry() -> Iterator[None]:
    from app.artist_art_jobs.registry import reset_artist_art_backfill

    reset_artist_art_backfill()
    yield
    reset_artist_art_backfill()


@pytest.fixture(autouse=True)
def reset_reorganize_backfill_registry() -> Iterator[None]:
    from app.reorganize_jobs.registry import reset_reorganize_backfill

    reset_reorganize_backfill()
    yield
    reset_reorganize_backfill()


@pytest.fixture(autouse=True)
def reset_disk_sync_registry() -> Iterator[None]:
    from app.disk_sync_jobs.registry import reset_disk_sync

    reset_disk_sync()
    yield
    reset_disk_sync()


@pytest.fixture(autouse=True)
def reset_bank_index() -> Iterator[None]:
    """Drop the bank store's module-level summary index around every test.

    The index is keyed by resolved bank-dir path; per-test tmp dirs never
    collide, but the glob-count perf pin and the external-file test rely on a
    clean slate, so reset both sides.
    """
    from app.bank.store import reset_bank_index as _reset

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _clear_beets_globals() -> Iterator[None]:
    """Reset beets' global confuse + plugin singletons between every test.

    Why this is autouse for ALL tests (not just adapter tests): beets caches
    the resolved ``beets.config`` and loaded plugins in module globals. Any
    test that calls ``setup_beets()`` mutates those, and a later unrelated
    test that happens to read ``beets.config`` (e.g. via the import session)
    would see the leaked state. Centralising here means individual test files
    no longer have to remember to repeat this fixture.

    Implementation note: confuse's ``LazyConfig.clear()`` (core.py:749) does
    NOT reset ``_materialized``; without flipping it back to False the next
    ``setup_beets()`` force-resolve short-circuits at ``LazyConfig.resolve()``'s
    guard (core.py:728) and the user's ``config.yaml`` is silently ignored.
    """
    from app.beets.setup import reset_beets_globals

    saved_env = {
        k: os.environ.get(k)
        for k in (
            "BEETSDIR",
            "MUSICDROP_BEETS_DIR",
        )
    }
    yield
    # Delegate to the production helper (handle=None: the autouse owns no
    # library). Single source of truth — if a future beets version needs an
    # 8th clear, only ``reset_beets_globals`` changes and this fixture
    # inherits the fix. The handle-closing branch is exercised by
    # ``test_reset_closes_the_library``; the no-handle branch is exercised by
    # ``test_reset_accepts_none_handle``.
    reset_beets_globals()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def beets_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LibraryHandle]:
    """Hermetic beets library opened under a tmp BEETSDIR.

    Writes a minimal ``config.yaml`` (musicbrainz plugin only — keep startup
    cheap), points ``settings.beets_dir`` at the tmp dir, then runs
    ``setup_beets()`` against it. Tests that want a different ``config.yaml``
    should write their own BEFORE calling ``setup_beets`` directly rather than
    relying on this fixture.
    """
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"directory: {music_dir}\n"
        "library: library.db\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n  copy: yes\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path))
    handle = setup_beets(str(tmp_path))
    try:
        yield handle
    finally:
        close_library(handle.lib)


@pytest.fixture
def beets_library_config_path(beets_library: LibraryHandle) -> Path:
    """Path to the ``config.yaml`` backing the active :class:`LibraryHandle`.

    Test_config_api uses this to ``os.utime`` the file between two GETs and
    assert the endpoint surfaces the new mtime / sets ``apply_pending``.
    Resolved off the handle (not ``tmp_path``) so the two stay in lockstep
    even if the fixture's layout changes.
    """
    return beets_library.config_path


@pytest.fixture
def empty_lib(tmp_path: Path) -> "Library":
    """A hermetic, empty beets Library on a temp path."""
    from beets.library import Library

    return Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))


@pytest.fixture
def duplicates_lib(tmp_path: Path) -> "Library":
    """A hermetic library seeded with known duplicate + unique albums.

    Real files on disk + explicit path_formats so resolve's Album.move works.
      - mb-1 x2 : "Radiohead / In Rainbows", 10 + 9 tracks (a STRICT dup; keeper=10)
      - mb-2 x1 : "Daft Punk / Discovery", 14 tracks (unique - not a dup)
      - untagged x2 : "Boards of Canada / Music Has the Right", no mb_albumid
                      (a FUZZY-only dup - caught by normalized artist+title)
    """
    import os

    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add_album(*, mb: str, artist: str, album: str, n: int, folder: str) -> None:
        items = []
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        for i in range(1, n + 1):
            f = base / f"{i:02d} Track {i}.mp3"
            f.write_bytes(b"\x00")  # placeholder bytes; tests never read audio
            it = Item(album=album, albumartist=artist, artist=artist, title=f"Track {i}", track=i)
            it.path = os.fsencode(str(f))
            items.append(it)
        al = lib.add_album(items)  # adds the items too — do NOT lib.add() first
        if mb:
            al["mb_albumid"] = mb  # detection reads album-level mb_albumid
        al.store()

    add_album(
        mb="mb-1", artist="Radiohead", album="In Rainbows", n=10, folder="Radiohead/In Rainbows"
    )
    add_album(
        mb="mb-1",
        artist="Radiohead",
        album="In Rainbows",
        n=9,
        folder="Radiohead/In Rainbows (1)",
    )
    add_album(mb="mb-2", artist="Daft Punk", album="Discovery", n=14, folder="Daft Punk/Discovery")
    add_album(
        mb="", artist="Boards of Canada", album="Music Has the Right", n=10, folder="BoC/MHTRTC"
    )
    add_album(
        mb="",
        artist="Boards of Canada",
        album="Music Has the Right",
        n=10,
        folder="BoC/MHTRTC (1)",
    )
    return lib


@pytest.fixture
def edit_lib(tmp_path: "Path") -> "Library":
    """A hermetic library with one album of REAL FLAC files (tag-writable).

    Unlike ``duplicates_lib`` (which writes ``b"\\x00"`` stubs because it only
    moves files), the edit feature calls ``item.try_write()`` which tags the
    audio via mutagen — so the seed files must be valid audio. Each track is a
    copy of ``tests/fixtures/silent.flac``. Explicit ``path_formats`` so
    ``item.destination()`` / ``item.move()`` can resolve a destination.

        Radiohead / In Rainbows : 3 tracks (mb-edit), album id is the first added
    """
    import os
    import shutil

    from beets.library import Item

    sample = Path(__file__).parent / "fixtures" / "silent.flac"
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    base = music / "Radiohead" / "In Rainbows"
    base.mkdir(parents=True, exist_ok=True)
    items = []
    titles = ["15 Step", "Bodysnatchers", "Nude"]
    for i, title in enumerate(titles, start=1):
        f = base / f"{i:02d} {title}.flac"
        shutil.copyfile(sample, f)
        it = Item(
            album="In Rainbows",
            albumartist="Radiohead",
            artist="Radiohead",
            title=title,
            track=i,
            disc=1,
        )
        it.path = os.fsencode(str(f))
        items.append(it)
    album = lib.add_album(items)
    album["mb_albumid"] = "mb-edit"
    album["genre"] = "Alternative Rock"
    album["year"] = 2007
    album.store()
    return lib


@pytest.fixture
def reorganize_lib(tmp_path: "Path") -> "Library":
    """A hermetic library for reorganize tests.

    path_formats = "$albumartist/$album/$track $title". Seeds:
      - "Radiohead / In Rainbows" 3 trk, filed in WRONG dir "junk/ir" -> WILL MOVE
      - "Daft Punk / Discovery" 2 trk, filed CORRECTLY -> already in place
      - "Boards of Canada / Geogaddi" 2 trk, right dir but WRONG filenames -> rename-in-place
      - one SINGLETON "Aphex Twin / Xtal" in WRONG dir -> WILL MOVE
    Real files on disk (b"\\x00" stubs; tests never read audio) so Album.move works.
    """
    import os

    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def album(*, artist: str, name: str, titles: list[str], folder: str, names: list[str]) -> None:
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        items = []
        for i, (title, fname) in enumerate(zip(titles, names, strict=True), start=1):
            f = base / fname
            f.write_bytes(b"\x00")
            it = Item(album=name, albumartist=artist, artist=artist, title=title, track=i, disc=1)
            it.path = os.fsencode(str(f))
            items.append(it)
        lib.add_album(items).store()

    # mis-filed -> destination "Radiohead/In Rainbows/0N <title>.mp3" differs
    album(
        artist="Radiohead",
        name="In Rainbows",
        titles=["15 Step", "Bodysnatchers", "Nude"],
        folder="junk/ir",
        names=["a.mp3", "b.mp3", "c.mp3"],
    )
    # already correct
    album(
        artist="Daft Punk",
        name="Discovery",
        titles=["One More Time", "Aerodynamic"],
        folder="Daft Punk/Discovery",
        names=["01 One More Time.mp3", "02 Aerodynamic.mp3"],
    )
    # right dir, wrong filenames -> rename-in-place
    album(
        artist="Boards of Canada",
        name="Geogaddi",
        titles=["Ready Lets Go", "Music Is Math"],
        folder="Boards of Canada/Geogaddi",
        names=["x.mp3", "y.mp3"],
    )

    # singleton (album_id is None): add via lib.add(), not add_album
    s_dir = music / "loose"
    s_dir.mkdir(parents=True, exist_ok=True)
    sf = s_dir / "z.mp3"
    sf.write_bytes(b"\x00")
    si = Item(artist="Aphex Twin", albumartist="Aphex Twin", title="Xtal", track=1)
    si.path = os.fsencode(str(sf))
    lib.add(si)
    return lib


@pytest.fixture
def client(beets_library: LibraryHandle) -> Iterator[TestClient]:
    """TestClient with ``app.state.beets_library`` wired to a real handle.

    Used by endpoints that read ``request.app.state.beets_library`` directly
    (no FastAPI dependency to override) — currently the Config view at
    ``GET /api/config``. The endpoint's snapshot builder calls
    ``handle.config_path.stat()``, so the placeholder handle from
    ``make_test_handle`` would explode; we point at the real ``beets_library``
    fixture instead.

    Construction order matters: TestClient is built WITHOUT a ``with`` block
    so the lifespan handler (which would also try to set ``app.state.beets_library``
    + open an httpx client) does not run.
    """
    from app.main import app

    prior = getattr(app.state, "beets_library", None)
    app.state.beets_library = beets_library
    try:
        yield TestClient(app)
    finally:
        if prior is None:
            del app.state.beets_library
        else:
            app.state.beets_library = prior
