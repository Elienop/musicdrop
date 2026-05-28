# Beets-config-honor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `data/beets/config.yaml` the authoritative source of truth for MusicDrop's beets behavior, and add a read-only Config view at `/settings`.

**Architecture:** Two layers behind the existing beets-adapter (`app/beets/`).
- **Layer 1** rewrites `setup_beets()` to be a faithful mirror of beets' own `_setup`: copy starter template if missing → set `BEETSDIR` → force-resolve confuse → `plugins.load_plugins()` → read `library:` / `directory:` from `config` → open `Library`. Removes `MUSICDROP_BEETS_LIBRARY_PATH`/`MUSICDROP_BEETS_LIBRARY_DIRECTORY` env vars; adds single `MUSICDROP_BEETS_DIR` (default `data/beets`).
- **Layer 2** adds `GET /api/config` returning the effective YAML (redacted via confuse's `flatten(redact=True)` + a safety-net regex mask), and a `/settings` route rendering it with a mtime-based "restart needed" banner.

**Tech Stack:** Python 3.11+ · FastAPI · Pydantic · beets 2.11.0 (embedded, venv) · confuse · PyYAML; React 19 · Vite · Tailwind v4 · shadcn · TanStack Query v5 · openapi-fetch.

**Spec:** `docs/superpowers/specs/2026-05-28-beets-config-honor-design.md`.

**Branch:** `feat/beets-config` off `main` (user creates; this plan does not push).

---

## File Structure

### Backend — new files
- `backend/app/beets/config.starter.yaml` — versioned starter template, copied into `<BEETSDIR>/config.yaml` on first run.
- `backend/app/beets/config_snapshot.py` — `build_config_snapshot()` + redaction helpers.
- `backend/app/models/config_api.py` — `BeetsConfigSnapshot` Pydantic model.
- `backend/app/api/config_.py` — `GET /api/config` router.
- `backend/tests/test_setup_beets.py` — covers the rewritten setup_beets end-to-end.
- `backend/tests/test_config_snapshot.py` — redaction + restart-required logic.
- `backend/tests/test_config_api.py` — endpoint tests.

### Backend — modified files
- `backend/app/beets/setup.py` — full rewrite (faithful `_setup` mirror).
- `backend/app/beets/library.py` — `LibraryHandle` dataclass extension; signature of `open_library` may simplify.
- `backend/app/config.py` — drop 3 fields, add `beets_dir`.
- `backend/app/main.py` — `_resolve_library` signature change + register `config_.router`.
- `backend/pyproject.toml` — extend mypy `disallow_untyped_calls=false` override for new modules touching beets.
- `backend/tests/conftest.py` — rewrite the beets-library fixture to use a tmp BEETSDIR (build `config.yaml` then call new `setup_beets(beets_dir=...)`).
- `backend/tests/test_albums.py` — update the lines that monkeypatch `settings.beets_library_path` / `settings.beets_library_directory` to use the new fixture pattern (write a temp `config.yaml`).
- `backend/tests/test_import_setup.py` — extend the existing regression to also assert `deezer` is present after setup with the default starter.

### Frontend — new files
- `frontend/src/api/useBeetsConfig.ts` — TanStack Query hook.
- `frontend/src/pages/settings/SettingsPage.tsx` — the Config view.
- `frontend/src/pages/settings/SettingsPage.test.tsx`.

### Frontend — modified files
- `frontend/src/App.tsx` — add `<Route path="/settings" element={<SettingsPage />} />`.
- `frontend/src/components/layout/Header.tsx` — add a `NavLink` after the "Import" link.
- `frontend/src/api/schema.d.ts` — regenerated.
- `frontend/openapi.json` — regenerated.

### Local migration (manual, not code)
- `backend/.env` — remove `MUSICDROP_BEETS_LIBRARY_PATH` and `MUSICDROP_BEETS_LIBRARY_DIRECTORY`.
- `data/beets/config.yaml` — flip values to match new reality (autotag/copy/write yes; plugins [musicbrainz, deezer]).

---

## Task 1: Starter template + `setup_beets()` rewrite + `LibraryHandle` extension

**Files:**
- Create: `backend/app/beets/config.starter.yaml`
- Modify: `backend/app/beets/setup.py` (full rewrite)
- Modify: `backend/app/beets/library.py` (extend `LibraryHandle`)
- Test: `backend/tests/test_setup_beets.py` (create)

- [ ] **Step 1: Write the starter template**

Create `backend/app/beets/config.starter.yaml`:

```yaml
# data/beets/config.yaml — MusicDrop starter
# This file is yours. MusicDrop reads it; it never writes back.
# Edit and restart MusicDrop to apply changes.

# Library location. Paths are relative to this file's directory.
directory: ../music         # imported files land here
library: library.db         # SQLite library DB next to this file

# Metadata sources used to match albums during import.
# musicbrainz + deezer are no-auth. Add spotify/discogs/beatport/tidal here
# once you fill in their credentials (see beets docs).
plugins:
  - musicbrainz
  - deezer

# Import behavior. These defaults are what the candidate-review UI expects.
import:
  autotag: yes        # show candidates for human review
  copy: yes           # copy files into `directory`
  move: no            # if yes, move source files instead of copying
  write: yes          # write tags back to imported files on Apply

# Full reference: https://beets.readthedocs.io/en/stable/reference/config.html
```

- [ ] **Step 2: Extend `LibraryHandle` in `backend/app/beets/library.py`**

The current `LibraryHandle` wraps just the `Library`. Replace its dataclass with:

```python
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from beets.library import Library


@dataclass
class LibraryHandle:
    lib: Library
    beets_dir: Path
    config_path: Path
    loaded_at: datetime
    file_mtime_at_load: float  # raw stat.st_mtime for direct comparison
```

Keep the module's existing `open_library()` if simple; or inline it into `setup_beets()`. The existing imports of `get_path_formats` / `get_replacements` from `beets.ui` stay.

- [ ] **Step 3: Write failing test — copies starter when missing**

Create `backend/tests/test_setup_beets.py`:

```python
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest

from app.beets.setup import setup_beets


@pytest.fixture(autouse=True)
def _clear_beets_globals() -> Iterator[None]:
    """Each test gets a clean beets.config singleton + plugin registry."""
    import beets
    from beets import plugins
    # Snapshot env keys we may mutate
    saved_env = {k: os.environ.get(k) for k in
                 ("BEETSDIR", "MUSICDROP_BEETS_LIBRARY_PATH",
                  "MUSICDROP_BEETS_LIBRARY_DIRECTORY")}
    yield
    # Reset confuse + plugins to defaults so the next test starts fresh
    beets.config.clear()
    beets.config.read(user=False, defaults=True)
    plugins._instances.clear()
    plugins._classes.clear()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_setup_copies_starter_when_missing(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    try:
        cfg = tmp_path / "config.yaml"
        assert cfg.exists()
        starter = (
            Path(__file__).parent.parent / "app" / "beets" / "config.starter.yaml"
        )
        assert cfg.read_text() == starter.read_text()
        assert isinstance(handle.loaded_at, datetime)
        assert handle.config_path == cfg
        assert handle.beets_dir == tmp_path.resolve()
    finally:
        handle.lib.close()
```

Run: `uv run pytest backend/tests/test_setup_beets.py::test_setup_copies_starter_when_missing -v`
Expected: **FAIL** — signature of `setup_beets()` doesn't match yet (current takes `library_path, directory`; this calls with single `beets_dir`).

- [ ] **Step 4: Implement the new `setup_beets()`**

Replace `backend/app/beets/setup.py` entirely:

```python
"""Embedded beets startup — faithful mirror of beets' own _setup.

Reference: beets/ui/__init__.py:807-827 (_setup), :830-864 (_configure),
:879-899 (_open_library), all from beets 2.11.0 in venv.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import beets
from beets import plugins
from beets.library import Library
from beets.ui import get_path_formats, get_replacements

from app.beets.library import LibraryHandle

logger = logging.getLogger(__name__)


def setup_beets(beets_dir: str) -> LibraryHandle:
    """Open a beets Library under `beets_dir`, honoring its config.yaml.

    Sequence (do not reorder — see spec):
      1. Resolve and ensure BEETSDIR exists.
      2. Copy starter template if config.yaml is missing.
      3. Set BEETSDIR env (confuse reads it during config_dir()).
      4. Warn if old MUSICDROP_BEETS_LIBRARY_* env vars are still set.
      5. Snapshot file mtime BEFORE first-resolve (for restart-hint logic).
      6. Force confuse's lazy resolve (loads default + user file).
      7. Load plugins — reads config["plugins"].as_str_seq().
      8. Read library/directory from config (mirror beets/ui:879-889).
      9. Open Library.
      10. Notify plugins ("library_opened").
      11. Return handle with snapshot data for the Config view.
    """
    beets_dir_path = Path(beets_dir).resolve()
    beets_dir_path.mkdir(parents=True, exist_ok=True)
    cfg_path = beets_dir_path / "config.yaml"

    if not cfg_path.exists():
        starter = Path(__file__).parent / "config.starter.yaml"
        shutil.copy(starter, cfg_path)
        logger.info("Copied starter config to %s", cfg_path)

    os.environ["BEETSDIR"] = str(beets_dir_path)

    for old in ("MUSICDROP_BEETS_LIBRARY_PATH", "MUSICDROP_BEETS_LIBRARY_DIRECTORY"):
        if os.environ.get(old):
            logger.warning(
                "%s is no longer honored. Move the value into %s "
                "(under `library:` or `directory:`), then remove this env var.",
                old, cfg_path,
            )

    file_mtime_at_load = cfg_path.stat().st_mtime

    beets.config["dummy"].exists()  # force confuse's lazy resolve

    plugins.load_plugins()

    lib_path = beets.config["library"].as_filename()
    directory = beets.config["directory"].as_filename()

    lib = Library(
        lib_path,
        directory=directory,
        path_formats=get_path_formats(),
        replacements=get_replacements(),
    )
    plugins.send("library_opened", lib=lib)

    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir_path,
        config_path=cfg_path,
        loaded_at=datetime.now(timezone.utc),
        file_mtime_at_load=file_mtime_at_load,
    )
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest backend/tests/test_setup_beets.py::test_setup_copies_starter_when_missing -v`
Expected: **PASS**.

- [ ] **Step 6: Run mypy + ruff**

Run: `uv run mypy backend/app/beets/setup.py backend/app/beets/library.py`
Expected: zero errors. If beets-untyped-call errors trip, extend the mypy override in `pyproject.toml` (Task 3 handles this; if blocking here, add a scoped `# type: ignore[no-untyped-call]` with a trailing reason comment).

Run: `uv run ruff check backend/app/beets/ && uv run ruff format --check backend/app/beets/`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/beets/config.starter.yaml backend/app/beets/setup.py \
        backend/app/beets/library.py backend/tests/test_setup_beets.py
git commit -m "feat(beets-config): setup_beets mirrors _setup; add starter template + LibraryHandle snapshot fields"
```

---

## Task 2: Honor library/directory/plugins from file (additional `setup_beets` tests)

**Files:**
- Modify: `backend/tests/test_setup_beets.py`
- Modify: `backend/tests/test_import_setup.py` (extend)

- [ ] **Step 1: Add `test_setup_honors_library_and_directory_from_file`**

Append to `backend/tests/test_setup_beets.py`:

```python
def test_setup_honors_library_and_directory_from_file(tmp_path: Path) -> None:
    music_dir = tmp_path / "my_music"
    music_dir.mkdir()
    lib_file = tmp_path / "my_library.db"

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"directory: {music_dir}\n"
        f"library: {lib_file}\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n"
    )
    handle = setup_beets(str(tmp_path))
    try:
        # Library.path and Library.directory are bytes in beets
        assert Path(os.fsdecode(handle.lib.path)) == lib_file
        assert Path(os.fsdecode(handle.lib.directory)) == music_dir
    finally:
        handle.lib.close()
```

- [ ] **Step 2: Add `test_setup_loads_user_plugins`**

Append:

```python
def test_setup_loads_user_plugins(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "directory: ../music\nlibrary: library.db\n"
        "plugins:\n  - musicbrainz\n  - deezer\n"
        "import:\n  autotag: yes\n"
    )
    handle = setup_beets(str(tmp_path))
    try:
        from beets import metadata_plugins
        names = {p.name for p in metadata_plugins.find_metadata_source_plugins()}
        assert "musicbrainz" in names
        assert "deezer" in names
    finally:
        handle.lib.close()
```

- [ ] **Step 3: Add `test_setup_leaves_existing_config_alone`**

Append:

```python
def test_setup_leaves_existing_config_alone(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    custom = (
        "directory: ../music\nlibrary: library.db\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n  copy: no\n"
    )
    cfg.write_text(custom)
    handle = setup_beets(str(tmp_path))
    try:
        assert cfg.read_text() == custom
    finally:
        handle.lib.close()
```

- [ ] **Step 4: Extend `test_import_setup.py` for Deezer-default**

Open `backend/tests/test_import_setup.py`. The existing test should call `setup_beets()` with the new signature (no library path/dir args — call with a tmp BEETSDIR). After the call, assert BOTH `musicbrainz` AND `deezer` are present in `metadata_plugins.find_metadata_source_plugins()` — proving the default starter ships both.

If the existing test currently passes `None, None` to setup_beets (old signature), update it to:

```python
def test_default_starter_loads_musicbrainz_and_deezer(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    try:
        from beets import metadata_plugins
        names = {p.name for p in metadata_plugins.find_metadata_source_plugins()}
        assert "musicbrainz" in names
        assert "deezer" in names
    finally:
        handle.lib.close()
```

(Keep any other tests in the file; this is an addition or replacement of the existing setup-smoke test.)

- [ ] **Step 5: Run the suite**

Run: `uv run pytest backend/tests/test_setup_beets.py backend/tests/test_import_setup.py -v`
Expected: **all PASS**.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_setup_beets.py backend/tests/test_import_setup.py
git commit -m "test(beets-config): assert setup honors library/directory/plugins from file"
```

---

## Task 3: Settings diff + `main.py` glue + test fixture rewrite

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_albums.py` (around lines 340-366)

- [ ] **Step 1: Update `Settings`**

Replace the relevant fields in `backend/app/config.py`:

```python
# Remove these three:
#   beets_config_path: str | None = None
#   beets_library_path: str | None = None
#   beets_library_directory: str | None = None

# Add this:
beets_dir: str = "data/beets"
```

(Keep `model_config`, `app_name`, `version`, and all the `artist_image_*` fields.)

- [ ] **Step 2: Update `_resolve_library` in `main.py`**

In `backend/app/main.py`, change:

```python
def _resolve_library() -> LibraryHandle | None:
    return setup_beets(settings.beets_library_path, settings.beets_library_directory)
```

to:

```python
def _resolve_library() -> LibraryHandle:
    return setup_beets(settings.beets_dir)
```

Note the return type changes from `LibraryHandle | None` to `LibraryHandle` — `setup_beets()` now always returns one (no more "missing library returns None" branch; missing dir gets created, missing file gets the starter). Update all callers to drop the `None` check.

- [ ] **Step 3: Extend mypy override**

In `backend/pyproject.toml`, locate the `[[tool.mypy.overrides]]` block that sets `disallow_untyped_calls = false` for beets-touching modules. Add the new modules:

```toml
module = [
    "app.beets.library",
    "app.beets.import_mapping",
    "app.beets.import_session",
    "app.beets.setup",
    "app.beets.config_snapshot",   # <-- new
    "app.import_jobs.runner",
    "tests.test_albums",
    "tests.test_artists",
    "tests.test_search",
    "tests.test_import_mapping",
    "tests.test_import_session",
    "tests.test_import_runner",
    "tests.test_import_setup",
    "tests.test_setup_beets",      # <-- new
    "tests.test_config_snapshot",  # <-- new
    "tests.test_config_api",       # <-- new
]
disallow_untyped_calls = false
```

- [ ] **Step 4: Rewrite the `beets_library` fixture in `conftest.py`**

In `backend/tests/conftest.py`, locate the existing fixture that monkeypatches `settings.beets_library_path` / `beets_library_directory`. Replace with:

```python
@pytest.fixture
def beets_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LibraryHandle]:
    """Hermetic beets library under a tmp BEETSDIR.

    Writes a minimal config.yaml, then runs setup_beets() against it.
    Tests that want different values can write their own config.yaml
    BEFORE this fixture runs (use a parametrized factory instead).
    """
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"directory: {music_dir}\nlibrary: library.db\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n  copy: yes\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path))
    handle = setup_beets(str(tmp_path))
    try:
        yield handle
    finally:
        handle.lib.close()
```

If multiple tests need different fixture values, add a factory fixture (`beets_library_factory`) that takes config overrides.

- [ ] **Step 5: Update `test_albums.py` around lines 340-366**

Find the lines that read:

```python
monkeypatch.setattr(settings, "beets_library_path", ...)
monkeypatch.setattr(settings, "beets_library_directory", ...)
```

Replace with a temp-config-yaml write before the setup_beets call (or rely on the new `beets_library` fixture if the test can use it directly). Adjust assertions if they referenced the old settings fields.

- [ ] **Step 6: Run the test suites that touched fixtures**

Run: `uv run pytest backend/tests/test_albums.py backend/tests/test_setup_beets.py backend/tests/test_import_setup.py -v`
Expected: all PASS.

- [ ] **Step 7: Run mypy + ruff on the changed files**

Run: `uv run mypy backend/app/config.py backend/app/main.py backend/tests/`
Expected: clean.

Run: `uv run ruff check backend/ && uv run ruff format --check backend/`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add backend/app/config.py backend/app/main.py backend/pyproject.toml \
        backend/tests/conftest.py backend/tests/test_albums.py
git commit -m "refactor(beets-config): drop MUSICDROP_BEETS_LIBRARY_* env vars; add MUSICDROP_BEETS_DIR; rewrite test fixture"
```

---

## Task 4: Fail-fast on invalid YAML + deprecation warning logs

**Files:**
- Modify: `backend/tests/test_setup_beets.py`

(Both behaviors are already implemented in Task 1's `setup_beets()` — this task only writes the tests that pin them.)

- [ ] **Step 1: Add `test_setup_fails_fast_on_invalid_yaml`**

Append to `backend/tests/test_setup_beets.py`:

```python
def test_setup_fails_fast_on_invalid_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("directory: ../music\n  this: is: not [valid YAML\n")
    with pytest.raises(Exception):  # confuse raises a yaml error during first-resolve
        setup_beets(str(tmp_path))
```

- [ ] **Step 2: Add `test_setup_logs_deprecation_for_old_env_vars`**

Append:

```python
def test_setup_logs_deprecation_for_old_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MUSICDROP_BEETS_LIBRARY_PATH", "/leftover/path")
    monkeypatch.setenv("MUSICDROP_BEETS_LIBRARY_DIRECTORY", "/leftover/dir")
    with caplog.at_level(logging.WARNING, logger="app.beets.setup"):
        handle = setup_beets(str(tmp_path))
    try:
        msgs = [r.message for r in caplog.records]
        assert any("MUSICDROP_BEETS_LIBRARY_PATH" in m for m in msgs)
        assert any("MUSICDROP_BEETS_LIBRARY_DIRECTORY" in m for m in msgs)
    finally:
        handle.lib.close()
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest backend/tests/test_setup_beets.py -v`
Expected: all PASS (no impl changes needed — Task 1 already wrote the deprecation logic and confuse fails-fast naturally).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_setup_beets.py
git commit -m "test(beets-config): fail-fast on invalid YAML + deprecation warning logs"
```

---

## Task 5: `BeetsConfigSnapshot` model + snapshot builder with redaction

**Files:**
- Create: `backend/app/models/config_api.py`
- Create: `backend/app/beets/config_snapshot.py`
- Create: `backend/tests/test_config_snapshot.py`

- [ ] **Step 1: Write the Pydantic model**

Create `backend/app/models/config_api.py`:

```python
from datetime import datetime

from pydantic import BaseModel


class BeetsConfigSnapshot(BaseModel):
    """Read-only snapshot of beets' effective config + file freshness."""

    yaml_text: str
    """Rendered post-merge effective config as YAML, with secrets redacted."""

    config_path: str
    """Absolute path to the user-owned <BEETSDIR>/config.yaml."""

    loaded_at: datetime
    """When setup_beets() ran (UTC). The in-memory snapshot is from this moment."""

    file_modified_at: datetime | None
    """Current st_mtime of config_path (UTC). None if the file has been deleted."""

    restart_required: bool
    """True if the file is missing OR has been modified since loaded_at."""
```

- [ ] **Step 2: Write the snapshot builder**

Create `backend/app/beets/config_snapshot.py`:

```python
"""Build a BeetsConfigSnapshot from the in-memory beets config + LibraryHandle.

The yaml_text is rendered using:
  1. beets.config.flatten(redact=True) — confuse's per-view redaction
     (e.g. spotify.client_secret marked .redact=True at spotify.py:175-176).
  2. A safety-net regex mask for keys that match (secret|token|password|
     apikey|api_key|auth_token) — covers third-party plugins that didn't
     mark .redact via confuse.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import beets
import yaml

from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot

SECRET_KEY_PATTERN = re.compile(
    r"(secret|token|password|apikey|api_key|auth_token)", re.IGNORECASE
)
REDACTED = "REDACTED"


def build_config_snapshot(handle: LibraryHandle) -> BeetsConfigSnapshot:
    flat = beets.config.flatten(redact=True)
    _mask_secrets_in_place(flat, SECRET_KEY_PATTERN)
    yaml_text = yaml.safe_dump(
        _to_plain(flat), sort_keys=False, default_flow_style=False
    )

    file_missing = not handle.config_path.exists()
    file_modified_at: datetime | None = None
    current_mtime: float | None = None
    if not file_missing:
        current_mtime = handle.config_path.stat().st_mtime
        file_modified_at = datetime.fromtimestamp(current_mtime, tz=timezone.utc)

    restart_required = file_missing or (
        current_mtime is not None and current_mtime > handle.file_mtime_at_load
    )

    return BeetsConfigSnapshot(
        yaml_text=yaml_text,
        config_path=str(handle.config_path),
        loaded_at=handle.loaded_at,
        file_modified_at=file_modified_at,
        restart_required=restart_required,
    )


def _mask_secrets_in_place(d: Any, pattern: re.Pattern[str]) -> None:
    """Recursively walk a flattened-confuse mapping and mask string values
    whose key matches the secret pattern."""
    if not isinstance(d, dict):
        return
    for k, v in list(d.items()):
        if isinstance(v, dict):
            _mask_secrets_in_place(v, pattern)
        elif isinstance(v, str) and pattern.search(k):
            d[k] = REDACTED


def _to_plain(d: Any) -> Any:
    """Convert nested confuse AttrDict / OrderedDict to plain dicts so PyYAML
    doesn't emit type-tags."""
    if isinstance(d, dict):
        return {k: _to_plain(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_to_plain(v) for v in d]
    return d
```

- [ ] **Step 3: Write the four tests**

Create `backend/tests/test_config_snapshot.py`:

```python
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import beets
import pytest

from app.beets.library import LibraryHandle
from app.beets.config_snapshot import build_config_snapshot
from app.beets.setup import setup_beets


@pytest.fixture
def loaded_handle(tmp_path: Path) -> Iterator[LibraryHandle]:
    handle = setup_beets(str(tmp_path))
    try:
        yield handle
    finally:
        handle.lib.close()


def test_yaml_text_redacts_per_view_flag(loaded_handle: LibraryHandle) -> None:
    beets.config["spotify"]["client_secret"].set("supersecret")
    beets.config["spotify"]["client_secret"].redact = True
    snap = build_config_snapshot(loaded_handle)
    assert "supersecret" not in snap.yaml_text
    assert "REDACTED" in snap.yaml_text


def test_yaml_text_safety_net_masks_unmarked_secrets(
    loaded_handle: LibraryHandle,
) -> None:
    beets.config["mything"]["api_key"].set("leakme")
    snap = build_config_snapshot(loaded_handle)
    assert "leakme" not in snap.yaml_text
    assert "REDACTED" in snap.yaml_text


def test_restart_required_when_mtime_advances(loaded_handle: LibraryHandle) -> None:
    cfg = loaded_handle.config_path
    newer = cfg.stat().st_mtime + 10
    os.utime(cfg, (newer, newer))
    snap = build_config_snapshot(loaded_handle)
    assert snap.restart_required is True
    assert snap.file_modified_at is not None


def test_file_modified_at_none_when_file_missing(
    loaded_handle: LibraryHandle,
) -> None:
    loaded_handle.config_path.unlink()
    snap = build_config_snapshot(loaded_handle)
    assert snap.file_modified_at is None
    assert snap.restart_required is True


def test_fresh_snapshot_has_no_restart_required(
    loaded_handle: LibraryHandle,
) -> None:
    snap = build_config_snapshot(loaded_handle)
    assert snap.restart_required is False
    assert isinstance(snap.loaded_at, datetime)
    assert snap.loaded_at.tzinfo is not None
```

Run: `uv run pytest backend/tests/test_config_snapshot.py -v`
Expected: all PASS.

- [ ] **Step 4: Run mypy + ruff**

Run: `uv run mypy backend/app/beets/config_snapshot.py backend/app/models/config_api.py backend/tests/test_config_snapshot.py`
Expected: clean.

Run: `uv run ruff check backend/ && uv run ruff format --check backend/`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/config_api.py backend/app/beets/config_snapshot.py \
        backend/tests/test_config_snapshot.py
git commit -m "feat(beets-config): BeetsConfigSnapshot + redacted YAML builder"
```

---

## Task 6: `GET /api/config` router + OpenAPI regeneration

**Files:**
- Create: `backend/app/api/config_.py`
- Modify: `backend/app/main.py` (register router)
- Create: `backend/tests/test_config_api.py`
- Modify: `frontend/src/api/schema.d.ts` (regenerated)
- Modify: `frontend/openapi.json` (regenerated)

- [ ] **Step 1: Write failing test — endpoint returns snapshot**

Create `backend/tests/test_config_api.py`:

```python
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient


def test_get_config_returns_snapshot(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "yaml_text", "config_path", "loaded_at",
        "file_modified_at", "restart_required",
    ):
        assert key in body
    assert isinstance(body["yaml_text"], str)
    assert body["yaml_text"]  # non-empty
    assert body["restart_required"] is False


def test_get_config_reflects_mtime_change(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Second GET after a touch should show restart_required + new mtime,
    but yaml_text should be unchanged (we serve the in-memory snapshot)."""
    first = client.get("/api/config").json()
    time.sleep(0.01)  # ensure st_mtime advances by at least the FS granularity
    os.utime(
        beets_library_config_path,
        (time.time() + 5, time.time() + 5),
    )
    second = client.get("/api/config").json()
    assert second["restart_required"] is True
    assert second["yaml_text"] == first["yaml_text"]
    assert second["file_modified_at"] != first["file_modified_at"]
```

(The `client` fixture from `conftest.py` must wire `app.state.beets_library` to a fresh `LibraryHandle`. Add `beets_library_config_path` as a small helper fixture in `conftest.py` that returns the same `tmp_path / "config.yaml"`.)

Run: `uv run pytest backend/tests/test_config_api.py -v`
Expected: **FAIL** (no router yet).

- [ ] **Step 2: Implement the router**

Create `backend/app/api/config_.py`:

```python
"""Read-only Config view endpoint."""

from fastapi import APIRouter, Request

from app.beets.config_snapshot import build_config_snapshot
from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot

router = APIRouter()


@router.get("/config", response_model=BeetsConfigSnapshot, tags=["config"])
def get_config(request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return build_config_snapshot(handle)
```

- [ ] **Step 3: Register the router in `main.py`**

In `backend/app/main.py`, find the existing router-include block and add `config_`:

```python
from app.api import albums, artists, config_, health, import_, search

# ... after existing app.include_router calls:
app.include_router(config_.router, prefix="/api")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest backend/tests/test_config_api.py -v`
Expected: PASS.

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy backend/app/api/config_.py backend/app/main.py`
Expected: clean.

Run: `uv run ruff check backend/ && uv run ruff format --check backend/`
Expected: clean.

- [ ] **Step 6: Regenerate OpenAPI types**

From `frontend/`:

```bash
npm run gen:api
```

Expected: `frontend/src/api/schema.d.ts` updated with the new `/api/config` route and `BeetsConfigSnapshot` interface; `frontend/openapi.json` updated. Both files are tracked.

- [ ] **Step 7: Commit (backend + regen artifacts together)**

```bash
git add backend/app/api/config_.py backend/app/main.py \
        backend/tests/test_config_api.py \
        frontend/src/api/schema.d.ts frontend/openapi.json
git commit -m "feat(beets-config): GET /api/config + regen OpenAPI types"
```

---

## Task 7: `useBeetsConfig` hook + `SettingsPage` component + tests

**Files:**
- Create: `frontend/src/api/useBeetsConfig.ts`
- Create: `frontend/src/pages/settings/SettingsPage.tsx`
- Create: `frontend/src/pages/settings/SettingsPage.test.tsx`

- [ ] **Step 1: Write the hook**

Create `frontend/src/api/useBeetsConfig.ts`:

```ts
import { useQuery } from "@tanstack/react-query";

import { openapi } from "@/api/openapi";

export function useBeetsConfig() {
  return useQuery({
    queryKey: ["beets-config"],
    queryFn: async () => {
      const { data, error, response } = await openapi.GET("/api/config");
      if (!response.ok || !data) {
        throw new Error(error?.detail ?? "Failed to load config");
      }
      return data;
    },
    staleTime: 30_000,
  });
}
```

(Adjust the openapi-fetch import path to match the existing convention in the repo — check `frontend/src/api/useImport.ts` for the exact pattern.)

- [ ] **Step 2: Write failing tests for SettingsPage**

Create `frontend/src/pages/settings/SettingsPage.test.tsx`:

```tsx
import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { SettingsPage } from "@/pages/settings/SettingsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const CONFIG_URL = `${window.location.origin}/api/config`;

function snapshotFixture(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    yaml_text:
      "directory: /music\nlibrary: library.db\nplugins:\n  - musicbrainz\n  - deezer\n",
    config_path: "/abs/data/beets/config.yaml",
    loaded_at: "2026-05-28T14:23:00Z",
    file_modified_at: "2026-05-28T14:23:00Z",
    restart_required: false,
    ...overrides,
  };
}

describe("SettingsPage", () => {
  test("renders the snapshot YAML and config_path", async () => {
    server.use(http.get(CONFIG_URL, () => HttpResponse.json(snapshotFixture())));
    renderWithProviders(<SettingsPage />, { route: "/settings", path: "/settings" });

    expect(await screen.findByText(/Beets configuration/i)).toBeInTheDocument();
    expect(screen.getByText(/data\/beets\/config\.yaml/)).toBeInTheDocument();
    const pre = await screen.findByTestId("config-yaml");
    expect(pre).toHaveTextContent("directory: /music");
    expect(pre).toHaveTextContent("plugins:");
    expect(pre).toHaveTextContent("- deezer");
  });

  test("shows restart banner when restart_required", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json(
          snapshotFixture({
            restart_required: true,
            file_modified_at: "2026-05-28T15:00:00Z",
          }),
        ),
      ),
    );
    renderWithProviders(<SettingsPage />, { route: "/settings", path: "/settings" });
    expect(
      await screen.findByText(/restart MusicDrop to apply/i),
    ).toBeInTheDocument();
  });

  test("hides restart banner when fresh", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json(snapshotFixture({ restart_required: false })),
      ),
    );
    renderWithProviders(<SettingsPage />, { route: "/settings", path: "/settings" });
    await screen.findByText(/Beets configuration/i);
    expect(screen.queryByText(/restart MusicDrop to apply/i)).not.toBeInTheDocument();
  });

  test("shows error state when fetch fails", async () => {
    server.use(http.get(CONFIG_URL, () => HttpResponse.json({ detail: "boom" }, { status: 500 })));
    renderWithProviders(<SettingsPage />, { route: "/settings", path: "/settings" });
    expect(
      await screen.findByText(/could not load configuration/i),
    ).toBeInTheDocument();
  });
});
```

Run: `npm test -- SettingsPage` (or your local equivalent)
Expected: **FAIL** (component doesn't exist).

- [ ] **Step 3: Build the SettingsPage**

Create `frontend/src/pages/settings/SettingsPage.tsx`:

```tsx
import { useBeetsConfig } from "@/api/useBeetsConfig";

function formatRelative(iso: string): string {
  const then = new Date(iso);
  const diffMs = Date.now() - then.getTime();
  const mins = Math.round(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins === 1) return "1 minute ago";
  if (mins < 60) return `${mins} minutes ago`;
  const hours = Math.round(mins / 60);
  if (hours === 1) return "1 hour ago";
  if (hours < 24) return `${hours} hours ago`;
  return then.toLocaleString();
}

export function SettingsPage(): JSX.Element {
  const { data, isLoading, isError, error } = useBeetsConfig();

  return (
    <div className="mx-auto max-w-4xl p-6">
      <h1 className="mb-2 text-2xl font-semibold">Beets configuration</h1>

      {isLoading && (
        <div className="text-sm text-gray-500" role="status">
          Loading configuration…
        </div>
      )}

      {isError && (
        <div className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-900">
          Could not load configuration. {(error as Error)?.message}
        </div>
      )}

      {data && (
        <>
          <p className="mb-4 text-sm text-gray-600">
            Loaded from <code className="font-mono">{data.config_path}</code>{" "}
            · {formatRelative(data.loaded_at)}
          </p>

          {data.restart_required && (
            <div
              role="alert"
              className="mb-4 rounded border border-yellow-400 bg-yellow-50 p-3 text-sm text-yellow-900"
            >
              <strong>config.yaml was modified</strong>
              {data.file_modified_at && (
                <> at {new Date(data.file_modified_at).toLocaleTimeString()}</>
              )}
              {" "}— restart MusicDrop to apply the changes.
            </div>
          )}

          <pre
            data-testid="config-yaml"
            className="overflow-auto rounded border border-gray-200 bg-gray-50 p-4 font-mono text-sm leading-relaxed"
          >
            {data.yaml_text}
          </pre>
        </>
      )}
    </div>
  );
}
```

(Adjust classnames + container layout to match the existing page conventions in the repo — check `frontend/src/pages/import/ImportPage.tsx` for the exact wrapper pattern.)

- [ ] **Step 4: Run the tests**

Run: `npm test -- SettingsPage`
Expected: all PASS.

- [ ] **Step 5: Type-check the frontend**

Run: `npm run typecheck` (or `tsc --noEmit`)
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/useBeetsConfig.ts \
        frontend/src/pages/settings/SettingsPage.tsx \
        frontend/src/pages/settings/SettingsPage.test.tsx
git commit -m "feat(beets-config): SettingsPage + useBeetsConfig hook"
```

---

## Task 8: Route + nav link wiring

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/layout/Header.tsx`

- [ ] **Step 1: Add the route**

In `frontend/src/App.tsx`, find the existing routes block and add:

```tsx
import { SettingsPage } from "@/pages/settings/SettingsPage";

// inside <Routes>:
<Route path="/settings" element={<SettingsPage />} />
```

- [ ] **Step 2: Add the nav link**

In `frontend/src/components/layout/Header.tsx`, find the existing nav-link cluster (probably contains Library / Search / Import). After the "Import" link, add:

```tsx
<NavLink to="/settings" className={navLinkClass}>
  Settings
</NavLink>
```

(Use the exact same `className` / props as the existing links — search for `to="/import"` and copy the styling pattern verbatim.)

- [ ] **Step 3: Run full frontend tests**

Run: `npm test`
Expected: all PASS — adding a route + a link should not break existing tests. If a Header test enumerates all expected links, update it to include "Settings".

- [ ] **Step 4: Type-check**

Run: `npm run typecheck`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/App.tsx frontend/src/components/layout/Header.tsx
git commit -m "feat(beets-config): wire /settings route + Header nav link"
```

---

## Task 9: Full-green sweep + live smoke walkthrough

**Files:** any final fixes discovered during sweep.

- [ ] **Step 1: Full backend sweep**

Run: `uv run pytest -v` (all backend tests)
Expected: all PASS.

Run: `uv run mypy`
Expected: zero errors.

Run: `uv run ruff check && uv run ruff format --check`
Expected: clean.

- [ ] **Step 2: Full frontend sweep**

From `frontend/`:

```bash
npm run typecheck
npm test
npm run build  # catches Vite build issues
```

Expected: all PASS.

- [ ] **Step 3: Migrate local `.env` + `data/beets/config.yaml`**

Per spec § "Migration":

```bash
# .env: remove these two lines
- MUSICDROP_BEETS_LIBRARY_PATH=...
- MUSICDROP_BEETS_LIBRARY_DIRECTORY=...
```

Edit `data/beets/config.yaml` so it has `autotag: yes`, `copy: yes`, `write: yes`, `move: no`, and `plugins: [musicbrainz, deezer]`. Keep your existing absolute `directory:` and `library:` paths.

- [ ] **Step 4: Live smoke walkthrough**

```bash
# backend
cd backend && uv run uvicorn app.main:app --port 3030 --reload

# frontend (separate terminal)
cd frontend && npm run dev
```

Verify in browser:
1. App boots; logs show "Copied starter config to ..." if missing, OR the existing file loads without the deprecation warning (since we deleted the env vars).
2. Navigate to `/settings` — Config view renders the effective YAML; `import.autotag: yes`, `plugins:` contains musicbrainz + deezer, `restart_required: false`.
3. Edit `data/beets/config.yaml` (e.g., add a comment), save.
4. Reload the `/settings` page — the yellow restart banner appears with the new mtime; the YAML body is unchanged (still the in-memory snapshot — correct).
5. Restart the backend; reload `/settings` — banner is gone; YAML reflects the edit.
6. Smoke-run an import from `/import` to confirm autotag + deezer still work end-to-end (don't apply against real music — just verify candidates load).

- [ ] **Step 5: Capture any walkthrough fixes**

If the walkthrough surfaces bugs (e.g., a styling issue, a missing import handler), fix them in dedicated micro-commits — one per fix — referencing the walkthrough in the message.

- [ ] **Step 6: Final commit (only if Step 5 had fixes)**

```bash
# example
git add <fixed-files>
git commit -m "fix(beets-config): <one-line description of walkthrough fix>"
```

- [ ] **Step 7: Push the branch and open the PR**

(User does this — Claude does not push or open PRs unless explicitly asked.)

```bash
git push -u origin feat/beets-config
gh pr create --title "Feat/beets-config" --body "$(cat <<'EOF'
## Summary
- Layer 1: setup_beets mirrors beets' _setup; honors data/beets/config.yaml fully (library, directory, plugins, import.*)
- Layer 2: GET /api/config + /settings page rendering effective YAML with restart-required banner
- Drops MUSICDROP_BEETS_LIBRARY_PATH/DIRECTORY env vars; adds MUSICDROP_BEETS_DIR (default data/beets)
- Starter template copied on first run if missing; Deezer enabled by default (no-auth)

## Test plan
- [x] pytest green
- [x] mypy --strict green
- [x] ruff green
- [x] npm test green
- [x] Live walkthrough: /settings renders; edit + reload shows banner; restart clears banner; import still works
EOF
)"
```

---

## Out of scope (deferred follow-ups)

These are explicitly NOT part of this slice — separate brainstorm → spec → plan cycles:

- **Layer 3: write-back editor.** Full Settings page with form controls, safe YAML round-trip, validation, per-plugin config helpers.
- **YAML syntax highlighting** in the Config view.
- **Curated panel** above the raw YAML (per-plugin auth status indicators, "what this means" tooltips).
- **Hot-reload of config** without restart.
- **Multi-user / multi-config** support.
- **Plugin credential management UI** (Spotify / Discogs / Beatport / Tidal).

---

## Self-review checklist (run before handing off)

- ✅ Spec coverage: every spec section maps to a task. Layer 1 → T1-T4. Layer 2 → T5-T8. Tests/migration/ship → T9.
- ✅ No placeholders: every step has the actual code or command needed.
- ✅ Type/method consistency: `setup_beets(beets_dir: str)` signature used identically in T1 / T3 (caller) / fixtures (T3 / T5 / T6). `LibraryHandle` fields (`beets_dir`, `config_path`, `loaded_at`, `file_mtime_at_load`) defined in T1 and consumed in T5/T6. `BeetsConfigSnapshot` schema identical between T5 (backend) and T7 (frontend tests).
- ✅ Each task ends with a `commit` step.
- ✅ Tests precede impl (TDD) where impl is new; tests follow impl when the impl is a pre-existing behavior we're pinning (T4 — fail-fast already lives in T1's code).
- ✅ DRY: starter template only defined once (T1 step 1); referenced by hash-compare in T1's test rather than re-pasting.
