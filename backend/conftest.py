"""Root conftest — keeps the whole suite out of the developer's PERSONAL beets dir.

**The hole this closes.** beets stores the importer's scratch state at
``config["statefile"].as_filename()`` (``beets/importer/state.py:57``).
``as_filename()`` joins a relative default onto ``config_dir()`` (confuse
``templates.py``), and ``config_dir()`` returns ``$BEETSDIR`` when that is set
and otherwise a *platform* directory derived from ``XDG_CONFIG_HOME``/``HOME``
— ``~/.config/beets`` on Linux (confuse ``core.py``). ``config_dir()`` also
calls ``os.makedirs`` on whatever it returns, so even a read-only resolve
*creates* that directory on a machine that has none.

**How bad it actually is depends on the developer's own machine**, which is why
this was under-measured for months. Running the pre-fix suite under a throwaway
``HOME``:

* with **no** personal ``~/.config/beets/config.yaml`` — 1 file written
  (``state.pickle``), and the suite is green. This is the case the original bug
  report measured, and it reads as a bounded annoyance;
* with a personal ``config.yaml`` present — **14 files written**, including a
  real ``library.db`` and **eleven** ``library.db-before-*.bak``
  schema-migration backups, plus a genuinely red suite
  (``tests/test_albums.py::test_lifespan_opens_library_from_settings`` fails).

The second case is the normal one: MusicDrop is a beets web UI, so its
contributors are beets users. Those ``.bak`` files are beets running pending
migrations against a real personal library — the same accident as the
2026-08-15 incident where an unguarded ``beet --version`` migrated that library.

**Why a process-wide floor rather than fixing the offending test.** "Fix the one
offender" is only true on a machine with no beets config; otherwise at least two
tests reach the platform dir and *which* ones depends on machine state. This
class has already been patched per-test twice (the two
``test_artist_image_endpoint.py`` lifespan tests, via ``_pin_settings_at``) and
grew back. The floor is O(1) and closes the class regardless of machine state.
Per-test isolation is still the right default everywhere else — the suite has
~2,600 ``tmp_path`` uses and they are not going anywhere.

**Why this file is at the backend root rather than in ``tests/``.** pytest
imports the rootdir conftest before ``tests/conftest.py`` and before any test
module, so the floor cannot be out-ordered by an import that some future edit
adds higher up the chain. Measured: only ``--noconftest`` and ``--confcutdir``
defeat it; ``--rootdir``, ``-c``, a cwd change, a path argument and
``pytest-xdist`` (one sandbox per worker) all keep it live.

**Where the sandbox lives.** One throwaway directory per pytest process, removed
when the interpreter exits. Session-scoped rather than per-test on purpose:
``config_dir()`` memoises nothing and re-reads the environment on every call, so
a floor set once is enough and is immune to fixture ordering. It composes with
``tests/conftest.py::_clear_beets_globals`` — that fixture *saves and restores*
``BEETSDIR``, so with the floor in place the value it restores is always the
sandbox and its "restore to unset" branch can never fire for it. Tests that want
their own beets dir (``beets_library``, anything calling ``setup_beets``) still
override it and are still restored back to the sandbox.

Note the sharing this MOVES rather than removes: every test now shares one
``state.pickle`` inside the sandbox instead of one in ``~/.config/beets``. That
is strictly better — the sandbox starts empty each run, where the old file
persisted across runs and held real history — but tests that read importer state
can still see what an earlier test left. Pre-seeding the sandbox with non-empty
importer state changes no result today.

**The second floor: the stored password hash.** ``tests/conftest.py``'s autouse
``password_hash_file`` fixture pins ``app.auth.source.live_password_hash_path``
per test, and that is where the WRITE protection lives. It cannot cover the two
reads that happen at IMPORT time, before any fixture exists:

* ``app/main.py`` resolves the boot posture clause while the module is imported;
* ``tests/test_health.py`` builds a module-level ``TestClient``, whose seam
  cookie is minted from ``effective_password()`` (``tests/conftest.py``).

With a real ``password-hash`` in the configured beets dir, that second read minted
a cookie bound to the real stored hash, the autouse fixture then repointed the
resolver at an empty tmp dir, and the gate rejected the suite's own cookie —
measured on this branch as ``tests/test_health.py`` failing ``401 == 200``. So
the resolver is pinned HERE too, at the same place and for the same reason the
``BEETSDIR`` floor is: before anything can import ``app.main``.
"""

import os
import tempfile
from pathlib import Path

from confuse.util import config_dirs

import app.auth.source as _password_source

#: The suite's throwaway ``BEETSDIR``. ``TemporaryDirectory`` keeps a finalizer
#: that removes it at interpreter exit; ``ignore_cleanup_errors`` so a lingering
#: sqlite handle can never turn teardown into a crash.
_SANDBOX = tempfile.TemporaryDirectory(
    prefix="musicdrop-suite-beetsdir-", ignore_cleanup_errors=True
)

#: Absolute path of that sandbox. Imported by ``tests/test_beetsdir_isolation.py``.
SUITE_BEETSDIR = Path(_SANDBOX.name).resolve()

# THE FLOOR. Set at import, before any test module or fixture exists.
os.environ["BEETSDIR"] = str(SUITE_BEETSDIR)

#: Every directory confuse would treat as "the beets config dir" if ``BEETSDIR``
#: were unset — ``~/.config/beets`` first, then the /etc fallbacks. Exists so the
#: pin in ``tests/test_beetsdir_isolation.py`` can assert the sandbox is not one
#: of them, rather than hard-coding a path that would rot on another platform.
PLATFORM_BEETS_DIRS: tuple[Path, ...] = tuple(Path(d) / "beets" for d in config_dirs())

#: Where the stored password hash resolves for the whole pytest PROCESS. Inside
#: the same throwaway sandbox, so it is removed with it, and under a name of its
#: own so a test that inspects the sandbox's beets files does not trip over it.
#: Nothing in the suite writes here — the autouse fixture repoints every test at
#: its own ``tmp_path`` — which is the point: the import-time readers find no
#: stored hash, exactly as they do on a machine that has never run setup.
SUITE_PASSWORD_HASH_PATH = SUITE_BEETSDIR / "suite-password-hash"

# THE SECOND FLOOR, and it has to be applied by importing the resolver's module
# here: ``app.main`` reads it while IT is imported, so a fixture cannot get in
# first. Assigning the module attribute (rather than the env var or
# ``settings.beets_dir``) pins the one seam every production read and write goes
# through — the same seam ``tests/conftest.py::password_hash_file`` re-pins per
# test, which saves and restores this value.
_password_source.live_password_hash_path = lambda: SUITE_PASSWORD_HASH_PATH
