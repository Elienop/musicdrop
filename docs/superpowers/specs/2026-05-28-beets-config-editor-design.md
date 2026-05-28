# Beets-config Editor (Layer 3) — Design Spec

**Date:** 2026-05-28
**Status:** Approved (brainstorm complete; ready for plan)
**Branch:** `feat/beets-config-editor`
**Predecessor:** Layers 1 + 2 merged on `main` as PR #8 (commit `e13e906`) — file-canonical config + read-only `/settings` view

---

## Goal

Make MusicDrop's `/settings` page **editable**. The user can edit `<BEETSDIR>/config.yaml` in-browser via a CodeMirror 6 YAML editor; Save writes the file with validation + comment preservation; a single Apply button does an in-process reload so the new config takes effect without a process restart.

This closes the loop on the "config belongs to beets, MusicDrop is the GUI" posture: read (Layer 2) + write (Layer 3) = a coherent GUI for the file beets owns.

---

## Background

Layer 1 + 2 shipped the read side: `setup_beets()` honors `data/beets/config.yaml`, `GET /api/config` returns the redacted effective YAML + an `apply_pending`/`restart_required` mtime-derived flag, and `/settings` renders it read-only in a `<pre>` block. To take effect, a user currently has to (a) edit the file in `$EDITOR`, (b) `pkill uvicorn` + relaunch.

Layer 3 collapses (a) and (b) into in-browser actions: edit → Save → Apply.

The L1/L2 spec ([`docs/superpowers/specs/2026-05-28-beets-config-honor-design.md`](2026-05-28-beets-config-honor-design.md) § "Out of scope") explicitly named Layer 3 as the deferred follow-up:

> **Layer 3: write-back editor.** Full Settings page with form controls, safe YAML round-trip, validation, per-plugin credential helpers. Own brainstorm → spec → plan.

This is that spec.

---

## Decisions made

Each decision below was validated against authoritative primary sources (YAML 1.2.2 spec, ruamel.yaml maintainer docs, Pydantic v2 docs, FastAPI/Starlette docs, CodeMirror reference) during the brainstorm. Citations are inline in the relevant sections below.

| Question | Choice | Why |
|---|---|---|
| Editor shape | **Raw YAML editor** (CodeMirror 6) | Single source of truth; matches "file is canonical" from L1/L2; no form ↔ raw sync complexity; v1 minimum. |
| Apply UX | **One-click Apply** | Save persists the file; a separate **Apply** button does in-process reload. Apply is disabled while an import is in flight. |
| Secrets display | **REDACTED inline + preserve-on-save rule** | Editor shows `client_secret: REDACTED`. Backend save rule: if the saved YAML still has `REDACTED` at a key that was redacted in the snapshot, preserve the existing on-disk value. No new endpoint needed for credentials. |
| Validation depth | **YAML syntax + Pydantic schema for ~13 modeled keys** | Two-pass: ruamel parse + Pydantic v2 validation. Unmodeled keys pass through silently (default `extra='ignore'`). |
| Concurrency | **mtime + SHA-256 CAS** | `base_mtime_ns: int` + content-hash tie-breaker on the GET response; backend rejects Save with 409 if either has changed. UI shows side-by-side diff and lets user reload or overwrite. |
| Editor library | **CodeMirror 6** (`@uiw/react-codemirror` + `@codemirror/lang-yaml` + `@codemirror/lint` + `@codemirror/merge`) | ~130-170 kB gzipped vs Monaco's ~1.2 MB; YAML mode and async lint and merge view all official packages. |
| YAML library | **ruamel.yaml** (write-back) + PyYAML (read-display, unchanged from L1/L2) | ruamel is the only Python library with sanctioned round-trip; PyYAML stays in `config_snapshot.py` because confuse uses it. |
| Boolean style | **Accept normalization to `true`/`false` on first Save** | ruamel maintainer's documented invariant: *"emitted booleans always as true resp. false"* ([yaml.dev](https://yaml.dev/doc/ruamel.yaml/detail/)). Beets reads `true`/`false` identically to `yes`/`no` via PyYAML's YAML 1.1 schema. Pure cosmetic change. |
| Reload semantics | **`async def` apply handler + `asyncio.Lock` + `run_in_threadpool` for the blocking rebuild** | FastAPI-blessed offload pattern ([fastapi.tiangolo.com/async/](https://fastapi.tiangolo.com/async/)); lock + threadpool is the only safe shape given beets' process-global singletons. |
| Versioning | **Pin `beets==2.11.*`, `ruamel.yaml>=0.15.93`** in `backend/pyproject.toml` | beets 3.0.0 has open TODOs for the private hooks we depend on (`_instances`, `listeners`, `_materialized`); pinning is mandatory for correctness across upgrades. ruamel needs 0.15.93+ for `y`/`Y`/`n`/`N` YAML-1.1 boolean parsing ([SF #285](https://sourceforge.net/p/ruamel-yaml/tickets/285/)). |
| Deferred for L4 | Curated form for the ~13 common keys + UI for plugin credential rotation + YAML 1.2 migration | Layer-3 v1 is the raw YAML editor; the form layer can grow on top later. |

---

## Architecture

Two endpoints on top of the existing `/api/config`, plus an in-process reload, plus a single CodeMirror editor on `/settings`.

```
+----------------------------------------+
|              SettingsPage              |
| /settings (existing route)             |
|                                        |
|  ┌──────────────────────────────────┐  |
|  │ CodeMirror 6 — YAML editor       │  |
|  │ basicSetup + yaml() + lint +     │  |
|  │ Compartment(readonly toggle) +   │  |
|  │ Prec.high(Mod-s)                 │  |
|  └──────────────────────────────────┘  |
|  [Validate] [Save] [Apply changes]     |
+----------------------------------------+
         │             │            │
         │ POST        │ POST       │ POST
         │ /validate   │ /save      │ /apply
         ▼             ▼            ▼
+----------------------------------------+
|         backend/app/api/config_.py     |
|                                        |
|  GET    /api/config         (L1/L2)    |
|  POST   /api/config/validate (NEW)     |
|  POST   /api/config/save     (NEW)     |
|  POST   /api/config/apply    (NEW)     |
+----------------------------------------+
         │             │            │
         ▼             ▼            ▼
+----------------------------------------+
|       app/beets/config_editor.py       |
|                                        |
|  parse(yaml_text) -> CommentedMap      |
|  validate(map) -> list[ErrorDict]      |
|  save(handle, req) -> Snapshot         |
|    ├ mtime + SHA-256 CAS check         |
|    ├ secret-preserve merge             |
|    └ atomic write (fsync + dir-fsync)  |
|  reset_beets_globals(handle)           |
|  apply(app, settings) -> Snapshot      |
+----------------------------------------+
```

No new beets imports leak outside `app/beets/`. The adapter boundary holds.

---

## Layer 3 — Backend: Save flow

### New endpoint: `POST /api/config/save`

**Request body (`app/models/config_editor.py`):**
```python
class SaveRequest(BaseModel):
    yaml_text: str
    base_mtime_ns: int       # echoed from the snapshot the client loaded
    base_sha256: str         # echoed from the snapshot the client loaded
```

**Responses:**
- **200** — `BeetsConfigSnapshot` (with `apply_pending: true`)
- **409** — `{ detail, current_snapshot, current_yaml_text, current_sha256 }` — CAS conflict; UI shows diff
- **422** — `{ detail: list[ErrorDict] }` — YAML parse error OR Pydantic validation error

### Sequence (`app/beets/config_editor.py`)

```python
async def save(handle: LibraryHandle, req: SaveRequest) -> BeetsConfigSnapshot:
    # 1. Parse with ruamel — defaults to typ='rt' (round-trip), per yaml.dev maintainer docs
    yaml = YAML()                    # default round-trip
    yaml.version = (1, 1)            # `yes`/`no` parse as bool (SF #285)
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096

    try:
        new_map = yaml.load(req.yaml_text)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise HTTPException(422, {
            "detail": [{"loc": "", "msg": str(e),
                        "line": mark.line if mark else None,
                        "column": mark.column if mark else None}]
        })

    # 2. Schema validate (Pydantic v2). Use the documented `loc_to_dot_sep` helper
    # (vendored from pydantic.dev/.../errors/errors/). Unknown keys are ignored
    # via the default `extra='ignore'` — they live on disk in the CommentedMap.
    errors = validate_known_keys(new_map)
    if errors:
        raise HTTPException(422, {"detail": errors})

    # 3. mtime + SHA-256 CAS — both must match what the client loaded.
    # Use st_mtime_ns (int) not st_mtime (float) — see CPython bpo-39484.
    on_disk_bytes = handle.config_path.read_bytes()
    on_disk_mtime_ns = handle.config_path.stat().st_mtime_ns
    on_disk_sha = hashlib.sha256(on_disk_bytes).hexdigest()
    if on_disk_mtime_ns != req.base_mtime_ns or on_disk_sha != req.base_sha256:
        snap = build_config_snapshot(handle)
        raise HTTPException(409, {
            "detail": "File changed on disk",
            "current_snapshot": snap,
            "current_yaml_text": on_disk_bytes.decode("utf-8"),
            "current_sha256": on_disk_sha,
        })

    # 4. Secret-preserve merge — if a key whose snapshot value was REDACTED is
    # unchanged in the new YAML, copy the on-disk value back into new_map.
    on_disk_map = yaml.load(on_disk_bytes.decode("utf-8"))
    for path in find_redacted_paths(on_disk_map):
        if walk_get(new_map, path) == REDACTED_TOMBSTONE:
            walk_set(new_map, path, walk_get(on_disk_map, path))

    # 5. Atomic write — fsync the tempfile, copystat (mode), os.replace,
    # then fsync the parent directory. Per Dan Luu & LWN: dir-fsync is
    # required for crash safety on ext4. atomicwrites is deprecated by its
    # own author in favor of this recipe.
    atomic_write(handle.config_path, new_map, yaml)

    # 6. Return new snapshot — apply_pending derives from mtime, so True now.
    return build_config_snapshot(handle)
```

### `atomic_write` (`app/beets/config_editor.py`)

```python
def atomic_write(dst: Path, data: CommentedMap, yaml: YAML) -> None:
    tmp = dst.parent / f".{dst.name}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f)
        f.flush()
        os.fsync(f.fileno())                # data durable on disk

    if dst.exists():
        shutil.copystat(dst, tmp)           # preserve mode/atime/mtime/flags/xattrs
                                            # NOT uid/gid (per shutil docs); ok since
                                            # MusicDrop owns its own config file
    else:
        os.chmod(tmp, 0o644)                # first-write fallback

    os.replace(tmp, dst)                    # atomic on POSIX same-FS rename

    dir_fd = os.open(dst.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)                    # otherwise rename can be lost on
                                            # power-cut even on ext4 (Dan Luu)
    finally:
        os.close(dir_fd)
```

### `validate_known_keys` (`app/beets/config_editor.py`)

```python
def validate_known_keys(parsed_yaml: dict) -> list[dict]:
    try:
        KnownKeysSchema.model_validate(parsed_yaml)
        return []
    except ValidationError as e:
        return [
            {
                "loc": loc_to_dot_sep(err["loc"]),
                "msg": err["msg"],
                "type": err["type"],
            }
            for err in e.errors()
        ]
```

### Pydantic schema (`app/models/config_editor.py`)

Per [Pydantic v2 docs](https://pydantic.dev/docs/validation/latest/concepts/models/): the default `extra='ignore'` is what we want. Unknown beets keys (per-plugin sub-configs etc.) are dropped at the Pydantic layer — but they're never written from Pydantic, only validated. The on-disk YAML keeps them via the ruamel `CommentedMap`.

```python
# loc_to_dot_sep vendored verbatim from pydantic.dev/.../errors/errors/
def loc_to_dot_sep(loc: tuple[str | int, ...]) -> str:
    path = ""
    for i, x in enumerate(loc):
        if isinstance(x, str):
            if i > 0:
                path += "."
            path += x
        elif isinstance(x, int):
            path += f"[{x}]"
        else:                                # pragma: no cover
            raise TypeError("Unexpected type")
    return path


def _writable_parent(p: Path) -> Path:
    parent = p.expanduser().resolve().parent
    if not parent.exists() or not os.access(parent, os.W_OK):
        raise ValueError(f"parent directory {parent} is not writable")
    return p


WritablePath = Annotated[Path, AfterValidator(_writable_parent)]


PluginName = Literal[
    "musicbrainz", "deezer", "spotify", "discogs", "beatport",
    "tidal", "lyrics", "fetchart", "chroma", "lastgenre", "embedart",
    "replaygain", "scrub",
]


class ImportSection(BaseModel):
    model_config = ConfigDict(extra="ignore")     # default; explicit for clarity
    copy: bool = True
    move: bool = False
    write: bool = True
    autotag: bool = True
    singletons: bool = False
    incremental: bool = False
    duplicate_action: Literal["skip", "keep", "remove", "merge", "ask"] = "ask"


class MatchSection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    strong_rec_thresh: float = Field(default=0.04, ge=0.0, le=1.0)
    medium_rec_thresh: float = Field(default=0.25, ge=0.0, le=1.0)


class KnownKeysSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")
    directory: WritablePath
    library: Path
    plugins: list[PluginName] = Field(default_factory=list)
    import_: ImportSection = Field(default_factory=ImportSection, alias="import")
    match: MatchSection = Field(default_factory=MatchSection)
```

### Modules touched

- **New:** `backend/app/beets/config_editor.py` — save, validate_known_keys, find_redacted_paths (extracted), walk_get/walk_set, atomic_write, reset_beets_globals (Section 4), apply.
- **New:** `backend/app/models/config_editor.py` — `SaveRequest`, `KnownKeysSchema`, `ImportSection`, `MatchSection`, `loc_to_dot_sep`, error shape `ValidationErrorItem`.
- **Extended:** `backend/app/api/config_.py` — three new routes (`POST /validate`, `POST /save`, `POST /apply`).
- **Extended:** `backend/app/beets/config_snapshot.py` — adds `sha256: str` and `mtime_ns: int` to `BeetsConfigSnapshot`; `find_redacted_paths` extracted as shared helper.
- **Extended:** `backend/app/models/config_api.py` — `BeetsConfigSnapshot` gains `sha256`, `mtime_ns`; renames `restart_required` → `apply_pending` (semantics unchanged, name clarified).
- **Extended:** `backend/app/main.py` — lifespan creates `app.state.beets_swap_lock = asyncio.Lock()`.

### Error shape (`POST /save`)

```json
{
  "detail": [
    {"loc": "import.copy", "msg": "Input should be a valid boolean", "type": "bool_parsing"},
    {"loc": "match.strong_rec_thresh", "msg": "Input should be less than or equal to 1", "type": "less_than_equal"}
  ]
}
```

The frontend uses CodeMirror's `linter()` async source to map each `loc` to a line/column via ruamel `.lc.value(...)` walk (Section "Frontend"). 0-based line numbers from ruamel; add `+1` for CodeMirror's 1-based `state.doc.line(n)`.

---

## Layer 3 — Backend: Apply flow

### New endpoint: `POST /api/config/apply`

**Request body:** empty.

**Responses:**
- **200** — `BeetsConfigSnapshot` (with `apply_pending: false`)
- **409** — `{ detail }` when `has_active_job()` returns True
- **500** — `{ detail, recovery }` if `setup_beets()` raises during rebuild

### Sequence (`app/beets/config_editor.py`)

```python
async def apply(request: Request) -> BeetsConfigSnapshot:
    app = request.app
    registry: ImportJobRegistry = app.state.import_registry

    # 1. Gate on active import. Use the existing single-slot API.
    # _ACTIVE_PHASES = {scanning, reviewing, applying} per registry.py.
    if registry.has_active_job():
        raise HTTPException(409, {"detail": "Import in progress — Apply available when it finishes"})

    # 2. Acquire swap lock. FastAPI/Starlette docs don't mandate this but it's
    # required for correctness (two concurrent /apply calls would race).
    async with app.state.beets_swap_lock:
        old_handle: LibraryHandle = app.state.beets_library
        settings: Settings = app.state.settings

        # 3. Offload the BLOCKING rebuild to FastAPI's threadpool.
        # Per fastapi.tiangolo.com/async/: `def`-style work goes here.
        try:
            new_handle = await run_in_threadpool(
                _rebuild_beets_handle, old_handle, settings.beets_dir
            )
        except Exception as e:
            # No rollback — beets globals are torn down. Process degraded.
            # Save-time validation makes this vanishingly rare; recovery is restart.
            raise HTTPException(500, {
                "detail": f"Apply failed during rebuild: {e}",
                "recovery": "Restart MusicDrop. The saved config on disk is the new one; cold start will load it.",
            })

        # 4. Atomic swap (single attribute set; Python FAQ guarantees atomicity).
        app.state.beets_library = new_handle

    return build_config_snapshot(new_handle)


def _rebuild_beets_handle(old_handle: LibraryHandle, beets_dir: str) -> LibraryHandle:
    """Blocking; must NOT run in the event loop."""
    reset_beets_globals(old_handle)
    return setup_beets(beets_dir)
```

### `reset_beets_globals` (`app/beets/setup.py`)

The cleanup is factored from the existing conftest autouse fixture. Seven explicit clears, all required (verified against beets 2.11 source). All are private APIs with upstream TODOs targeting beets 3.0.0 — that's why we pin `beets==2.11.*`.

```python
def reset_beets_globals(handle: LibraryHandle) -> None:
    """Teardown all beets/confuse/plugins process-global state.
    Mirrors beets' own `unload_plugins` (beets/test/helper.py:460-466).

    THIS IS A BEETS-2.11-PINNED COMPATIBILITY SHIM. Beets 3.x will require
    revisiting — see TODO at upstream beets/plugins.py:455 and PR #5887.
    """
    import beets
    from beets import metadata_plugins, plugins
    from beets.plugins import BeetsPlugin

    with suppress(Exception):
        close_library(handle.lib)

    # confuse: truncate sources + re-arm LazyConfig
    beets.config.clear()
    beets.config._materialized = False                       # private; required

    # beets plugins teardown — same set as beets' test helper
    plugins._instances.clear()
    BeetsPlugin.listeners.clear()                            # event handlers
    BeetsPlugin._raw_listeners.clear()                       # backing dict

    # all three @cache decorators in metadata_plugins.py
    metadata_plugins.find_metadata_source_plugins.cache_clear()
    metadata_plugins.get_metadata_source.cache_clear()
    metadata_plugins.get_penalty.cache_clear()
```

The conftest autouse fixture (`backend/tests/conftest.py:_clear_beets_globals`) becomes a thin wrapper: env-var snapshot/restore around `reset_beets_globals()`. DRY win — production and tests share the teardown.

### Why no rollback

Beets' `config`, `plugins._instances`, and the `BEETSDIR` env are **process-globals** — you cannot hold two `LibraryHandle`s simultaneously. "Build new before teardown" is impossible without a subprocess fork. Rebuild failures are made vanishingly rare by:

- **Save-time YAML parse** — file is known-good when Apply runs.
- **Save-time Pydantic validation** — known-key types are right.
- **Plugin allowlist in `KnownKeysSchema.plugins`** — `plugins.load_plugins()` won't fail on unknown names.

The 500 response includes a `recovery` hint pointing at process restart.

### Import-in-flight gating

The existing `ImportJobRegistry` (`backend/app/import_jobs/registry.py:38, 98-100`) is single-slot: `has_active_job() -> bool` returns True when the job's phase is in `_ACTIVE_PHASES = {scanning, reviewing, applying}`. Apply checks this first; if True, returns 409 with a human-readable message.

This is **mandatory, not defensive** — `WebImportSession.run_import_worker` *writes* `config["threaded"] = False` at run start (`backend/app/beets/import_session.py:296`). Mid-import swap would lose that write.

### `get_library` dependency

[Starlette Requests](https://www.starlette.io/requests/) blesses `request.app.state.x`; FastAPI docs bless `Depends(...)` for shared resources. Every handler reads through:

```python
def get_library(request: Request) -> LibraryHandle:
    return request.app.state.beets_library
```

This **resolves fresh per-request** — so post-swap, an in-flight request that has already captured `handle` keeps using the old one (consistent within its transaction), and new requests pick up the new handle.

---

## Layer 3 — Frontend

### Files

- `frontend/src/api/useBeetsConfig.ts` — extended: gains `useSaveConfig()` + `useApplyConfig()` + `useValidateConfig()` mutations.
- `frontend/src/api/useActiveImport.ts` — new tiny hook polling `has_active_job` for the Apply button's gating.
- `frontend/src/pages/settings/SettingsPage.tsx` — extended with the CodeMirror editor + state machine + buttons + diff modal.
- `frontend/src/pages/settings/SettingsConflict.tsx` — new component wrapping `@codemirror/merge` for the 409 view.
- `frontend/src/pages/settings/SettingsPage.test.tsx` — extended with the new flows.
- `frontend/src/api/schema.d.ts` + `frontend/openapi.json` — regenerated.

### New deps (`frontend/package.json`)

```
"@uiw/react-codemirror": "^4.25",
"@codemirror/lang-yaml": "^6.1",
"@codemirror/lint": "^6.9",
"@codemirror/merge": "^6.12",
```

(Pure CM6, no Monaco; ~150 kB gzipped total per [bundlephobia](https://bundlephobia.com/api/size?package=codemirror).)

### Editor recipe (`SettingsPage.tsx`)

Composition order matters — `basicSetup` registers a default keymap that our Mod-s must out-rank via `Prec.high`:

```ts
const editableCompartment = new Compartment();
const themeCompartment = new Compartment();

const initialDoc = snapshot.yaml_text;

const view = new EditorView({
  state: EditorState.create({
    doc: initialDoc,
    extensions: [
      basicSetup,                                          // history, default keymap
      yaml(),                                              // language
      lintGutter(),                                        // gutter dots
      linter(async (view) => {                             // debounced backend lint
        const text = view.state.doc.toString();
        try {
          await mutate.mutateAsync({ yaml_text: text });   // POST /api/config/validate
          return [];
        } catch (err) {
          return mapBackendErrorsToDiagnostics(err, view);
        }
      }, { delay: 500 }),                                  // built-in debounce
      Prec.high(keymap.of([{
        key: "Mod-s",
        preventDefault: true,
        run: (v) => { void save(v.state.doc.toString()); return true; },
      }])),
      editableCompartment.of([
        EditorState.readOnly.of(true),
        EditorView.editable.of(false),
        EditorView.contentAttributes.of({ tabindex: "0" }),
      ]),
      themeCompartment.of(shadcnTheme),
      EditorView.updateListener.of((u) => {
        if (u.docChanged) setDirty(u.state.doc.toString() !== initialDoc);
      }),
    ],
  }),
  parent: editorRef.current!,
});
```

After a successful Save, call `forceLinting(view)` to clear stale diagnostics.

### Mapping backend errors → CodeMirror Diagnostics

Backend returns `{loc: "import.copy", msg: "...", type: "..."}`. Frontend walks the loaded `CommentedMap` along the dotted path and uses ruamel's `.lc.value(...)` line+col (returned by the validate endpoint as part of the error so the frontend doesn't need ruamel):

```ts
// Server includes pre-resolved line/col in each error
type ValidationErrorItem = {
  loc: string;          // "import.copy"
  msg: string;
  type: string;
  line?: number;        // 1-based, ready for CodeMirror
  column?: number;
};

function mapBackendErrorsToDiagnostics(
  errs: ValidationErrorItem[], view: EditorView
): Diagnostic[] {
  return errs.map(e => {
    const line = view.state.doc.line(e.line ?? 1);
    return {
      from: line.from + (e.column ?? 0),
      to: line.to,
      severity: "error" as const,
      message: `${e.loc}: ${e.msg}`,
    };
  });
}
```

This means the **backend** does the ruamel `.lc.value(...)` walk (using `walk_get(commented_map, loc_path).lc...`) and includes the resolved 1-based line+col in each error. Cleaner contract — frontend doesn't need to parse YAML again.

### Page state machine (`SettingsPage.tsx`)

```
            ┌─────────┐
            │  CLEAN  │  (no edits; Save+Apply disabled)
            └────┬────┘
       click │ Edit
                ▼
            ┌─────────┐
            │  DIRTY  │  (edited; Save primary; banner: "Unsaved changes")
            └────┬────┘
              click │ Save  ────────────► API 422 ────► markers in editor
                    ▼                      API 409 ────► open MergeView
            ┌─────────┐
            │ SAVING  │  (Save in flight)
            └────┬────┘
            success │
                    ▼
            ┌──────────────┐
            │ APPLY_PENDING│  (saved on disk; Apply primary; banner: "Apply to load")
            └────┬─────────┘
              click │ Apply (disabled if has_active_job)
                    ▼
            ┌─────────┐
            │ APPLYING│
            └────┬────┘
            success │  /  failure: degraded notice + restart hint
                    ▼
            ┌─────────┐
            │  CLEAN  │
            └─────────┘
```

### 409 CAS-conflict view (`SettingsConflict.tsx`)

On 409, swap the editor for a `MergeView`:

```ts
new MergeView({
  a: { doc: localBuffer },                                 // user's pending edits
  b: { doc: serverDoc, extensions: [
    EditorState.readOnly.of(true),
    EditorView.editable.of(false),
  ]},
  parent: containerRef.current!,
  revertControls: "b-to-a",
  highlightChanges: true,
  gutter: true,
  collapseUnchanged: {},
});
```

UI shows two buttons: **Reload** (drop local edits, accept server) and **Overwrite anyway** (Save again with the new `base_mtime_ns` + `base_sha256` from the conflict response).

### Header behavior under L3

The existing `apply_pending` (renamed from `restart_required`) flag now drives:

- **Apply button styling** — primary blue if `apply_pending && !has_active_job`, disabled if `has_active_job`.
- **Helper text** under disabled-Apply: "1 import running — Apply available when it finishes."

---

## Error policy summary

| Failure | HTTP | Body | UI surface |
|---|---|---|---|
| YAML parse error | 422 | `{detail: [{loc:"", msg, line, column}]}` | Editor gutter marker |
| Pydantic validation error | 422 | `{detail: list[ValidationErrorItem]}` | Per-key gutter markers |
| mtime or SHA-256 mismatch | 409 | `{detail, current_snapshot, current_yaml_text, current_sha256}` | MergeView modal |
| Import in flight on Apply | 409 | `{detail: "Import in progress — ..."}` | Disabled Apply + helper text |
| `setup_beets()` raises on Apply | 500 | `{detail, recovery: "Restart MusicDrop..."}` | Banner: "Degraded — saved config on disk, restart to recover" |
| Atomic write IOError | 500 | propagated | Generic save-failed banner |

---

## Tests

### Backend (pytest)

**`backend/tests/test_config_editor.py` (new):**

- `test_save_succeeds_against_starter` — happy path: load starter, no edits, Save → 200.
- `test_save_normalizes_yes_no_to_true_false` — pin the maintainer's documented behavior. Post-save, file contains `true`/`false`.
- `test_save_preserves_comments` — comment above a key survives an edit elsewhere.
- `test_save_preserves_blank_lines` — caveat-test; expect ruamel's documented "after block scalars" preservation.
- `test_save_preserves_secret_when_unchanged` — set spotify.client_secret on disk; save with editor showing REDACTED at that path; verify on-disk value untouched.
- `test_save_overwrites_secret_when_user_typed_new_value` — same but the editor's value is `"new-secret-123"`; verify on-disk value updated.
- `test_save_drops_secret_when_user_deleted_key` — verify intentional delete works.
- `test_save_422_on_invalid_yaml` — write `this: is: not [valid`; expect 422 with line/column.
- `test_save_422_on_schema_error_known_key` — `import.copy: maybe`; expect 422 with `loc="import.copy"`, valid `line`/`column`.
- `test_save_422_with_pre_resolved_line_col` — verify backend walks ruamel `.lc.value(...)` and returns 1-based positions.
- `test_save_passes_unknown_keys_through` — set `myplugin.weird_key: 42`; verify saved file still has it (ruamel CommentedMap preserves; Pydantic `extra='ignore'` doesn't reject).
- `test_save_409_on_mtime_change` — change file mtime between GET and Save; expect 409 with current snapshot + current_yaml_text + current_sha256.
- `test_save_409_on_sha_change_same_mtime` — bytes change but mtime preserved via `os.utime`; expect 409 (SHA-256 tie-breaker catches it).
- `test_atomic_write_preserves_mode` — pre-existing config with mode 0o600; save; assert mode unchanged.
- `test_atomic_write_first_time_chmods_644` — delete file before save; verify mode 0o644 (defensive — shouldn't happen in production).

**`backend/tests/test_apply.py` (new):**

- `test_apply_succeeds_end_to_end` — modify file, call apply, verify `app.state.beets_library` is a new handle, `apply_pending` is False.
- `test_apply_409_when_import_active` — start a fake job, call apply, expect 409 with helpful detail.
- `test_apply_500_when_setup_beets_raises` — monkeypatch `setup_beets` to raise; verify 500 with `recovery` hint.
- `test_apply_holds_lock_during_swap` — concurrent `apply()` calls serialize via the asyncio lock.
- `test_apply_uses_threadpool_for_rebuild` — `run_in_threadpool` is called for the blocking work (mockable via `monkeypatch`).
- `test_get_library_dependency_returns_fresh_handle_post_swap` — call apply, then verify `get_library(request)` returns the new handle.

**`backend/tests/test_reset_beets_globals.py` (new):**

Post-reset invariants — pinned because every operation touches a private API:

- `_instances == []`
- `BeetsPlugin.listeners == {}`
- `BeetsPlugin._raw_listeners == {}`
- `beets.config._materialized is False`
- `metadata_plugins.find_metadata_source_plugins.cache_info().currsize == 0`
- `metadata_plugins.get_metadata_source.cache_info().currsize == 0`
- `metadata_plugins.get_penalty.cache_info().currsize == 0`
- After reset, calling `setup_beets()` produces a `LibraryHandle` that resolves a fresh metadata-source plugin list (loads musicbrainz + deezer from the starter).

### Frontend (Vitest + RTL)

**`frontend/src/pages/settings/SettingsPage.test.tsx` (extended):**

- `clicking Edit unlocks the editor` (Compartment reconfigure pinned).
- `typing marks the page dirty` — banner appears.
- `Mod-s triggers Save` — keymap is `Prec.high`, basicSetup default doesn't intercept.
- `Save success transitions to apply-pending state` — Apply button becomes primary.
- `Apply success refetches snapshot and returns to clean` — `apply_pending` is false.
- `Apply disabled while import is active` — useActiveImport returns true, button is disabled with helper text.
- `409 opens the MergeView modal` — fixture returns 409 with `current_yaml_text`, modal opens with two-pane diff.
- `Reload from MergeView drops local edits` — local buffer replaced with server doc.
- `Overwrite anyway re-saves with new base_mtime_ns/base_sha256` — fixture verifies the second Save request has updated CAS tokens.
- `422 from Save renders inline gutter markers` — assert presence of CodeMirror lint markers at expected lines.

---

## Versioning + dependencies

### Backend (`backend/pyproject.toml`)

```diff
 dependencies = [
-  "beets>=2.11.0",
+  "beets==2.11.*",            # private hooks; see Section 4
+  "ruamel.yaml>=0.15.93",     # y/Y/n/N YAML 1.1 booleans (SF #285)
   ...
 ]
```

The pin is mandatory, not preferential — `BeetsPlugin._raw_listeners`, `_materialized`, etc., have upstream TODOs for refactor in beets 3.0.0.

### Frontend (`frontend/package.json`)

```diff
+  "@uiw/react-codemirror": "^4.25",
+  "@codemirror/lang-yaml": "^6.1",
+  "@codemirror/lint": "^6.9",
+  "@codemirror/merge": "^6.12",
```

---

## Ship plan

- **Branch:** `feat/beets-config-editor` off `main`.
- **One PR** — slice is ~15-20 files, smaller than the L1+L2 slice we just shipped.
- **Same flow:** feature branch → push → PR → squash-merge → delete. User pushes, creates, and merges.
- **Pre-merge checklist:**
  - `uv run pytest` green (backend, all suites)
  - `uv run mypy` clean (`--strict`)
  - `uv run ruff check && uv run ruff format --check` clean
  - `npm typecheck && npm test && npm run build` clean
  - Live walkthrough: type a bool flip + comment in `/settings`, Save → banner; Apply → banner clears; verify new behavior on next import. Test 409 by editing the file in `$EDITOR` while the tab is open. Test 422 by typing `import.copy: maybe`.

---

## Out of scope (deferred follow-ups)

- **Layer 4 — Curated form panel** on top of the raw editor for the ~13 high-frequency keys (booleans, dropdowns, plugin checkboxes). The raw editor stays as the escape hatch.
- **Per-plugin credential management UI** — guided OAuth-style flows for Spotify/Discogs/Beatport/Tidal. v1 = paste secret into raw YAML.
- **YAML 1.2 migration** — let the user opt their file into 1.2 (drops `yes`/`no` as bool). Beets/PyYAML default is 1.1; this would be a multi-step user-facing migration. Not v1.
- **YAML version directive (`%YAML 1.1`)** — if the user adds one, honor it. v1 just assumes 1.1.
- **Diff-preview on Save** — show "here's what changes on disk" before clicking Save. Nice-to-have.
- **Undo across sessions** — `Cmd-Z` works within a session (basicSetup history); cross-session undo would need a `.previous` backup file. Not v1.
- **Form ↔ YAML two-way sync** — Layer 4 territory.

---

## Open questions resolved during brainstorm

| Q | Decision | Authority |
|---|---|---|
| Editor library | CodeMirror 6 + @uiw/react-codemirror | Bundle size (~150 kB vs Monaco ~1.2 MB) |
| YAML library for write | ruamel.yaml (write only); keep PyYAML for read | Maintainer-blessed round-trip pattern |
| Boolean preservation | Accept normalization to `true`/`false` | ruamel maintainer: *"emitted booleans always as true resp. false"* |
| Pydantic `extra` setting | `extra='ignore'` (default) | We validate, don't serialize — unknowns live in CommentedMap, not the model |
| `loc_to_dot_sep` source | Vendor from official docs (not exported) | Pydantic Errors page |
| CodeMirror lint debounce | Built-in `linter(source, {delay: 500})` | CodeMirror reference manual |
| Save shortcut | `Prec.high(keymap.of([{key: "Mod-s"}]))` | CodeMirror system guide |
| Read-only ↔ editable toggle | `Compartment` + `reconfigure` | CodeMirror Configuration example |
| CAS conflict UI | `@codemirror/merge` `MergeView` | Reuses already-loaded CM6 deps; ~16-20 kB marginal |
| Apply handler shape | `async def` + `asyncio.Lock` + `run_in_threadpool` | FastAPI async docs + Starlette concurrency notes |
| `app.state` access | `Depends(get_library)` reading `request.app.state.x` | Starlette Requests doc pattern |
| Concurrency token | mtime_ns + SHA-256 (both fields) | apenwarr's mtime caveats + cheap hash hedge |
| File write recipe | tempfile + fsync + copystat + os.replace + dir-fsync | Python docs + Dan Luu + LWN; `atomicwrites` deprecated by maintainer |
| beets reset recipe | 7 explicit clears mirroring beets' own `unload_plugins` | beets/test/helper.py:460-466 |
| Import-active check | `registry.has_active_job()` (single-slot) | Existing `backend/app/import_jobs/registry.py:38, 98-100` |
| Versions | `beets==2.11.*`, `ruamel.yaml>=0.15.93` | Private-hook compatibility + boolean-parsing fix |

---

## Primary sources cited

**YAML:**
- [YAML 1.2.2 spec](https://yaml.org/spec/1.2.2/) — canonical reference
- [YAML 1.2.2 changelog](https://yaml.org/spec/1.2.2/ext/changes/) — `yes`/`no` removed from bool tag
- [YAML 1.1 bool type](https://yaml.org/type/bool.html) — legacy regex (`y|Y|yes|Yes|...`)

**ruamel.yaml (Anthon van der Neut):**
- [yaml.dev/doc/ruamel.yaml/](https://yaml.dev/doc/ruamel.yaml/) — maintainer's authoritative site
- [yaml.dev/doc/ruamel.yaml/detail/](https://yaml.dev/doc/ruamel.yaml/detail/) — boolean emit invariant
- [yaml.dev/doc/ruamel.yaml/overview/](https://yaml.dev/doc/ruamel.yaml/overview/) — round-trip preservation guarantees
- [yaml.dev/doc/ruamel.yaml/example/](https://yaml.dev/doc/ruamel.yaml/example/) — canonical edit-a-config recipe
- [SF ticket #285](https://sourceforge.net/p/ruamel-yaml/tickets/285/) — YAML 1.1 `y`/`Y`/`n`/`N` fixed in 0.15.93

**Python stdlib:**
- [os.replace](https://docs.python.org/3/library/os.html#os.replace) — POSIX atomicity
- [shutil.copystat](https://docs.python.org/3/library/shutil.html#shutil.copystat) — mode + xattr preservation
- [os.stat_result.st_mtime_ns](https://docs.python.org/3/library/os.html#os.stat_result.st_mtime_ns) — nanosecond mtime
- [Python FAQ — atomic ops](https://docs.python.org/3/faq/library.html#what-kinds-of-global-value-mutation-are-thread-safe)
- [CPython bpo-39484](https://bugs.python.org/issue39484) — `st_mtime` float precision loss

**Linux:**
- [man 7 inode](https://man7.org/linux/man-pages/man7/inode.7.html) — ext4 ns mtime since 2.6.23
- [LWN: That massive filesystem thread](https://lwn.net/Articles/322823/) — fsync semantics
- [Dan Luu — Files are hard](https://danluu.com/file-consistency/) — parent-directory fsync requirement
- [apenwarr — mtime considered harmful](https://apenwarr.ca/log/20181113) — mtime CAS edge cases

**Pydantic (Samuel Colvin):**
- [docs.pydantic.dev/latest/concepts/models/](https://docs.pydantic.dev/latest/concepts/models/) — `extra` defaults
- [docs.pydantic.dev/latest/concepts/validators/](https://docs.pydantic.dev/latest/concepts/validators/) — `AfterValidator` over `BeforeValidator`
- [docs.pydantic.dev/latest/concepts/fields/](https://docs.pydantic.dev/latest/concepts/fields/) — `Field(ge=…, le=…)`
- [docs.pydantic.dev/latest/errors/errors/](https://docs.pydantic.dev/latest/errors/errors/) — `loc_to_dot_sep` example
- [docs.pydantic.dev/latest/concepts/performance/](https://docs.pydantic.dev/latest/concepts/performance/) — `model_validate(dict)` vs `model_validate_json(str)`
- [Pydantic #6828](https://github.com/pydantic/pydantic/issues/6828) — `extra` doesn't propagate transitively

**FastAPI / Starlette:**
- [fastapi.tiangolo.com/advanced/events/](https://fastapi.tiangolo.com/advanced/events/) — lifespan
- [fastapi.tiangolo.com/async/](https://fastapi.tiangolo.com/async/) — `def`-handlers run in threadpool
- [fastapi.tiangolo.com/tutorial/background-tasks/](https://fastapi.tiangolo.com/tutorial/background-tasks/) — BackgroundTasks semantics
- [starlette.io/applications/](https://www.starlette.io/applications/) — `app.state`
- [starlette.io/requests/](https://www.starlette.io/requests/) — `request.app.state`
- [starlette.io/lifespan/](https://www.starlette.io/lifespan/) — teardown ordering

**CodeMirror 6 (Marijn Haverbeke):**
- [codemirror.net/docs/ref/](https://codemirror.net/docs/ref/) — reference manual
- [codemirror.net/docs/guide/](https://codemirror.net/docs/guide/) — system guide (precedence, Compartment)
- [codemirror.net/examples/lint/](https://codemirror.net/examples/lint/) — async source, `lintGutter()`, actions
- [codemirror.net/examples/readonly/](https://codemirror.net/examples/readonly/) — readonly + Compartment
- [codemirror.net/examples/config/](https://codemirror.net/examples/config/) — Compartment toggle pattern
- [github.com/codemirror/merge](https://github.com/codemirror/merge) — MergeView, `revertControls`

**Beets internals (verified against pinned venv source):**
- `beets/test/helper.py:460-466` — `unload_plugins` teardown (mirrored by `reset_beets_globals`)
- `beets/metadata_plugins.py:158-168` — three `@cache` decorators
- `beets/plugins.py:480` — `_instances` list
- `beets/__init__.py:52` — `config = IncludeLazyConfig(...)` (process global)
- `confuse/core.py:421-426, 749-753` — `Configuration.clear` + `LazyConfig.clear` + `_materialized`
- [PR #5887](https://github.com/beetbox/beets/pull/5887) — beets 3.0.0 plugin-loading refactor
- [confuse #138](https://github.com/beetbox/confuse/issues/138) — hot-reload unsanctioned

---

## Net assessment

The design is now **smaller, simpler, and better-grounded** than the initial four-section sketch. The canonical-sources review collapsed seven would-be-clever moves into doc-blessed defaults (`extra='ignore'`, default `YAML()`, accept bool normalization, built-in lint debounce, etc.) and surfaced three real correctness issues (missing `BeetsPlugin.listeners` clear, missing parent-dir fsync, mtime float precision loss). Every load-bearing technical claim is now cited.

The five brainstorm decisions (raw editor / one-click Apply / REDACTED inline / Pydantic schema validation / mtime CAS) all hold without modification.
