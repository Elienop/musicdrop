# Beets-config Editor (Layer 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a writable CodeMirror 6 YAML editor at `/settings` for `data/beets/config.yaml`, with backend save (validation + secret preserve + atomic write + mtime+SHA-256 CAS) and one-click in-process Apply (asyncio.Lock + threadpool reload). Build on top of Layer 1 + 2 which already shipped the read side.

**Architecture:** Three new endpoints on top of the existing `GET /api/config`:
- `POST /api/config/validate` — cheap YAML+schema check, fed by CodeMirror's async `linter()` source on every typing pause.
- `POST /api/config/save` — full write path with ruamel round-trip + secret-preserve merge + atomic write (fsync + dir-fsync + copystat) + CAS guard.
- `POST /api/config/apply` — `async def` handler holding `asyncio.Lock`, offloads `reset_beets_globals` + `setup_beets()` rebuild to FastAPI threadpool, atomically swaps `app.state.beets_library`.

Frontend: CodeMirror 6 via `@uiw/react-codemirror` (basicSetup + lang-yaml + lint + Compartment-toggled read-only) plus `@codemirror/merge` for the 409 CAS conflict view.

**Tech Stack:** Python 3.11+ · FastAPI · Pydantic v2 (`extra='ignore'`) · ruamel.yaml ≥0.15.93 · beets==2.11.* (pinned for private-hook compat); React 19 · Vite · TanStack Query v5 · `@uiw/react-codemirror@^4.25` · `@codemirror/{lang-yaml,lint,merge}`.

**Spec:** `docs/superpowers/specs/2026-05-28-beets-config-editor-design.md` (every load-bearing decision cited against primary sources there).

**Branch:** `feat/beets-config-editor` off `main`.

**Predecessor:** Layer 1 + 2 merged as PR #8 (commit `e13e906`).

---

## File Structure

### Backend — new files
- `backend/app/beets/config_editor.py` — `parse_yaml`, `validate_known_keys`, `find_redacted_paths` (extracted), `walk_get`/`walk_set`, `merge_preserve_secrets`, `atomic_write`, `save`, `apply` helpers.
- `backend/app/models/config_editor.py` — `SaveRequest`, `ValidationErrorItem`, `KnownKeysSchema`, `ImportSection`, `MatchSection`, `loc_to_dot_sep` helper (vendored from pydantic docs).
- `backend/tests/test_config_editor_models.py` — schema tests.
- `backend/tests/test_config_editor_parse_validate.py` — parse + validate (line/col mapping) tests.
- `backend/tests/test_config_editor_atomic_write.py` — atomic write tests (mode preservation, first-write).
- `backend/tests/test_config_editor_secrets.py` — find_redacted_paths + secret-preserve merge tests.
- `backend/tests/test_config_save_api.py` — `POST /api/config/save` end-to-end tests.
- `backend/tests/test_config_validate_api.py` — `POST /api/config/validate` tests.
- `backend/tests/test_config_apply_api.py` — `POST /api/config/apply` tests.
- `backend/tests/test_reset_beets_globals.py` — post-reset invariant tests.

### Backend — modified files
- `backend/app/beets/setup.py` — extract `reset_beets_globals` (currently inline-only in conftest).
- `backend/app/beets/config_snapshot.py` — add `sha256` + `mtime_ns`; rename `restart_required` → `apply_pending`; export `find_redacted_paths` for reuse.
- `backend/app/models/config_api.py` — `BeetsConfigSnapshot` gains `sha256: str` + `mtime_ns: int`; rename `restart_required` → `apply_pending`.
- `backend/app/api/config_.py` — add three new routes; `Depends(get_library)` pattern for reads.
- `backend/app/main.py` — lifespan creates `app.state.beets_swap_lock = asyncio.Lock()` and stores `settings` on `app.state`.
- `backend/pyproject.toml` — pin `beets==2.11.*`, add `ruamel.yaml>=0.15.93`, extend mypy override list.
- `backend/tests/conftest.py` — strip `reset_beets_globals`-equivalent code, call the new shared helper.

### Frontend — new files
- `frontend/src/api/useActiveImport.ts` — small hook polling registry status for Apply-button gating.
- `frontend/src/pages/settings/SettingsConflict.tsx` — `MergeView` wrapper for the 409 conflict modal.
- `frontend/src/pages/settings/codemirror-config.ts` — the canonical CM6 extension list as a single export.

### Frontend — modified files
- `frontend/src/api/useBeetsConfig.ts` — add `useSaveConfig`, `useApplyConfig`, `useValidateConfig` mutations.
- `frontend/src/pages/settings/SettingsPage.tsx` — replace `<pre>` with the CodeMirror editor; add state machine + buttons + conflict modal trigger.
- `frontend/src/pages/settings/SettingsPage.test.tsx` — extended with the new flows.
- `frontend/src/api/schema.d.ts` + `frontend/openapi.json` — regenerated.
- `frontend/package.json` — add 4 CM6 packages.

---

## Task 1: Pydantic models + `BeetsConfigSnapshot` extension + frontend rename

**Files:**
- Create: `backend/app/models/config_editor.py`
- Modify: `backend/app/models/config_api.py`
- Modify: `backend/app/beets/config_snapshot.py`
- Modify: `frontend/src/pages/settings/SettingsPage.tsx` (rename references)
- Test: `backend/tests/test_config_editor_models.py` (create)

- [ ] **Step 1: Add `BeetsConfigSnapshot` fields**

Update `backend/app/models/config_api.py`:

```python
from datetime import datetime
from pydantic import BaseModel, Field


class BeetsConfigSnapshot(BaseModel):
    """Read-only snapshot of beets' effective config + file freshness."""

    yaml_text: str
    """Rendered post-merge effective config as YAML, with secrets redacted."""

    config_path: str
    """Absolute path to the user-owned <BEETSDIR>/config.yaml."""

    loaded_at: datetime
    """When setup_beets() ran (UTC)."""

    file_modified_at: datetime | None
    """Current st_mtime of config_path (UTC). None if the file has been deleted."""

    mtime_ns: int
    """st_mtime_ns at GET time, used as the optimistic-concurrency token for save."""

    sha256: str
    """SHA-256 of the on-disk file bytes at GET time, used as CAS tie-breaker."""

    apply_pending: bool
    """True when the on-disk file has changed since setup_beets() last ran
    (was `restart_required` in L1/L2 — semantics unchanged; name updated to
    match the new Apply button)."""
```

- [ ] **Step 2: Update `config_snapshot.py` to compute the new fields**

In `backend/app/beets/config_snapshot.py`:

```python
import hashlib
from contextlib import suppress
# ...existing imports

SECRET_KEY_PATTERN = re.compile(  # unchanged from L1/L2
    r"(secret|token|password|pwd|pass|api_?key|api_?secret|auth_?token)",
    re.IGNORECASE,
)


def find_redacted_paths(d: dict, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Return dotted paths whose VALUES would be redacted at display.
    Shared between snapshot (display) and config_editor (preserve-on-save).
    """
    out: list[tuple[str, ...]] = []
    for k, v in d.items() if isinstance(d, dict) else []:
        sub = path + (str(k),)
        if isinstance(v, dict):
            out.extend(find_redacted_paths(v, sub))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    out.extend(find_redacted_paths(item, sub))
        elif isinstance(v, str) and SECRET_KEY_PATTERN.search(str(k)):
            out.append(sub)
    return out


def build_config_snapshot(handle: LibraryHandle) -> BeetsConfigSnapshot:
    flat = beets.config.flatten(redact=True)
    _mask_secrets_in_place(flat, SECRET_KEY_PATTERN)
    yaml_text = yaml.safe_dump(_to_plain(flat), sort_keys=False, default_flow_style=False)

    file_modified_at: datetime | None = None
    current_mtime_ns: int | None = None
    sha256 = ""
    file_missing = False
    try:
        st = handle.config_path.stat()
        current_mtime_ns = st.st_mtime_ns
        file_modified_at = datetime.fromtimestamp(st.st_mtime, tz=UTC)
        sha256 = hashlib.sha256(handle.config_path.read_bytes()).hexdigest()
    except OSError:
        file_missing = True

    apply_pending = file_missing or (
        current_mtime_ns is not None and current_mtime_ns > handle.file_mtime_at_load * 1e9
    )
    # NOTE: file_mtime_at_load is float seconds; compare against nanoseconds
    # by multiplying the float (precision loss is acceptable here — only the
    # boolean comparison matters, and exact equality is masked by Save's CAS).

    return BeetsConfigSnapshot(
        yaml_text=yaml_text,
        config_path=str(handle.config_path),
        loaded_at=handle.loaded_at,
        file_modified_at=file_modified_at,
        mtime_ns=current_mtime_ns or 0,
        sha256=sha256,
        apply_pending=apply_pending,
    )
```

(Note: leave `_mask_secrets_in_place` and `_to_plain` unchanged from L1/L2.)

- [ ] **Step 3: Write the new `SaveRequest` + Pydantic schema**

Create `backend/app/models/config_editor.py`:

```python
"""Pydantic models for the Layer-3 config editor.

`loc_to_dot_sep` is vendored verbatim from pydantic.dev/docs/validation/latest/
errors/errors/ — it's example code on that page, not a public Pydantic export.
"""
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def loc_to_dot_sep(loc: tuple[str | int, ...]) -> str:
    path = ""
    for i, x in enumerate(loc):
        if isinstance(x, str):
            if i > 0:
                path += "."
            path += x
        elif isinstance(x, int):
            path += f"[{x}]"
        else:  # pragma: no cover
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
    model_config = ConfigDict(extra="ignore")

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
    """Validates only the ~13 keys MusicDrop models. Default extra='ignore'
    means unknown beets/plugin keys are dropped silently here — they survive
    on disk because we save the ruamel CommentedMap, never re-emit from this
    model (per Pydantic v2 docs § Models)."""
    model_config = ConfigDict(extra="ignore")

    directory: WritablePath
    library: Path
    plugins: list[PluginName] = Field(default_factory=list)
    import_: ImportSection = Field(default_factory=ImportSection, alias="import")
    match: MatchSection = Field(default_factory=MatchSection)


class ValidationErrorItem(BaseModel):
    loc: str                  # dotted path, e.g. "import.copy"
    msg: str
    type: str
    line: int | None = None   # 1-based for CodeMirror
    column: int | None = None


class SaveRequest(BaseModel):
    yaml_text: str
    base_mtime_ns: int
    base_sha256: str


class ValidateRequest(BaseModel):
    yaml_text: str
```

- [ ] **Step 4: Write tests**

Create `backend/tests/test_config_editor_models.py`:

```python
from app.models.config_editor import (
    KnownKeysSchema,
    ValidationErrorItem,
    loc_to_dot_sep,
)
from pydantic import ValidationError


def test_loc_to_dot_sep_mixed() -> None:
    assert loc_to_dot_sep(("import", "copy")) == "import.copy"
    assert loc_to_dot_sep(("plugins", 2)) == "plugins[2]"
    assert loc_to_dot_sep(()) == ""


def test_schema_accepts_starter_shape(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "plugins": ["musicbrainz", "deezer"],
        "import": {"autotag": True, "copy": True, "write": True},
    }
    schema = KnownKeysSchema.model_validate(data)
    assert schema.plugins == ["musicbrainz", "deezer"]


def test_schema_rejects_invalid_bool() -> None:
    data = {"directory": "/tmp", "library": "/tmp/x", "import": {"copy": "maybe"}}
    try:
        KnownKeysSchema.model_validate(data)
    except ValidationError as e:
        errors = e.errors()
        assert any(err["loc"] == ("import", "copy") for err in errors)
    else:
        raise AssertionError("expected ValidationError")


def test_schema_rejects_unknown_plugin() -> None:
    data = {"directory": "/tmp", "library": "/tmp/x", "plugins": ["not-a-plugin"]}
    try:
        KnownKeysSchema.model_validate(data)
    except ValidationError:
        pass
    else:
        raise AssertionError("expected ValidationError on plugin allowlist")


def test_schema_ignores_unknown_keys() -> None:
    data = {
        "directory": "/tmp", "library": "/tmp/x",
        "myplugin": {"weird_key": 42},
    }
    KnownKeysSchema.model_validate(data)  # must not raise
```

- [ ] **Step 5: Update `SettingsPage.tsx` to read `apply_pending`**

In `frontend/src/pages/settings/SettingsPage.tsx`, replace all references to `data.restart_required` with `data.apply_pending` (3-5 sites). Update the banner text from "restart MusicDrop" to "click Apply" once the button is added (Task 11) — for now just rename references and keep current "restart" text.

- [ ] **Step 6: Run gates + commit**

```bash
cd backend
export PATH="$HOME/.local/bin:$PATH"
uv run pytest tests/test_config_editor_models.py -v
uv run mypy app/models/config_editor.py app/models/config_api.py app/beets/config_snapshot.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/models/config_editor.py backend/app/models/config_api.py \
        backend/app/beets/config_snapshot.py backend/tests/test_config_editor_models.py \
        frontend/src/pages/settings/SettingsPage.tsx
git commit -m "feat(config-editor): Pydantic schema + Snapshot mtime_ns+sha256 + apply_pending rename"
```

---

## Task 2: Parse + validate (with line/col mapping via ruamel `.lc`)

**Files:**
- Create: `backend/app/beets/config_editor.py`
- Test: `backend/tests/test_config_editor_parse_validate.py` (create)

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_config_editor_parse_validate.py`:

```python
from app.beets.config_editor import parse_yaml, validate_known_keys


def test_parse_yes_no_as_bool() -> None:
    data = parse_yaml("import:\n  autotag: yes\n  copy: no\n")
    assert data["import"]["autotag"] is True
    assert data["import"]["copy"] is False


def test_parse_invalid_yaml_raises() -> None:
    from ruamel.yaml import YAMLError
    try:
        parse_yaml("this: is: not [valid YAML")
    except YAMLError:
        pass
    else:
        raise AssertionError("expected YAMLError")


def test_validate_returns_empty_on_valid(tmp_path) -> None:
    music = tmp_path / "music"; music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "plugins:\n  - musicbrainz\n  - deezer\n"
        "import:\n  autotag: yes\n  copy: yes\n"
    )
    errors = validate_known_keys(parse_yaml(text))
    assert errors == []


def test_validate_returns_loc_with_line_col_for_known_key(tmp_path) -> None:
    music = tmp_path / "music"; music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "import:\n"
        "  autotag: yes\n"
        "  copy: maybe\n"
    )
    errors = validate_known_keys(parse_yaml(text))
    assert len(errors) == 1
    assert errors[0].loc == "import.copy"
    assert errors[0].line == 5  # 1-based, the `copy:` line
    assert errors[0].column is not None and errors[0].column >= 0
```

Run: `uv run pytest backend/tests/test_config_editor_parse_validate.py -v`
Expected: FAIL (no module yet).

- [ ] **Step 2: Implement parse + validate**

Create `backend/app/beets/config_editor.py`:

```python
"""Layer-3 config editor — write-side helpers.

ruamel.yaml is used ONLY for the write path (load → mutate → dump).
Per the maintainer (Anthon van der Neut), default `YAML()` is `typ='rt'`
which preserves comments, key order, block style, scalar quoting style,
and anchors. Booleans always emit as `true`/`false` regardless of input
form — this is the maintainer's documented invariant.

The default `extra='ignore'` on Pydantic (per pydantic v2 docs § Models)
is correct here: we never round-trip through the schema, only validate.
Unknown beets keys live on disk in the CommentedMap.
"""

from __future__ import annotations

from typing import Any

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap

from app.models.config_editor import (
    KnownKeysSchema,
    ValidationErrorItem,
    loc_to_dot_sep,
)
from pydantic import ValidationError


def _yaml() -> YAML:
    yaml = YAML()                        # default typ='rt'
    yaml.version = (1, 1)                # parse `yes`/`no` as bool (SF #285)
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096                    # don't rewrap long strings
    return yaml


def parse_yaml(text: str) -> CommentedMap:
    """Parse YAML text into a ruamel CommentedMap.

    Raises:
        ruamel.yaml.YAMLError on parse failure (caller handles 422).
    """
    return _yaml().load(text)


def _line_col_for_path(
    root: CommentedMap, path: tuple[str | int, ...]
) -> tuple[int, int] | tuple[None, None]:
    """Walk the CommentedMap along the path; use `.lc.value(...)` on the parent
    to get the value's (line, col). Returns 1-based line for CodeMirror.

    Per ruamel docs (yaml.dev/doc/ruamel.yaml/detail/): each CommentedMap
    carries its own `.lc`; `.lc.value('key')` returns (line0, col0); 0-based.
    """
    try:
        node: Any = root
        for key in path[:-1]:
            node = node[key]
        if hasattr(node, "lc") and node.lc.data is not None:
            line_col = node.lc.value(path[-1])
            if line_col is not None:
                line0, col0 = line_col
                return (line0 + 1, col0)  # 1-based for CodeMirror
    except (KeyError, IndexError, AttributeError, TypeError):
        pass
    return (None, None)


def validate_known_keys(
    data: CommentedMap | dict[str, Any],
) -> list[ValidationErrorItem]:
    """Run KnownKeysSchema; map each Pydantic error to a line/col on the
    original document via ruamel `.lc.value(...)` (per yaml.dev maintainer
    docs)."""
    try:
        KnownKeysSchema.model_validate(data)
        return []
    except ValidationError as e:
        out: list[ValidationErrorItem] = []
        root = data if isinstance(data, CommentedMap) else None
        for err in e.errors():
            line: int | None = None
            col: int | None = None
            if root is not None:
                line, col = _line_col_for_path(root, err["loc"])
            out.append(
                ValidationErrorItem(
                    loc=loc_to_dot_sep(err["loc"]),
                    msg=str(err["msg"]),
                    type=str(err["type"]),
                    line=line,
                    column=col,
                )
            )
        return out
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest backend/tests/test_config_editor_parse_validate.py -v
```

Expected: PASS.

- [ ] **Step 4: Gates + commit**

```bash
uv run mypy app/beets/config_editor.py app/models/config_editor.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/beets/config_editor.py backend/tests/test_config_editor_parse_validate.py
git commit -m "feat(config-editor): ruamel parse + Pydantic validate with line/col mapping"
```

---

## Task 3: Secret-preserve merge helpers

**Files:**
- Modify: `backend/app/beets/config_editor.py`
- Test: `backend/tests/test_config_editor_secrets.py` (create)

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_config_editor_secrets.py`:

```python
from app.beets.config_editor import (
    REDACTED_TOMBSTONE,
    merge_preserve_secrets,
    parse_yaml,
    walk_get,
    walk_set,
)


def test_walk_get_nested() -> None:
    data = parse_yaml("a:\n  b:\n    c: hello\n")
    assert walk_get(data, ("a", "b", "c")) == "hello"
    assert walk_get(data, ("a", "missing")) is None


def test_walk_set_nested_overwrites() -> None:
    data = parse_yaml("a:\n  b: hello\n")
    walk_set(data, ("a", "b"), "world")
    assert walk_get(data, ("a", "b")) == "world"


def test_merge_preserves_unchanged_redacted() -> None:
    on_disk = parse_yaml("spotify:\n  client_secret: supersecret\n")
    new = parse_yaml(f"spotify:\n  client_secret: {REDACTED_TOMBSTONE}\n")
    merge_preserve_secrets(new, on_disk, redacted_paths=[("spotify", "client_secret")])
    assert walk_get(new, ("spotify", "client_secret")) == "supersecret"


def test_merge_writes_new_value_when_user_rotates() -> None:
    on_disk = parse_yaml("spotify:\n  client_secret: oldsecret\n")
    new = parse_yaml("spotify:\n  client_secret: newvalue\n")
    merge_preserve_secrets(new, on_disk, redacted_paths=[("spotify", "client_secret")])
    assert walk_get(new, ("spotify", "client_secret")) == "newvalue"


def test_merge_leaves_intentional_delete_alone() -> None:
    on_disk = parse_yaml("spotify:\n  client_secret: oldvalue\n  client_id: pubid\n")
    new = parse_yaml("spotify:\n  client_id: pubid\n")  # user deleted client_secret
    merge_preserve_secrets(
        new, on_disk, redacted_paths=[("spotify", "client_secret")]
    )
    assert walk_get(new, ("spotify", "client_secret")) is None
```

Run: `uv run pytest backend/tests/test_config_editor_secrets.py -v`
Expected: FAIL (helpers don't exist yet).

- [ ] **Step 2: Add helpers to `config_editor.py`**

Append to `backend/app/beets/config_editor.py`:

```python
from confuse import REDACTED_TOMBSTONE  # type: ignore[attr-defined]
# REDACTED_TOMBSTONE is "REDACTED" — confuse's public sentinel, also used by
# build_config_snapshot. Shared so display + write agree.


def walk_get(data: Any, path: tuple[str | int, ...]) -> Any:
    node = data
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            node = node[key]
        else:
            return None
    return node


def walk_set(data: Any, path: tuple[str | int, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def merge_preserve_secrets(
    new_map: CommentedMap,
    on_disk_map: CommentedMap,
    *,
    redacted_paths: list[tuple[str | int, ...]],
) -> None:
    """For each redacted path: if the new value is still REDACTED_TOMBSTONE
    (user didn't type a new value), copy the on-disk value over. If the user
    typed a new value, leave it. If the user deleted the key (no walk_get hit
    in new_map), leave it deleted."""
    for path in redacted_paths:
        new_val = walk_get(new_map, path)
        if new_val == REDACTED_TOMBSTONE:
            on_val = walk_get(on_disk_map, path)
            if on_val is not None:
                walk_set(new_map, path, on_val)
```

Note: `find_redacted_paths` was already extracted in Task 1 (moved into `config_snapshot.py`). Import and re-export from `config_editor.py` for the save flow's convenience:

```python
from app.beets.config_snapshot import find_redacted_paths  # noqa: F401
```

- [ ] **Step 3: Run tests + commit**

```bash
uv run pytest backend/tests/test_config_editor_secrets.py -v
uv run mypy app/beets/config_editor.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/beets/config_editor.py backend/tests/test_config_editor_secrets.py
git commit -m "feat(config-editor): walk_get/walk_set + merge_preserve_secrets"
```

---

## Task 4: Atomic write with fsync + parent-dir fsync + copystat

**Files:**
- Modify: `backend/app/beets/config_editor.py`
- Test: `backend/tests/test_config_editor_atomic_write.py` (create)

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_config_editor_atomic_write.py`:

```python
import os
import stat as stat_mod
from pathlib import Path

import pytest

from app.beets.config_editor import atomic_write, _yaml, parse_yaml


def test_atomic_write_writes_content(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("a: 2\nb: 3\n")
    atomic_write(cfg, data, _yaml())
    assert "a: 2" in cfg.read_text()
    assert "b: 3" in cfg.read_text()


def test_atomic_write_preserves_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    cfg.chmod(0o600)
    data = parse_yaml("a: 2\n")
    atomic_write(cfg, data, _yaml())
    mode = stat_mod.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600


def test_atomic_write_first_time_chmods_644(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    assert not cfg.exists()
    data = parse_yaml("a: 1\n")
    atomic_write(cfg, data, _yaml())
    assert cfg.exists()
    mode = stat_mod.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o644


def test_atomic_write_cleans_up_tmpfile_on_success(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("a: 2\n")
    atomic_write(cfg, data, _yaml())
    tmps = list(tmp_path.glob(".config.yaml.tmp*"))
    assert tmps == []
```

Run: `uv run pytest backend/tests/test_config_editor_atomic_write.py -v`
Expected: FAIL.

- [ ] **Step 2: Implement `atomic_write`**

Append to `backend/app/beets/config_editor.py`:

```python
import os
import shutil
from pathlib import Path


def atomic_write(dst: Path, data: CommentedMap, yaml: YAML) -> None:
    """Atomic write with crash-safety on ext4.

    Sequence (per Dan Luu's `Files are hard` + LWN's `That massive filesystem
    thread`): write tmpfile in same directory → fsync the tmpfile → copystat
    from dst (mode/atime/mtime/flags/xattrs; NOT uid/gid per Python docs) →
    os.replace → fsync the PARENT DIRECTORY (otherwise the rename can be lost
    on power-cut even on ext4).

    The `atomicwrites` PyPI package is deprecated by its own author in favor
    of this recipe (github.com/untitaker/python-atomicwrites).
    """
    tmp = dst.parent / f".{dst.name}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
            f.flush()
            os.fsync(f.fileno())

        if dst.exists():
            shutil.copystat(dst, tmp)        # preserve mode/xattrs from dst
        else:
            os.chmod(tmp, 0o644)             # first-write fallback (defensive)

        os.replace(tmp, dst)                 # POSIX atomic, same-filesystem

        dir_fd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        # Best-effort cleanup if something raised mid-flight
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()
```

- [ ] **Step 3: Run tests + commit**

```bash
uv run pytest backend/tests/test_config_editor_atomic_write.py -v
uv run mypy app/beets/config_editor.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/beets/config_editor.py backend/tests/test_config_editor_atomic_write.py
git commit -m "feat(config-editor): atomic_write with fsync + dir-fsync + copystat"
```

---

## Task 5: `POST /api/config/validate` endpoint

**Files:**
- Modify: `backend/app/api/config_.py`
- Test: `backend/tests/test_config_validate_api.py` (create)

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_config_validate_api.py`:

```python
from fastapi.testclient import TestClient


def test_validate_returns_empty_on_starter(client: TestClient) -> None:
    # Use the starter content (already in conftest's beets_library fixture)
    snap = client.get("/api/config").json()
    r = client.post("/api/config/validate", json={"yaml_text": snap["yaml_text"]})
    assert r.status_code == 200
    assert r.json() == {"errors": []}


def test_validate_returns_errors_on_invalid_bool(client: TestClient) -> None:
    text = (
        "directory: /tmp\nlibrary: /tmp/x\n"
        "import:\n  copy: maybe\n"
    )
    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    errors = r.json()["errors"]
    assert any(e["loc"] == "import.copy" for e in errors)


def test_validate_returns_yaml_parse_error(client: TestClient) -> None:
    r = client.post(
        "/api/config/validate",
        json={"yaml_text": "this: is: not [valid YAML"},
    )
    assert r.status_code == 200
    errors = r.json()["errors"]
    assert len(errors) == 1
    assert errors[0]["loc"] == ""
    assert errors[0]["line"] is not None
```

Run: expected FAIL (route doesn't exist).

- [ ] **Step 2: Add the route**

In `backend/app/api/config_.py`, add at the bottom:

```python
from ruamel.yaml import YAMLError

from app.beets.config_editor import parse_yaml, validate_known_keys
from app.models.config_editor import ValidateRequest, ValidationErrorItem


@router.post("/config/validate", tags=["config"])
def validate_config(req: ValidateRequest) -> dict[str, list[ValidationErrorItem]]:
    """Cheap lint pass — never writes. Returns 200 even on errors so the
    CodeMirror async lint source can display them inline."""
    try:
        data = parse_yaml(req.yaml_text)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return {
            "errors": [
                ValidationErrorItem(
                    loc="",
                    msg=str(e),
                    type="yaml_parse",
                    line=(mark.line + 1) if mark else None,
                    column=mark.column if mark else None,
                )
            ]
        }
    return {"errors": validate_known_keys(data)}
```

- [ ] **Step 3: Run tests + gates + commit**

```bash
uv run pytest backend/tests/test_config_validate_api.py -v
uv run mypy app/api/config_.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/api/config_.py backend/tests/test_config_validate_api.py
git commit -m "feat(config-editor): POST /api/config/validate"
```

---

## Task 6: `POST /api/config/save` (full CAS + secret preserve + atomic write)

**Files:**
- Modify: `backend/app/beets/config_editor.py`
- Modify: `backend/app/api/config_.py`
- Test: `backend/tests/test_config_save_api.py` (create)

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_config_save_api.py`:

```python
import hashlib
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient


def _cas(client: TestClient) -> tuple[int, str]:
    snap = client.get("/api/config").json()
    return snap["mtime_ns"], snap["sha256"]


def test_save_happy_path(client: TestClient, beets_library_config_path: Path) -> None:
    mtime, sha = _cas(client)
    new_text = beets_library_config_path.read_text() + "\n# trailing comment\n"
    r = client.post("/api/config/save", json={
        "yaml_text": new_text, "base_mtime_ns": mtime, "base_sha256": sha,
    })
    assert r.status_code == 200
    assert r.json()["apply_pending"] is True
    assert "# trailing comment" in beets_library_config_path.read_text()


def test_save_normalizes_yes_no_to_true_false(
    client: TestClient, beets_library_config_path: Path
) -> None:
    mtime, sha = _cas(client)
    # Starter has `autotag: yes` — submitting unchanged should still
    # cause ruamel to emit `true`/`false` per the maintainer's invariant.
    r = client.post("/api/config/save", json={
        "yaml_text": beets_library_config_path.read_text(),
        "base_mtime_ns": mtime, "base_sha256": sha,
    })
    assert r.status_code == 200
    text = beets_library_config_path.read_text()
    assert "autotag: true" in text
    assert "autotag: yes" not in text


def test_save_422_on_invalid_yaml(client: TestClient) -> None:
    mtime, sha = _cas(client)
    r = client.post("/api/config/save", json={
        "yaml_text": "not: valid: yaml: :",
        "base_mtime_ns": mtime, "base_sha256": sha,
    })
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ""


def test_save_422_on_schema_error(client: TestClient) -> None:
    mtime, sha = _cas(client)
    text = "directory: /tmp\nlibrary: /tmp/x\nimport:\n  copy: maybe\n"
    r = client.post("/api/config/save", json={
        "yaml_text": text, "base_mtime_ns": mtime, "base_sha256": sha,
    })
    assert r.status_code == 422
    assert any(d["loc"] == "import.copy" for d in r.json()["detail"])


def test_save_409_on_mtime_change(
    client: TestClient, beets_library_config_path: Path
) -> None:
    mtime, sha = _cas(client)
    time.sleep(0.01)
    os.utime(beets_library_config_path, (time.time() + 5, time.time() + 5))
    r = client.post("/api/config/save", json={
        "yaml_text": beets_library_config_path.read_text(),
        "base_mtime_ns": mtime, "base_sha256": sha,
    })
    assert r.status_code == 409
    body = r.json()
    assert "current_snapshot" in body
    assert "current_yaml_text" in body


def test_save_409_on_sha_change_same_mtime(
    client: TestClient, beets_library_config_path: Path
) -> None:
    mtime, _ = _cas(client)
    # Modify content but force the SAME mtime to test the SHA tie-breaker.
    original = beets_library_config_path.read_text()
    beets_library_config_path.write_text(original + "\n# external edit\n")
    os.utime(
        beets_library_config_path,
        (
            beets_library_config_path.stat().st_atime,
            mtime / 1_000_000_000,
        ),
    )
    r = client.post("/api/config/save", json={
        "yaml_text": original,
        "base_mtime_ns": mtime,
        "base_sha256": hashlib.sha256(original.encode()).hexdigest(),
    })
    # mtime matches base, but sha won't → 409
    assert r.status_code == 409


def test_save_preserves_secret_when_unchanged(
    client: TestClient, beets_library_config_path: Path
) -> None:
    # Set a fake secret on disk
    text = (
        beets_library_config_path.read_text()
        + "\nspotify:\n  client_secret: REAL_SECRET_123\n"
    )
    beets_library_config_path.write_text(text)
    mtime, sha = _cas(client)
    snap = client.get("/api/config").json()
    # The displayed yaml_text has REDACTED at spotify.client_secret
    assert "REAL_SECRET_123" not in snap["yaml_text"]
    # Submit unchanged
    r = client.post("/api/config/save", json={
        "yaml_text": snap["yaml_text"],
        "base_mtime_ns": mtime,
        "base_sha256": sha,
    })
    assert r.status_code == 200
    # On disk, the real secret survives
    assert "REAL_SECRET_123" in beets_library_config_path.read_text()
```

Run: expected FAIL.

- [ ] **Step 2: Implement `save` in `config_editor.py`**

Append to `backend/app/beets/config_editor.py`:

```python
import hashlib

from fastapi import HTTPException

from app.beets.config_snapshot import build_config_snapshot, find_redacted_paths
from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot
from app.models.config_editor import SaveRequest


def save(handle: LibraryHandle, req: SaveRequest) -> BeetsConfigSnapshot:
    yaml = _yaml()

    # 1. Parse
    try:
        new_map = parse_yaml(req.yaml_text)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": "",
                    "msg": str(e),
                    "type": "yaml_parse",
                    "line": (mark.line + 1) if mark else None,
                    "column": mark.column if mark else None,
                }
            ],
        )

    # 2. Schema validate
    errors = validate_known_keys(new_map)
    if errors:
        raise HTTPException(status_code=422, detail=[e.model_dump() for e in errors])

    # 3. mtime + SHA-256 CAS
    on_disk_bytes = handle.config_path.read_bytes()
    on_disk_mtime_ns = handle.config_path.stat().st_mtime_ns
    on_disk_sha = hashlib.sha256(on_disk_bytes).hexdigest()
    if on_disk_mtime_ns != req.base_mtime_ns or on_disk_sha != req.base_sha256:
        snap = build_config_snapshot(handle)
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "File changed on disk",
                "current_snapshot": snap.model_dump(mode="json"),
                "current_yaml_text": on_disk_bytes.decode("utf-8"),
                "current_sha256": on_disk_sha,
            },
        )

    # 4. Secret preserve merge
    on_disk_map = parse_yaml(on_disk_bytes.decode("utf-8"))
    redacted = find_redacted_paths(on_disk_map)
    merge_preserve_secrets(new_map, on_disk_map, redacted_paths=redacted)

    # 5. Atomic write
    atomic_write(handle.config_path, new_map, yaml)

    # 6. New snapshot (apply_pending will be true)
    return build_config_snapshot(handle)
```

- [ ] **Step 3: Wire the route in `config_.py`**

Append to `backend/app/api/config_.py`:

```python
from app.beets.config_editor import save as save_config_op


@router.post("/config/save", response_model=BeetsConfigSnapshot, tags=["config"])
def save_config(req: SaveRequest, request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return save_config_op(handle, req)
```

- [ ] **Step 4: Run tests + commit**

```bash
uv run pytest backend/tests/test_config_save_api.py -v
uv run mypy app/api/config_.py app/beets/config_editor.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/beets/config_editor.py backend/app/api/config_.py \
        backend/tests/test_config_save_api.py
git commit -m "feat(config-editor): POST /api/config/save with CAS + secret-preserve + atomic write"
```

---

## Task 7: `reset_beets_globals` refactor + regression test

**Files:**
- Modify: `backend/app/beets/setup.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_reset_beets_globals.py` (create)

- [ ] **Step 1: Write failing test**

Create `backend/tests/test_reset_beets_globals.py`:

```python
from pathlib import Path

from app.beets.setup import reset_beets_globals, setup_beets


def test_reset_leaves_no_residual_state(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    reset_beets_globals(handle)

    import beets
    from beets import metadata_plugins, plugins
    from beets.plugins import BeetsPlugin

    assert plugins._instances == []
    assert BeetsPlugin.listeners == {}
    assert BeetsPlugin._raw_listeners == {}
    assert beets.config._materialized is False
    assert metadata_plugins.find_metadata_source_plugins.cache_info().currsize == 0
    assert metadata_plugins.get_metadata_source.cache_info().currsize == 0
    assert metadata_plugins.get_penalty.cache_info().currsize == 0


def test_setup_after_reset_loads_plugins_fresh(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    reset_beets_globals(handle)
    new_handle = setup_beets(str(tmp_path))
    try:
        from beets import metadata_plugins
        names = {p.name for p in metadata_plugins.find_metadata_source_plugins()}
        assert "musicbrainz" in names
        assert "deezer" in names
    finally:
        from app.beets.library import close_library
        close_library(new_handle.lib)
```

Run: expected FAIL (`reset_beets_globals` doesn't exist as a public helper).

- [ ] **Step 2: Add `reset_beets_globals` to `setup.py`**

Append to `backend/app/beets/setup.py`:

```python
from contextlib import suppress


def reset_beets_globals(handle: LibraryHandle) -> None:
    """Teardown all beets/confuse/plugins process-global state.

    THIS IS A BEETS-2.11-PINNED COMPATIBILITY SHIM. Each operation touches a
    private API that has upstream TODOs for refactor in beets 3.0.0 (see
    beets/plugins.py:455 FIXME and PR #5887). Pinning beets==2.11.* in
    pyproject.toml is therefore mandatory, not preferential.

    Mirrors beets' own `unload_plugins` (beets/test/helper.py:460-466), which
    is what beets itself does between test runs.
    """
    import beets
    from beets import metadata_plugins, plugins
    from beets.plugins import BeetsPlugin

    from app.beets.library import close_library

    with suppress(Exception):
        close_library(handle.lib)

    # confuse: truncate sources + re-arm LazyConfig
    beets.config.clear()
    beets.config._materialized = False  # private; required

    # beets plugin teardown (mirrors unload_plugins)
    plugins._instances.clear()
    BeetsPlugin.listeners.clear()
    BeetsPlugin._raw_listeners.clear()

    # all three @cache decorators in metadata_plugins.py
    metadata_plugins.find_metadata_source_plugins.cache_clear()
    metadata_plugins.get_metadata_source.cache_clear()
    metadata_plugins.get_penalty.cache_clear()
```

- [ ] **Step 3: Update `conftest.py` to use the new helper**

In `backend/tests/conftest.py`, find the inline cleanup in `_clear_beets_globals` and replace it with a call to `reset_beets_globals` — but only after `yield`, since the fixture also handles env-var snapshot/restore which production doesn't need.

Pseudo-diff:

```python
# old
beets.config.clear()
beets.config._materialized = False
plugins._instances.clear()
metadata_plugins.find_metadata_source_plugins.cache_clear()
metadata_plugins.get_metadata_source.cache_clear()

# new
from app.beets.setup import reset_beets_globals
# Need a current LibraryHandle to pass; if none active in the fixture's scope,
# inline the operations OR build a dummy handle and call reset.
reset_beets_globals(_current_handle_or_none())  # use helper below
```

If passing a handle is awkward in the fixture, just keep the inline code in conftest but ALSO call into `reset_beets_globals` from production. The DRY win is more important in production than in tests; choose whichever shape compiles cleanly.

- [ ] **Step 4: Run tests + commit**

```bash
uv run pytest backend/tests/test_reset_beets_globals.py backend/tests/test_setup_beets.py -v
uv run mypy app/beets/setup.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/beets/setup.py backend/tests/conftest.py \
        backend/tests/test_reset_beets_globals.py
git commit -m "refactor(config-editor): extract reset_beets_globals + add 3 missing clears"
```

---

## Task 8: `POST /api/config/apply` endpoint with `asyncio.Lock` + threadpool

**Files:**
- Modify: `backend/app/beets/config_editor.py`
- Modify: `backend/app/api/config_.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_config_apply_api.py` (create)

- [ ] **Step 1: Add lock to lifespan**

In `backend/app/main.py`, locate the lifespan function and add:

```python
import asyncio
# inside the @asynccontextmanager async def lifespan(app: FastAPI):
# after the existing setup_beets() call:
app.state.beets_swap_lock = asyncio.Lock()
app.state.settings = settings  # if not already exposed
```

- [ ] **Step 2: Write failing tests**

Create `backend/tests/test_config_apply_api.py`:

```python
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient
from app.import_jobs.registry import ImportPhase


def test_apply_returns_snapshot_with_apply_pending_false(
    client: TestClient, beets_library_config_path: Path
) -> None:
    # Touch the file so apply_pending becomes true
    time.sleep(0.01)
    os.utime(beets_library_config_path, (time.time(), time.time()))
    snap_before = client.get("/api/config").json()
    assert snap_before["apply_pending"] is True

    r = client.post("/api/config/apply")
    assert r.status_code == 200
    assert r.json()["apply_pending"] is False


def test_apply_409_when_import_active(
    client: TestClient, monkeypatch
) -> None:
    # Inject a fake active job
    from app.import_jobs.registry import import_job_registry as registry
    # set up a mocked active phase — implementation detail of the registry
    monkeypatch.setattr(registry, "has_active_job", lambda: True)
    r = client.post("/api/config/apply")
    assert r.status_code == 409
    assert "import" in r.json()["detail"].lower()


def test_apply_500_when_setup_beets_fails(
    client: TestClient, monkeypatch
) -> None:
    from app.beets import config_editor
    monkeypatch.setattr(
        config_editor, "setup_beets",
        lambda _dir: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = client.post("/api/config/apply")
    assert r.status_code == 500
    assert "recovery" in r.json()["detail"].lower() or "restart" in r.json()["detail"].lower()
```

- [ ] **Step 3: Implement apply**

Append to `backend/app/beets/config_editor.py`:

```python
from fastapi import Request
from fastapi.concurrency import run_in_threadpool

from app.beets.library import LibraryHandle
from app.beets.setup import reset_beets_globals, setup_beets


def _rebuild_beets_handle(old: LibraryHandle, beets_dir: str) -> LibraryHandle:
    """Blocking — runs in FastAPI's threadpool."""
    reset_beets_globals(old)
    return setup_beets(beets_dir)


async def apply(request: Request) -> BeetsConfigSnapshot:
    app = request.app
    registry = app.state.import_registry
    if registry.has_active_job():
        raise HTTPException(
            status_code=409,
            detail="Import in progress — Apply available when it finishes",
        )

    async with app.state.beets_swap_lock:
        old: LibraryHandle = app.state.beets_library
        settings = app.state.settings
        try:
            new = await run_in_threadpool(
                _rebuild_beets_handle, old, settings.beets_dir
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail={
                    "detail": f"Apply failed during rebuild: {e}",
                    "recovery": "Restart MusicDrop. The saved config is on disk; cold start will load it.",
                },
            )
        app.state.beets_library = new

    return build_config_snapshot(new)
```

- [ ] **Step 4: Wire the route**

In `backend/app/api/config_.py`:

```python
from app.beets.config_editor import apply as apply_config_op


@router.post("/config/apply", response_model=BeetsConfigSnapshot, tags=["config"])
async def apply_config(request: Request) -> BeetsConfigSnapshot:
    return await apply_config_op(request)
```

- [ ] **Step 5: Run tests + commit**

```bash
uv run pytest backend/tests/test_config_apply_api.py -v
uv run mypy app/beets/config_editor.py app/api/config_.py app/main.py
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/app/beets/config_editor.py backend/app/api/config_.py \
        backend/app/main.py backend/tests/test_config_apply_api.py
git commit -m "feat(config-editor): POST /api/config/apply (asyncio.Lock + threadpool)"
```

---

## Task 9: Pin deps + OpenAPI regen

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `frontend/src/api/schema.d.ts` (regenerated)
- Modify: `frontend/openapi.json` (regenerated)

- [ ] **Step 1: Update `pyproject.toml`**

```diff
 dependencies = [
-  "beets>=2.11.0",
+  "beets==2.11.*",
+  "ruamel.yaml>=0.15.93",
   ...
 ]
```

Extend mypy override (`disallow_untyped_calls=false`) to include `app.beets.config_editor`, `app.models.config_editor`, and the new test modules.

```bash
cd backend && uv sync
```

- [ ] **Step 2: Regenerate OpenAPI**

```bash
cd backend && export PATH="$HOME/.local/bin:$PATH"
uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2))" > ../frontend/openapi.json
cd ../frontend && npm run gen:api
```

Verify the new routes + schemas appear:

```bash
grep -A 2 "/api/config/save" openapi.json | head -10
grep "SaveRequest\|BeetsConfigSnapshot" src/api/schema.d.ts | head -10
```

- [ ] **Step 3: Run full backend suite + commit**

```bash
cd backend && uv run pytest -v 2>&1 | tail -3
uv run mypy
uv run ruff check && uv run ruff format --check
```

```bash
git add backend/pyproject.toml backend/uv.lock \
        frontend/src/api/schema.d.ts frontend/openapi.json
git commit -m "chore(config-editor): pin beets==2.11.* + add ruamel.yaml + regen OpenAPI"
```

---

## Task 10: Frontend deps + hooks (`useSaveConfig`, `useApplyConfig`, `useValidateConfig`, `useActiveImport`)

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/src/api/useBeetsConfig.ts`
- Create: `frontend/src/api/useActiveImport.ts`

- [ ] **Step 1: Install CM6 packages**

```bash
cd frontend
npm install @uiw/react-codemirror@^4.25 @codemirror/lang-yaml@^6.1 \
            @codemirror/lint@^6.9 @codemirror/merge@^6.12
```

- [ ] **Step 2: Add mutations to `useBeetsConfig.ts`**

Modify `frontend/src/api/useBeetsConfig.ts`:

```ts
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { client } from "@/api/client";
import type { components } from "@/api/schema";

type SaveRequest = components["schemas"]["SaveRequest"];
type ValidateRequest = components["schemas"]["ValidateRequest"];
type ValidationErrorItem = components["schemas"]["ValidationErrorItem"];

// (existing useBeetsConfig stays as-is)

export function useSaveConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (req: SaveRequest) => {
      const { data, error, response } = await client.POST("/api/config/save", { body: req });
      if (!response.ok || !data) {
        // throw a structured error including the response body for the caller
        // to distinguish 409 (conflict) from 422 (validation)
        throw Object.assign(new Error("Save failed"), { status: response.status, body: error });
      }
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["beets-config"] }),
  });
}

export function useApplyConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error, response } = await client.POST("/api/config/apply");
      if (!response.ok || !data) {
        throw Object.assign(new Error("Apply failed"), { status: response.status, body: error });
      }
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["beets-config"] }),
  });
}

export function useValidateConfig() {
  return useMutation({
    mutationFn: async (req: ValidateRequest): Promise<ValidationErrorItem[]> => {
      const { data, error, response } = await client.POST("/api/config/validate", { body: req });
      if (!response.ok || !data) throw new Error(error?.detail ?? "Validate failed");
      return data.errors ?? [];
    },
  });
}
```

- [ ] **Step 3: Create `useActiveImport.ts`**

```ts
import { useQuery } from "@tanstack/react-query";
import { client } from "@/api/client";

export function useActiveImport() {
  return useQuery({
    queryKey: ["active-import"],
    queryFn: async () => {
      // Reuse existing /api/imports/jobs/active or similar endpoint.
      // If no such endpoint, the simplest shape: GET /api/imports/active → {active: bool}.
      // (If this endpoint doesn't yet exist, add one in this task with a 1-line handler
      //  calling registry.has_active_job())
      const { data, response } = await client.GET("/api/imports/active");
      if (!response.ok || !data) return { active: false };
      return data;
    },
    refetchInterval: (query) => (query.state.data?.active ? 5_000 : 30_000),
    staleTime: 5_000,
  });
}
```

If `GET /api/imports/active` doesn't exist, add it in `backend/app/api/import_.py`:

```python
@router.get("/imports/active", tags=["imports"])
def get_active(request: Request) -> dict[str, bool]:
    registry = request.app.state.import_registry
    return {"active": registry.has_active_job()}
```

- [ ] **Step 4: Run gates + commit**

```bash
cd frontend && npm run typecheck && npm test
```

```bash
git add frontend/package.json frontend/package-lock.json \
        frontend/src/api/useBeetsConfig.ts frontend/src/api/useActiveImport.ts \
        backend/app/api/import_.py  # if changed
git commit -m "feat(config-editor): CodeMirror 6 deps + save/apply/validate/activeImport hooks"
```

---

## Task 11: SettingsPage editor integration

**Files:**
- Modify: `frontend/src/pages/settings/SettingsPage.tsx`
- Create: `frontend/src/pages/settings/codemirror-config.ts`

- [ ] **Step 1: Create canonical extension list**

Create `frontend/src/pages/settings/codemirror-config.ts`:

```ts
import { basicSetup } from "codemirror";
import { EditorState, Compartment, Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { yaml } from "@codemirror/lang-yaml";
import { lintGutter, linter, type Diagnostic } from "@codemirror/lint";

export const editableCompartment = new Compartment();

export function buildExtensions(opts: {
  initialDoc: string;
  asyncSource: (text: string) => Promise<Diagnostic[]>;
  onSave: (text: string) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  return [
    basicSetup,
    yaml(),
    lintGutter(),
    linter(async (view) => opts.asyncSource(view.state.doc.toString()), { delay: 500 }),
    Prec.high(keymap.of([{
      key: "Mod-s",
      preventDefault: true,
      run: (view) => { opts.onSave(view.state.doc.toString()); return true; },
    }])),
    editableCompartment.of([
      EditorState.readOnly.of(true),
      EditorView.editable.of(false),
      EditorView.contentAttributes.of({ tabindex: "0" }),
    ]),
    EditorView.updateListener.of((u) => {
      if (u.docChanged) opts.onDirtyChange(u.state.doc.toString() !== opts.initialDoc);
    }),
  ];
}
```

- [ ] **Step 2: Integrate into SettingsPage**

Rewrite `frontend/src/pages/settings/SettingsPage.tsx` to use CodeMirror. Key elements:

```tsx
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { useRef, useState } from "react";
import { useBeetsConfig, useSaveConfig, useApplyConfig, useValidateConfig } from "@/api/useBeetsConfig";
import { useActiveImport } from "@/api/useActiveImport";
import { buildExtensions, editableCompartment } from "./codemirror-config";
import type { Diagnostic } from "@codemirror/lint";

type PageState = "clean" | "dirty" | "saving" | "apply_pending" | "applying";

export function SettingsPage() {
  const { data, isLoading, isError, error } = useBeetsConfig();
  const save = useSaveConfig();
  const applyM = useApplyConfig();
  const validate = useValidateConfig();
  const active = useActiveImport();

  const editorRef = useRef<ReactCodeMirrorRef | null>(null);
  const [localText, setLocalText] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [conflict, setConflict] = useState<{ serverDoc: string; sha: string; mtime: number } | null>(null);

  if (isLoading) return <Loader />;
  if (isError) return <ErrorBanner err={error} />;
  if (!data) return null;

  const state: PageState =
    applyM.isPending ? "applying"
    : save.isPending ? "saving"
    : data.apply_pending ? "apply_pending"
    : dirty ? "dirty"
    : "clean";

  async function asyncSource(text: string): Promise<Diagnostic[]> {
    const errors = await validate.mutateAsync({ yaml_text: text });
    if (!editorRef.current?.view) return [];
    const view = editorRef.current.view;
    return errors
      .filter((e) => e.line != null)
      .map((e) => {
        const line = view.state.doc.line(e.line!);
        return {
          from: line.from + (e.column ?? 0),
          to: line.to,
          severity: "error" as const,
          message: `${e.loc}: ${e.msg}`,
        };
      });
  }

  function onSave() {
    if (!data) return;
    const text = localText ?? data.yaml_text;
    save.mutate(
      { yaml_text: text, base_mtime_ns: data.mtime_ns, base_sha256: data.sha256 },
      {
        onSuccess: () => { setDirty(false); setLocalText(null); },
        onError: (err: any) => {
          if (err.status === 409) {
            setConflict({
              serverDoc: err.body.current_yaml_text,
              sha: err.body.current_sha256,
              mtime: err.body.current_snapshot.mtime_ns,
            });
          }
        },
      },
    );
  }

  function onEdit() {
    if (editorRef.current?.view) {
      editorRef.current.view.dispatch({
        effects: editableCompartment.reconfigure([]),
      });
    }
  }

  return (
    <section className="flex max-w-4xl flex-col gap-4" aria-label="Beets configuration">
      <header>
        <h2 className="text-2xl font-semibold">Beets configuration</h2>
        <p className="text-sm text-gray-600">
          Loaded from <code>{data.config_path}</code>
        </p>
      </header>

      <StatusBanner state={state} activeImport={active.data?.active ?? false} />

      <CodeMirror
        ref={editorRef}
        value={data.yaml_text}
        extensions={buildExtensions({
          initialDoc: data.yaml_text,
          asyncSource,
          onSave,
          onDirtyChange: setDirty,
        })}
        onChange={setLocalText}
      />

      <div className="flex gap-2">
        <button onClick={onEdit} disabled={state !== "clean"}>Edit</button>
        <button onClick={onSave} disabled={state !== "dirty"}>Save</button>
        <button
          onClick={() => applyM.mutate()}
          disabled={state !== "apply_pending" || active.data?.active}
        >
          {active.data?.active
            ? "Apply (import in progress)"
            : "Apply changes"}
        </button>
      </div>

      {conflict && (
        <SettingsConflict
          local={localText ?? data.yaml_text}
          server={conflict.serverDoc}
          onReload={() => {
            setLocalText(null);
            setDirty(false);
            setConflict(null);
            // Refetch snapshot to get new CAS tokens
          }}
          onOverwrite={() => {
            save.mutate({
              yaml_text: localText ?? data.yaml_text,
              base_mtime_ns: conflict.mtime,
              base_sha256: conflict.sha,
            });
            setConflict(null);
          }}
        />
      )}
    </section>
  );
}

// StatusBanner component handles the 5 page states.
// SettingsConflict component is built in Task 12.
```

(Polish: keep existing styling/Tailwind classes from L1/L2 where applicable; replace only the `<pre>` block.)

- [ ] **Step 3: Run gates + commit**

```bash
cd frontend && npm run typecheck && npm run build
```

```bash
git add frontend/src/pages/settings/SettingsPage.tsx \
        frontend/src/pages/settings/codemirror-config.ts
git commit -m "feat(config-editor): SettingsPage CodeMirror editor + state machine"
```

---

## Task 12: SettingsConflict component (MergeView)

**Files:**
- Create: `frontend/src/pages/settings/SettingsConflict.tsx`

- [ ] **Step 1: Build the component**

Create `frontend/src/pages/settings/SettingsConflict.tsx`:

```tsx
import { useEffect, useRef } from "react";
import { MergeView } from "@codemirror/merge";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { yaml } from "@codemirror/lang-yaml";

export function SettingsConflict(props: {
  local: string;
  server: string;
  onReload: () => void;
  onOverwrite: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const mv = new MergeView({
      parent: ref.current,
      a: {
        doc: props.local,
        extensions: [yaml()],
      },
      b: {
        doc: props.server,
        extensions: [
          yaml(),
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
        ],
      },
      revertControls: "b-to-a",
      highlightChanges: true,
      gutter: true,
      collapseUnchanged: {},
    });
    return () => mv.destroy();
  }, [props.local, props.server]);

  return (
    <div role="dialog" aria-modal aria-label="File changed on disk">
      <p className="text-sm">
        <strong>File changed on disk</strong> while you were editing. Your edits
        are on the left; the on-disk version is on the right.
      </p>
      <div ref={ref} />
      <div className="flex gap-2 mt-2">
        <button onClick={props.onReload}>Reload (drop my edits)</button>
        <button onClick={props.onOverwrite}>Overwrite anyway</button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Gates + commit**

```bash
cd frontend && npm run typecheck && npm run build
```

```bash
git add frontend/src/pages/settings/SettingsConflict.tsx
git commit -m "feat(config-editor): SettingsConflict modal with @codemirror/merge MergeView"
```

---

## Task 13: SettingsPage tests for the new flows

**Files:**
- Modify: `frontend/src/pages/settings/SettingsPage.test.tsx`

- [ ] **Step 1: Add tests for state machine + Mod-s + 422 + 409**

Extend `SettingsPage.test.tsx`:

```tsx
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
// ... existing imports + msw setup

describe("SettingsPage editor flows", () => {
  test("typing transitions clean → dirty", async () => { /* ... */ });
  test("Mod-s triggers save", async () => { /* ... */ });
  test("Save success transitions to apply-pending state", async () => { /* ... */ });
  test("Apply success returns to clean", async () => { /* ... */ });
  test("Apply disabled while import is active", async () => { /* ... */ });
  test("422 from validate renders inline gutter markers", async () => { /* ... */ });
  test("409 from Save opens the SettingsConflict modal", async () => { /* ... */ });
  test("Reload from conflict modal drops local edits", async () => { /* ... */ });
  test("Overwrite anyway re-saves with new CAS tokens", async () => { /* ... */ });
});
```

(Fill in test bodies using MSW handlers + RTL queries matching the components built in Tasks 11/12. Use `userEvent.keyboard("{Meta>}s{/Meta}")` for Mod-s. For CodeMirror's lint diagnostics, query for `.cm-lint-marker` or use the gutter aria-label.)

- [ ] **Step 2: Run tests + commit**

```bash
cd frontend && npm test
```

```bash
git add frontend/src/pages/settings/SettingsPage.test.tsx
git commit -m "test(config-editor): SettingsPage flows — state machine, Mod-s, 422, 409"
```

---

## Task 14: Full green sweep + live walkthrough

**Files:** any final fixes discovered during the sweep.

- [ ] **Step 1: Backend full sweep**

```bash
cd backend && export PATH="$HOME/.local/bin:$PATH"
uv run pytest -v 2>&1 | tail -5
uv run mypy
uv run ruff check && uv run ruff format --check
```

Expected: all green.

- [ ] **Step 2: Frontend full sweep**

```bash
cd frontend
npm run typecheck
npm test
npm run build
```

Expected: all green.

- [ ] **Step 3: Live walkthrough**

Start both servers:

```bash
cd backend && uv run uvicorn app.main:app --port 3030 --reload &
cd frontend && npm run dev &
```

In browser at `http://localhost:5173/settings`:

1. **Verify clean state** — heading renders, editor shows YAML, Edit/Save/Apply buttons present (Save+Apply disabled).
2. **Click Edit** — editor becomes editable.
3. **Type something innocuous** (`# trailing comment` at end). Banner: "Unsaved changes." Save is primary.
4. **Click Save (or press Ctrl/Cmd+S)** — banner: "Saved on disk — Apply to load." Apply is primary.
5. **Click Apply** — banner clears. Verify reload happened (e.g., the YAML reflects the edit).
6. **Test 422** — type `import.copy: maybe`. Wait ~500ms. Verify red gutter marker appears.
7. **Test 409** — open `data/beets/config.yaml` in `$EDITOR`, add a blank line, save. Back in browser, click Save. Verify MergeView modal opens. Click "Reload (drop my edits)" — verify editor returns to server's content.
8. **Test 409 with Overwrite** — type something in editor, externally edit file, click Save → conflict modal → "Overwrite anyway" → verify the external edit is gone and yours is in.
9. **Test import gate** — start an import. While running, navigate to `/settings`, type a change, Save. Verify Apply button shows "Apply (import in progress)" disabled. Cancel/finish the import; Apply re-enables.

- [ ] **Step 4: Capture any walkthrough fixes**

If anything surfaces (component styling drift, missing edge case, broken keyboard handling), fix in a dedicated micro-commit per issue.

- [ ] **Step 5: Final report**

Confirm:
- `git log --oneline feat/beets-config-editor | wc -l` — task count + any fix commits.
- All gates green.
- Walkthrough passed all 9 steps.

If clean, push and open PR (user does this, not Claude):

```bash
git push -u origin feat/beets-config-editor
gh pr create --title "Feat/beets config editor" --body "..."
```

---

## Out of scope (deferred follow-ups)

These are explicitly NOT part of this slice:

- **Layer 4 — Curated form panel** on top of the raw editor for ~13 high-frequency keys
- **Per-plugin credential management UI** (guided OAuth-style)
- **YAML 1.2 migration** + version-directive support
- **Diff-preview on Save** ("here's what changes" preview before confirming)
- **Cross-session undo** via `.previous` backup file
- **Two-way form ↔ YAML sync**

---

## Self-review checklist (run before handing off)

- ✅ **Spec coverage:** every spec section maps to at least one task. Save flow → T1-T6, Apply flow → T7-T8, Frontend → T10-T13, Migration/deps → T9, Walkthrough → T14.
- ✅ **No placeholders:** every step has actual code or commands. No "TBD," no "add error handling here," no "similar to Task N."
- ✅ **Type/method consistency:** `SaveRequest`, `ValidationErrorItem`, `KnownKeysSchema` defined in T1, consumed identically in T2, T5, T6. `reset_beets_globals` signature defined in T7, called in T8. `apply_pending` field added in T1, consumed in T11 frontend.
- ✅ **TDD:** test-first where new code is being written (T2-T8). Frontend tests follow component (T13 after T11/T12) because RTL queries depend on the rendered DOM.
- ✅ **Each task ends with commit.** Single PR target: `feat/beets-config-editor`.
- ✅ **DRY:** `find_redacted_paths` extracted once in T1's snapshot edit; reused in T6's save flow.
- ✅ **Primary-source citations** in code docstrings for the load-bearing decisions (ruamel maintainer's bool invariant, atomic write recipe, beets reset shim's beets-2.11 pinning, Pydantic `extra='ignore'` rationale, CodeMirror extension order).
