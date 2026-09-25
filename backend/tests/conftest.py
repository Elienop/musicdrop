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

The third load-bearing thing here is not a fixture at all — see
``_install_session_cookie_on_every_test_client`` below, which is what keeps the
session gate (``app/auth/gate.py``) from 401-ing the whole suite.

The fourth is ``password_hash_file`` (autouse), which pins the stored-password
file at a per-test tmp path so no test can read or write the one under the
developer's real beets library — the same class of hole the ``BEETSDIR`` floor
below closes, for a file two production routes WRITE.

The fifth is not in this file: ``backend/conftest.py`` (the ROOT conftest,
imported before this one) pins ``BEETSDIR`` at a throwaway sandbox for the whole
process, so nothing here can resolve beets' config against the developer's
personal ``~/.config/beets``. Read its docstring before changing anything that
touches ``BEETSDIR``.
"""

import base64
import contextlib
import fcntl
import hashlib
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import pytest
from fastapi.testclient import TestClient

from app.auth.session import SESSION_COOKIE_NAME, mint_session_token
from app.auth.source import effective_password, password_hash_path
from app.beets.library import LibraryHandle, _music_dir, close_library
from app.beets.protected import ProtectedTrees, protected_trees
from app.beets.setup import setup_beets
from app.config import settings
from app.main import app as _real_app

if TYPE_CHECKING:
    from beets.library import Library

#: A fixed 32-byte signing secret for the whole suite. Fixed rather than random
#: so a token minted in a subprocess test (test_origin_guard, test_host_guard)
#: or written into a hand-built ASGI scope (test_security_headers) verifies
#: against the same key the in-process app is using.
TEST_SESSION_SECRET = b"0123456789abcdef0123456789abcdef"


def session_cookie_value() -> str:
    """A freshly minted, valid session token for :data:`TEST_SESSION_SECRET`.

    Minted through the PRODUCTION function, never a hand-rolled equivalent:
    that is what makes the suite exercise the gate instead of bypassing it. If
    the token format or the signature changes and the gate stops accepting what
    this mints, ~2,500 tests go red — which is the intended alarm.

    The EFFECTIVE hash is read HERE, at mint time, because the signing key is
    derived from it (``app/auth/session.py::_signing_key``) — through
    ``effective_password()``, the same resolver the gate asks, so a test that
    stores a password in the (pinned) hash file gets a cookie the gate accepts.
    A test whose fixture patches the hash and THEN builds a client gets a cookie
    bound to the patched value; one that patches it after construction has
    invalidated its own cookie, and must re-mint or clear the jar. That is the
    same rule real clients live under — rotating the password signs everyone out
    — so the suite is not being given a special case.
    """
    return mint_session_token(TEST_SESSION_SECRET, effective_password()[0])


def low_cost_stored_hash(password: str, *, n: int = 1024, r: int = 8, p: int = 1) -> str:
    """A ``scrypt$...`` hash of ``password``, written out by hand and cheap.

    Two reasons, and the first is not speed:

    * it is a LITERAL reconstruction of the wire format, so a change to the
      separator, the field order or the algorithm tag breaks the tests that use
      it instead of moving silently with the production code;
    * n=1024 makes a verify ~1 ms instead of ~160 ms, which is the difference
      between a fast file and twenty tests spending three seconds in a KDF.

    ``tests/test_auth_credentials.py`` pins what ``hash_password()`` itself
    writes, so the real OWASP parameters are not left untested by the shortcut.
    Shared from here because three files need it: the login/status tests, the
    first-run setup tests and the change-password tests.
    """
    salt = b"sixteen-byte-slt"
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=128 * r * (n + p + 2) + 1024 * 1024,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(n),
            str(r),
            str(p),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


#: How long a request may take before :func:`answer_before_a_fifo_blocks` calls
#: it blocked. The routes it guards answer in under 0.1 s.
FIFO_DEADLINE_SECONDS = 5.0

T = TypeVar("T")


def answer_before_a_fifo_blocks(call: Callable[[], T], fifo: Path) -> T:
    """``call()``'s result, or a failure (not a hang) if it is still running at the deadline.

    A read blocked opening ``fifo`` is then released by a writer that opens and
    closes it, so a blocked save gives ``_SAVE_LOCK`` back to the tests after it.
    """
    answered: list[T] = []
    raised: list[Exception] = []

    def run() -> None:
        try:
            answered.append(call())
        except Exception as exc:
            raised.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(FIFO_DEADLINE_SECONDS)
    blocked = worker.is_alive()
    release_by = time.monotonic() + 10
    while worker.is_alive() and time.monotonic() < release_by:
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        time.sleep(0.05)
    assert not blocked, f"still blocked on {fifo} after {FIFO_DEADLINE_SECONDS} s"
    if raised:
        raise raised[0]
    return answered[0]


def _install_session_cookie_on_every_test_client() -> None:
    """Make every ``TestClient`` in the suite arrive authenticated.

    The session gate is secure by DEFAULT, so without this the ~140 client
    construction sites across the suite would each need a cookie. Two details
    make this work where the obvious alternatives do not:

    * it patches the CLASS OBJECT's ``__init__`` rather than a module
      attribute. ``fastapi.testclient.TestClient`` IS
      ``starlette.testclient.TestClient`` (a re-export, verified: the two names
      are the same object), and the suite imports it both ways — patching one
      module's name would silently miss every file that used the other;
    * it runs at MODULE level, not in a fixture. ``tests/test_health.py`` builds
      its client at import time, which is before any fixture has run.

    Cookies are set on the instance's jar AFTER the real ``__init__``, so a
    caller's own ``cookies=``/``headers=`` arguments survive — EXCEPT a
    ``musicdrop_session`` passed to the constructor, which this overwrites.
    A test that needs a specific session cookie (an expired one, a tampered
    one, none at all) must therefore set it on the jar AFTER construction, or
    call ``client.cookies.clear()``; every such test in this suite does. See
    ``test_session_gate.py::test_clearing_the_seam_cookie_makes_the_gate_bite``,
    the positive control proving this stamping is load-bearing.
    """
    _real_app.state.session_secret = TEST_SESSION_SECRET
    original_init = TestClient.__init__

    def __init__(self: TestClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.cookies.set(SESSION_COOKIE_NAME, session_cookie_value())

    # Patching the class in place is the point: one assignment covers both
    # import paths and all ~140 construction sites.
    TestClient.__init__ = __init__  # type: ignore[method-assign]  # see the docstring


_install_session_cookie_on_every_test_client()


@pytest.fixture(autouse=True)
def password_hash_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AUTOUSE FLOOR: no test can read or write a REAL ``password-hash`` file.

    ``app/auth/source.py`` derives the file's path from ``settings.beets_dir``,
    which ``backend/.env`` aims at the developer's REAL beets library on a dev
    box. Two production routes WRITE that file (``POST /api/auth/setup`` and
    ``POST /api/auth/password``) and three code paths read it on every request,
    so a test that forgot to isolate itself would plant a credential in the real
    library — or, worse, overwrite the owner's own.

    Pinned at ``live_password_hash_path``, the single seam every production read
    and write goes through, rather than at ``settings.beets_dir``: that keeps the
    floor independent of the ~50 tests that repoint ``beets_dir`` for their own
    reasons, so pointing it at a real library still cannot reach a real hash
    file. ``tests/test_password_file_isolation.py`` is the decoy pin for exactly
    that case, in the shape ``test_beetsdir_isolation.py`` uses.

    Returns the pinned path, so a test that wants to seed or inspect the stored
    hash asks for this fixture by name instead of re-deriving it.
    """
    pinned = password_hash_path(str(tmp_path / "auth-source"))
    monkeypatch.setattr("app.auth.source.live_password_hash_path", lambda: pinned)
    return pinned


@pytest.fixture(autouse=True)
def _resolve_hosts_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public IP so the SSRF guard is inert by default.

    ``app.artwork.download.assert_public_url`` (added to the artist-image fetch +
    download paths) calls ``socket.getaddrinfo`` to reject private/loopback/
    metadata targets. The artwork tests mock the HTTP layer with respx but use
    synthetic hosts (``img``, ``cdn.test``, ...) that don't resolve offline, so
    without this the guard would raise on them. Tests that assert the guard
    actually *blocks* (test_artwork_ssrf) re-stub ``getaddrinfo`` in the test
    body — that override runs after this fixture and wins.
    """

    def fake(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@contextlib.contextmanager
def write_leased(path: Path) -> Iterator[None]:
    """Hold a write lease on ``path``, or skip: an open of it blocks, or answers EAGAIN.

    The fault of choice for "the store could not answer", because it denies ROOT
    too — a maintainer running this suite inside the shipped image is root
    (``Dockerfile`` declares no ``USER``), where a ``chmod 000`` file denies
    nothing and a chmod-staged test would report green having injected no fault.
    Measured 2026-09-14: the leased key answers an ``O_NONBLOCK`` open with
    EAGAIN in 0.0000 s, while a plain ``open`` of it waits for
    ``/proc/sys/fs/lease-break-time``.

    SIGIO is ignored for the duration: the kernel signals the lease HOLDER to
    release, a test is both holder and reader, and SIGIO's default action is to
    terminate. The release is suppressed because ``F_UNLCK`` with no lease held
    raises EAGAIN, which turned the skip into an error (code seat W3).
    """
    holder = os.open(path, os.O_RDONLY)
    previous = signal.signal(signal.SIGIO, signal.SIG_IGN)
    try:
        try:
            fcntl.fcntl(holder, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        except OSError as exc:  # no CAP_LEASE, or a filesystem without them
            pytest.skip(f"a write lease could not be taken here: {exc}")
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.fcntl(holder, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        os.close(holder)
        signal.signal(signal.SIGIO, previous)


def origins_for(trash_dir: Path) -> Path:
    """The Trash origin store that belongs beside ``trash_dir``.

    Production resolves the two independently from settings
    (``app.beets.trash.resolve_trash_dir`` / ``resolve_trash_origins_dir``), both
    defaulting under ``<beets_dir>``. Tests build the pair through this helper so
    the shape is written down once: a SIBLING of the Trash dir, never a child —
    inside it, a record file would land in the entry namespace ``iterdir`` walks
    and show up as a trashed album of its own.
    """
    return trash_dir.parent / "trash-origins"


def protected_for(
    lib: "Library | None" = None,
    *,
    trash_dir: Path | None = None,
    origins_dir: Path | None = None,
) -> ProtectedTrees:
    """The identity set a destructive request builds, for a test's own fixtures.

    The real :func:`app.beets.protected.protected_trees`, so a test calling a
    mover or the remover exercises the guard rather than an empty stand-in. A
    piece the caller has no handle on resolves under a path that is not there
    and therefore has no identity — the same way production drops a store that
    has not been created yet.
    """
    absent = Path("/nonexistent-musicdrop-absent")
    library_path = Path(os.fsdecode(lib.path)) if lib is not None else absent / "library.db"
    return protected_trees(
        settings=settings,
        music_dir=Path(_music_dir(lib)) if lib is not None else absent / "music",
        beets_dir=library_path.parent,
        trash_dir=trash_dir if trash_dir is not None else absent / "trash",
        origins_dir=origins_dir if origins_dir is not None else absent / "trash-origins",
        library_path=library_path,
    )


def beets_dir_for(tmp_path: Path) -> Path:
    """A beets data dir that does not nest with the music root under ``tmp_path``.

    ``app.beets.store_layout`` refuses ``B == M``, ``M`` inside ``B`` and ``B``
    inside ``M``, and every library fixture here puts the music root at
    ``<tmp_path>/music`` — so handing ``tmp_path`` ITSELF to
    :func:`make_test_handle` builds a handle the delete, duplicates, trash and
    reorganize paths all refuse with a 503. A sibling is the shape the shipped
    image has (``/music`` and ``/data``).

    Created eagerly because a caller that leaves ``trash_dir`` empty gets
    ``<B>/trash``, and sqlite needs the directory to exist before it will open a
    database inside it.
    """
    beets = tmp_path / "beets"
    beets.mkdir(parents=True, exist_ok=True)
    return beets


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
    * ``file_operation`` is ``in_place``, which beets' defaults never load.
    """
    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir.resolve(),
        config_path=Path("__placeholder__"),
        loaded_at=datetime(1970, 1, 1, tzinfo=UTC),
        file_mtime_at_load=0.0,
        file_operation="in_place",
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


def library_with_no_rows(tmp_path: Path) -> "Library":
    """An empty library for a test that must pass one but has no rows to protect.

    ``empty_one``/``empty_all`` take a ``Library`` because the cross-check that
    keeps an Empty from destroying an album's only copy fails OPEN without one —
    a data-safety guard whose off switch is forgetting an argument. Tests about
    the SWEEP rather than about that guard pass this: a real library, one real
    query, zero rows.
    """
    return build_library(str(beets_dir_for(tmp_path) / "library.db"), str(tmp_path / "music"))


def trash_the_folder_as_released(
    lib: "Library", album: Any, *, trash_dir: Path, origins_dir: Path
) -> Path:
    """Put ``album`` in Trash the way RELEASED versions' whole-folder mover did.

    That mover is gone, but records it wrote are on users' disks and restore by
    MOVE-BACK, so the shape has to be buildable without it. Captured from a real
    run before the deletion: the album's folder moved WHOLE to
    ``<trash>/<folder name>`` (files, art and sidecars alike, tracked or not),
    the origin record ``<origins>/<entry name>.json`` holding
    ``{"moved": "folder", "origin": <the folder>, ...}``, and the album's rows
    dropped afterwards.

    Hand-written rather than routed through ``trash_folder``: this is a FIXTURE
    for a released on-disk shape, and it must not move when a live mover does.
    The ``(n)`` suffix is the allocator's, kept because a test that deletes the
    same album twice depends on it; ``_fit_name``'s NAME_MAX shortening is NOT
    reproduced — a test about that boundary belongs on a live mover.
    """
    import shutil

    from app.beets.trash_origins import write_trash_origin

    items = list(album.items())
    folders = {os.path.dirname(os.fsdecode(it.path)) for it in items}
    assert len(folders) == 1, f"the released mover moved ONE folder, got {sorted(folders)}"
    source = folders.pop()
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / os.path.basename(source)
    counter = 1
    while dest.exists():
        dest = trash_dir / f"{os.path.basename(source)} ({counter})"
        counter += 1
    shutil.move(source, str(dest))
    write_trash_origin(origins_dir, dest.name, origin=source, moved="folder")
    album.remove(delete=False)
    return dest


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
def close_bank_connections() -> Iterator[None]:
    """Close the bank store's open databases around every test.

    Connections are kept per database file; each test's bank sits in its own
    tmp dir, so without this every test would leave one open file behind.
    """
    from app.bank.store import close_connections

    close_connections()
    yield
    close_connections()


@pytest.fixture
def inbox_bank_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the bank the inbox routes ask at this test's own tmp dir.

    The three inbox routes read the bank through ``get_bank_dir()``, and the
    suite's ``beets_dir`` is the cwd-relative default: without this a test
    opens ``backend/data/beets/bank/bank.db``, the dev checkout's own bank.
    """
    bank_dir = tmp_path / "bank"
    monkeypatch.setattr(settings, "bank_dir", str(bank_dir))
    return bank_dir


@pytest.fixture(autouse=True)
def reset_artwork_log_throttle() -> Iterator[None]:
    """Forget throttled artwork conditions around every test.

    The throttle keys on the CONDITION and is module-global, so without this one
    test's warning silently suppresses the next test's — the suite would pass or
    fail on ordering, which is exactly the kind of failure a log assertion is
    supposed to catch rather than cause.
    """
    from app.artwork.degrade import reset_log_throttle

    reset_log_throttle()
    yield
    reset_log_throttle()


@pytest.fixture(autouse=True)
def reset_browse_cache() -> Iterator[None]:
    """Drop the browse cache's module-level rows around every test.

    Keyed by resolved DB path; per-test tmp dirs never collide, but the
    scan-count pin relies on a clean slate."""
    from app.beets.browse import invalidate_browse_cache as _invalidate

    _invalidate()
    yield
    _invalidate()


@pytest.fixture(autouse=True)
def _clear_beets_globals() -> Iterator[None]:
    """Reset beets' global confuse + plugin singletons between every test.

    Why this is autouse for ALL tests (not just adapter tests): beets caches
    the resolved ``beets.config`` and loaded plugins in module globals. Any
    test that calls ``setup_beets()`` mutates those, and a later unrelated
    test that happens to read ``beets.config`` (e.g. via the import session)
    would see the leaked state. Centralising here means individual test files
    no longer have to remember to repeat this fixture.

    Implementation note: the reset drops every confuse source, ``config.set()``
    override and redaction and re-arms the lazy read (confuse's
    ``LazyConfig.clear()`` alone leaves ``_materialized`` set, core.py:749).
    ``setup_beets()`` re-reads ``config.yaml`` either way; the reset is for the
    tests that read ``beets.config`` without calling it.

    The env loop below SAVES AND RESTORES; it does not redirect. That is safe
    only because ``backend/conftest.py`` sets ``BEETSDIR`` at import, before any
    fixture runs — so the saved value is always the suite sandbox and the
    ``v is None`` branch (which would ``pop`` the variable and re-expose the
    developer's platform beets dir to the next test) can never fire for it.
    Do not move the floor into a fixture without re-checking that.
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

    BEETSDIR is ``<tmp_path>/beets``, a SIBLING of the music dir, rather than
    ``tmp_path`` itself: ``app.beets.store_layout`` refuses a beets data
    directory that contains the music library, so the old shape was a layout the
    app declines to boot with — the Save/Validate/Apply gates flagged every
    document written against it.
    """
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    cfg = beets_dir / "config.yaml"
    cfg.write_text(
        f"directory: {music_dir}\n"
        "library: library.db\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n  copy: yes\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets_dir))
    handle = setup_beets(str(beets_dir))
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
    album["genres"] = "Alternative Rock"
    album["year"] = 2007
    album.store()
    return lib


@pytest.fixture
def rename_lib(tmp_path: "Path") -> "Library":
    """A hermetic library for artist-rename tests (REAL tag-writable FLACs).

        Fayrouz / Best Of : 2 tracks   (the rename subject, album 1)
        Fayrouz / Live    : 1 track    (the rename subject, album 2)
        Fairuz  / Legend  : 1 track    (an existing artist to merge INTO)

    Files are seeded at their CURRENT path-format destination
    ($albumartist/$album/$track $title), so renaming the albumartist is what
    makes them move.
    """
    import os
    import shutil

    from beets.library import Item

    sample = Path(__file__).parent / "fixtures" / "silent.flac"
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add_album(artist: str, album: str, titles: list[str]) -> None:
        base = music / artist / album
        base.mkdir(parents=True, exist_ok=True)
        items = []
        for i, title in enumerate(titles, start=1):
            f = base / f"{i:02d} {title}.flac"
            shutil.copyfile(sample, f)
            it = Item(
                album=album,
                albumartist=artist,
                artist=artist,
                title=title,
                track=i,
                disc=1,
            )
            it.path = os.fsencode(str(f))
            items.append(it)
        lib.add_album(items)

    add_album("Fayrouz", "Best Of", ["Habaytak", "Nassam"])
    add_album("Fayrouz", "Live", ["Kifak Inta"])
    add_album("Fairuz", "Legend", ["Zahrat"])
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
