# Beets-config-honor — Design Spec

**Date:** 2026-05-28
**Status:** Approved (brainstorm complete, ready for plan)
**Branch:** `feat/beets-config`

## Goal

Make `data/beets/config.yaml` the authoritative source of truth for MusicDrop's beets behavior, and add a read-only Config view at `/settings` that surfaces the effective configuration. After this slice: a user edits one file (or starts from a copied-on-first-run template); MusicDrop honors it for library path, directory, plugins, import behavior, and everything else beets reads from its config singleton.

## Background (verified state of the codebase, 2026-05-28)

- `data/beets/config.yaml` exists but is **silently ignored** at runtime. `setup_beets()` never calls `beets.config.set_file(...)` and never sets `BEETSDIR`. The on-disk file is dormant; beets runs entirely on bundled `config_default.yaml` defaults.
- `Library(...)` is constructed with `library_path=settings.beets_library_path` and `directory=settings.beets_library_directory`, both from `.env` (`MUSICDROP_BEETS_LIBRARY_PATH` / `MUSICDROP_BEETS_LIBRARY_DIRECTORY`). These ctor args **win unconditionally** — `Library.__init__` (`beets/library/library.py:34-50` in venv 2.11.0) does not read `config["library"]` / `config["directory"]`.
- `plugins.load_plugins()` is the FIRST step in `setup_beets()` — it runs before any config file could be applied. Plugin instances cache their `config[...]` reads at `__init__` (`beets/plugins.py:472-495`); the `plugins:` list in `config.yaml` is effectively frozen at whatever beets's default thinks it is.
- `Settings.beets_config_path` is declared at `backend/app/config.py:10` and **read nowhere** in the backend.

Net effect: a new user (and the current one) sees the candidate-review screen function on bundled defaults; nothing in `data/beets/config.yaml` matters.

## Architecture

Two layers, both behind the existing beets-adapter boundary (`app/beets/`). No new `import beets` outside that directory.

- **Layer 1 — honor the file.** `setup_beets()` becomes a faithful mirror of beets' own `_setup`. The beets `config` singleton, plugin registry, and `Library` all derive from one file at `<BEETSDIR>/config.yaml`. The "ignore the file, hardcode from .env" path is removed.
- **Layer 2 — read-only Config view.** New `GET /api/config` endpoint serves the effective config as redacted YAML. New `/settings` route renders it with a "restart required" hint when the file has been touched since startup.

## Decisions made (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Path resolution | **`BEETSDIR` env (`MUSICDROP_BEETS_DIR`, default `data/beets`)** | Beets-canonical (matches what beets CLI users expect); one knob for deployment. |
| Precedence with old env vars | **File wins; old env vars removed** | One source of truth; matches "config belongs to beets." |
| Fresh-install behavior | **Copy starter template once if missing** | "Just works" UX. Template is versioned in the repo and diff-able; never written to after the first copy. |
| Default plugins in starter | **`[musicbrainz, deezer]`** | Deezer is no-auth and dramatically improves match rate on modern releases (fixes the ADMT/"Homeless" miss observed during the import walkthrough). |
| Config-view shape | **Full effective YAML (redacted)** | Most honest; auto-updates as beets evolves; matches "config belongs to beets." No curation in this slice. |
| Refresh policy | **Startup snapshot + restart hint on mtime change** | Matches beets' own behavior (no hot-reload). Plugin instances + confuse layering make true reload semantically tricky. |
| YAML syntax highlighting | **Out of scope (follow-up)** | A single read-only viewer doesn't justify a 50–200kb syntax-highlighter dep. |
| Bundled-plugin auth status enrichment | **Out of scope (follow-up)** | Curation work; this slice ships raw YAML per the Q3 choice. |
| Layer 3 (write-back editor) | **Out of scope (separate spec later)** | Requires RW + safe YAML round-trip + validation; own brainstorm cycle. |

## Layer 1 — `setup_beets()` rewrite

### File: `backend/app/beets/setup.py`

Faithful mirror of beets' own `_setup` in `beets/ui/__init__.py:807-827`, adapted for embedded mode.

```python
def setup_beets(beets_dir: str) -> LibraryHandle:
    """
    Mirror beets' own _setup() with embedded-mode adjustments.
    Faithful to beets 2.11.0 internals.
    """
    # 1. Resolve and ensure BEETSDIR
    beets_dir_path = Path(beets_dir).resolve()
    beets_dir_path.mkdir(parents=True, exist_ok=True)
    cfg_path = beets_dir_path / "config.yaml"

    # 2. One-time starter copy if missing
    if not cfg_path.exists():
        starter = Path(__file__).parent / "config.starter.yaml"
        shutil.copy(starter, cfg_path)
        logger.info("Copied starter config to %s", cfg_path)

    # 3. Set BEETSDIR before any beets.config access (confuse env-resolves here)
    os.environ["BEETSDIR"] = str(beets_dir_path)

    # 4. Deprecation warnings for old env vars
    for old in ("MUSICDROP_BEETS_LIBRARY_PATH", "MUSICDROP_BEETS_LIBRARY_DIRECTORY"):
        if os.environ.get(old):
            logger.warning(
                "%s is no longer honored. "
                "Move the value into %s (under `library:` or `directory:`), "
                "then remove this env var.",
                old, cfg_path,
            )

    # 5. Capture file mtime BEFORE first-resolve
    file_mtime_at_load = cfg_path.stat().st_mtime

    # 6. Force confuse's lazy resolve (loads default + user file)
    beets.config["dummy"].exists()  # cheap touch

    # 7. Load plugins AFTER config is settled (reads config["plugins"])
    plugins.load_plugins()

    # 8. Pull library and directory paths from config (mirrors beets/ui/__init__.py:879-889)
    lib_path = beets.config["library"].as_filename()
    directory = beets.config["directory"].as_filename()

    # 9. Open the library
    lib = Library(
        lib_path,
        directory=directory,
        path_formats=get_path_formats(),
        replacements=get_replacements(),
    )

    # 10. Notify plugins
    plugins.send("library_opened", lib=lib)

    # 11. Stash everything needed for the Config view
    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir_path,
        config_path=cfg_path,
        loaded_at=datetime.now(timezone.utc),
        file_mtime_at_load=file_mtime_at_load,
    )
```

### Why each line is non-negotiable

- **Step 3 sets BEETSDIR before step 6.** Confuse reads `BEETSDIR` inside `Configuration.config_dir()` (`confuse/core.py:579-615`); if we set it after first-resolve, confuse has already locked in the default platform configdir's user file (or none). Env-before-resolve.
- **Step 6 forces resolve.** `beets.config` is `LazyConfig` (`beets/__init__.py:35-52`); nothing is loaded until first access. The `.exists()` touch is cheap.
- **Step 7 is AFTER step 6.** Today it's the first thing — that's why `plugins:` in `config.yaml` is ignored. After this rewrite, the file's `plugins:` is what loads.
- **Steps 8–9 mirror beets' own `_open_library`** at `beets/ui/__init__.py:879-889`. Previously we passed `settings.beets_library_path` / `settings.beets_library_directory`, which hard-coded around the config file. Now we read those keys through confuse.
- **Step 10 keeps the existing `Library(...)` ctor args** because `Library.__init__` doesn't read `config["library"]` / `config["directory"]` itself — beets reads those keys in the *caller*. We're being the caller correctly.

### `LibraryHandle` extension (`backend/app/beets/library.py`)

Currently `LibraryHandle` wraps just the `Library`. Extend to carry the snapshot data:

```python
@dataclass
class LibraryHandle:
    lib: Library
    beets_dir: Path
    config_path: Path
    loaded_at: datetime
    file_mtime_at_load: float  # raw stat.st_mtime for direct comparison
```

### Settings (`backend/app/config.py`) diff

```diff
- beets_config_path: str | None = None              # was unwired; remove
- beets_library_path: str | None = None             # now in config.yaml's library:
- beets_library_directory: str | None = None        # now in config.yaml's directory:
+ beets_dir: str = "data/beets"                     # env: MUSICDROP_BEETS_DIR
```

### `backend/app/main.py` diff (`_resolve_library`)

```diff
- return setup_beets(settings.beets_library_path, settings.beets_library_directory)
+ return setup_beets(settings.beets_dir)
```

### Error policy

- **Missing file** → starter is copied; proceeds normally.
- **Invalid YAML** → confuse raises during step 6 (first-resolve); FastAPI startup fails with the parse error in logs. No silent fallback — that would hide misconfiguration.
- **Missing `library:` or `directory:` keys in user file** → confuse falls back to `config_default.yaml` (`library.db`, `~/Music`). That's beets' own behavior; we don't override.
- **`<BEETSDIR>` is read-only or starter copy fails** → `mkdir` / `shutil.copy` raises `PermissionError`; FastAPI startup fails.

### Surviving runtime override

`config["threaded"] = False` in `WebImportSession.__init__` (`app/beets/import_session.py:296`) **stays**. It's a deliberate single-thread / global-singleton safety override. Any `threaded: yes` in the user's file is ignored at import time. This is the one documented exception to "file is canonical." Comment in code points at this spec.

## Layer 1 — starter template (`backend/app/beets/config.starter.yaml`)

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

Lives at `backend/app/beets/config.starter.yaml`. Versioned in the repo. Copy is one-time, on first run if missing. After that, the user-owned `<BEETSDIR>/config.yaml` is untouchable.

## Layer 2 — `GET /api/config`

### Pydantic models (`backend/app/models/config_api.py`)

```python
class BeetsConfigSnapshot(BaseModel):
    yaml_text: str                       # rendered post-merge YAML, redacted
    config_path: str                     # absolute path to <BEETSDIR>/config.yaml
    loaded_at: datetime                  # when setup_beets() ran
    file_modified_at: datetime | None    # current st_mtime; None if file is missing
    restart_required: bool               # file_missing OR file_mtime > file_mtime_at_load
```

### Backend route (`backend/app/api/config_.py`)

```python
router = APIRouter()

@router.get("/config", response_model=BeetsConfigSnapshot)
def get_config(request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return build_config_snapshot(handle)
```

### Snapshot builder (`backend/app/beets/config_snapshot.py`)

```python
SECRET_KEY_PATTERN = re.compile(
    r"(secret|token|password|apikey|api_key|auth_token)", re.IGNORECASE
)

def build_config_snapshot(handle: LibraryHandle) -> BeetsConfigSnapshot:
    # Confuse per-key redaction (e.g. spotify.client_secret → "REDACTED")
    flat = beets.config.flatten(redact=True)

    # Safety-net mask for unmarked secrets (third-party plugins)
    _mask_secrets_in_place(flat, SECRET_KEY_PATTERN)

    yaml_text = yaml.safe_dump(flat, sort_keys=False, default_flow_style=False)

    file_modified_at: datetime | None = None
    file_missing = not handle.config_path.exists()
    current_mtime: float | None = None
    if not file_missing:
        current_mtime = handle.config_path.stat().st_mtime
        file_modified_at = datetime.fromtimestamp(current_mtime, tz=timezone.utc)

    restart_required = (
        file_missing
        or (current_mtime is not None and current_mtime > handle.file_mtime_at_load)
    )

    return BeetsConfigSnapshot(
        yaml_text=yaml_text,
        config_path=str(handle.config_path),
        loaded_at=handle.loaded_at,
        file_modified_at=file_modified_at,
        restart_required=restart_required,
    )

def _mask_secrets_in_place(d: dict, pattern: re.Pattern) -> None:
    for k, v in d.items():
        if isinstance(v, dict):
            _mask_secrets_in_place(v, pattern)
        elif isinstance(v, str) and pattern.search(k):
            d[k] = "REDACTED"
```

`beets.config.flatten(redact=True)` is documented at confuse `core.py:273-290` (verified in venv 2.11.0). The safety-net is belt-and-suspenders for third-party plugins that didn't mark `view.redact = True`.

### Router registration (`backend/app/main.py`)

```python
from app.api import albums, artists, config_, health, import_, search
...
app.include_router(config_.router, prefix="/api")
```

## Layer 2 — frontend

### Files

- `frontend/src/api/useBeetsConfig.ts` — TanStack Query hook
- `frontend/src/pages/settings/SettingsPage.tsx` — the page
- `frontend/src/pages/settings/SettingsPage.test.tsx`
- `frontend/src/App.tsx` — new `<Route path="/settings" element={<SettingsPage />} />`
- `frontend/src/components/layout/Header.tsx` — new "Settings" link after "Import"
- `frontend/src/api/schema.d.ts` + `frontend/openapi.json` — regenerated via `npm run gen:api`

### Hook (`useBeetsConfig.ts`)

```ts
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

### Page (`SettingsPage.tsx`)

Renders:

- **Header:** `"Beets configuration"` heading + small text `"Loaded from <config_path> · <relative time, e.g. 2 minutes ago>"`.
- **Restart banner (yellow):** only when `restart_required: true`. Text: `"config.yaml was modified at <time> — restart MusicDrop to apply the changes."`
- **Body:** `<pre className="font-mono ...">` containing `data.yaml_text`. Scroll-x and scroll-y if long.
- **Loading state:** spinner / skeleton.
- **Error state:** plain "Could not load configuration. <error>" — no retry button (rare; refresh works).

No syntax highlighting. No editing. No reload button.

### Nav update (`Header.tsx`)

Add a new `<NavLink to="/settings">Settings</NavLink>` after the existing "Import" link. Same styling as existing nav links.

### OpenAPI regeneration

After backend changes land, run `npm run gen:api`. Both `frontend/src/api/schema.d.ts` and `frontend/openapi.json` are tracked — regen commits MUST include both.

## Tests

### Backend (pytest)

**`backend/tests/test_setup_beets.py` (new):**

- `test_setup_copies_starter_when_missing` — point at empty tmp BEETSDIR; assert `<tmp>/config.yaml` exists with starter content (byte-compare against `app/beets/config.starter.yaml`).
- `test_setup_leaves_existing_config_alone` — pre-create `config.yaml` with custom content; assert byte-for-byte unchanged after setup.
- `test_setup_loads_user_plugins` — fixture `config.yaml` with `plugins: [musicbrainz, deezer]`; assert `metadata_plugins.find_metadata_source_plugins()` returns names containing both. Extends the existing `test_import_setup.py` regression.
- `test_setup_honors_library_and_directory_from_file` — fixture `config.yaml` with specific `library:` and `directory:` paths; assert `LibraryHandle.lib.path` matches the file's value.
- `test_setup_fails_fast_on_invalid_yaml` — write malformed YAML; assert `setup_beets()` raises with a clear error.
- `test_setup_logs_deprecation_for_old_env_vars` — set `MUSICDROP_BEETS_LIBRARY_PATH`; assert a `WARNING` log line is emitted via `caplog`.

**`backend/tests/test_config_snapshot.py` (new):**

- `test_yaml_text_redacts_per_view_flag` — set `beets.config["spotify"]["client_secret"] = "supersecret"` and mark `.redact = True`; assert `"supersecret"` not in `yaml_text`, `"REDACTED"` in `yaml_text`.
- `test_yaml_text_safety_net_masks_unmarked_secrets` — set `beets.config["mything"]["api_key"] = "leakme"` with no redact flag; assert masked.
- `test_restart_required_when_mtime_advances` — load handle; `os.utime(config_path, (newer, newer))`; assert snapshot returns `restart_required: True`.
- `test_file_modified_at_none_when_file_missing` — load handle; delete the file; assert `file_modified_at: None` and `restart_required: True`.

**`backend/tests/test_config_api.py` (new):**

- `test_get_config_returns_snapshot` — startup → GET `/api/config` → 200 with all fields populated, `restart_required: False`.
- `test_get_config_reflects_mtime_change_but_yaml_text_unchanged` — write to `config.yaml` between two GETs; second GET shows new mtime + `restart_required: True` but `yaml_text` is unchanged (we serve the in-memory snapshot, not the file).

### Frontend (Vitest + RTL)

**`frontend/src/pages/settings/SettingsPage.test.tsx`:**

- `renders the snapshot` — mock `/api/config` with a fixture YAML; assert `<pre>` contains the YAML text; header shows path + relative loaded-at line.
- `shows restart banner when restart_required` — fixture with `true`; assert banner present with the modified-at time.
- `hides restart banner when fresh` — fixture with `false`; assert banner absent.

### Test infrastructure adjustments

- `backend/tests/conftest.py` — the existing `beets_library` fixture currently uses `settings.beets_library_path` / `settings.beets_library_directory` monkeypatches. It must be rewritten to point at a tmp `BEETSDIR` containing a minimal test `config.yaml`, then call `setup_beets(beets_dir=tmp_path)`.
- `backend/tests/test_albums.py:340-366` currently monkeypatches the old settings fields. These references must be updated to write a temporary `config.yaml` with the desired values instead.

## Migration (one-time, for our setup)

Manual; greenfield single-user:

1. **`backend/.env`:**
   ```diff
   - MUSICDROP_BEETS_LIBRARY_PATH=/mnt/data/projects/MusicDrop/data/beets/library.db
   - MUSICDROP_BEETS_LIBRARY_DIRECTORY=/mnt/data/projects/MusicDrop/data/music
   ```
   No new env var needed unless you want a non-default BEETSDIR (default is `data/beets`).

2. **`data/beets/config.yaml`** — flip values to match the new reality:
   ```diff
     directory: /mnt/data/projects/MusicDrop/data/music
     library: /mnt/data/projects/MusicDrop/data/beets/library.db
   + plugins:
   +   - musicbrainz
   +   - deezer
     import:
   -   copy: no
   -   move: no
   -   autotag: no
   +   autotag: yes
   +   copy: yes
   +   move: no
   +   write: yes
   ```

If `MUSICDROP_BEETS_LIBRARY_PATH` / `MUSICDROP_BEETS_LIBRARY_DIRECTORY` env vars are still set after this slice ships, `setup_beets()` logs one `WARNING` line per stale var at startup. Log-only — no UI surface.

## Ship plan

- **Branch:** `feat/beets-config` off `main` (same convention as `feat/import`).
- **One PR, no chunking** — slice is ~10–15 files, much smaller than the import slice.
- **Flow:** feature branch → push → PR → squash-merge → delete branch. User pushes, creates, and merges the PR; Claude does not.
- **Pre-merge checklist:**
  - `uv run pytest` green (backend, all suites)
  - `uv run mypy` clean (`--strict`)
  - `uv run ruff check` and `uv run ruff format --check` clean
  - `npm test` green (frontend)
  - Live smoke walkthrough: start MusicDrop with the new code; navigate to `/settings`; verify the YAML renders with `import.autotag: yes`, `plugins: [musicbrainz, deezer]`; edit `data/beets/config.yaml` (touch + change a value); refresh `/settings`; verify the restart banner appears with the new mtime.

## Out of scope (deferred follow-ups)

- **Layer 3: write-back editor.** Full Settings page with form controls, safe YAML round-trip (preserve hand-edits / comments), validation, per-plugin config helpers. Own brainstorm → spec → plan.
- **YAML syntax highlighting** in the Config view.
- **Curated panel** above the raw YAML (per-plugin auth status indicators, "what this means" tooltips, etc.).
- **Hot-reload of config** without restart. Restart-required is the deliberate policy.
- **Multi-user / multi-config** support. Single-instance, single-config.
- **Plugin credential management UI.** For now, users edit `config.yaml` directly to add `spotify.client_id` / `discogs.user_token` / etc.

## Open questions resolved during brainstorm

| Q | Decision |
|---|---|
| File-canonical vs env-override-the-file vs hybrid? | **File-canonical, `MUSICDROP_BEETS_DIR` is the only env knob.** |
| Starter template lifecycle? | **Copy on first-run if missing; never write again.** |
| Default plugins in starter? | **`[musicbrainz, deezer]` — Deezer is no-auth and fixes modern-release misses.** |
| Config view shape? | **Full effective YAML, redacted; no curation in this slice.** |
| Refresh policy? | **Startup snapshot + restart hint on mtime change.** |
| Header nav placement? | **Text link "Settings" after "Import."** |
| YAML syntax highlighting? | **Punt to follow-up.** |
