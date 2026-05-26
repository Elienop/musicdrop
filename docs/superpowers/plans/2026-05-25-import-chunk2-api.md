# Import API + ImportJob Lifecycle (beets) — Chunk 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend import **API** — the `POST /api/import` start endpoint, the `GET /api/import/{job}` state/queue poll, the per-album candidate fetch + choice endpoints, and `POST /api/import/{job}/apply-ready` — on top of an in-memory `ImportJob` lifecycle that runs chunk-1's `WebImportSession`/`run_import_worker` on a dedicated daemon thread, enforces **one import at a time**, and bridges beets' blocking decisions to the async API via chunk-1's `ImportBridge`. Backend-tested hermetically; no frontend.

**Architecture:** A new `app/import_jobs/` package holds the lifecycle. `ImportJobRegistry` is a process-global, **single-slot** registry (one active job; a second start → 409). Starting a job builds an `ImportBridge`, constructs a real `WebImportSession(lib, None, [fsencode(path)], None, bridge)` from the beets `Library` already opened on `app.state.beets_library` (the existing `get_library` dependency), and runs `run_import_worker(session)` on a daemon `threading.Thread`. The worker runs beets serially (`config["threaded"]=False`, chunk 1); strong matches auto-apply, uncertain ones are parked on the bridge. Async endpoints **drain** newly-parked albums into the job's queue with the **non-blocking** `bridge.get_parked(timeout=0)` and deliver decisions with `bridge.push_choice`. The worker entry point is **injectable** (an `ImportRunner` protocol): production builds the real session+thread; tests inject a fake runner that drives the *real* bridge with a canned `ParkedAlbum` then finishes — so the full start→state→choice→done cycle is tested with no MusicBrainz/audio. New Pydantic models (`app/models/import_api.py`) map the job/bridge/queue state to the HTTP contract; no beets object leaks past the adapter.

**Tech Stack:** Python 3.11+, FastAPI 0.136 + Pydantic v2, beets 2.11.0 (pinned), stdlib `threading`, pytest + `fastapi.testclient.TestClient`. mypy `--strict` and Ruff must stay green.

---

## Beets/FastAPI facts this plan encodes (verified against the merged `feat/import` code + the venv at 2.11.0)

These were confirmed by reading the merged chunk-1 source and the venv. Encode them exactly; do not re-guess.

- **`get_library` (the dependency) returns `LibraryHandle | None`** (`backend/app/api/albums.py:11-19`). `LibraryHandle = beets.library.Library` (`backend/app/beets/library.py:37`). It reads `request.app.state.beets_library` (set once in the lifespan, `backend/app/main.py:69-70`) and is **overridden in tests** via `app.dependency_overrides[get_library] = lambda: temp_library` (`backend/tests/test_albums.py:79-83`). Endpoints in this chunk reuse this exact dependency.
- **A real beets `Library` is constructed by `open_library(library_path, directory)`** (`backend/app/beets/library.py:40-46`) → `Library(library_path, directory=directory)`. Tests build one directly: `Library(str(tmp_path / "library.db"), directory=str(tmp_path))` (`backend/tests/test_albums.py:30-33`). `lib.directory` is the music root (bytes).
- **`WebImportSession.__init__(self, lib, loghandler, paths, query, bridge)`** (`backend/app/beets/import_session.py:104-115`) → calls `super().__init__(lib, loghandler, paths, query)` then stores `self.bridge`. The beets base is `ImportSession.__init__(self, lib, loghandler: logging.Handler | None, paths: Sequence[PathBytes] | None, query: dbcore.Query | None)` (venv `beets/importer/session.py`). So for a **path import**: `loghandler=None` (beets installs a `NullHandler`), `paths=[os.fsencode(path)]`, `query=None`.
- **`run_import_worker(session)`** (`backend/app/beets/import_session.py:203-212`) sets `config["threaded"] = False` then calls `session.run()`. `ImportSession.run()` builds the pipeline and runs `pl.run_sequential()`; it **catches `ImportAbortError` and returns silently** (venv `session.py`, tail verified). Any *other* exception propagates out of `run()` — so the worker thread target MUST wrap the call in try/except to mark the job failed.
- **Abort is beets' native `ImportAbortError`** (NOT a custom class). `WebImportSession._apply_choice` raises `from beets.importer.session import ImportAbortError` for `ImportAction.abort` (`backend/app/beets/import_session.py:183-184`); `run()` catches it. So an abort choice ends the import cleanly and `run()` returns normally — the worker then marks the job done, not failed.
- **`ImportBridge` methods** (`backend/app/beets/import_session.py:45-96`), all thread-safe:
  - `park(parked: ParkedAlbum) -> ImportChoice` — worker side; blocks. (Not called by the API.)
  - `get_parked(timeout: float | None = None) -> ParkedAlbum | None` — consumer side; `timeout=0` returns immediately (`None` if the queue is empty). This is the **non-blocking drain** the API uses.
  - `push_choice(album_index: int, choice: ImportChoice) -> None` — raises `KeyError` for an unknown/unparked index, `RuntimeError` for a duplicate push to an already-answered slot.
  - `pending_count() -> int` — albums currently parked-and-waiting.
- **`ParkedAlbum`** (`backend/app/models/import_models.py:113-121`): `album_index: int`, `folder: str`, `candidate: Candidate`. **`Candidate`** (`import_models.py:92-110`): `confidence: float`, `recommendation: Recommendation`, `data_source`/`data_url: str | None`, `changed_fields: list[str]`, `album_before`/`album_after: AlbumChange`, `tracks: list[TrackChange]`, `missing`/`unmatched`, `options: list[CandidateOption]`. **`ImportChoice`** (`import_models.py:140-146`): `action: ImportAction`, `candidate_index: int | None = None`. **`ImportAction`** (`import_models.py:124-137`): `apply`/`skip`/`asis`/`astracks`/`abort`.
- **FastAPI endpoint style** (`backend/app/api/search.py`, `albums.py`, `artists.py`): `router = APIRouter(tags=[...])`; `async def` handlers; `Depends(get_library)`; `HTTPException(status_code=..., detail=...)`; routers mounted in `main.py` with `app.include_router(router, prefix="/api")`. `from fastapi import status` exposes `status.HTTP_409_CONFLICT` (=409), `HTTP_404_NOT_FOUND` (=404), `HTTP_400_BAD_REQUEST` (=400) — all confirmed importable.
- **The bridge calls used by the API are non-blocking** (`get_parked(timeout=0)` and `push_choice` return immediately), so the endpoints can be plain `async def` with **no `anyio.to_thread`** — they never block the event loop. (`anyio` 4.13.0 is present if ever needed; not needed here.)
- **mypy override**: `pyproject.toml:44-46` lists modules with `disallow_untyped_calls = false` (they touch beets' untyped surface). The production runner module + any test module that builds a beets `Library`/`WebImportSession` go in this list. The registry, models, and API router are pure-typed and stay OUT of it.
- **Test infra**: `tests/conftest.py` provides the `anyio_backend` fixture; `TestClient(app)` + `app.dependency_overrides` is the established pattern. `app.state` is shared across `TestClient` instances within a process, but the registry is a **module-level singleton** reset by a fixture each test (see Task 2).

---

## Design decisions (grounded, YAGNI)

- **Where the job lives — a module-level singleton, not `app.state`.** `app.state.beets_library` is set once at startup and never mutated (`main.py:69`); a per-request handler reading it is fine. The import job, by contrast, is **mutated across many requests** (start, drain on each poll, push choices, finish on the worker thread). A module-level `ImportJobRegistry` instance in `app/import_jobs/registry.py` is the simplest correct home: one import-wide source of truth, trivially importable by the router, and reset between tests with a fixture. (We do NOT scatter it on `app.state`, which would couple it to a specific `app` instance and complicate the worker thread that has no request.)
- **Single active job (spec "imports run serially … a second start is queued or rejected — global-singleton safety").** We **reject** with 409 (the spec's stated option; queueing is YAGNI for v1). The registry holds at most one job; a job is "active" while its phase is `scanning`/`reviewing`/`applying`. Once `done`/`failed`, a new start replaces it.
- **Injectable runner = the test seam.** `ImportJobRegistry.start(...)` accepts an `ImportRunner` (a `Protocol` with `run(path: str, bridge: ImportBridge, on_finish, on_error) -> None`). Production = `BeetsImportRunner` (builds the real `WebImportSession` + daemon thread). Tests inject a `FakeImportRunner` that, on its own thread, parks one canned `ParkedAlbum` via `bridge.park(...)` (then applies whatever choice arrives) and calls `on_finish`. This keeps the **real `ImportBridge`** in the loop — only beets' `run()` is replaced — so start→state→drain→choice→done is exercised end-to-end with no network/audio. The registry's default runner is the real one; the test fixture swaps it.
- **"apply-ready" vs chunk-1 auto-apply (spec "Apply ready" + "the queue surfaces beets' judgment").** Chunk 1 already auto-applies **strong** matches inside the worker (they never park — they are applied by beets immediately). So in v1 there is **no separate batch of strong albums sitting un-applied** to flush. `POST /api/import/{job}/apply-ready` therefore means: **apply the top candidate for every album that is parked-and-waiting whose recommendation is `strong`** — i.e. push `ImportChoice(action=apply)` to each such parked album in one call. (In practice strong albums rarely park; this endpoint exists for faithfulness to the spec's "Apply ready" affordance and to handle any parked album the user wants to accept-as-matched in bulk. Medium/low/none stay for individual review.) This is a thin loop over the drained queue calling `push_choice`; it adds no new worker behavior.
- **Progress/`phase` is derived, not pushed.** The worker is opaque (beets exposes no fine progress hook we rely on — `ImportSession` public members are decision hooks only). So the job phase is a coarse state machine the registry owns: `scanning` at start → `reviewing` once ≥1 album has been parked **or** the worker finished with parked albums waiting → `applying`/`done` set by the worker's `on_finish` → `failed` set by `on_error`. Polling drains the bridge; `progress` is `{parked, decided}` counts. This is faithful (we surface beets' parked/decided reality) without inventing per-track progress.

---

## File structure

| File | Create/Modify | Responsibility |
| --- | --- | --- |
| `backend/app/import_jobs/__init__.py` | Create | Package marker for the import-job lifecycle (pure-typed; no beets). |
| `backend/app/models/import_api.py` | Create | HTTP-contract Pydantic models: `ImportPhase` (str enum), `StartImportRequest`, `StartImportResponse`, `ImportAlbumStatus` (str enum), `ImportAlbumSummary` (queue row), `ImportProgress`, `ImportJobState` (the `GET` response), `ApplyReadyResponse`. No beets imports. |
| `backend/app/import_jobs/runner.py` | Create | The `ImportRunner` `Protocol` + `BeetsImportRunner` (builds the real `WebImportSession` from a `Library`+path and runs `run_import_worker` on a daemon thread; reports completion/error via callbacks). The ONLY new module that touches beets here → goes in the mypy override. |
| `backend/app/import_jobs/registry.py` | Create | `ImportJob` (dataclass: id, phase, bridge, queue of parked albums + their per-album status, worker thread handle, error message, summary) + `ImportJobRegistry` (single-slot; `start`, `get`, `drain`, `record_choice`, `apply_ready`, lifecycle transitions; thread-safe). Module-level `registry` singleton + `reset_registry()` for tests. Pure-typed (imports `ImportBridge`/models, not beets). |
| `backend/app/api/import_.py` | Create | The FastAPI router: `POST /import`, `GET /import/{job}`, `GET /import/{job}/albums/{idx}`, `POST /import/{job}/albums/{idx}/choice`, `POST /import/{job}/apply-ready`. Thin — validates input, delegates to the registry, maps registry/bridge state to `import_api` models, maps bridge `KeyError`→404 / `RuntimeError`→409. |
| `backend/app/main.py` | Modify | Mount the import router: `app.include_router(import_router, prefix="/api")`. |
| `backend/tests/conftest.py` | Modify | Add a `reset_import_registry` autouse fixture that calls `reset_registry()` before/after each test so the single-slot registry never leaks a job across tests. |
| `backend/tests/test_import_registry.py` | Create | Hermetic unit tests for `ImportJobRegistry` + `ImportJob` using a `FakeImportRunner` (canned `ParkedAlbum` over the real `ImportBridge`): start, single-slot rejection, drain accumulates queue, record_choice round-trips, finish→done, error→failed, apply-ready applies strong parked albums. No beets `Library`, no network. |
| `backend/app/import_jobs/fakes.py` | Create | `FakeImportRunner` — a test-only `ImportRunner` that parks N canned `ParkedAlbum`s over the real bridge then finishes; used by both the registry tests and the API tests. (Lives in app/ so both test modules import one definition — DRY — and it stays pure-typed.) |
| `backend/tests/test_import_api.py` | Create | API tests via `TestClient` with the `FakeImportRunner` injected into the registry: start→202 `{job_id}`; second start→409; bad/empty path→422; `GET` state with the parked queue; `GET albums/{idx}` candidate payload; `POST choice` (apply/skip) → drives the fake worker to done; unknown index→404; a second choice after the worker advanced→404 (the realistic over-HTTP duplicate outcome); abort→clean done; worker crash→failed (never 500); apply-ready. (The narrow duplicate-RACE 409 is unit-tested at the registry level in `test_import_registry.py`.) |
| `backend/tests/test_import_runner.py` | Create | A focused test that `BeetsImportRunner` constructs a real `WebImportSession` from a hermetic beets `Library` + a tmp folder and starts a daemon thread, using a monkeypatched `WebImportSession.run` (or `tag_album`) so **no network/audio** is touched — proving the production wiring (Library→session→thread) is correct without a full beets run. Goes in the mypy override (builds a `Library`). |
| `backend/pyproject.toml` | Modify | Add `app.import_jobs.runner`, `tests.test_import_runner` (build/construct beets objects) to the `disallow_untyped_calls = false` override list. |

**Decomposition rationale:** models (pure) → registry + fakes (pure, depend on models + chunk-1 `ImportBridge`) → API router (depends on registry + models) → real beets runner (touches beets; isolated + its own test) → mount + full-suite green. The `FakeImportRunner` lets the registry and API be fully tested before the real beets runner exists, so the hard concurrency/lifecycle logic is validated first and the beets wiring is a thin, separately-tested adapter.

---

## Conventions to follow (from the existing codebase)

- **Pydantic style** (`app/models/album.py`, `search.py`, `import_models.py`): plain `class X(BaseModel)`; explicit `int | None`/`str | None`; `StrEnum` for string enums (matches `import_models.Recommendation`); short comments on non-obvious fields; no config classes unless needed.
- **Router style** (`app/api/search.py`, `albums.py`, `artists.py`): `APIRouter(tags=[...])`; `async def`; `Annotated[..., Depends(get_library)]`; `HTTPException`; `response_model=` on each route. Reuse `from app.api.albums import get_library` (do not redefine it).
- **Adapter boundary** (CLAUDE.md rule 3): beets imports ONLY under `app/beets/`. This chunk adds beets contact in exactly one new place — `app/import_jobs/runner.py` imports `WebImportSession`/`run_import_worker` **from `app.beets.import_session`** (it imports our adapter, not beets directly) and `os.fsencode`. The registry, models, router, and fakes import **no beets at all**.
- **Hermetic library fixture** (`tests/test_albums.py:29-33`): `Library(str(tmp_path / "library.db"), directory=str(tmp_path))`; build `Item(...)` in memory, `item.path = os.fsencode(...)`. Used only by `test_import_runner.py`.
- **Single-slot state reset**: the registry is module-global, so a fixture MUST reset it each test (Task 2) — mirroring how `app.dependency_overrides.clear()` resets overrides in `test_albums.py:48`.
- **Commit style:** Conventional Commits, no `Co-Authored-By` (global + project rule). Stay on `feat/import`; no push (the user opens the PR).
- **Dev commands run from repo root with** `uv --directory backend run ...`.

---

## Task 1: HTTP-contract Pydantic models

**Files:**
- Create: `backend/app/models/import_api.py`
- Test: `backend/tests/test_import_api.py` (created here with model-only tests; HTTP tests are appended in Task 4)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_import_api.py`:

```python
from app.models.import_api import (
    ApplyReadyResponse,
    ImportAlbumStatus,
    ImportAlbumSummary,
    ImportJobState,
    ImportPhase,
    ImportProgress,
    StartImportRequest,
    StartImportResponse,
)
from app.models.import_models import Recommendation


def test_import_phase_values() -> None:
    assert [p.value for p in ImportPhase] == [
        "scanning",
        "reviewing",
        "applying",
        "done",
        "failed",
    ]


def test_album_status_values() -> None:
    assert [s.value for s in ImportAlbumStatus] == ["needs_review", "decided"]


def test_start_request_defaults_options_to_none() -> None:
    req = StartImportRequest(path="/music/incoming")
    assert req.path == "/music/incoming"
    assert req.options is None


def test_start_response_carries_job_id() -> None:
    resp = StartImportResponse(job_id="abc123")
    assert resp.job_id == "abc123"


def test_job_state_round_trips() -> None:
    state = ImportJobState(
        job_id="j1",
        phase=ImportPhase.reviewing,
        progress=ImportProgress(parked=2, decided=1),
        albums=[
            ImportAlbumSummary(
                index=0,
                folder="/music/incoming/Radiohead - OK Computer",
                artist="Radiohead",
                album="OK Computer",
                recommendation=Recommendation.medium,
                confidence=75.5,
                status=ImportAlbumStatus.needs_review,
            )
        ],
        summary=None,
        error=None,
    )
    dumped = state.model_dump()
    assert dumped["phase"] == "reviewing"
    assert dumped["progress"] == {"parked": 2, "decided": 1}
    assert dumped["albums"][0]["recommendation"] == "medium"
    assert dumped["albums"][0]["status"] == "needs_review"
    assert dumped["summary"] is None
    assert dumped["error"] is None


def test_apply_ready_response() -> None:
    resp = ApplyReadyResponse(applied=3)
    assert resp.applied == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.import_api'`.

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/import_api.py`:

```python
"""HTTP-contract Pydantic models for the import API (chunk 2).

These are the request/response shapes the import endpoints take and return.
They map the in-memory ImportJob/ImportBridge/queue state to JSON; no beets
object ever reaches here. The per-album Candidate payload reuses
app.models.import_models.Candidate (the chunk-1 contract).
"""

from enum import StrEnum

from pydantic import BaseModel

from app.models.import_models import Recommendation


class ImportPhase(StrEnum):
    """Coarse lifecycle phase of an import job (the registry owns transitions).

    scanning  -> the worker is reading/grouping/looking up (no album parked yet)
    reviewing -> at least one album is parked-and-waiting for a decision
    applying  -> the worker is committing decisions (transient near the end)
    done      -> the import finished (incl. a clean abort)
    failed    -> the worker raised; ``error`` holds the message
    """

    scanning = "scanning"
    reviewing = "reviewing"
    applying = "applying"
    done = "done"
    failed = "failed"


class ImportAlbumStatus(StrEnum):
    """Per-album state within the review queue."""

    needs_review = "needs_review"
    decided = "decided"


class StartImportRequest(BaseModel):
    """Body of ``POST /api/import``.

    ``path`` is a server-side folder (maps 1:1 to ``beet import <path>``).
    ``options`` is reserved for future per-import overrides (copy/move/autotag);
    v1 reads those from the user's beets config, so it is accepted but unused.
    """

    path: str
    options: dict[str, str] | None = None


class StartImportResponse(BaseModel):
    """Response of a successful ``POST /api/import``: the new job's id."""

    job_id: str


class ImportProgress(BaseModel):
    """Coarse progress counters derived from the bridge/queue."""

    parked: int
    decided: int


class ImportAlbumSummary(BaseModel):
    """One row in the review queue (the GET /api/import/{job} listing).

    The full per-album diff is fetched separately via
    ``GET /api/import/{job}/albums/{index}``; this is the compact triage row.
    """

    index: int
    folder: str
    artist: str | None
    album: str | None
    recommendation: Recommendation
    confidence: float
    status: ImportAlbumStatus


class ImportJobState(BaseModel):
    """Response of ``GET /api/import/{job}``: phase + progress + the queue."""

    job_id: str
    phase: ImportPhase
    progress: ImportProgress
    albums: list[ImportAlbumSummary]
    # A short human summary once done (e.g. "2 added, 1 skipped"); None until then.
    summary: str | None
    # The worker's failure message when phase == failed; None otherwise.
    error: str | None


class ApplyReadyResponse(BaseModel):
    """Response of ``POST /api/import/{job}/apply-ready``: how many were applied."""

    applied: int
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. (`import_api.py` imports only our models; no override needed. The test imports only our models.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/import_api.py backend/tests/test_import_api.py
git commit -m "feat(import): add HTTP-contract models for the import API"
```

---

## Task 2: ImportJob + ImportJobRegistry (single-slot lifecycle) + FakeImportRunner

**Files:**
- Create: `backend/app/import_jobs/__init__.py`
- Create: `backend/app/import_jobs/registry.py`
- Create: `backend/app/import_jobs/fakes.py`
- Modify: `backend/tests/conftest.py` (add the `reset_import_registry` autouse fixture)
- Test: `backend/tests/test_import_registry.py`

- [ ] **Step 1: Create the package marker**

Create `backend/app/import_jobs/__init__.py`:

```python
"""In-memory import-job lifecycle (chunk 2).

Owns the single active ImportJob, drains chunk-1's ImportBridge into a review
queue, and delivers user choices back to the worker. Pure-typed: imports the
ImportBridge + our models, never beets directly (the beets contact lives in
app/import_jobs/runner.py and app/beets/).
"""
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_import_registry.py`:

```python
import threading

import pytest

from app.beets.import_session import ImportBridge
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.models.import_api import ImportAlbumStatus, ImportPhase
from app.models.import_models import (
    AlbumChange,
    Candidate,
    ImportAction,
    ImportChoice,
    ParkedAlbum,
    Recommendation,
)


def _candidate(rec: Recommendation, *, confidence: float = 75.5) -> Candidate:
    blank = AlbumChange(artist="Radiohead", album="OK Computer", year=None, label=None, country=None, media=None)
    return Candidate(
        confidence=confidence,
        recommendation=rec,
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        changed_fields=["album"],
        album_before=blank,
        album_after=blank,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )


def _parked(index: int, rec: Recommendation) -> ParkedAlbum:
    return ParkedAlbum(
        album_index=index,
        folder=f"/music/incoming/album{index}",
        candidate=_candidate(rec),
    )


def test_start_returns_job_and_marks_active() -> None:
    registry = ImportJobRegistry(runner=FakeImportRunner([_parked(0, Recommendation.medium)]))
    job_id = registry.start("/music/incoming")
    assert job_id
    job = registry.get(job_id)
    assert job is not None
    assert registry.has_active_job() is True


def test_second_start_while_active_raises() -> None:
    registry = ImportJobRegistry(runner=FakeImportRunner([_parked(0, Recommendation.medium)]))
    registry.start("/music/incoming")
    with pytest.raises(RuntimeError):
        registry.start("/music/other")


def test_drain_accumulates_parked_albums_into_queue() -> None:
    fake = FakeImportRunner([_parked(0, Recommendation.medium), _parked(1, Recommendation.low)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    # The fake parks both albums on its worker thread; drain (timeout=0) pulls
    # whatever has landed. Poll until both are queued (fast; canned, no network).
    deadline = threading.Event()
    summaries = []
    for _ in range(200):
        summaries = registry.drain(job_id)
        if len(summaries) == 2:
            break
        deadline.wait(0.01)
    assert {s.index for s in summaries} == {0, 1}
    assert all(s.status is ImportAlbumStatus.needs_review for s in summaries)
    assert registry.state(job_id).phase is ImportPhase.reviewing


def test_record_choice_marks_decided_and_unblocks_worker() -> None:
    fake = FakeImportRunner([_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    ev = threading.Event()
    for _ in range(200):
        if registry.drain(job_id):
            break
        ev.wait(0.01)

    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.apply))
    # The fake applies the choice and finishes; phase becomes done.
    for _ in range(200):
        if registry.state(job_id).phase is ImportPhase.done:
            break
        ev.wait(0.01)
    state = registry.state(job_id)
    assert state.phase is ImportPhase.done
    assert state.albums[0].status is ImportAlbumStatus.decided


def test_unknown_index_choice_raises_keyerror() -> None:
    fake = FakeImportRunner([_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    ev = threading.Event()
    for _ in range(200):
        if registry.drain(job_id):
            break
        ev.wait(0.01)
    with pytest.raises(KeyError):
        registry.record_choice(job_id, 99, ImportChoice(action=ImportAction.apply))


def test_record_choice_duplicate_raises_runtimeerror() -> None:
    # The 409 path: two choices RACE the same still-unconsumed reply slot. We
    # force it deterministically — the worker is blocked inside bridge.park's
    # reply.get(); two synchronous push_choice calls (no thread switch between
    # them, GIL held) fill the maxsize=1 reply queue, so the 2nd raises
    # RuntimeError before the worker drains it. This proves record_choice
    # PROPAGATES the bridge's RuntimeError (the router maps it to 409).
    fake = FakeImportRunner([_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    ev = threading.Event()
    for _ in range(200):
        if registry.drain(job_id):
            break
        ev.wait(0.01)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    with pytest.raises(RuntimeError):
        # Second push to the same slot before the worker consumes the first.
        registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))


def test_worker_error_marks_job_failed() -> None:
    fake = FakeImportRunner([], fail_with="boom")
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    ev = threading.Event()
    for _ in range(200):
        if registry.state(job_id).phase is ImportPhase.failed:
            break
        ev.wait(0.01)
    state = registry.state(job_id)
    assert state.phase is ImportPhase.failed
    assert state.error == "boom"


def test_apply_ready_applies_strong_parked_albums() -> None:
    fake = FakeImportRunner([_parked(0, Recommendation.strong), _parked(1, Recommendation.low)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    ev = threading.Event()
    for _ in range(200):
        if len(registry.drain(job_id)) == 2:
            break
        ev.wait(0.01)
    applied = registry.apply_ready(job_id)
    # Only the strong-rec album is auto-applied; the low one stays for review.
    assert applied == 1
    state = registry.state(job_id)
    by_index = {s.index: s for s in state.albums}
    assert by_index[0].status is ImportAlbumStatus.decided
    assert by_index[1].status is ImportAlbumStatus.needs_review
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.import_jobs.fakes'` (and `.registry`).

- [ ] **Step 4: Write minimal implementation — the runner protocol stub, the fake, then the registry**

First create the `ImportRunner` protocol (the real implementation comes in Task 5; the registry only depends on the protocol). Create `backend/app/import_jobs/runner.py`:

```python
"""The import-runner seam.

``ImportRunner`` is the injectable boundary between the registry (pure
lifecycle) and the actual import engine. Production uses ``BeetsImportRunner``
(Task 5), which builds a real WebImportSession and runs it on a daemon thread.
Tests inject a fake that drives the same ImportBridge with canned albums.

This module imports beets only via our adapter (app.beets.import_session); it is
in the mypy disallow_untyped_calls override because it constructs beets objects.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Protocol

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker


class ImportRunner(Protocol):
    """Starts an import on its own thread, reporting completion via callbacks.

    ``on_finish`` is called (with no args) when the import ends normally
    (including a clean abort); ``on_error`` is called with the exception message
    when the worker raises. Implementations MUST be non-blocking (spawn a thread
    and return) so the API start endpoint returns immediately.
    """

    def run(
        self,
        path: str,
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None: ...


class BeetsImportRunner:
    """Production runner: a real WebImportSession on a daemon thread.

    The beets Library is captured at construction (the process-wide library the
    app opened at startup). ``run`` builds the session for ``path`` and starts a
    daemon thread that calls ``run_import_worker`` (which forces
    config["threaded"]=False and calls session.run()), translating any crash
    into ``on_error`` and a normal return (incl. abort) into ``on_finish``.
    """

    def __init__(self, lib: object) -> None:
        self._lib = lib

    def run(
        self,
        path: str,
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        session = WebImportSession(
            self._lib,
            None,  # loghandler -> beets installs a NullHandler
            [os.fsencode(path)],
            None,  # query -> path import, not a library query
            bridge,
        )

        def target() -> None:
            try:
                run_import_worker(session)
            # Broad by design: any worker crash must become a failed job, never
            # an unhandled thread exception (which the API could not surface).
            except Exception as exc:
                on_error(str(exc) or exc.__class__.__name__)
            else:
                on_finish()

        threading.Thread(target=target, name="musicdrop-import", daemon=True).start()
```

Then create the fake. Create `backend/app/import_jobs/fakes.py`:

```python
"""Test-only ImportRunner that drives the real ImportBridge with canned albums.

Shared by the registry tests and the API tests (one definition — DRY). It runs
on its own daemon thread, parks each canned ParkedAlbum via ``bridge.park``
(which blocks until a choice arrives, exactly like the real worker), then calls
``on_finish``. With ``fail_with`` set it instead reports an error immediately.
No beets, no network.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.beets.import_session import ImportBridge
from app.models.import_models import ParkedAlbum


class FakeImportRunner:
    """A canned ImportRunner for hermetic lifecycle/API tests."""

    def __init__(self, parked: list[ParkedAlbum], fail_with: str | None = None) -> None:
        self._parked = parked
        self._fail_with = fail_with

    def run(
        self,
        path: str,
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        def target() -> None:
            if self._fail_with is not None:
                on_error(self._fail_with)
                return
            try:
                for album in self._parked:
                    # park() blocks until the consumer pushes a choice for this
                    # album — same contract as the real worker's choose_match.
                    bridge.park(album)
            # Broad by design: mirror the real worker's guard so a canned-album
            # bug surfaces as a failed job rather than a silent dead thread.
            except Exception as exc:
                on_error(str(exc) or exc.__class__.__name__)
                return
            on_finish()

        threading.Thread(target=target, name="fake-import", daemon=True).start()
```

Then create the registry. Create `backend/app/import_jobs/registry.py`:

```python
"""The in-memory, single-slot import-job registry.

Holds at most one active ImportJob. Starting a second import while one is active
raises RuntimeError (the API maps this to 409). The registry drains chunk-1's
ImportBridge into a review queue (non-blocking, timeout=0) and delivers user
choices to the worker. Thread-safe: the worker thread mutates phase/summary via
callbacks while API threads read state and push choices.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from app.beets.import_session import ImportBridge
from app.import_jobs.runner import BeetsImportRunner, ImportRunner
from app.models.import_api import (
    ImportAlbumStatus,
    ImportAlbumSummary,
    ImportJobState,
    ImportPhase,
    ImportProgress,
)
from app.models.import_models import (
    Candidate,
    ImportAction,
    ImportChoice,
    ParkedAlbum,
    Recommendation,
)

# Phases in which a job still owns the single import slot.
_ACTIVE_PHASES = {ImportPhase.scanning, ImportPhase.reviewing, ImportPhase.applying}


@dataclass
class _QueuedAlbum:
    """A parked album plus its review status within a job."""

    parked: ParkedAlbum
    status: ImportAlbumStatus = ImportAlbumStatus.needs_review


@dataclass
class ImportJob:
    """One import's full in-memory state."""

    id: str
    bridge: ImportBridge
    phase: ImportPhase = ImportPhase.scanning
    albums: dict[int, _QueuedAlbum] = field(default_factory=dict)
    summary: str | None = None
    error: str | None = None


class ImportJobRegistry:
    """Single-slot registry of the active (or last) import job."""

    def __init__(self, runner: ImportRunner | None = None) -> None:
        # Default to the real beets runner; tests pass a FakeImportRunner. The
        # real runner needs the Library, set later via attach_library(); until
        # then a default runner with lib=None is replaced before first use.
        self._runner = runner
        self._lib: object | None = None
        self._job: ImportJob | None = None
        self._lock = threading.Lock()

    # ----- wiring -----

    def attach_library(self, lib: object | None) -> None:
        """Provide the beets Library the production runner builds sessions from."""
        self._lib = lib

    def _resolve_runner(self) -> ImportRunner:
        if self._runner is not None:
            return self._runner
        return BeetsImportRunner(self._lib)

    # ----- lifecycle -----

    def has_active_job(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.phase in _ACTIVE_PHASES

    def start(self, path: str) -> str:
        """Start an import; raise RuntimeError if one is already active."""
        with self._lock:
            if self._job is not None and self._job.phase in _ACTIVE_PHASES:
                raise RuntimeError("an import is already running")
            job = ImportJob(id=uuid.uuid4().hex, bridge=ImportBridge())
            self._job = job

        runner = self._resolve_runner()
        runner.run(
            path,
            job.bridge,
            on_finish=lambda: self._on_finish(job.id),
            on_error=lambda message: self._on_error(job.id, message),
        )
        return job.id

    def _on_finish(self, job_id: str) -> None:
        with self._lock:
            if self._job is not None and self._job.id == job_id and self._job.phase != ImportPhase.failed:
                self._job.phase = ImportPhase.done
                self._job.summary = self._summarize(self._job)

    def _on_error(self, job_id: str, message: str) -> None:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                self._job.phase = ImportPhase.failed
                self._job.error = message

    @staticmethod
    def _summarize(job: ImportJob) -> str:
        decided = sum(1 for a in job.albums.values() if a.status is ImportAlbumStatus.decided)
        return f"{decided} album(s) decided of {len(job.albums)}"

    # ----- access -----

    def get(self, job_id: str) -> ImportJob | None:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                return self._job
            return None

    def drain(self, job_id: str) -> list[ImportAlbumSummary]:
        """Pull any newly-parked albums into the queue and return the queue rows.

        Non-blocking: uses bridge.get_parked(timeout=0). Promotes a still-active
        job to ``reviewing`` once at least one album is queued.
        """
        job = self._require(job_id)
        while True:
            parked = job.bridge.get_parked(timeout=0)
            if parked is None:
                break
            with self._lock:
                if parked.album_index not in job.albums:
                    job.albums[parked.album_index] = _QueuedAlbum(parked=parked)
        with self._lock:
            if job.albums and job.phase == ImportPhase.scanning:
                job.phase = ImportPhase.reviewing
            return self._summaries(job)

    def candidate(self, job_id: str, index: int) -> Candidate:
        """Return the full Candidate for one parked album (drains first)."""
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            queued = job.albums.get(index)
        if queued is None:
            raise KeyError(index)
        return queued.parked.candidate

    def record_choice(self, job_id: str, index: int, choice: ImportChoice) -> None:
        """Deliver a decision to the worker and mark the album decided.

        Propagates the bridge's KeyError (unknown index) and RuntimeError
        (duplicate choice) to the caller; the API maps them to 404/409.
        """
        self.drain(job_id)
        job = self._require(job_id)
        job.bridge.push_choice(index, choice)  # KeyError / RuntimeError bubble up
        with self._lock:
            queued = job.albums.get(index)
            if queued is not None:
                queued.status = ImportAlbumStatus.decided

    def apply_ready(self, job_id: str) -> int:
        """Apply the top candidate for every parked, undecided, strong album.

        Strong matches normally auto-apply in the worker and never park; this
        flushes any strong album still awaiting a decision (spec's "Apply ready"
        affordance). Returns the count applied. Medium/low/none stay for review.
        """
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            targets = [
                index
                for index, q in job.albums.items()
                if q.status is ImportAlbumStatus.needs_review
                and q.parked.candidate.recommendation is Recommendation.strong
            ]
        applied = 0
        for index in targets:
            job.bridge.push_choice(index, ImportChoice(action=ImportAction.apply))
            with self._lock:
                job.albums[index].status = ImportAlbumStatus.decided
            applied += 1
        return applied

    def state(self, job_id: str) -> ImportJobState:
        """Drain, then return the full job state for the GET endpoint."""
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            decided = sum(
                1 for a in job.albums.values() if a.status is ImportAlbumStatus.decided
            )
            return ImportJobState(
                job_id=job.id,
                phase=job.phase,
                progress=ImportProgress(parked=len(job.albums), decided=decided),
                albums=self._summaries(job),
                summary=job.summary,
                error=job.error,
            )

    # ----- helpers -----

    def _require(self, job_id: str) -> ImportJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    @staticmethod
    def _summaries(job: ImportJob) -> list[ImportAlbumSummary]:
        rows: list[ImportAlbumSummary] = []
        for index in sorted(job.albums):
            queued = job.albums[index]
            cand = queued.parked.candidate
            rows.append(
                ImportAlbumSummary(
                    index=index,
                    folder=queued.parked.folder,
                    artist=cand.album_after.artist or cand.album_before.artist,
                    album=cand.album_after.album or cand.album_before.album,
                    recommendation=cand.recommendation,
                    confidence=cand.confidence,
                    status=queued.status,
                )
            )
        return rows


# Process-global single-slot registry. The router imports this instance; the
# lifespan calls attach_library on it. Tests call reset_registry to swap in a
# fresh registry (with a FakeImportRunner) so no job leaks across tests.
registry = ImportJobRegistry()


def reset_registry(runner: ImportRunner | None = None) -> ImportJobRegistry:
    """Replace the global registry (test helper). Returns the new instance."""
    global registry
    registry = ImportJobRegistry(runner=runner)
    return registry
```

The registry above is the fully-typed final form: `Candidate`, `ImportAction`, `ImportChoice`, `ParkedAlbum`, and `Recommendation` are all imported on the single top-level `from app.models.import_models import (...)` line (no inline imports), `candidate` is annotated `-> Candidate`, and there are no `# type: ignore` comments. Write it exactly as shown — it passes `mypy --strict` because the registry touches no untyped beets surface (the beets contact is isolated in `app/import_jobs/runner.py`).

- [ ] **Step 5: Add the registry-reset autouse fixture**

In `backend/tests/conftest.py`, append:

```python
from collections.abc import Iterator


@pytest.fixture(autouse=True)
def reset_import_registry() -> Iterator[None]:
    """Reset the global single-slot import registry around every test.

    The registry is module-global mutable state (one active job); without this
    a job started in one test would block ``start`` in the next with a 409.
    """
    from app.import_jobs.registry import reset_registry

    reset_registry()
    yield
    reset_registry()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv --directory backend run pytest tests/test_import_registry.py -v`
Expected: PASS (7 tests).

- [ ] **Step 7: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. If mypy flags untyped beets calls in `app.import_jobs.runner` (it constructs `WebImportSession`), it is already slated for the override in Task 5 Step 1 — add `app.import_jobs.runner` to the `disallow_untyped_calls = false` list now and note it. The registry/fakes are pure-typed and stay out.

Add `app.import_jobs.runner` to the override list in `backend/pyproject.toml` (line ~45):

```toml
module = ["app.beets.library", "app.beets.import_mapping", "app.beets.import_session", "app.import_jobs.runner", "tests.test_albums", "tests.test_artists", "tests.test_search", "tests.test_import_mapping", "tests.test_import_session"]
```

- [ ] **Step 8: Commit**

```bash
git add backend/app/import_jobs/__init__.py backend/app/import_jobs/registry.py backend/app/import_jobs/runner.py backend/app/import_jobs/fakes.py backend/tests/conftest.py backend/tests/test_import_registry.py backend/pyproject.toml
git commit -m "feat(import): add single-slot ImportJob registry + injectable runner seam"
```

---

## Task 3: The import API router

**Files:**
- Create: `backend/app/api/import_.py`
- Modify: `backend/app/main.py` (mount the router)
- Test: `backend/tests/test_import_api.py` (HTTP tests appended in Task 4; this task wires the router and a smoke import-path)

- [ ] **Step 1: Write the failing test (append to `tests/test_import_api.py`)**

Append a minimal wiring test that the router mounts and rejects a blank path (this fails until the router exists and is mounted):

```python
from fastapi.testclient import TestClient

from app.main import app


def test_start_import_blank_path_is_422() -> None:
    # An empty path fails validation (min_length=1) -> 422, not a 500/500-less crash.
    resp = TestClient(app).post("/api/import", json={"path": "   "})
    assert resp.status_code in (400, 422)


def test_get_unknown_job_is_404() -> None:
    resp = TestClient(app).get("/api/import/does-not-exist")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_api.py::test_get_unknown_job_is_404 -v`
Expected: FAIL — 404 route not found in a different shape, or the route is missing (the import router is not mounted yet). It will pass once Task 3 mounts the router and the handlers exist.

- [ ] **Step 3: Write minimal implementation — the router**

Create `backend/app/api/import_.py`:

```python
"""The import API router (chunk 2).

Thin endpoints over the in-memory ImportJobRegistry. Start an import, poll its
state/queue, fetch one album's full Candidate, push a per-album choice, or batch
apply the ready (strong) ones. The registry owns all lifecycle + threading; this
layer only validates input and maps registry/bridge exceptions to HTTP codes.

No beets imports: the registry + models are the whole surface here.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import StringConstraints

from app.import_jobs.registry import registry
from app.models.import_api import (
    ApplyReadyResponse,
    ImportJobState,
    StartImportRequest,
    StartImportResponse,
)
from app.models.import_models import Candidate, ImportChoice

router = APIRouter(tags=["import"])

# A path that is non-empty after stripping (a blank/whitespace path is a 422).
NonBlankPath = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _StartBody(StartImportRequest):
    """Start body with a validated non-blank path."""

    path: NonBlankPath


@router.post("/import", response_model=StartImportResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_import(body: _StartBody) -> StartImportResponse:
    try:
        job_id = registry.start(body.path)
    except RuntimeError:
        # An import is already running (single-slot policy).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An import is already running"
        ) from None
    return StartImportResponse(job_id=job_id)


@router.get("/import/{job_id}", response_model=ImportJobState)
async def get_import_state(job_id: str) -> ImportJobState:
    try:
        return registry.state(job_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found"
        ) from None


@router.get("/import/{job_id}/albums/{index}", response_model=Candidate)
async def get_import_album(job_id: str, index: Annotated[int, Path(ge=0)]) -> Candidate:
    try:
        return registry.candidate(job_id, index)
    except KeyError:
        # Either the job or the album index is unknown.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None


@router.post("/import/{job_id}/albums/{index}/choice", status_code=status.HTTP_204_NO_CONTENT)
async def post_import_choice(
    job_id: str, index: Annotated[int, Path(ge=0)], choice: ImportChoice
) -> None:
    try:
        registry.record_choice(job_id, index, choice)
    except KeyError:
        # No job, or no album parked at this index.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None
    except RuntimeError:
        # A choice was already pushed for this album (duplicate).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A choice was already submitted"
        ) from None


@router.post("/import/{job_id}/apply-ready", response_model=ApplyReadyResponse)
async def post_apply_ready(job_id: str) -> ApplyReadyResponse:
    try:
        applied = registry.apply_ready(job_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found"
        ) from None
    return ApplyReadyResponse(applied=applied)
```

- [ ] **Step 4: Mount the router + attach the library in the lifespan**

In `backend/app/main.py`, add the import next to the other routers:

```python
from app.api.import_ import router as import_router
```

Add the mount after the existing `app.include_router(search_router, prefix="/api")` line:

```python
app.include_router(import_router, prefix="/api")
```

Inside the `lifespan` function, after `app.state.beets_library = lib`, attach the library to the registry so the production runner can build sessions:

```python
    from app.import_jobs.registry import registry as import_registry

    import_registry.attach_library(lib)
```

(The `reset_registry` test fixture swaps the global instance with a fake-runner registry before each test, so `attach_library` in the real lifespan only matters for the running server and the one real-runner test in Task 5.)

- [ ] **Step 5: Run the wiring tests to verify they pass**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: PASS (the Task-1 model tests + the two wiring tests).

- [ ] **Step 6: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. The router imports no beets; it stays out of the mypy override.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/import_.py backend/app/main.py backend/tests/test_import_api.py
git commit -m "feat(import): add import API router + mount + library wiring"
```

---

## Task 4: Full API flow tests (start → state → choice → done) with the fake runner

**Files:**
- Test: `backend/tests/test_import_api.py` (append the end-to-end HTTP tests)

These tests inject a `FakeImportRunner` into the global registry (via `reset_registry`) so the *real* `ImportBridge` is driven with canned albums — no beets, no network. They exercise every endpoint over `TestClient`.

- [ ] **Step 1: Write the failing tests (append to `tests/test_import_api.py`)**

Append:

```python
import time

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.models.import_models import (
    AlbumChange,
    Candidate,
    ImportAction,
    ParkedAlbum,
    Recommendation,
)


def _api_candidate(rec: Recommendation, *, confidence: float = 75.5) -> Candidate:
    after = AlbumChange(
        artist="Radiohead", album="OK Computer", year=1997, label="Parlophone", country="GB", media="CD"
    )
    before = AlbumChange(
        artist="Radiohead", album="OK Computr", year=None, label=None, country=None, media=None
    )
    return Candidate(
        confidence=confidence,
        recommendation=rec,
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        changed_fields=["album"],
        album_before=before,
        album_after=after,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )


def _api_parked(index: int, rec: Recommendation) -> ParkedAlbum:
    return ParkedAlbum(
        album_index=index, folder=f"/music/incoming/album{index}", candidate=_api_candidate(rec)
    )


def _client_with_fake(parked: list[ParkedAlbum], fail_with: str | None = None) -> TestClient:
    reset_registry(runner=FakeImportRunner(parked, fail_with=fail_with))
    return TestClient(app)


def _poll(client: TestClient, job_id: str, predicate, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll helper; predicate is an inline lambda
    for _ in range(attempts):
        state = client.get(f"/api/import/{job_id}").json()
        if predicate(state):
            return state
        time.sleep(0.01)
    return client.get(f"/api/import/{job_id}").json()


def test_start_returns_202_and_job_id() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    resp = client.post("/api/import", json={"path": "/music/incoming"})
    assert resp.status_code == 202
    assert resp.json()["job_id"]


def test_second_concurrent_import_is_409() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    first = client.post("/api/import", json={"path": "/music/incoming"})
    assert first.status_code == 202
    second = client.post("/api/import", json={"path": "/music/other"})
    assert second.status_code == 409


def test_state_lists_parked_album_with_review_status() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    assert state["phase"] == "reviewing"
    album = state["albums"][0]
    assert album["index"] == 0
    assert album["album"] == "OK Computer"
    assert album["artist"] == "Radiohead"
    assert album["recommendation"] == "medium"
    assert album["status"] == "needs_review"
    assert state["progress"] == {"parked": 1, "decided": 0}


def test_get_album_returns_full_candidate() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    resp = client.get(f"/api/import/{job_id}/albums/0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["album_after"]["album"] == "OK Computer"
    assert body["album_before"]["album"] == "OK Computr"
    assert body["data_url"] == "https://mb/a1"
    assert body["changed_fields"] == ["album"]


def test_get_album_unknown_index_is_404() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    assert client.get(f"/api/import/{job_id}/albums/99").status_code == 404


def test_choice_apply_drives_job_to_done() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)

    resp = client.post(
        f"/api/import/{job_id}/albums/0/choice", json={"action": "apply", "candidate_index": None}
    )
    assert resp.status_code == 204

    state = _poll(client, job_id, lambda s: s["phase"] == "done")
    assert state["phase"] == "done"
    assert state["albums"][0]["status"] == "decided"
    assert state["summary"] is not None


def test_choice_unknown_index_is_404() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    resp = client.post(f"/api/import/{job_id}/albums/99/choice", json={"action": "skip"})
    assert resp.status_code == 404


def test_second_choice_after_decided_is_404() -> None:
    # Realistic over-HTTP outcome of a second choice: once the worker consumes
    # the first reply, ImportBridge.park POPS the reply slot, so a second
    # push_choice for that index raises KeyError -> 404. (A true 409 requires two
    # pushes RACING the same still-unconsumed slot — not reachable through
    # sequential HTTP calls; the 409 mapping is unit-tested in the registry, see
    # test_record_choice_duplicate_raises_runtimeerror in test_import_registry.py.)
    client = _client_with_fake(
        [_api_parked(0, Recommendation.medium), _api_parked(1, Recommendation.low)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    # Decide album 0; the worker then advances to park album 1 and drops slot 0.
    _poll(client, job_id, lambda s: any(a["index"] == 0 for a in s["albums"]))
    first = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "skip"})
    assert first.status_code == 204
    # Wait for the worker to advance (album 1 appears) so slot 0 is surely gone.
    _poll(client, job_id, lambda s: any(a["index"] == 1 for a in s["albums"]))
    second = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "skip"})
    assert second.status_code == 404


def test_abort_choice_ends_job_cleanly() -> None:
    client = _client_with_fake([_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    # The fake worker's bridge.park returns the abort choice; the fake then calls
    # on_finish (it does not raise), so the job ends 'done', never 'failed'.
    resp = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "abort"})
    assert resp.status_code == 204
    state = _poll(client, job_id, lambda s: s["phase"] in ("done", "failed"))
    assert state["phase"] == "done"


def test_worker_crash_marks_failed_never_500() -> None:
    client = _client_with_fake([], fail_with="lookup exploded")
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: s["phase"] == "failed")
    assert state["phase"] == "failed"
    assert state["error"] == "lookup exploded"


def test_apply_ready_applies_only_strong() -> None:
    client = _client_with_fake(
        [_api_parked(0, Recommendation.strong), _api_parked(1, Recommendation.low)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 2)
    resp = client.post(f"/api/import/{job_id}/apply-ready")
    assert resp.status_code == 200
    assert resp.json()["applied"] == 1
    state = client.get(f"/api/import/{job_id}").json()
    by_index = {a["index"]: a for a in state["albums"]}
    assert by_index[0]["status"] == "decided"
    assert by_index[1]["status"] == "needs_review"
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: PASS (model tests + wiring tests + the end-to-end flow tests above).

If a flow test is flaky due to thread timing, raise the `attempts` in `_poll`/the registry tests (each attempt is 10ms; 200 ≈ 2s — generous for in-process canned albums) — do NOT add real sleeps to production code.

- [ ] **Step 3: Add the API test module to the mypy override (it builds beets-free models only — but uses `FakeImportRunner`/`ParkedAlbum`; check)**

`tests/test_import_api.py` builds only our Pydantic models + `FakeImportRunner` (no beets `Library`/`AlbumMatch`), so it should pass strict mypy without the override. Run mypy in Step 4; only if it flags an untyped call, add `tests.test_import_api` to the override list. The `_poll` helper carries `# type: ignore[no-untyped-def]` for its callable arg (a test-local convenience); keep that single annotation.

- [ ] **Step 4: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_import_api.py
git commit -m "test(import): cover start/state/choice/apply-ready/abort/crash via fake runner"
```

---

## Task 5: BeetsImportRunner production-wiring test (real Library, no network)

**Files:**
- Modify: `backend/pyproject.toml` (add `tests.test_import_runner` to the override; `app.import_jobs.runner` was added in Task 2)
- Test: `backend/tests/test_import_runner.py`

`BeetsImportRunner` was implemented in Task 2 (the registry needs the protocol + a default). This task adds a test proving it builds a real `WebImportSession` from a hermetic beets `Library` and a tmp folder, and runs it on a daemon thread, **without any MusicBrainz/audio** — by monkeypatching `WebImportSession.run` to a no-op so `run_import_worker` exercises the real construction + thread + callback path. This is the deliberate "real-audio `run()` deferred to the API chunk" decision from chunk 1, resolved as: we verify the *wiring* (Library→session→thread→callbacks) with a stubbed `run()`; a full real-audio end-to-end remains deferred (no sample file is shipped; see Out of scope).

- [ ] **Step 1: Add the override for the new test module**

In `backend/pyproject.toml`, add `tests.test_import_runner` to the `disallow_untyped_calls = false` list (it constructs a beets `Library`):

```toml
module = ["app.beets.library", "app.beets.import_mapping", "app.beets.import_session", "app.import_jobs.runner", "tests.test_albums", "tests.test_artists", "tests.test_search", "tests.test_import_mapping", "tests.test_import_session", "tests.test_import_runner"]
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_import_runner.py`:

```python
import threading
from pathlib import Path

import pytest
from beets.library import Library

from app.beets import import_session as import_session_mod
from app.beets.import_session import ImportBridge, WebImportSession
from app.import_jobs.runner import BeetsImportRunner


def test_beets_runner_builds_session_and_invokes_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real (empty) beets library; no audio files, no network. We stub
    # WebImportSession.run so run_import_worker exercises the real construction
    # + daemon-thread + on_finish path WITHOUT a MusicBrainz/audio import.
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))

    captured: dict[str, object] = {}

    def fake_run(self: WebImportSession) -> None:
        captured["paths"] = list(self.paths)
        captured["lib_is_real"] = self.lib is lib
        captured["bridge_is_real"] = isinstance(self.bridge, ImportBridge)

    monkeypatch.setattr(WebImportSession, "run", fake_run)

    finished = threading.Event()
    errored: dict[str, str] = {}

    runner = BeetsImportRunner(lib)
    runner.run(
        str(tmp_path / "incoming"),
        ImportBridge(),
        on_finish=finished.set,
        on_error=lambda message: errored.__setitem__("message", message),
    )

    assert finished.wait(timeout=2.0)
    assert errored == {}
    assert captured["lib_is_real"] is True
    assert captured["bridge_is_real"] is True
    # beets normalizes the path to bytes via normpath; the basename survives.
    # captured["paths"] is typed ``object`` (the dict is dict[str, object]); the
    # session stores a list[bytes], so assert against the repr to stay typed.
    assert b"incoming" in repr(captured["paths"]).encode()


def test_beets_runner_crash_routes_to_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))

    def boom(self: WebImportSession) -> None:
        raise ValueError("kaboom")

    monkeypatch.setattr(WebImportSession, "run", boom)

    finished = threading.Event()
    errored: dict[str, str] = {}
    runner = BeetsImportRunner(lib)
    runner.run(
        str(tmp_path / "incoming"),
        ImportBridge(),
        on_finish=finished.set,
        on_error=lambda message: (errored.__setitem__("message", message), finished.set()) and None,
    )
    assert finished.wait(timeout=2.0)
    assert errored["message"] == "kaboom"
    # Reference the module import so ruff keeps it (used for the patch target docs).
    assert import_session_mod.WebImportSession is WebImportSession
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_runner.py -v`
Expected: It may already PASS if `BeetsImportRunner` from Task 2 is correct — that is fine (this task is primarily a verification + override addition). If it FAILS, the failure pinpoints a wiring bug in `BeetsImportRunner` (e.g. wrong ctor arg order to `WebImportSession`); fix `app/import_jobs/runner.py` to match the verified signature `WebImportSession(lib, None, [fsencode(path)], None, bridge)`.

- [ ] **Step 4: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_import_runner.py backend/pyproject.toml
git commit -m "test(import): verify BeetsImportRunner builds a real session on a daemon thread"
```

---

## Task 6: Full-suite green + boundary check + self-review

**Files:** none (verification only).

- [ ] **Step 1: Run the entire backend test suite**

Run: `uv --directory backend run pytest`
Expected: PASS — all existing tests plus the new `test_import_api.py`, `test_import_registry.py`, `test_import_runner.py`. No existing test regresses (this chunk adds new modules + one router mount + one autouse fixture).

- [ ] **Step 2: Full typecheck**

Run: `uv --directory backend run mypy`
Expected: `Success: no issues found`. Confirm the override list in `pyproject.toml` includes `app.import_jobs.runner`, `tests.test_import_runner` (and any test module mypy flagged for untyped beets calls).

- [ ] **Step 3: Lint + format check**

Run: `uv --directory backend run ruff check && uv --directory backend run ruff format --check`
Expected: no errors. Run `uv --directory backend run ruff format` and re-commit if formatting differs.

- [ ] **Step 4: Confirm the beets-boundary rule holds (CLAUDE.md rule 3)**

Run: `uv --directory backend run python -c "import subprocess,sys; out=subprocess.run(['grep','-rEn','import beets|from beets|import beetsplug','app','--include=*.py'],capture_output=True,text=True).stdout; bad=[l for l in out.splitlines() if not l.startswith('app/beets/')]; print('\n'.join(bad)); sys.exit(1 if bad else 0)"`
Expected: empty output, exit 0 — every beets import lives under `app/beets/`. `app/import_jobs/runner.py` imports `WebImportSession`/`run_import_worker` from `app.beets.import_session` (our adapter) and `os` only — no direct `beets`/`beetsplug` import — so it does not appear. The router, registry, models, and fakes import no beets.

- [ ] **Step 5: Confirm a second start is rejected against a live app (lifespan smoke)**

Run: `uv --directory backend run pytest tests/test_import_api.py::test_second_concurrent_import_is_409 -v`
Expected: PASS — the single-slot policy holds end-to-end through the router.

- [ ] **Step 6: Final commit if anything changed**

```bash
git add -A
git commit -m "chore(import): API + ImportJob lifecycle green — tests, mypy strict, ruff" || echo "nothing to commit"
```

---

## Self-review checklist (run after writing all tasks)

**Spec coverage** — every chunk-2 requirement (spec §"Backend architecture"/"API"/"Concurrency model"/"Error handling"/"Testing", Build chunk 2) maps to a task:
- `ImportJob` lifecycle + in-memory single-slot registry, one import at a time (409 on second) → Task 2 (`ImportJobRegistry`, `_ACTIVE_PHASES`, `start` raising) + Task 3/4 (router 409).
- Worker integration: real `WebImportSession` (`lib, None, [fsencode(path)], None, bridge`) on a daemon `threading.Thread` via `run_import_worker` (not BackgroundTasks) → Task 2 (`BeetsImportRunner`) + Task 5 (verified).
- `POST /api/import` `{path, options?}` → `{job_id}`, 409 if running, 400/422 bad path → Tasks 1 (`StartImportRequest`/`Response`), 3 (router + `NonBlankPath`), 4 (tests).
- `GET /api/import/{job}` phase + progress + album queue (folder/album/artist/recommendation/confidence/status) + summary, with non-blocking drain (`get_parked(timeout=0)`) → Tasks 1 (`ImportJobState`/`ImportAlbumSummary`/`ImportProgress`), 2 (`drain`/`state`), 4 (tests).
- `GET /api/import/{job}/albums/{idx}` full `Candidate` → Tasks 2 (`candidate`), 3 (route), 4 (test).
- `POST .../albums/{idx}/choice` → `push_choice`, `KeyError`→404, `RuntimeError`→409 → Tasks 2 (`record_choice` propagation; `test_record_choice_duplicate_raises_runtimeerror` covers the 409 race), 3 (route mapping), 4 (`test_second_choice_after_decided_is_404` covers the realistic over-HTTP path; the 409 race is registry-level because `ImportBridge.park` pops the slot once the worker consumes the first reply — verified).
- `POST .../apply-ready` (clarified: push apply to parked strong albums; strong already auto-apply in the worker per chunk 1) → Tasks 2 (`apply_ready`), 3 (route), 4 (test).
- Polling transport (no SSE/websocket) → all GET-based; stated in Architecture.
- New models `ImportJobState`/`ImportPhase`/`ImportAlbumSummary`/`StartImportRequest`/`StartImportResponse`/`ApplyReadyResponse`/`ImportProgress`/`ImportAlbumStatus` → Task 1.
- Error handling: bad/empty path → 422/400 (not 500); second import → 409; worker exception → `failed` phase + message (never 500 from a crash); abort choice → `ImportAbortError` → `run()` stops → job `done` (clean) → Tasks 2 (`_on_error`/`_on_finish`), 4 (`test_worker_crash_marks_failed_never_500`, `test_abort_choice_ends_job_cleanly`).
- Test-injection seam (substitute the worker with a fake that drives the real bridge) → Tasks 2 (`ImportRunner` protocol + `FakeImportRunner`), 4 (injected via `reset_registry`).
- Thread↔async bridging: endpoints are plain `async def`; bridge calls are non-blocking (`timeout=0`/`push_choice`), so no `anyio.to_thread` needed → Architecture + Task 3.
- mypy strict + ruff green, new beets-touching modules added to the override (`app.import_jobs.runner`, `tests.test_import_runner`) → Tasks 2, 5, 6.

**Placeholder scan:** no "TBD"/"handle edge cases"/"add error handling"/"similar to Task N"; every code, test, and command step is complete. The registry in Task 2 is shown in its fully-typed final form (single top-level `import_models` import, no inline imports, no `# type: ignore`); the only `# type: ignore` in any plan code is the `_poll` test helper's `[no-untyped-def]` for its callable argument (a test-local convenience, per repo convention — and it carries a trailing reason via the surrounding comment in Task 4 Step 3).

**Type consistency:** model names (`ImportPhase`, `ImportAlbumStatus`, `ImportAlbumSummary`, `ImportProgress`, `ImportJobState`, `StartImportRequest`, `StartImportResponse`, `ApplyReadyResponse`) and registry/runner symbols (`ImportJobRegistry`, `ImportJob`, `ImportRunner`, `BeetsImportRunner`, `FakeImportRunner`, `registry`, `reset_registry`, and methods `start`/`get`/`drain`/`candidate`/`record_choice`/`apply_ready`/`state`/`has_active_job`/`attach_library`) are used identically across Tasks 2–6. The chunk-1 symbols consumed (`ImportBridge`, `WebImportSession`, `run_import_worker`, `get_parked`/`push_choice`, `ParkedAlbum`, `Candidate`, `ImportChoice`, `ImportAction`, `Recommendation`) match the merged signatures verified above.

## Out of scope for chunk 2 (do NOT build here)
- **All frontend** — the FE flow shell, candidate-review screen, apply/done UI (chunks 3–5).
- **Watched-folder / incremental auto-import** (separate spec; our later layer over `import.incremental`).
- **Settings / config UI** (separate spec; v1 reads the user's beets `config.yaml` as-is).
- **SSE / websocket progress** — polling only in v1 (a later optimization).
- **enter-MBID / search-again re-lookup, duplicate-resolution UI, singletons** — chunk-1 `WebImportSession` skips singletons and no-ops duplicate resolution; not surfaced via the API here.
- **Fine-grained per-track import progress** — beets exposes no hook we rely on; the phase state machine (`scanning`/`reviewing`/`applying`/`done`/`failed`) + parked/decided counts are the v1 progress signal.
- **A full real-audio `ImportSession.run()` end-to-end test** — no sample audio is shipped; Task 5 verifies the Library→session→thread→callback wiring with a stubbed `run()`. A real-audio smoke test (a tiny checked-in fixture exercising `read_tasks`→`tag_album`→`manipulate_files`) is deferred to chunk 5 ("live screenshot end-to-end"), where the apply/done path is wired and a real sample import is the natural acceptance check.
- **Persisting jobs across process restarts** — the registry is in-memory by design (YAGNI; a restart cancels an in-flight import, acceptable for v1).
