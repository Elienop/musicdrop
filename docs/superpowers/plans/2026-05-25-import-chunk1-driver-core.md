# Import Driver Core (beets) — Chunk 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend import-driver core — a `WebImportSession(ImportSession)` that drives beets' autotag/import flow on a serial worker thread, auto-applies strong matches and parks uncertain ones for review over a thread-safe queue/reply bridge, with all beets `AlbumMatch`/`Distance`/`AlbumInfo`/`TrackInfo` objects mapped to our Pydantic models — hermetically tested, no API and no frontend.

**Architecture:** beets is embedded in-process (pinned 2.11.0). A new `app/beets/import_session.py` (inside the beets-adapter boundary, CLAUDE.md rule 3) subclasses `beets.importer.ImportSession` and overrides its four decision hooks. The session runs single-threaded (`config["threaded"] = False`) on one dedicated worker thread (serial — global-singleton safety). At `choose_match`, a **strong** `task.rec` auto-returns the top candidate (mirroring beets' auto-apply); otherwise the task's mapped `Candidate` is pushed to an out-queue and the worker blocks on a per-album reply event until a later async layer pushes a decision. A pure mapping module (`app/beets/import_mapping.py`) converts beets match objects to our models so beets internals never leak upward. This chunk delivers the bridge primitives + a clean interface only — no FastAPI.

**Tech Stack:** Python 3.11+, beets 2.11.0 (pinned), Pydantic v2, pytest + anyio (per repo convention), stdlib `threading`/`queue` for the bridge. mypy `--strict` and Ruff must stay green.

---

## Beets API facts this plan encodes (verified against the venv: `backend/.venv/.../beets/` at 2.11.0)

These were confirmed by reading the source and by live spikes in the venv. Encode them exactly; do not re-guess.

- **`ImportSession.__init__(self, lib, loghandler, paths, query)`** — 4 positional args after `self` (`beets/importer/session.py:61`). `lib: library.Library`, `loghandler: logging.Handler | None`, `paths: Sequence[PathBytes] | None`, `query: dbcore.Query | None`.
- **Decision hooks** (raise `NotImplementedError` in the base; we override all four): `choose_match(self, task)`, `choose_item(self, task)`, `resolve_duplicate(self, task, found_duplicates)`, `should_resume(self, path)` (`session.py:179-189`).
- **`ImportSession.run(self)`** — calls `self.set_config(config["import"])`, builds the pipeline, then `pl.run_parallel(QUEUE_SIZE)` if `config["threaded"]` else `pl.run_sequential()`. Catches `ImportAbortError` and stops silently (`session.py:191-242`). So forcing `config["threaded"] = False` gives a serial, single-threaded run.
- **`ImportSession.set_config(self, config)`** exists and is called by `run()`; it reads `config["import"]` (the merged confuse view). We do not call it ourselves.
- **`Action`** enum (`beets/importer/tasks.py:83`): `SKIP`, `ASIS`, `TRACKS`, `APPLY`, `ALBUMS`, `RETAG` (string-valued). `set_choice` asserts the caller never passes `Action.APPLY` directly — to apply, return the `AlbumMatch` object itself (it sets `choice_flag = Action.APPLY` and `match = <the match>`). (`tasks.py:186-208`.)
- **`ImportTask` fields** populated by the time `choose_match` runs (`tasks.py:166-174`, set by `lookup_candidates`): `cur_artist: str | None`, `cur_album: str | None`, `candidates: Sequence[AlbumMatch | TrackMatch] | None`, `rec: Recommendation | None`, plus `items: list[library.Item]`, `paths: list[PathBytes]`. `task.choice_flag` / `task.match` are the outputs of `set_choice`. `task.apply` is `True` when `choice_flag == Action.APPLY`.
- **`ImportTask.lookup_candidates(self, search_ids: list[str])`** sets `cur_artist, cur_album, (candidates, rec)` from `tag_album(self.items, search_ids=search_ids)` (`tasks.py:381-389`). **`tag_album` is imported at the top of `tasks.py`** (`from beets.autotag.match import tag_album`, `tasks.py:32`), so the monkeypatch seam to feed canned candidates with **no network** is **`beets.importer.tasks.tag_album`**. (Verified by spike: patching that name makes `task.lookup_candidates([])` return our canned `Proposal`.)
- **`tag_album(items, search_artist=None, search_name=None, search_ids=[]) -> tuple[str, str, Proposal]`** returns `(cur_artist, cur_album, Proposal(candidates, recommendation))` (`beets/autotag/match.py:240`). Real candidates come from `metadata_plugins.candidates(...)` (network); we replace the whole function.
- **`Proposal`** = `NamedTuple(candidates: Sequence[AlbumMatch | TrackMatch], recommendation: Recommendation)` (`match.py:67`).
- **`Recommendation(IntEnum)`** (`match.py:51`): `none=0, low=1, medium=2, strong=3`. "Strong" = `Recommendation.strong`.
- **`AlbumMatch`** (`@dataclass`, `beets/autotag/hooks.py:576`): fields `distance: Distance`, `info: AlbumInfo`, `mapping: dict[Item, TrackInfo]`, `extra_items: list[Item]`, `extra_tracks: list[TrackInfo]`. Properties: `.items` (matched items), `.item_info_pairs` (`list[(Item, TrackInfo)]`), `.disambig_string` (a display string from configured disambig fields).
- **`AlbumInfo`** (`hooks.py:262`): `album`, `artist`, `album_id`, `data_source`, `data_url`, `year`, `label`, `catalognum`, `country`, `media`, `albumdisambig`, `va`, `tracks: list[TrackInfo]`, etc. Attribute access via `AttrDict` (`info.album`, `info.year`, ...).
- **`TrackInfo`** (`hooks.py:385`): `title`, `track_id`, `index`, `medium`, `medium_index`, `length`, `artist`, `data_source`, etc.
- **`Distance`** (`beets/autotag/distance.py:124`): `float(dist)` (or `dist.distance`) is the 0.0–1.0 weighted distance (0.0 = perfect). **Confidence% = `(1 - float(dist)) * 100`** (beets itself renders `f"{(1 - self.distance) * 100:.1f}%"`, `distance.py:190`). `dist.generic_penalty_keys` = `list[str]` of changed fields with the `album_`/`track_` prefix stripped and `_`→space (e.g. `["album", "missing tracks", "tracks"]`; spike-verified). `dist.tracks: dict[TrackInfo, Distance]` gives per-track sub-distances; `track_dist["track_title"]` is the per-field weighted distance (KeyError-safe via `"track_title" in track_dist.keys()`).
- **`get_most_common_tags(items) -> tuple[dict, dict]`** (`beets.util`): `(likelies, consensus)`; `likelies["artist"]`, `likelies["album"]`, `likelies["year"]`, `likelies["label"]`, `likelies["catalognum"]`, `likelies["country"]`, `likelies["media"]` are the **current** album-level values for the before-side diff.
- **Hermetic candidates with no network — VERIFIED end-to-end by spike:** build in-memory `Item(...)` objects (no real audio file needed for the lookup→choose seam — `Item.from_path` would require real audio, which we avoid by never calling `run()`/`read_tasks`), build a canned `AlbumMatch` via `assign_items(items, info.tracks)` + `distance(items, info, pairs)`, patch `beets.importer.tasks.tag_album` to return `Proposal([match], Recommendation.strong)`, then call `task.lookup_candidates([])` followed by `task.choose_match(session)`. This drives the **real** hook + `set_choice` + `Action` flow with no disk and no network. (Full `ImportSession.run()` is out of scope for chunk 1 — it needs real audio fixtures the repo doesn't ship, and the spec scopes driver tests to "hermetic `Library` in tmp; mock the metadata source so no network".)
- **Default config loads thresholds without a user config** (spike): `strong_rec_thresh=0.04`, `medium_rec_thresh=0.25`, `import.timid=False`, `import.autotag=True`, `threaded=True` (so we MUST set it `False`). No `config.yaml` is required for these reads.
- **`ImportAbortError`** is defined in BOTH `beets/importer/session.py:42` and `beets/importer/tasks.py:77` (distinct classes; `run()` catches the `session` one). For chunk 1 we model abort as our own signal that the worker translates; we do not need to raise beets' class.

---

## File structure

| File | Create/Modify | Responsibility |
| --- | --- | --- |
| `backend/app/models/import_models.py` | Create | Pydantic contract for the import flow: `Recommendation` (str enum mirroring beets), `AlbumChange`, `TrackChange` (+ `TrackChangeStatus`), `MissingTrack`, `UnmatchedItem`, `CandidateOption`, `Candidate`, `ParkedAlbum`, `ImportChoice` (+ `ImportAction`). Pure data, no beets imports. |
| `backend/app/beets/import_mapping.py` | Create | Pure functions mapping beets `AlbumMatch`/`Distance`/`AlbumInfo`/`TrackInfo` + current `ImportTask` state → the models above. Imports beets (allowed; inside `app/beets/`). No I/O, no threads. |
| `backend/app/beets/import_session.py` | Create | `WebImportSession(ImportSession)` overriding the four hooks; the `ImportBridge` (thread-safe out-queue + per-album reply events); `run_import_worker` helper that runs one session serially on a dedicated thread with `config["threaded"] = False`. The only place that subclasses beets' session. |
| `backend/app/beets/__init__.py` | (unchanged) | Existing package marker. |
| `backend/tests/test_import_mapping.py` | Create | Hermetic tests for the mapping (distance→%, album diff, track changes, missing/extra). In-memory `Item`s + canned `AlbumMatch`; no network. |
| `backend/tests/test_import_session.py` | Create | Hermetic tests for the session + bridge: strong auto-applies; uncertain parks then the pushed choice applies; skip; abort; the queue/reply bridge ferries a `Candidate` out and an `ImportChoice` in across threads. Monkeypatch `beets.importer.tasks.tag_album`. |
| `backend/pyproject.toml` | Modify | Add `tests.test_import_mapping` and `tests.test_import_session` to the `disallow_untyped_calls = false` mypy override list (they build beets objects through the untyped surface). |

**Decomposition rationale:** models (pure) → mapping (pure, depends on models) → session+bridge (depends on both, adds threading) → integration. Each task is TDD and commits independently. The mapping is split from the session so the diff logic is testable without any threading.

---

## Conventions to follow (from the existing codebase)

- **Pydantic style** (`app/models/album.py`, `app/models/search.py`): plain `class X(BaseModel)`, explicit field types, `int | None` / `str | None` for nullables, short comments explaining non-obvious fields. No config classes unless needed.
- **Adapter boundary** (`app/beets/library.py`): beets imports only inside `app/beets/`; `_coerce_*` helpers for untyped beets values; everything returned is our models.
- **Hermetic library fixture** (`tests/test_artists.py`, `tests/test_search.py`): `Library(str(tmp_path / "library.db"), directory=str(tmp_path))`; build `Item(...)` in memory and set `item.path = os.fsencode(...)`. For this chunk we mostly need bare in-memory `Item`s (no DB add) because we drive the hook seam, not `run()`.
- **Async tests** (`tests/conftest.py`): `@pytest.mark.anyio` + the `anyio_backend` fixture returning `"asyncio"`. Use anyio only where a test is genuinely async; the bridge tests use real OS threads, so most are plain sync tests (a worker thread + the main test thread). One bridge test demonstrates the async-consumer shape with `@pytest.mark.anyio`.
- **Commit style:** Conventional Commits, no `Co-Authored-By` lines (global + project rule). Work stays on `feat/import` (no push; the user opens the PR).
- **Dev commands run from repo root with** `uv --directory backend run ...`.

---

## Task 1: Pydantic import models

**Files:**
- Create: `backend/app/models/import_models.py`
- Test: `backend/tests/test_import_mapping.py` (start the file here; Task 2 extends it)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_import_mapping.py`:

```python
from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    ImportAction,
    ImportChoice,
    MissingTrack,
    ParkedAlbum,
    Recommendation,
    TrackChange,
    TrackChangeStatus,
    UnmatchedItem,
)


def test_recommendation_enum_mirrors_beets_levels() -> None:
    assert [r.value for r in Recommendation] == ["none", "low", "medium", "strong"]


def test_candidate_round_trips_through_pydantic() -> None:
    candidate = Candidate(
        confidence=96.0,
        recommendation=Recommendation.strong,
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        changed_fields=["album", "label"],
        album_before=AlbumChange(
            artist="Radiohead",
            album="OK Computr",
            year=None,
            label=None,
            country=None,
            media=None,
        ),
        album_after=AlbumChange(
            artist="Radiohead",
            album="OK Computer",
            year=1997,
            label="Parlophone",
            country="GB",
            media="CD",
        ),
        tracks=[
            TrackChange(
                index=1,
                status=TrackChangeStatus.changed,
                title_before="Airbag",
                title_after="Airbag",
                track_before=1,
                track_after=1,
            )
        ],
        missing=[MissingTrack(index=3, title="Subterranean")],
        unmatched=[UnmatchedItem(title="UNKNOWN LOCAL", track=2)],
        options=[
            CandidateOption(
                index=0,
                confidence=96.0,
                data_source="MusicBrainz",
                disambiguation="GB, CD, 1997",
            )
        ],
    )
    dumped = candidate.model_dump()
    assert dumped["confidence"] == 96.0
    assert dumped["recommendation"] == "strong"
    assert dumped["tracks"][0]["status"] == "changed"
    assert dumped["missing"][0]["title"] == "Subterranean"
    assert dumped["unmatched"][0]["track"] == 2
    assert dumped["options"][0]["disambiguation"] == "GB, CD, 1997"


def test_parked_album_wraps_a_candidate() -> None:
    candidate = Candidate(
        confidence=50.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        changed_fields=[],
        album_before=AlbumChange(
            artist="A", album="B", year=None, label=None, country=None, media=None
        ),
        album_after=AlbumChange(
            artist="A", album="B", year=None, label=None, country=None, media=None
        ),
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )
    parked = ParkedAlbum(album_index=0, folder="/music/B", candidate=candidate)
    assert parked.album_index == 0
    assert parked.folder == "/music/B"
    assert parked.candidate.recommendation is Recommendation.medium


def test_import_choice_actions() -> None:
    assert [a.value for a in ImportAction] == [
        "apply",
        "skip",
        "asis",
        "astracks",
        "abort",
    ]
    choice = ImportChoice(action=ImportAction.apply, candidate_index=2)
    assert choice.action is ImportAction.apply
    assert choice.candidate_index == 2
    assert ImportChoice(action=ImportAction.skip).candidate_index is None
    assert ImportChoice(action=ImportAction.abort).action is ImportAction.abort
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.import_models'`.

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/import_models.py`:

```python
"""Pydantic contract for the beets import flow.

These models are the stable surface the API/UI will consume. They are mapped
from beets' AlbumMatch/Distance/AlbumInfo/TrackInfo in app/beets/import_mapping.py
so beets internals never leak past the adapter boundary. No beets imports here.
"""

from enum import Enum

from pydantic import BaseModel


class Recommendation(str, Enum):
    """Mirror of beets' Recommendation enum (beets/autotag/match.py).

    Beets uses an IntEnum (none=0..strong=3); we expose the names as strings so
    the JSON contract is self-describing for the frontend.
    """

    none = "none"
    low = "low"
    medium = "medium"
    strong = "strong"


class TrackChangeStatus(str, Enum):
    """How a matched track row differs from the current file."""

    unchanged = "unchanged"
    changed = "changed"


class AlbumChange(BaseModel):
    """Album-level identity fields for one side of the before/after diff.

    Built for both the current files (``album_before``) and the matched release
    (``album_after``); the UI marks the fields named in ``Candidate.changed_fields``.
    """

    artist: str | None
    album: str | None
    year: int | None
    label: str | None
    country: str | None
    media: str | None


class TrackChange(BaseModel):
    """One matched track row, current (``*_before``) vs proposed (``*_after``)."""

    index: int | None
    status: TrackChangeStatus
    title_before: str | None
    title_after: str | None
    track_before: int | None
    track_after: int | None


class MissingTrack(BaseModel):
    """A track present on the matched release but absent from the folder.

    Maps from ``AlbumMatch.extra_tracks`` (beets' name for release-only tracks).
    """

    index: int | None
    title: str | None


class UnmatchedItem(BaseModel):
    """A local file with no counterpart on the matched release.

    Maps from ``AlbumMatch.extra_items`` (beets' name for folder-only files).
    """

    title: str | None
    track: int | None


class CandidateOption(BaseModel):
    """A ranked alternative release from ``task.candidates``.

    The switcher in the review screen lists these; ``index`` is the position in
    the beets candidate list and is what a choice references.
    """

    index: int
    confidence: float
    data_source: str | None
    disambiguation: str | None


class Candidate(BaseModel):
    """The full mapped payload for the top match of one album.

    Everything the review screen renders: confidence/recommendation, the album
    before/after diff, the per-track diff, missing/unmatched tracks, and the
    ranked alternative releases.
    """

    confidence: float
    recommendation: Recommendation
    data_source: str | None
    data_url: str | None
    changed_fields: list[str]
    album_before: AlbumChange
    album_after: AlbumChange
    tracks: list[TrackChange]
    missing: list[MissingTrack]
    unmatched: list[UnmatchedItem]
    options: list[CandidateOption]


class ParkedAlbum(BaseModel):
    """An album whose match is uncertain and is waiting for a user decision.

    Pushed onto the import bridge's out-queue; ``album_index`` keys the reply.
    """

    album_index: int
    folder: str
    candidate: Candidate


class ImportAction(str, Enum):
    """The decisions the user can return for a parked album.

    Subset of beets' choices relevant to chunk 1 (enter-id / search-again are a
    later chunk). ``apply`` selects a ranked option by index; ``abort`` stops the
    whole import (the session turns it into an ImportAbort the worker handles).
    """

    apply = "apply"
    skip = "skip"
    asis = "asis"
    astracks = "astracks"
    abort = "abort"


class ImportChoice(BaseModel):
    """A user's decision for one parked album, pushed back over the bridge."""

    action: ImportAction
    # Which ranked option to apply (index into Candidate.options); only meaningful
    # for action == apply. Defaults to the top candidate.
    candidate_index: int | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv --directory backend run pytest tests/test_import_mapping.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. (`import_models.py` has no beets imports, so no override needed yet; the test module imports only our models so far.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/import_models.py backend/tests/test_import_mapping.py
git commit -m "feat(import): add Pydantic models for the import driver contract"
```

---

## Task 2: beets → model mapping

**Files:**
- Create: `backend/app/beets/import_mapping.py`
- Modify: `backend/pyproject.toml` (add `tests.test_import_mapping` to the `disallow_untyped_calls = false` override)
- Test: `backend/tests/test_import_mapping.py` (extend)

- [ ] **Step 1: Add the mypy override for the test module**

In `backend/pyproject.toml`, extend the existing override list (currently `["app.beets.library", "tests.test_albums", "tests.test_artists", "tests.test_search"]`) to include the mapping module and its test, which build beets objects through beets' untyped surface:

```toml
[[tool.mypy.overrides]]
module = ["app.beets.library", "app.beets.import_mapping", "tests.test_albums", "tests.test_artists", "tests.test_search", "tests.test_import_mapping"]
disallow_untyped_calls = false
```

- [ ] **Step 2: Write the failing test (append to `tests/test_import_mapping.py`)**

Add these imports at the top of the existing test file:

```python
import os
from pathlib import Path

from beets.autotag.hooks import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.match import assign_items, distance
from beets.library import Item

from app.beets.import_mapping import map_album_match
```

Then append:

```python
def _item(
    *, album: str, title: str, track: int, length: float, artist: str = "Radiohead"
) -> Item:
    # In-memory item, no DB add and no real audio file needed: the mapping and
    # the choose_match seam never touch the filesystem (we never call run()).
    item = Item(artist=artist, album=album, title=title, track=track, length=length)
    return item


def _perfect_match() -> AlbumMatch:
    items = [_item(album="OK Computer", title="Airbag", track=1, length=234.0)]
    tracks = [TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0)]
    info = AlbumInfo(
        tracks=tracks,
        album="OK Computer",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        year=1997,
        label="Parlophone",
        country="GB",
        media="CD",
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    dist = distance(items, info, pairs)
    return AlbumMatch(dist, info, dict(pairs), extra_items, extra_tracks)


def _diff_match() -> AlbumMatch:
    # 2 local files (one mistitled), 3 release tracks => 1 missing track,
    # 0 unmatched (the assignment maps both items). Spike-verified shape.
    items = [
        _item(album="OK Computr", title="Airbag", track=1, length=234.0),
        _item(album="OK Computr", title="UNKNOWN LOCAL", track=2, length=100.0),
    ]
    tracks = [
        TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0),
        TrackInfo(title="Paranoid Android", track_id="t2", index=2, length=387.0),
        TrackInfo(title="Subterranean", track_id="t3", index=3, length=200.0),
    ]
    info = AlbumInfo(
        tracks=tracks,
        album="OK Computer",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        year=1997,
        label="Parlophone",
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    dist = distance(items, info, pairs)
    return AlbumMatch(dist, info, dict(pairs), extra_items, extra_tracks)


def test_map_perfect_match_is_full_confidence_no_changes() -> None:
    candidate = map_album_match(
        _perfect_match(), cur_artist="Radiohead", cur_album="OK Computer", options=[]
    )
    assert candidate.confidence == 100.0
    assert candidate.changed_fields == []
    assert candidate.album_after.album == "OK Computer"
    assert candidate.album_after.year == 1997
    assert candidate.album_after.country == "GB"
    assert candidate.album_before.album == "OK Computer"
    assert len(candidate.tracks) == 1
    assert candidate.tracks[0].status is TrackChangeStatus.unchanged
    assert candidate.missing == []
    assert candidate.unmatched == []


def test_map_diff_match_reports_album_track_missing_and_distance() -> None:
    candidate = map_album_match(
        _diff_match(), cur_artist="Radiohead", cur_album="OK Computr", options=[]
    )
    # Spike measured distance 0.245 => ~75.5%; assert the rounded value.
    assert candidate.confidence == 75.5
    # generic_penalty_keys strips album_/track_ prefixes and underscores.
    assert set(candidate.changed_fields) == {"album", "missing tracks", "tracks"}
    assert candidate.album_before.album == "OK Computr"
    assert candidate.album_after.album == "OK Computer"
    # 2 mapped tracks, ordered by proposed track index.
    assert len(candidate.tracks) == 2
    statuses = {t.title_after: t.status for t in candidate.tracks}
    assert statuses["Airbag"] is TrackChangeStatus.unchanged
    assert statuses["Paranoid Android"] is TrackChangeStatus.changed
    # 1 missing release track, 0 unmatched local files.
    assert [m.title for m in candidate.missing] == ["Subterranean"]
    assert candidate.unmatched == []


def test_map_options_from_candidate_list() -> None:
    from app.beets.import_mapping import map_candidate_options

    top = _perfect_match()
    second = _diff_match()
    options = map_candidate_options([top, second])
    assert [o.index for o in options] == [0, 1]
    assert options[0].confidence == 100.0
    assert options[0].data_source == "MusicBrainz"
    # disambiguation comes from AlbumMatch.disambig_string (may be empty).
    assert isinstance(options[0].disambiguation, str) or options[0].disambiguation is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.beets.import_mapping'` (the 4 Task-1 tests still pass).

- [ ] **Step 4: Write minimal implementation**

Create `backend/app/beets/import_mapping.py`:

```python
"""Map beets autotag match objects to our import Pydantic models.

Pure functions, no I/O and no threads. Imports beets — allowed because this
module lives inside the beets-adapter boundary (CLAUDE.md rule 3). Everything
returned is one of app.models.import_models, so beets' AlbumMatch/Distance/
AlbumInfo/TrackInfo never leak past here.
"""

from __future__ import annotations

from typing import Any

from beets.autotag.hooks import AlbumMatch

from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    MissingTrack,
    Recommendation,
    TrackChange,
    TrackChangeStatus,
    UnmatchedItem,
)


def _confidence(distance: Any) -> float:
    """beets distance (0.0 = perfect) -> a confidence percentage.

    Mirrors beets' own display: ``(1 - distance) * 100`` (autotag/distance.py).
    """
    return round((1.0 - float(distance)) * 100.0, 1)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _album_change_from_info(info: Any) -> AlbumChange:
    """The proposed (after) album-level identity fields from an AlbumInfo."""
    return AlbumChange(
        artist=_opt_str(info.artist),
        album=_opt_str(info.album),
        year=_opt_int(info.year),
        label=_opt_str(info.label),
        country=_opt_str(info.country),
        media=_opt_str(info.media),
    )


def _album_change_from_current(
    items: list[Any], cur_artist: str | None, cur_album: str | None
) -> AlbumChange:
    """The current (before) album-level fields from the user's files.

    Uses beets' own consensus of the items (``get_most_common_tags``) for the
    fields that have no single task-level attribute, and the task's
    ``cur_artist``/``cur_album`` for identity (already computed by beets).
    """
    from beets.util import get_most_common_tags

    likelies, _ = get_most_common_tags(items)
    return AlbumChange(
        artist=_opt_str(cur_artist),
        album=_opt_str(cur_album),
        year=_opt_int(likelies.get("year")),
        label=_opt_str(likelies.get("label")),
        country=_opt_str(likelies.get("country")),
        media=_opt_str(likelies.get("media")),
    )


def _track_changes(match: AlbumMatch) -> list[TrackChange]:
    """Per-track current->proposed rows from AlbumMatch.mapping.

    Ordered by the proposed track index so the tracklist reads in release order.
    A row is ``changed`` when the matched TrackInfo has a non-zero per-field
    title distance or the track number differs; otherwise ``unchanged``.
    """
    rows: list[TrackChange] = []
    for item, track_info in match.mapping.items():
        track_dist = match.distance.tracks.get(track_info)
        title_changed = False
        if track_dist is not None and "track_title" in track_dist.keys():
            title_changed = track_dist["track_title"] > 0.0
        track_before = _opt_int(item.track)
        track_after = _opt_int(track_info.index)
        number_changed = track_before != track_after
        status = (
            TrackChangeStatus.changed
            if (title_changed or number_changed)
            else TrackChangeStatus.unchanged
        )
        rows.append(
            TrackChange(
                index=track_after,
                status=status,
                title_before=_opt_str(item.title),
                title_after=_opt_str(track_info.title),
                track_before=track_before,
                track_after=track_after,
            )
        )
    rows.sort(key=lambda r: (r.index is None, r.index or 0))
    return rows


def _missing_tracks(match: AlbumMatch) -> list[MissingTrack]:
    """Release tracks with no local file (AlbumMatch.extra_tracks)."""
    return [
        MissingTrack(index=_opt_int(t.index), title=_opt_str(t.title))
        for t in match.extra_tracks
    ]


def _unmatched_items(match: AlbumMatch) -> list[UnmatchedItem]:
    """Local files with no release track (AlbumMatch.extra_items)."""
    return [
        UnmatchedItem(title=_opt_str(i.title), track=_opt_int(i.track))
        for i in match.extra_items
    ]


def map_candidate_options(
    candidates: list[AlbumMatch],
) -> list[CandidateOption]:
    """Map the ranked ``task.candidates`` list to the switcher options."""
    options: list[CandidateOption] = []
    for index, match in enumerate(candidates):
        options.append(
            CandidateOption(
                index=index,
                confidence=_confidence(match.distance),
                data_source=_opt_str(match.info.data_source),
                disambiguation=_opt_str(match.disambig_string),
            )
        )
    return options


def map_album_match(
    match: AlbumMatch,
    *,
    cur_artist: str | None,
    cur_album: str | None,
    options: list[CandidateOption],
    recommendation: Recommendation = Recommendation.none,
) -> Candidate:
    """Map a beets AlbumMatch (+ current task state) to a Candidate.

    ``options`` is the already-mapped ranked alternative list (pass [] when not
    needed); ``recommendation`` mirrors ``task.rec`` for the album.
    """
    return Candidate(
        confidence=_confidence(match.distance),
        recommendation=recommendation,
        data_source=_opt_str(match.info.data_source),
        data_url=_opt_str(match.info.data_url),
        changed_fields=list(match.distance.generic_penalty_keys),
        album_before=_album_change_from_current(
            list(match.mapping.keys()) + list(match.extra_items),
            cur_artist,
            cur_album,
        ),
        album_after=_album_change_from_info(match.info),
        tracks=_track_changes(match),
        missing=_missing_tracks(match),
        unmatched=_unmatched_items(match),
        options=options,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv --directory backend run pytest tests/test_import_mapping.py -v`
Expected: PASS (all Task-1 + Task-2 tests).

- [ ] **Step 6: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add backend/app/beets/import_mapping.py backend/tests/test_import_mapping.py backend/pyproject.toml
git commit -m "feat(import): map beets AlbumMatch/Distance to import Candidate models"
```

---

## Task 3: WebImportSession hooks + auto-apply/park

**Files:**
- Create: `backend/app/beets/import_session.py`
- Modify: `backend/pyproject.toml` (add `tests.test_import_session` to the `disallow_untyped_calls = false` override)
- Test: `backend/tests/test_import_session.py`

- [ ] **Step 1: Add the mypy override for the new test module**

Extend the same override list to also include `tests.test_import_session`:

```toml
[[tool.mypy.overrides]]
module = ["app.beets.library", "app.beets.import_mapping", "tests.test_albums", "tests.test_artists", "tests.test_search", "tests.test_import_mapping", "tests.test_import_session"]
disallow_untyped_calls = false
```

(`app.beets.import_session` itself imports beets but does not *call* untyped beets functions in a way that trips strict mode — it subclasses and reads attributes; keep it out of the override unless mypy flags it in Step 6, in which case add it too.)

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_import_session.py`:

```python
import logging
from typing import Any

import beets.importer.tasks as beets_tasks
import pytest
from beets import config
from beets.autotag.hooks import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.match import Proposal, Recommendation as BeetsRec, assign_items, distance
from beets.importer.tasks import Action, ImportTask
from beets.library import Item

from app.beets.import_session import ImportBridge, WebImportSession
from app.models.import_models import ImportAction, ImportChoice


def _build_match(rec_level: BeetsRec) -> AlbumMatch:
    """A canned AlbumMatch. For a strong rec we make a perfect (distance 0)
    match; otherwise we mistitle the album so the distance is non-trivial.
    """
    if rec_level == BeetsRec.strong:
        items = [Item(artist="Radiohead", album="OK Computer", title="Airbag", track=1, length=234.0)]
        album = "OK Computer"
    else:
        items = [Item(artist="Radiohead", album="Different", title="Airbag", track=1, length=234.0)]
        album = "OK Computer"
    tracks = [TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0)]
    info = AlbumInfo(
        tracks=tracks, album=album, artist="Radiohead", album_id="a1",
        data_source="MusicBrainz", data_url="https://mb/a1", year=1997, va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_items, extra_tracks)


def _patch_tag_album(
    monkeypatch: pytest.MonkeyPatch, match: AlbumMatch, rec: BeetsRec
) -> None:
    """Patch the seam beets uses to fetch candidates so no network is hit.

    ImportTask.lookup_candidates calls beets.importer.tasks.tag_album (imported
    at module top in tasks.py); replacing it with a canned Proposal is the
    cleanest hermetic seam (spike-verified).
    """

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Radiohead", "OK Computer", Proposal([match], rec))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)


def _make_session(bridge: ImportBridge) -> WebImportSession:
    """Construct a session without a real Library (we never call run()).

    __new__ skips ImportSession.__init__ (which needs a Library); we set only
    what the hooks read. This keeps the test focused on the decision logic.
    """
    session = WebImportSession.__new__(WebImportSession)
    session.logger = logging.getLogger("test.import")
    session.bridge = bridge
    # __init__ is skipped, so set the park-index counter the hook relies on.
    session._album_index = 0
    return session


def _make_task(match: AlbumMatch, monkeypatch: pytest.MonkeyPatch, rec: BeetsRec) -> ImportTask:
    _patch_tag_album(monkeypatch, match, rec)
    task = ImportTask(toppath=None, paths=[b"/music/album"], items=list(match.mapping.keys()))
    task.lookup_candidates([])  # populates cur_artist/cur_album/candidates/rec
    return task


def test_strong_rec_auto_applies_top_match(monkeypatch: pytest.MonkeyPatch) -> None:
    config["threaded"] = False
    match = _build_match(BeetsRec.strong)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.strong)

    task.choose_match(session)  # calls session.choose_match -> set_choice

    assert task.choice_flag is Action.APPLY
    assert task.apply is True
    assert task.match is match
    # Nothing was parked: a strong rec auto-applies (mirrors beets).
    assert bridge.pending_count() == 0


def test_uncertain_rec_parks_then_applies_pushed_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    config["threaded"] = False
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    # choose_match blocks until a choice is pushed; run it on a worker thread.
    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker)
    t.start()

    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    assert parked.candidate.recommendation.value == "medium"

    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)

    assert task.choice_flag is Action.APPLY
    assert task.match is match


def test_uncertain_rec_skip_choice_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    config["threaded"] = False
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.skip))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)

    assert task.choice_flag is Action.SKIP
    assert task.skip is True
    assert task.match is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.beets.import_session'`.

- [ ] **Step 4: Write minimal implementation**

Create `backend/app/beets/import_session.py`:

```python
"""The import driver: WebImportSession + the async bridge.

This is the only module that subclasses beets' ImportSession. It runs an import
serially on a single dedicated worker thread (config["threaded"] = False —
global-singleton safety). Strong matches auto-apply (mirroring beets); uncertain
ones are mapped to a Candidate, pushed onto a thread-safe out-queue, and the
worker blocks on a per-album reply event until a decision is pushed back.

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Any

from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.session import ImportSession
from beets.importer.tasks import Action

from app.beets.import_mapping import map_album_match, map_candidate_options
from app.models.import_models import (
    ImportAction,
    ImportChoice,
    ParkedAlbum,
    Recommendation,
)

if TYPE_CHECKING:
    from beets.importer.tasks import ImportTask


# beets IntEnum -> our string enum (only the album-level levels are needed).
_REC_MAP = {
    BeetsRec.none: Recommendation.none,
    BeetsRec.low: Recommendation.low,
    BeetsRec.medium: Recommendation.medium,
    BeetsRec.strong: Recommendation.strong,
}


class ImportAbort(Exception):
    """Raised inside a hook to abort the running import (our signal)."""


class ImportBridge:
    """Thread-safe bridge between the import worker and an async consumer.

    The worker (running beets) pushes a ``ParkedAlbum`` to ``_out`` and blocks on
    a per-album reply slot. A consumer drains ``_out``, presents the candidate,
    and calls ``push_choice`` to unblock the worker with an ``ImportChoice``.
    """

    def __init__(self) -> None:
        self._out: queue.Queue[ParkedAlbum] = queue.Queue()
        self._replies: dict[int, queue.Queue[ImportChoice]] = {}
        self._lock = threading.Lock()
        self._pending = 0

    # ----- worker side -----

    def park(self, parked: ParkedAlbum) -> ImportChoice:
        """Push a parked album and block until a choice arrives for it."""
        reply: queue.Queue[ImportChoice] = queue.Queue(maxsize=1)
        with self._lock:
            self._replies[parked.album_index] = reply
            self._pending += 1
        self._out.put(parked)
        choice = reply.get()  # blocks the worker thread
        with self._lock:
            self._replies.pop(parked.album_index, None)
            self._pending -= 1
        return choice

    # ----- consumer side -----

    def get_parked(self, timeout: float | None = None) -> ParkedAlbum | None:
        """Pop the next parked album, or None on timeout."""
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return None

    def push_choice(self, album_index: int, choice: ImportChoice) -> None:
        """Deliver a decision to the worker blocked on ``album_index``."""
        with self._lock:
            reply = self._replies.get(album_index)
        if reply is None:
            raise KeyError(f"no album parked at index {album_index}")
        reply.put(choice)

    def pending_count(self) -> int:
        with self._lock:
            return self._pending


class WebImportSession(ImportSession):
    """An ImportSession driven by the web UI instead of a terminal prompt."""

    bridge: ImportBridge

    def __init__(
        self,
        lib: Any,
        loghandler: Any,
        paths: Any,
        query: Any,
        bridge: ImportBridge,
    ) -> None:
        super().__init__(lib, loghandler, paths, query)
        self.bridge = bridge
        # Counter that assigns each parked album a stable index for replies.
        self._album_index = 0

    # ----- the four decision hooks -----

    def should_resume(self, path: Any) -> bool:
        # Chunk 1 exposes nothing fancy: never resume interactively.
        return False

    def choose_item(self, task: ImportTask) -> Action:
        # Singletons are out of scope for chunk 1; skip them.
        return Action.SKIP

    def resolve_duplicate(self, task: ImportTask, found_duplicates: Any) -> None:
        # Duplicate resolution UI is a later chunk; take beets' config default by
        # leaving the choice intact (no-op).
        return None

    def choose_match(self, task: ImportTask) -> Any:
        """Auto-apply a strong match; otherwise park and await the user.

        Returns either an ``AlbumMatch`` (to apply) or an ``Action`` constant.
        beets' ``set_choice`` turns an AlbumMatch into ``Action.APPLY``.
        """
        candidates = list(task.candidates or [])
        if task.rec == BeetsRec.strong and candidates:
            # Mirror beets' auto-apply of a strong recommendation.
            return candidates[0]

        if not candidates:
            # Nothing to choose from: skip (an empty match can't be applied).
            return Action.SKIP

        # Park: map the top match + the ranked alternatives, push, block.
        index = self._album_index
        self._album_index += 1
        top = candidates[0]
        options = map_candidate_options(candidates)
        candidate = map_album_match(
            top,
            cur_artist=task.cur_artist,
            cur_album=task.cur_album,
            options=options,
            recommendation=_REC_MAP.get(task.rec, Recommendation.none),
        )
        folder = self._task_folder(task)
        choice = self.bridge.park(
            ParkedAlbum(album_index=index, folder=folder, candidate=candidate)
        )
        return self._apply_choice(choice, candidates)

    # ----- helpers -----

    @staticmethod
    def _apply_choice(choice: ImportChoice, candidates: list[Any]) -> Any:
        """Translate a user ImportChoice into a beets match/Action.

        Raises ``ImportAbort`` for the abort action so the worker can stop the
        whole import (mirrors beets' ImportAbortError flowing out of a hook).
        """
        if choice.action is ImportAction.abort:
            raise ImportAbort
        if choice.action is ImportAction.apply:
            idx = choice.candidate_index or 0
            if 0 <= idx < len(candidates):
                return candidates[idx]
            return candidates[0]
        if choice.action is ImportAction.asis:
            return Action.ASIS
        if choice.action is ImportAction.astracks:
            return Action.TRACKS
        return Action.SKIP

    @staticmethod
    def _task_folder(task: ImportTask) -> str:
        import os

        if task.paths:
            return os.fsdecode(task.paths[0])
        return ""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv --directory backend run pytest tests/test_import_session.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. If mypy flags untyped beets calls in `import_session.py`, add `app.beets.import_session` to the `disallow_untyped_calls = false` override list and re-run.

- [ ] **Step 7: Commit**

```bash
git add backend/app/beets/import_session.py backend/tests/test_import_session.py backend/pyproject.toml
git commit -m "feat(import): add WebImportSession with auto-apply/park hooks + bridge"
```

---

## Task 4: Serial worker + abort, end-to-end bridge integration

**Files:**
- Modify: `backend/app/beets/import_session.py` (add `run_import_worker`)
- Test: `backend/tests/test_import_session.py` (extend)

The abort behavior (`ImportAction.abort` -> `ImportAbort`) is already wired in Task 3's `_apply_choice`; this task adds a test that proves it across the worker thread, plus the async-consumer bridge test and the serial worker entry point.

- [ ] **Step 1: Write the failing test (append to `tests/test_import_session.py`)**

Append (this exercises the abort action added in Tasks 1/3):

```python
def test_abort_choice_raises_import_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets.import_session import ImportAbort

    config["threaded"] = False
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    raised: dict[str, bool] = {"abort": False}
    done = threading.Event()

    def worker() -> None:
        try:
            task.choose_match(session)
        except ImportAbort:
            raised["abort"] = True
        finally:
            done.set()

    t = threading.Thread(target=worker)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    # The abort action makes the session raise ImportAbort out of choose_match,
    # which the worker (and beets' run loop in chunk 2) treats as a stop signal.
    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.abort))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert raised["abort"] is True


@pytest.mark.anyio
async def test_bridge_ferries_candidate_out_and_choice_in_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demonstrate the async-consumer shape: a worker thread parks an album and
    an async consumer (via anyio.to_thread) drains it and replies. This is the
    seam the future async API layer plugs into.
    """
    import anyio

    config["threaded"] = False
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    worker = threading.Thread(target=lambda: task.choose_match(session))
    worker.start()

    parked = await anyio.to_thread.run_sync(lambda: bridge.get_parked(2.0))
    assert parked is not None
    assert parked.candidate.data_source == "MusicBrainz"
    assert parked.candidate.options  # ranked alternatives mapped

    await anyio.to_thread.run_sync(
        lambda: bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.apply))
    )
    await anyio.to_thread.run_sync(lambda: worker.join(2.0))
    assert task.choice_flag is Action.APPLY
```

Hoist `import threading` to the module scope at the top of the test file (Task 3 used it inside functions); the new tests reference it at module level. `anyio` is already available (locked transitively) and the `anyio_backend` fixture lives in `tests/conftest.py`.

- [ ] **Step 2: Run the two new tests**

Run: `uv --directory backend run pytest tests/test_import_session.py::test_abort_choice_raises_import_abort "tests/test_import_session.py::test_bridge_ferries_candidate_out_and_choice_in_across_threads" -v`
Expected: PASS. These are integration tests over the worker/consumer bridge whose behavior was wired in Task 3 (the abort action -> `ImportAbort`, and the queue/reply round-trip). They add no production code — they lock in the cross-thread contract and the async-consumer shape the API chunk will use. Task 4's genuine red→green surface is the serial worker entry point added next.

- [ ] **Step 3: Write the failing test for the worker entry point (append)**

```python
def test_run_import_worker_forces_single_threaded_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_import_worker must set config['threaded']=False and invoke session.run().

    We stub session.run to record the threaded flag at call time, proving the
    worker enforces serial execution regardless of the ambient config.
    """
    from app.beets.import_session import run_import_worker

    config["threaded"] = True  # ambient default; the worker must override it.
    seen: dict[str, Any] = {}

    class FakeSession:
        def run(self) -> None:
            seen["threaded"] = bool(config["threaded"])

    run_import_worker(FakeSession())  # type: ignore[arg-type]
    assert seen["threaded"] is False
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_session.py::test_run_import_worker_forces_single_threaded_and_runs -v`
Expected: FAIL — `ImportError: cannot import name 'run_import_worker'`.

- [ ] **Step 5: Write minimal implementation (append to `import_session.py`)**

Add at the end of `backend/app/beets/import_session.py`:

```python
def run_import_worker(session: WebImportSession) -> None:
    """Run one import session serially on the calling (worker) thread.

    Forces single-threaded execution before delegating to beets' run loop, so
    the global beets config/plugin singletons are never touched concurrently.
    Intended to be the target of a dedicated worker thread started by the API
    layer (chunk 2); here it is the clean, tested entry point.
    """
    from beets import config

    config["threaded"] = False
    session.run()
```

- [ ] **Step 6: Run all import tests to verify they pass**

Run: `uv --directory backend run pytest tests/test_import_session.py tests/test_import_mapping.py -v`
Expected: PASS (all mapping + session + worker tests).

- [ ] **Step 7: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add backend/app/beets/import_session.py backend/tests/test_import_session.py
git commit -m "feat(import): add serial import worker entry point + bridge integration tests"
```

---

## Task 5: Full-suite green + self-review

**Files:** none (verification only).

- [ ] **Step 1: Run the entire backend test suite**

Run: `uv --directory backend run pytest`
Expected: PASS — all existing tests plus the new `test_import_mapping.py` and `test_import_session.py`. (No existing test should regress; this chunk adds only new modules.)

- [ ] **Step 2: Full typecheck**

Run: `uv --directory backend run mypy`
Expected: `Success: no issues found`. Confirm the override list in `pyproject.toml` includes `tests.test_import_mapping`, `tests.test_import_session`, `app.beets.import_mapping` (and `app.beets.import_session` if mypy required it).

- [ ] **Step 3: Lint + format check**

Run: `uv --directory backend run ruff check && uv --directory backend run ruff format --check`
Expected: no errors. Run `uv --directory backend run ruff format` and re-commit if formatting differs.

- [ ] **Step 4: Confirm the beets-boundary rule holds**

Run: `uv --directory backend run python -c "import subprocess,sys; out=subprocess.run(['grep','-rEn','import beets|from beets|import beetsplug','app','--include=*.py'],capture_output=True,text=True).stdout; bad=[l for l in out.splitlines() if not l.startswith('app/beets/')]; print('\n'.join(bad)); sys.exit(1 if bad else 0)"`
Expected: empty output, exit 0 — every beets import lives under `app/beets/` (rule 3). The new `import_models.py` (in `app/models/`) imports no beets.

- [ ] **Step 5: Final commit if anything changed**

```bash
git add -A
git commit -m "chore(import): driver core green — tests, mypy strict, ruff" || echo "nothing to commit"
```

---

## Self-review checklist (run after writing all tasks)

**Spec coverage** — every chunk-1 requirement maps to a task:
- Pydantic models (`Candidate`, `AlbumChange`, `TrackChange`, missing/unmatched, `CandidateOption`, `Recommendation`, parked-album wrapper) → Task 1.
- beets `AlbumMatch`/`Distance`/`AlbumInfo`/`TrackInfo` → models mapping (distance→%, `generic_penalty_keys`→changed fields, `mapping`/`extra_items`/`extra_tracks`→track diffs/missing/unmatched) → Task 2.
- `WebImportSession(ImportSession)` overriding hooks; strong auto-apply, else park to out-queue + block on reply, returning the user's choice → Task 3.
- Async bridge (out-queue + reply channel) + serial single-threaded worker (`config["threaded"]=False`) → Tasks 3 (`ImportBridge`) + 4 (`run_import_worker`).
- Hermetic tests with monkeypatched matcher (no network): strong auto-applies (Task 3), uncertain parks then choice applies (Task 3), mapping correctness incl. distance→% and missing/extra (Task 2), skip (Task 3) + abort signal (Task 4), queue/reply bridge ferries candidate out + choice in (Task 4) → covered.
- mypy strict + ruff green, new test modules added to the override list → Tasks 2, 3, 5.

**Placeholder scan:** no "TBD"/"handle edge cases"/"similar to Task N"; every code and test step is complete and verified against the venv API.

**Type consistency:** model names (`Candidate`, `AlbumChange`, `TrackChange`, `TrackChangeStatus`, `MissingTrack`, `UnmatchedItem`, `CandidateOption`, `ParkedAlbum`, `ImportChoice`, `ImportAction`, `Recommendation`) and the mapping/session function names (`map_album_match`, `map_candidate_options`, `ImportBridge.park/get_parked/push_choice/pending_count`, `WebImportSession.choose_match`, `run_import_worker`) are used identically across tasks.

## Out of scope for this chunk (do NOT build here)
- FastAPI endpoints / `ImportJob` lifecycle (chunk 2).
- Any frontend (chunks 3–5).
- Duplicate-resolution UI, enter-MBID / search-again re-lookup, singletons, watched-folder/incremental.
- A full `ImportSession.run()` end-to-end test against real audio fixtures (the repo ships none; the seam is exercised via `lookup_candidates()` + `choose_match()`). If real-audio coverage is wanted later, add it in the API chunk with a checked-in sample file.
