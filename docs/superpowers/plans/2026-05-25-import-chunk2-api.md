# Import API + ImportJob Lifecycle (beets, SEQUENTIAL review) — Chunk 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend import **API** — `POST /api/import` (start), `GET /api/import/{job}` (poll the **live feed** + progress + summary), `GET /api/import/{job}/albums/{idx}` (the full `Candidate` for the album currently awaiting review), and `POST /api/import/{job}/albums/{idx}/choice` (decide it) — on top of an in-memory `ImportJob` lifecycle that runs chunk-1's `WebImportSession`/`run_import_worker` on a dedicated daemon thread, enforces **one import at a time**, and bridges beets' blocking decisions to the async API via chunk-1's `ImportBridge`. Backend-tested hermetically; no frontend.

**The model is SEQUENTIAL (verified — do not re-litigate).** beets imports **one album at a time**: `Pipeline.run_sequential` (`beets/util/pipeline.py:505-514`) pulls ONE task from the first stage and pushes it through **every** later stage — including `user_query` → `task.choose_match` — *before* reading the next task. Our worker forces `config["threaded"]=False` (chunk-1 `run_import_worker`), and `WebImportSession.choose_match` **blocks** inside `bridge.park()` until the album is decided. Therefore **only ONE uncertain album is ever parked at a time**; the next is read only after the current is decided. **Strong** matches **auto-apply** inside `choose_match` (it returns the `AlbumMatch`, never parks). So the API surfaces a **live feed**: auto-applied strong albums stream past as "applied"; each uncertain (medium/low/none) album surfaces one at a time to decide. A multi-album "review queue / review-in-any-order / batch apply-ready" is **NOT achievable** with this design and is **out of scope** — it would need a deferred-decision two-pass engine.

**Architecture:** A new `app/import_jobs/` package holds the lifecycle. `ImportJobRegistry` is a process-global, **single-slot** registry (one active job; a second start → 409). Starting a job builds an `ImportBridge`, constructs a real `WebImportSession(lib, None, [os.fsencode(path)], None, bridge)` from the beets `Library` already opened on `app.state.beets_library` (the existing `get_library` dependency), and runs `run_import_worker(session)` on a daemon `threading.Thread`. The worker runs beets serially; strong matches auto-apply, the (at most one) uncertain album is parked. **Chunk 1 is extended here** with a non-blocking **outcome channel** on `ImportBridge` so the worker reports *every* album it processes (auto-applied or needs-review) — without that, auto-applied strong albums are invisible to the API. Async endpoints **drain** that channel (and the parked album) with non-blocking calls on each poll, and deliver the decision for the parked album with `bridge.push_choice`. The worker entry point is **injectable** (an `ImportRunner` protocol): production builds the real session+thread; tests inject a fake runner that drives the *real* bridge — emitting a couple of canned "applied" outcomes + parking ONE uncertain album (blocks for the choice) + finishing — so the full sequential start→feed→choice→done cycle is tested with no MusicBrainz/audio. New Pydantic models (`app/models/import_api.py`) map the job/bridge/feed state to the HTTP contract; no beets object leaks past the adapter.

**Tech Stack:** Python 3.11+, FastAPI 0.136 + Pydantic v2, beets 2.11.0 (pinned, read from the venv), stdlib `threading` + `queue`, pytest + `fastapi.testclient.TestClient`. mypy `--strict` and Ruff must stay green.

---

## Beets/FastAPI facts this plan encodes (verified against the `feat/import` chunk-1 code + the venv at 2.11.0)

These were confirmed by reading the chunk-1 source and the venv. Encode them exactly; do not re-guess.

- **beets imports one album at a time (the load-bearing fact).** `ImportSession.run()` (venv `beets/importer/session.py:235-242`) picks `pl.run_parallel(...)` when `config["threaded"]` else `pl.run_sequential()`, inside a `try/except ImportAbortError: pass`. `Pipeline.pull()` (venv `beets/util/pipeline.py:491-514`) primes the coroutines then, for each `out` from the **first** stage, sends it through **every** later coroutine before taking the next `out` — so a stage that blocks (our `choose_match` → `bridge.park`) halts the whole pipeline. `run_import_worker` forces `config["threaded"]=False` (`backend/app/beets/import_session.py:203-212`). ⟹ at most ONE album is parked at any moment.
- **`choose_match` already auto-applies strong + parks uncertain** (`backend/app/beets/import_session.py:132-171`): `task.rec == BeetsRec.strong and candidates` → `return candidates[0]` (no park); no candidates → `Action.SKIP`; else map the top match → `Candidate`, `bridge.park(ParkedAlbum(...))` (blocks), then `_apply_choice(...)`. The per-album index is an unlocked single-writer counter `self._album_index` (serial-only, safe). **We extend this method** to also report an outcome on every path (Task 1).
- **`get_library` (the dependency) returns `LibraryHandle | None`** (`backend/app/api/albums.py:11-19`). `LibraryHandle = beets.library.Library` (`backend/app/beets/library.py:37`). It reads `request.app.state.beets_library` (set once in the lifespan, `backend/app/main.py:69-70`) and is **overridden in tests** via `app.dependency_overrides[get_library] = lambda: temp_library` (`backend/tests/test_albums.py`). The import router does **not** read the library through `get_library` — the registry holds the `Library` (attached at lifespan, Task 3) because the worker thread has no request — so no dependency override is needed for the import tests (they inject a `FakeImportRunner` instead).
- **A real beets `Library` is `Library(library_path, directory=directory)`** (`backend/app/beets/library.py:40-46`). Tests build one directly: `Library(str(tmp_path / "library.db"), directory=str(tmp_path))` (`backend/tests/test_albums.py`, `tests/test_import_session.py`).
- **`WebImportSession.__init__(self, lib, loghandler, paths, query, bridge)`** (`backend/app/beets/import_session.py:104-115`) → `super().__init__(lib, loghandler, paths, query)` then stores `self.bridge` + inits `self._album_index = 0`. For a **path import**: `loghandler=None` (beets installs a `NullHandler`), `paths=[os.fsencode(path)]`, `query=None`. Verified construction string: `WebImportSession(lib, None, [os.fsencode(path)], None, bridge)`.
- **`run_import_worker(session)`** (`backend/app/beets/import_session.py:203-212`) sets `config["threaded"]=False` then calls `session.run()`. `run()` **catches `ImportAbortError` and returns silently**; any *other* exception propagates out — so the worker-thread target MUST wrap the call in `try/except` to mark the job failed (never an unhandled thread exception).
- **Abort is beets' native `ImportAbortError`** (NOT a custom class). `WebImportSession._apply_choice` raises `ImportAbortError` for `ImportAction.abort` (`import_session.py:183-184`); `run()` catches it → the import ends cleanly and `run()` returns normally → the worker marks the job **done**, not failed.
- **`ImportBridge` methods** (`backend/app/beets/import_session.py:45-96`), all thread-safe:
  - `park(parked: ParkedAlbum) -> ImportChoice` — worker side; **blocks** until a choice is pushed for `parked.album_index`. (Not called by the API.)
  - `get_parked(timeout: float | None = None) -> ParkedAlbum | None` — consumer side; `timeout=0` returns immediately (`None` when the out-queue is empty). The **non-blocking drain** the API uses for the parked album.
  - `push_choice(album_index: int, choice: ImportChoice) -> None` — raises `KeyError` for an unknown/unparked index, `RuntimeError` for a duplicate push to an already-answered slot (the reply queue is `maxsize=1` → `queue.Full` → `RuntimeError`).
  - `pending_count() -> int` — albums currently parked-and-waiting (0 or 1 under serial import).
  - **(added in Task 1)** `note_outcome(outcome: AlbumOutcome) -> None` — worker side, **non-blocking** (`queue.Queue.put_nowait`); `drain_outcomes() -> list[AlbumOutcome]` — consumer side, non-blocking, returns every outcome queued since the last drain.
- **`ParkedAlbum`** (`backend/app/models/import_models.py:113-121`): `album_index: int`, `folder: str`, `candidate: Candidate`. **`Candidate`** (`import_models.py:92-110`): `confidence: float`, `recommendation: Recommendation`, `data_source`/`data_url: str | None`, `changed_fields: list[str]`, `album_before`/`album_after: AlbumChange`, `tracks: list[TrackChange]`, `missing`/`unmatched`, `options: list[CandidateOption]`. `AlbumChange` (`import_models.py:33-46`): `artist`/`album`/`label`/`country`/`media: str | None`, `year: int | None`. **`ImportChoice`** (`import_models.py:140-146`): `action: ImportAction`, `candidate_index: int | None = None`. **`ImportAction`** (`import_models.py:124-137`): `apply`/`skip`/`asis`/`astracks`/`abort`. **`Recommendation`** (`import_models.py:13-23`): `none`/`low`/`medium`/`strong` (a `StrEnum`).
- **The `AlbumMatch` fields the strong-path outcome needs** are already mapped in chunk-1: `_confidence(match.distance)` = `round((1 - distance) * 100, 1)` and `_REC_MAP`/`map_album_match`'s recommendation (`backend/app/beets/import_mapping.py:28-33`, `import_session.py:36-42`). `task.cur_artist`/`task.cur_album` are populated by `task.lookup_candidates([])` (verified in `tests/test_import_session.py:85-89`). `_task_folder(task)` already decodes `task.paths[0]` (`import_session.py:196-200`).
- **FastAPI endpoint style** (`backend/app/api/search.py`, `albums.py`): `router = APIRouter(tags=[...])`; `async def` handlers; `HTTPException(status_code=..., detail=...)`; routers mounted in `main.py` with `app.include_router(router, prefix="/api")`. `from fastapi import status` exposes `status.HTTP_202_ACCEPTED`, `HTTP_409_CONFLICT`, `HTTP_404_NOT_FOUND`, `HTTP_204_NO_CONTENT` — all importable.
- **The bridge calls used by the API are non-blocking** (`get_parked(timeout=0)`, `drain_outcomes()`, `push_choice`), so the endpoints are plain `async def` with **no `anyio.to_thread`** — they never block the event loop. (`anyio` 4.13.0 is present if ever needed; not needed here.)
- **mypy override**: `pyproject.toml:44-46` lists modules with `disallow_untyped_calls = false` (they touch beets' untyped surface). The production runner module + the runner test (build a beets `Library`/session) go in this list. The chunk-1 `app.beets.import_session`/`tests.test_import_session` are already there — the Task-1 extension stays inside them. The registry, models, fakes, and API router are pure-typed and stay OUT.
- **Test infra**: `tests/conftest.py` provides the `anyio_backend` fixture. `tests/test_import_session.py` already constructs a real `ImportBridge()` and drives `choose_match` across threads — the Task-1 outcome channel must keep those tests green. `TestClient(app)` is the established API pattern; the registry is a **module-level singleton** reset by an autouse fixture each test (Task 3).

---

## Design decisions (grounded, YAGNI)

- **Chunk-1 addition: a non-blocking outcome channel on `ImportBridge` + an `AlbumOutcome` model — minimal.** Today the bridge only carries *parked* (uncertain) albums via the blocking `park`/`get_parked` pair; auto-applied **strong** albums never park, so they are invisible to the API. The fix is the smallest thing that lets the API list *every* album the worker processed with its outcome and identify the one awaiting a decision: a second, **non-blocking** `queue.Queue[AlbumOutcome]` inside `ImportBridge`, written by the worker (`note_outcome`, `put_nowait`) and drained by the consumer (`drain_outcomes`, non-blocking). `WebImportSession.choose_match` emits exactly one outcome per album it processes:
  - **strong** → `note_outcome(AlbumOutcome(..., status="applied"))` then `return candidates[0]` (auto-apply, unchanged);
  - **no candidates** → `note_outcome(AlbumOutcome(..., status="skipped"))` then `return Action.SKIP`;
  - **uncertain** → `note_outcome(AlbumOutcome(..., status="needs_review"))` **and** `park(...)` (blocks). The outcome is emitted *before* `park` so the API sees the album the moment it's parked.
  `ParkedAlbum` is unchanged (it still carries the full `Candidate` for the review screen).
- **The "decided" final state lives in the registry, not a second worker outcome (the simpler correct option).** When the user's choice for the parked album resolves, the worker's `park()` returns the `ImportChoice` and beets applies it — the worker does **not** re-report. The **registry** already knows the index + chosen action (it called `push_choice`), so `record_choice` flips that album's row from `needs_review` to `decided` (storing the action) locally. Rationale: the worker has nothing new to add after `park` returns (the outcome — apply/skip/asis/astracks/abort — *is* the choice the registry pushed), so a second channel round-trip would be redundant bookkeeping. This keeps the worker emission a single pre-park `needs_review` note and avoids racing a post-park outcome against the registry's own state. (YAGNI: no second "decided" outcome event.)
- **`AlbumOutcome` lives in `app/models/import_models.py`** (next to `ParkedAlbum`, the other worker↔bridge payload), because both the bridge (`app/beets/import_session.py`) and the registry/API consume it, and `import_models.py` is the shared beets-free contract both already import. (Putting it in `import_api.py` would force `app/beets/` to import an `api` module — wrong direction.) It carries only what a feed row needs: `album_index`, `folder`, `artist`/`album` (nullable), `recommendation`, `confidence`, and a `status` enum (`AlbumOutcomeStatus`: `applied`/`needs_review`/`skipped`). The full `Candidate` is fetched separately via the parked album, so the outcome stays compact.
- **No multi-album queue, no `apply-ready`.** At most one album is parked at a time, so there is no batch of strong albums sitting un-applied to flush (chunk-1 auto-applies them inline). The job's **feed** = all drained outcomes (applied + skipped + the current needs_review one), keyed by `album_index`. There is **no** `apply_ready` endpoint/method and **no** `ApplyReadyResponse` model.
- **Where the job lives — a module-level singleton, not `app.state`.** `app.state.beets_library` is set once at startup and never mutated; a per-request handler reading it is fine. The import job, by contrast, is **mutated across many requests** (start, drain on each poll, push the choice, finish on the worker thread). A module-level `ImportJobRegistry` instance in `app/import_jobs/registry.py` is the simplest correct home: one import-wide source of truth, trivially importable by the router, reset between tests with a fixture. (We do NOT scatter it on `app.state`, which would couple it to a specific `app` instance and complicate the worker thread that has no request.)
- **Single active job (spec "imports run serially … a second start is queued or rejected").** We **reject** with 409 (queueing is YAGNI for v1). The registry holds at most one job; a job is "active" while its phase is `scanning`/`reviewing`/`applying`. Once `done`/`failed`, a new start replaces it.
- **Injectable runner = the test seam.** `ImportJobRegistry.start(...)` uses an `ImportRunner` (a `Protocol` with `run(path, bridge, on_finish, on_error) -> None`). Production = `BeetsImportRunner` (builds the real `WebImportSession` + daemon thread). Tests inject a `FakeImportRunner` that, on its own thread, emits a few canned `AlbumOutcome`s + parks ONE canned `ParkedAlbum` (blocks for the choice) + calls `on_finish` — driving the **real `ImportBridge`** so the sequential start→feed→drain→choice→done cycle is exercised with no network/audio. Only beets' `run()` is replaced.
- **Progress/`phase` is derived, not pushed.** beets exposes no fine progress hook we rely on. The phase is a coarse state machine the registry owns: `scanning` at start → `reviewing` once an album is **parked-and-waiting** (a `needs_review` outcome whose row is still undecided) → `done` set by the worker's `on_finish` → `failed` set by `on_error`. (`applying` exists in the enum for faithfulness but the worker exposes no signal to set it transiently; we do not invent one — `reviewing` covers the in-flight state.) `progress` = `{applied, needs_review}` counts derived from the feed. Faithful (we surface beets' real per-album outcomes) without inventing per-track progress.
- **Truthful summary.** On `done`, the summary counts across **all** outcomes — not just the user's decisions: `N imported` = auto-`applied` outcomes + needs_review rows decided with an apply-like action (`apply`/`asis`/`astracks`); `M skipped` = `skipped` outcomes + needs_review rows decided `skip`. (Abort ends the run early — the summary reflects whatever was processed before the abort.)

---

## File structure

| File | Create/Modify | Responsibility |
| --- | --- | --- |
| `backend/app/models/import_models.py` | **Modify** | Add `AlbumOutcomeStatus` (str enum: `applied`/`needs_review`/`skipped`) + `AlbumOutcome` (compact per-album feed payload). Beets-free; sits next to `ParkedAlbum`. |
| `backend/app/beets/import_session.py` | **Modify** | Add the outcome channel to `ImportBridge` (`_outcomes: queue.Queue[AlbumOutcome]`, `note_outcome`, `drain_outcomes`) and have `WebImportSession.choose_match` emit one `AlbumOutcome` on every path (strong→applied, none→skipped, uncertain→needs_review before park). A small helper builds the outcome from `task`+match. Keeps all chunk-1 tests green. |
| `backend/app/models/import_api.py` | Create | HTTP-contract models: `ImportPhase` (str enum), `StartImportRequest`, `StartImportResponse`, `ImportAlbumStatus` (str enum: `needs_review`/`decided`/`applied`/`skipped`), `ImportAlbumSummary` (a feed row), `ImportProgress`, `ImportJobState` (the `GET` response). No beets imports; no `ApplyReadyResponse`. |
| `backend/app/import_jobs/__init__.py` | Create | Package marker for the import-job lifecycle (pure-typed; no beets). |
| `backend/app/import_jobs/runner.py` | Create | The `ImportRunner` `Protocol` + `BeetsImportRunner` (builds the real `WebImportSession` from a `Library`+path, runs `run_import_worker` on a daemon thread, reports completion/error via callbacks). The ONLY new module that touches beets here → mypy override. |
| `backend/app/import_jobs/fakes.py` | Create | `FakeImportRunner` — a test-only `ImportRunner` that drives the **real** bridge: emits canned `applied` outcomes + parks ONE uncertain `ParkedAlbum` (blocks for the choice) + finishes (or parks the next after the choice). Shared by registry + API tests (DRY); pure-typed. |
| `backend/app/import_jobs/registry.py` | Create | `ImportJob` (dataclass: id, bridge, phase, ordered `dict[int, _FeedAlbum]`, summary, error) + `ImportJobRegistry` (single-slot; `start`, `get`, `drain`, `candidate`, `record_choice`, `state`, lifecycle transitions; thread-safe). Module-level `registry` singleton + `reset_registry()` for tests. Pure-typed. **No `apply_ready`.** |
| `backend/app/api/import_.py` | Create | The FastAPI router: `POST /import`, `GET /import/{job}`, `GET /import/{job}/albums/{idx}`, `POST /import/{job}/albums/{idx}/choice`. Thin — validates input, delegates to the registry, maps bridge `KeyError`→404 / `RuntimeError`→409. **No `apply-ready` route.** |
| `backend/app/main.py` | Modify | Mount the import router + attach the library to the registry in the lifespan. |
| `backend/tests/conftest.py` | Modify | Add a `reset_import_registry` autouse fixture so the single-slot registry never leaks a job across tests. |
| `backend/tests/test_import_session.py` | **Modify** | Add the outcome-channel TDD tests (Task 1): `note_outcome`/`drain_outcomes` round-trip; strong→`applied` outcome (no park); uncertain→`needs_review` outcome emitted at park; no-candidates→`skipped`. Keep every existing chunk-1 test passing. |
| `backend/tests/test_import_api.py` | Create | Model tests (Task 2) + full sequential HTTP flow (Task 5) via `TestClient` + `FakeImportRunner`: start→202; second→409; blank path→422; feed shows applied + the current needs_review row; fetch candidate; choice→advances/done; unknown index→404; second choice after advance→404; abort→clean done; worker crash→failed; truthful summary. |
| `backend/tests/test_import_registry.py` | Create | Hermetic unit tests for `ImportJobRegistry` + `ImportJob` over the **real `ImportBridge`** with `FakeImportRunner`: start, single-slot 409, drain accumulates the feed (applied + the ONE current parked), record_choice round-trips + marks decided + unblocks the worker, the duplicate-choice `RuntimeError` (409 race, forced deterministically), finish→done + truthful summary, error→failed. **No** multi-album-simultaneous-park test. |
| `backend/tests/test_import_runner.py` | Create | `BeetsImportRunner` builds a real `WebImportSession` from a hermetic beets `Library` + tmp folder and starts a daemon thread, with `WebImportSession.run` monkeypatched to a no-op (no network/audio) — proves the production wiring. mypy override (builds a `Library`). |
| `backend/pyproject.toml` | Modify | Add `app.import_jobs.runner`, `tests.test_import_runner` to the `disallow_untyped_calls = false` override list. |

**Decomposition rationale:** chunk-1 extension (outcome channel — the hardest correctness piece, validated against the real bridge first) → HTTP models (pure) → registry + fakes (pure; depend on models + the extended `ImportBridge`) → API router (depends on registry + models) → full sequential flow tests → real beets runner (touches beets; isolated + its own test) → mount + full-suite green. The `FakeImportRunner` lets the registry and API be fully tested before the real beets runner exists.

---

## Conventions to follow (from the existing codebase)

- **Pydantic style** (`app/models/import_models.py`, `album.py`): plain `class X(BaseModel)`; explicit `int | None`/`str | None`; `StrEnum` for string enums; short comments on non-obvious fields; no config classes unless needed.
- **Router style** (`app/api/search.py`, `albums.py`): `APIRouter(tags=[...])`; `async def`; `HTTPException`; `response_model=` on each route. The import router needs **no** `get_library` dependency (the registry owns the `Library`).
- **Adapter boundary** (CLAUDE.md rule 3): beets imports ONLY under `app/beets/`. This chunk adds beets contact in exactly one new place — `app/import_jobs/runner.py` imports `WebImportSession`/`run_import_worker` **from `app.beets.import_session`** (our adapter, not beets directly) + `os.fsencode`. The Task-1 changes to `import_session.py`/`import_models.py` stay inside the existing boundary. The registry, models, router, and fakes import **no beets**.
- **Hermetic library fixture** (`tests/test_albums.py`, `tests/test_import_session.py`): `Library(str(tmp_path / "library.db"), directory=str(tmp_path))`. Used by `test_import_runner.py`.
- **Single-slot state reset**: the registry is module-global, so a fixture MUST reset it each test (Task 3).
- **Commit style:** Conventional Commits, **no `Co-Authored-By`** (global + project rule). Stay on `feat/import`; no push (the user opens the PR).
- **Dev commands run from the repo root with** `uv --directory backend run ...`.

---

## Task 1: Chunk-1 addition — the `ImportBridge` outcome channel + `AlbumOutcome` (so every album is visible)

**Why:** the bridge carries only parked albums today; auto-applied strong albums never park and are invisible. Add a non-blocking outcome channel and emit one outcome per album so the API can render the live feed + a truthful summary. This is the architecture-defining piece — do it first and verify against the real `ImportBridge`.

**Files:**
- Modify: `backend/app/models/import_models.py` (add `AlbumOutcomeStatus` + `AlbumOutcome`)
- Modify: `backend/app/beets/import_session.py` (outcome channel on `ImportBridge` + emission in `choose_match`)
- Test: `backend/tests/test_import_session.py` (append the outcome tests; keep existing tests green)

- [ ] **Step 1: Write the failing tests (append to `tests/test_import_session.py`)**

Append these. They use the existing helpers (`_build_match`, `_make_session`, `_make_task`, `_patch_tag_album`) and the real `ImportBridge`/`AlbumOutcome`:

```python
from app.models.import_models import AlbumOutcome, AlbumOutcomeStatus


def test_bridge_outcome_channel_round_trips() -> None:
    bridge = ImportBridge()
    assert bridge.drain_outcomes() == []  # empty, non-blocking
    outcome = AlbumOutcome(
        album_index=0,
        folder="/music/album",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )
    bridge.note_outcome(outcome)
    drained = bridge.drain_outcomes()
    assert drained == [outcome]
    # Draining again yields nothing (the queue was consumed).
    assert bridge.drain_outcomes() == []


def test_strong_rec_emits_applied_outcome_without_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _build_match(BeetsRec.strong)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.strong)

    task.choose_match(session)

    assert bridge.pending_count() == 0  # never parked (unchanged chunk-1 behavior)
    outcomes = bridge.drain_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].status is AlbumOutcomeStatus.applied
    assert outcomes[0].recommendation is Recommendation.strong
    assert outcomes[0].album == "OK Computer"


def test_uncertain_rec_emits_needs_review_outcome_at_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    # The needs_review outcome is emitted BEFORE park, so it is already drainable
    # while the worker blocks on the reply.
    outcomes = bridge.drain_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].album_index == parked.album_index
    assert outcomes[0].status is AlbumOutcomeStatus.needs_review
    assert outcomes[0].recommendation is Recommendation.medium

    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)


def test_no_candidates_emits_skipped_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Artist", "Album", Proposal([], BeetsRec.none))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = ImportTask(
        toppath=None,
        paths=[b"/music/album"],
        items=[Item(artist="Artist", album="Album", title="X", track=1, length=10.0)],
    )
    task.lookup_candidates([])

    task.choose_match(session)

    assert bridge.pending_count() == 0
    outcomes = bridge.drain_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].status is AlbumOutcomeStatus.skipped
```

(Add `Recommendation` to the existing `from app.models.import_models import (...)` line in the test module if it is not already imported.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_session.py -v`
Expected: FAIL — `ImportError: cannot import name 'AlbumOutcome'` (then, once the model exists, `AttributeError: 'ImportBridge' object has no attribute 'drain_outcomes'`).

- [ ] **Step 3: Add the model**

In `backend/app/models/import_models.py`, after `ParkedAlbum` (and after the existing `Recommendation` enum it references), add:

```python
class AlbumOutcomeStatus(StrEnum):
    """What the worker did with one album, for the live import feed.

    applied      -> a strong match auto-applied (beets applied it inline)
    needs_review -> an uncertain match was parked and is awaiting a decision
    skipped      -> nothing to apply (no candidates), so the album was skipped
    """

    applied = "applied"
    needs_review = "needs_review"
    skipped = "skipped"


class AlbumOutcome(BaseModel):
    """A compact per-album record the worker emits for every album it processes.

    Pushed onto the import bridge's non-blocking outcome channel so the API can
    render the live feed (auto-applied + skipped + the current parked album) and
    a truthful summary. The full Candidate (for the review screen) travels
    separately on the parked album; this stays small on purpose.
    """

    album_index: int
    folder: str
    artist: str | None
    album: str | None
    recommendation: Recommendation
    confidence: float
    status: AlbumOutcomeStatus
```

- [ ] **Step 4: Add the outcome channel to `ImportBridge` + emit in `choose_match`**

In `backend/app/beets/import_session.py`:

Extend the import from our models:

```python
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    ImportAction,
    ImportChoice,
    ParkedAlbum,
    Recommendation,
)
```

In `ImportBridge.__init__`, add the non-blocking outcome queue alongside `_out`:

```python
        self._out: queue.Queue[ParkedAlbum] = queue.Queue()
        self._outcomes: queue.Queue[AlbumOutcome] = queue.Queue()
        self._replies: dict[int, queue.Queue[ImportChoice]] = {}
        self._lock = threading.Lock()
        self._pending = 0
```

Add the two methods (worker side `note_outcome`, consumer side `drain_outcomes`) — both non-blocking, no lock needed (`queue.Queue` is itself thread-safe):

```python
    # ----- worker side -----

    def note_outcome(self, outcome: AlbumOutcome) -> None:
        """Record what the worker did with one album (non-blocking).

        Called for EVERY album choose_match processes — auto-applied, skipped, or
        parked — so the consumer can render the full live feed. Never blocks.
        """
        self._outcomes.put_nowait(outcome)

    # ----- consumer side -----

    def drain_outcomes(self) -> list[AlbumOutcome]:
        """Pop every outcome queued since the last drain (non-blocking)."""
        drained: list[AlbumOutcome] = []
        while True:
            try:
                drained.append(self._outcomes.get_nowait())
            except queue.Empty:
                break
        return drained
```

(Place `note_outcome` near `park` in the worker-side block and `drain_outcomes` near `get_parked` in the consumer-side block, matching the file's existing section comments.)

Add a small private helper on `WebImportSession` that builds an `AlbumOutcome` from the task + a recommendation + status, reusing chunk-1's confidence mapping. Put it next to `_task_folder`:

```python
    def _outcome(
        self,
        index: int,
        task: ImportTask,
        recommendation: Recommendation,
        status: AlbumOutcomeStatus,
        *,
        match: Any | None = None,
    ) -> AlbumOutcome:
        """Build a compact feed outcome for one album.

        ``match`` (an AlbumMatch) supplies the confidence when present (applied /
        needs_review); a skip has no match, so confidence is 0.0.
        """
        confidence = _confidence(match.distance) if match is not None else 0.0
        return AlbumOutcome(
            album_index=index,
            folder=self._task_folder(task),
            artist=_opt_str(task.cur_artist),
            album=_opt_str(task.cur_album),
            recommendation=recommendation,
            confidence=confidence,
            status=status,
        )
```

This needs `_confidence` and `_opt_str` from the mapping module — import them at the top of `import_session.py` (they already exist and are pure):

```python
from app.beets.import_mapping import (
    _confidence,
    _opt_str,
    map_album_match,
    map_candidate_options,
)
```

> If importing the underscore-prefixed helpers reads poorly, the equally-valid alternative is to inline `round((1.0 - float(match.distance)) * 100.0, 1)` and a 3-line local `_opt_str`; pick one and keep it consistent. Importing the existing functions is preferred (single source of truth for the distance→% formula).

Now wire emission into `choose_match`. Assign the album index up-front so all paths can reference it, and emit before each return/park:

```python
    def choose_match(self, task: ImportTask) -> Any:
        """Auto-apply a strong match; otherwise park and await the user.

        Emits exactly one AlbumOutcome for the album (applied / skipped /
        needs_review) so the API can show it in the live feed. Returns either an
        ``AlbumMatch`` (to apply) or an ``Action`` constant.
        """
        candidates: list[Any] = list(task.candidates or [])
        # Each album gets a stable index for both its outcome and (if parked) its
        # reply slot. Serial-only: single-writer counter, no lock (config
        # ["threaded"] = False keeps choose_match on one thread).
        index = self._album_index
        self._album_index += 1
        rec = task.rec if task.rec is not None else BeetsRec.none
        recommendation = _REC_MAP.get(rec, Recommendation.none)

        if rec == BeetsRec.strong and candidates:
            # Mirror beets' auto-apply of a strong recommendation.
            self.bridge.note_outcome(
                self._outcome(
                    index, task, recommendation, AlbumOutcomeStatus.applied, match=candidates[0]
                )
            )
            return candidates[0]

        if not candidates:
            # Nothing to choose from: skip (an empty match can't be applied).
            self.bridge.note_outcome(
                self._outcome(index, task, recommendation, AlbumOutcomeStatus.skipped)
            )
            return Action.SKIP

        # Park: map the top match + ranked alternatives, emit needs_review, push,
        # block. The outcome is emitted BEFORE park so the API sees the album the
        # instant it parks (park then blocks on the reply).
        top = candidates[0]
        options = map_candidate_options(candidates)
        candidate = map_album_match(
            top,
            cur_artist=task.cur_artist,
            cur_album=task.cur_album,
            options=options,
            recommendation=recommendation,
        )
        folder = self._task_folder(task)
        self.bridge.note_outcome(
            self._outcome(index, task, recommendation, AlbumOutcomeStatus.needs_review, match=top)
        )
        choice = self.bridge.park(
            ParkedAlbum(album_index=index, folder=folder, candidate=candidate)
        )
        return self._apply_choice(choice, candidates)
```

(This is a refactor of the existing `choose_match`: the `index`/`rec`/`recommendation` lines move *above* the strong branch so every path can build an outcome; the strong/no-candidate branches gain a `note_outcome` call; the park branch gains a pre-park `note_outcome` and reuses the already-computed `recommendation`. The behavior beets sees — return `candidates[0]` / `Action.SKIP` / `_apply_choice(...)` — is unchanged, so all existing chunk-1 tests still pass.)

- [ ] **Step 5: Run to verify it passes — including ALL existing chunk-1 tests**

Run: `uv --directory backend run pytest tests/test_import_session.py tests/test_import_models.py tests/test_import_mapping.py -v`
Expected: PASS — the 4 new outcome tests **and** every pre-existing chunk-1 test (strong auto-apply, uncertain park/skip/asis/astracks, abort no-leak, the anyio bridge test, the worker single-threaded test, no-candidates skip). If any existing test regresses, the refactor changed observable behavior — revert to returning the same beets values and only *add* `note_outcome` calls.

- [ ] **Step 6: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. `app.beets.import_session` and `tests.test_import_session` are already in the `disallow_untyped_calls = false` override (`pyproject.toml:45`), so the beets-touching calls are fine. The new model in `import_models.py` is pure-typed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/import_models.py backend/app/beets/import_session.py backend/tests/test_import_session.py
git commit -m "feat(import): emit per-album outcomes on a non-blocking bridge channel"
```

---

## Task 2: HTTP-contract Pydantic models

**Files:**
- Create: `backend/app/models/import_api.py`
- Test: `backend/tests/test_import_api.py` (created here with model-only tests; HTTP tests are appended in Task 5)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_import_api.py`:

```python
from app.models.import_api import (
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
    assert [s.value for s in ImportAlbumStatus] == [
        "needs_review",
        "decided",
        "applied",
        "skipped",
    ]


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
        progress=ImportProgress(applied=1, needs_review=1),
        albums=[
            ImportAlbumSummary(
                index=0,
                folder="/music/incoming/Radiohead - OK Computer",
                artist="Radiohead",
                album="OK Computer",
                recommendation=Recommendation.strong,
                confidence=99.0,
                status=ImportAlbumStatus.applied,
            ),
            ImportAlbumSummary(
                index=1,
                folder="/music/incoming/Unknown",
                artist="Radiohead",
                album="OK Computer",
                recommendation=Recommendation.medium,
                confidence=75.5,
                status=ImportAlbumStatus.needs_review,
            ),
        ],
        summary=None,
        error=None,
    )
    dumped = state.model_dump()
    assert dumped["phase"] == "reviewing"
    assert dumped["progress"] == {"applied": 1, "needs_review": 1}
    assert dumped["albums"][0]["status"] == "applied"
    assert dumped["albums"][1]["status"] == "needs_review"
    assert dumped["summary"] is None
    assert dumped["error"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.import_api'`.

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/import_api.py`:

```python
"""HTTP-contract Pydantic models for the import API (chunk 2).

These are the request/response shapes the import endpoints take and return. They
map the in-memory ImportJob/ImportBridge/feed state to JSON; no beets object
ever reaches here. The per-album Candidate payload reuses
app.models.import_models.Candidate (the chunk-1 contract).

The review model is SEQUENTIAL: beets imports one album at a time, so this
exposes a live feed (auto-applied + skipped + the one current parked album), not
a browsable multi-album queue. There is no apply-ready shape.
"""

from enum import StrEnum

from pydantic import BaseModel

from app.models.import_models import Recommendation


class ImportPhase(StrEnum):
    """Coarse lifecycle phase of an import job (the registry owns transitions).

    scanning  -> the worker is reading/grouping/looking up (nothing parked yet)
    reviewing -> an album is parked-and-waiting for a decision
    applying  -> reserved (beets exposes no signal to set it transiently)
    done      -> the import finished (incl. a clean abort)
    failed    -> the worker raised; ``error`` holds the message
    """

    scanning = "scanning"
    reviewing = "reviewing"
    applying = "applying"
    done = "done"
    failed = "failed"


class ImportAlbumStatus(StrEnum):
    """Per-album state in the live feed.

    needs_review -> parked, awaiting the user's decision (the current album)
    decided      -> the user decided a parked album (apply/skip/asis/astracks)
    applied      -> a strong match auto-applied in the worker (never parked)
    skipped      -> the worker skipped it (no candidates)
    """

    needs_review = "needs_review"
    decided = "decided"
    applied = "applied"
    skipped = "skipped"


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
    """Coarse progress counters derived from the drained outcomes."""

    applied: int
    needs_review: int


class ImportAlbumSummary(BaseModel):
    """One row in the live feed (the GET /api/import/{job} listing).

    The full per-album diff is fetched separately via
    ``GET /api/import/{job}/albums/{index}`` (only meaningful while an album is
    ``needs_review``); this is the compact feed row.
    """

    index: int
    folder: str
    artist: str | None
    album: str | None
    recommendation: Recommendation
    confidence: float
    status: ImportAlbumStatus


class ImportJobState(BaseModel):
    """Response of ``GET /api/import/{job}``: phase + progress + the live feed."""

    job_id: str
    phase: ImportPhase
    progress: ImportProgress
    albums: list[ImportAlbumSummary]
    # A short human summary once done (e.g. "2 imported, 1 skipped"); None until then.
    summary: str | None
    # The worker's failure message when phase == failed; None otherwise.
    error: str | None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. (`import_api.py` imports only our models; no override needed.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/import_api.py backend/tests/test_import_api.py
git commit -m "feat(import): add HTTP-contract models for the sequential import API"
```

---

## Task 3: ImportJob + ImportJobRegistry (single-slot, sequential) + FakeImportRunner

**Files:**
- Create: `backend/app/import_jobs/__init__.py`
- Create: `backend/app/import_jobs/runner.py`
- Create: `backend/app/import_jobs/fakes.py`
- Create: `backend/app/import_jobs/registry.py`
- Modify: `backend/tests/conftest.py` (add the `reset_import_registry` autouse fixture)
- Modify: `backend/pyproject.toml` (add `app.import_jobs.runner` to the override)
- Test: `backend/tests/test_import_registry.py`

- [ ] **Step 1: Create the package marker**

Create `backend/app/import_jobs/__init__.py`:

```python
"""In-memory import-job lifecycle (chunk 2).

Owns the single active ImportJob, drains chunk-1's ImportBridge (per-album
outcomes + the at-most-one parked album) into a live feed, and delivers the
user's choice for the parked album back to the worker. Pure-typed: imports the
ImportBridge + our models, never beets directly (the beets contact lives in
app/import_jobs/runner.py and app/beets/).
"""
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_import_registry.py`. Note the **sequential** shape: the fake emits `applied` outcomes and parks **one** album at a time — there is no test that two albums are parked simultaneously (that is impossible under serial import).

```python
import threading

import pytest

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.models.import_api import ImportAlbumStatus, ImportPhase
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    ImportAction,
    ImportChoice,
    ParkedAlbum,
    Recommendation,
)


def _candidate(rec: Recommendation, *, confidence: float = 75.5) -> Candidate:
    album = AlbumChange(
        artist="Radiohead", album="OK Computer", year=1997, label=None, country=None, media=None
    )
    return Candidate(
        confidence=confidence,
        recommendation=rec,
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        changed_fields=["album"],
        album_before=album,
        album_after=album,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )


def _parked(index: int, rec: Recommendation) -> ParkedAlbum:
    return ParkedAlbum(
        album_index=index, folder=f"/music/incoming/album{index}", candidate=_candidate(rec)
    )


def _applied_outcome(index: int) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=index,
        folder=f"/music/incoming/album{index}",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )


def _poll(fn, want, attempts: int = 200) -> None:  # type: ignore[no-untyped-def]  # test-local poll: fn/want are inline callables
    ev = threading.Event()
    for _ in range(attempts):
        if want(fn()):
            return
        ev.wait(0.01)


def test_start_returns_job_and_marks_active() -> None:
    registry = ImportJobRegistry(runner=FakeImportRunner(parked=[_parked(0, Recommendation.medium)]))
    job_id = registry.start("/music/incoming")
    assert job_id
    assert registry.get(job_id) is not None
    assert registry.has_active_job() is True


def test_second_start_while_active_raises() -> None:
    registry = ImportJobRegistry(runner=FakeImportRunner(parked=[_parked(0, Recommendation.medium)]))
    registry.start("/music/incoming")
    with pytest.raises(RuntimeError):
        registry.start("/music/other")


def test_drain_builds_feed_with_applied_then_the_current_parked() -> None:
    # The fake emits one 'applied' outcome, then parks ONE uncertain album
    # (blocking) — the sequential reality. The feed shows both rows.
    fake = FakeImportRunner(
        applied=[_applied_outcome(0)], parked=[_parked(1, Recommendation.medium)]
    )
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 2)
    rows = {r.index: r for r in registry.state(job_id).albums}
    assert rows[0].status is ImportAlbumStatus.applied
    assert rows[1].status is ImportAlbumStatus.needs_review
    assert registry.state(job_id).phase is ImportPhase.reviewing
    assert registry.state(job_id).progress.applied == 1
    assert registry.state(job_id).progress.needs_review == 1


def test_record_choice_marks_decided_and_unblocks_worker() -> None:
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.apply))

    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.phase is ImportPhase.done
    assert state.albums[0].status is ImportAlbumStatus.decided
    assert state.summary is not None


def test_unknown_index_choice_raises_keyerror() -> None:
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    with pytest.raises(KeyError):
        registry.record_choice(job_id, 99, ImportChoice(action=ImportAction.apply))


def test_record_choice_duplicate_raises_runtimeerror() -> None:
    # The 409 path: two choices RACE the same still-unconsumed reply slot. We
    # force it deterministically — the worker is blocked inside bridge.park's
    # reply.get(); two synchronous push_choice calls (GIL held, no thread switch
    # between them) fill the maxsize=1 reply queue, so the 2nd raises
    # RuntimeError before the worker drains it. Proves record_choice PROPAGATES
    # the bridge's RuntimeError (the router maps it to 409).
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    with pytest.raises(RuntimeError):
        registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))


def test_worker_error_marks_job_failed() -> None:
    fake = FakeImportRunner(parked=[], fail_with="boom")
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.failed)
    state = registry.state(job_id)
    assert state.phase is ImportPhase.failed
    assert state.error == "boom"


def test_summary_counts_applied_and_decided_truthfully() -> None:
    # One auto-applied + one parked-then-applied -> "2 imported, 0 skipped".
    fake = FakeImportRunner(
        applied=[_applied_outcome(0)], parked=[_parked(1, Recommendation.medium)]
    )
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 2)
    registry.record_choice(job_id, 1, ImportChoice(action=ImportAction.apply))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    summary = registry.state(job_id).summary
    assert summary is not None
    assert "2 imported" in summary
    assert "0 skipped" in summary
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.import_jobs.fakes'` (and `.registry`).

- [ ] **Step 4: Write the runner protocol + the fake + the registry**

First the `ImportRunner` protocol (real impl in Task 6; the registry depends only on the protocol). Create `backend/app/import_jobs/runner.py`:

```python
"""The import-runner seam.

``ImportRunner`` is the injectable boundary between the registry (pure
lifecycle) and the actual import engine. Production uses ``BeetsImportRunner``
(verified in Task 6), which builds a real WebImportSession and runs it on a
daemon thread. Tests inject a fake that drives the same ImportBridge with canned
outcomes + a parked album.

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

    ``on_finish`` is called (no args) when the import ends normally (incl. a
    clean abort); ``on_error`` is called with the exception message when the
    worker raises. Implementations MUST be non-blocking (spawn a thread and
    return) so the API start endpoint returns immediately.
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

Then the fake. Create `backend/app/import_jobs/fakes.py`:

```python
"""Test-only ImportRunner that drives the real ImportBridge with canned data.

Models the SEQUENTIAL import: on its own daemon thread it first emits the canned
``applied`` outcomes (auto-applied strong albums), then parks each canned
``ParkedAlbum`` ONE AT A TIME via ``bridge.park`` (which blocks until a choice
arrives, exactly like the real worker's choose_match), emitting a needs_review
outcome before each park, then calls ``on_finish``. With ``fail_with`` set it
reports an error immediately. Shared by the registry + API tests. No beets, no
network.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.beets.import_session import ImportBridge
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    ParkedAlbum,
    Recommendation,
)


class FakeImportRunner:
    """A canned ImportRunner for hermetic lifecycle/API tests."""

    def __init__(
        self,
        parked: list[ParkedAlbum] | None = None,
        applied: list[AlbumOutcome] | None = None,
        fail_with: str | None = None,
    ) -> None:
        self._parked = parked or []
        self._applied = applied or []
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
                # Strong albums auto-apply first (no parking) — emit their feed
                # outcomes, mirroring the real worker's note_outcome on the
                # strong path.
                for outcome in self._applied:
                    bridge.note_outcome(outcome)
                # Then each uncertain album, ONE AT A TIME: emit needs_review,
                # then park() which BLOCKS until the consumer pushes a choice.
                for album in self._parked:
                    bridge.note_outcome(
                        AlbumOutcome(
                            album_index=album.album_index,
                            folder=album.folder,
                            artist=album.candidate.album_after.artist,
                            album=album.candidate.album_after.album,
                            recommendation=album.candidate.recommendation,
                            confidence=album.candidate.confidence,
                            status=AlbumOutcomeStatus.needs_review,
                        )
                    )
                    bridge.park(album)
            # Broad by design: mirror the real worker's guard so a canned-data
            # bug surfaces as a failed job rather than a silent dead thread.
            except Exception as exc:
                on_error(str(exc) or exc.__class__.__name__)
                return
            on_finish()

        # Reference Recommendation so the import is used even if a future canned
        # outcome needs it; keeps the shared helper module honest. (No-op.)
        _ = Recommendation
        threading.Thread(target=target, name="fake-import", daemon=True).start()
```

> If ruff flags the `_ = Recommendation` line as pointless, drop both it and the `Recommendation` import — it is only there in case a canned outcome is built inline; the tests pass `AlbumOutcome`s ready-made, so the import may be unnecessary. Keep imports minimal: include `Recommendation` only if the final fake body references it.

Then the registry. Create `backend/app/import_jobs/registry.py`:

```python
"""The in-memory, single-slot import-job registry (sequential review).

Holds at most one active ImportJob. Starting a second import while one is active
raises RuntimeError (the API maps it to 409). beets imports one album at a time,
so the registry drains chunk-1's ImportBridge — every per-album outcome
(non-blocking ``drain_outcomes``) plus the at-most-one parked album
(``get_parked(timeout=0)``) — into a live feed, and delivers the user's choice
for the parked album to the worker. Thread-safe: the worker thread mutates
phase/summary via callbacks while API threads read state and push the choice.
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
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    ImportAction,
    ImportChoice,
    ParkedAlbum,
)

# Phases in which a job still owns the single import slot.
_ACTIVE_PHASES = {ImportPhase.scanning, ImportPhase.reviewing, ImportPhase.applying}
# Decisions that count as "imported" in the truthful summary.
_APPLY_ACTIONS = {ImportAction.apply, ImportAction.asis, ImportAction.astracks}
# How a worker outcome maps to an initial feed-row status.
_OUTCOME_STATUS = {
    AlbumOutcomeStatus.applied: ImportAlbumStatus.applied,
    AlbumOutcomeStatus.skipped: ImportAlbumStatus.skipped,
    AlbumOutcomeStatus.needs_review: ImportAlbumStatus.needs_review,
}


@dataclass
class _FeedAlbum:
    """One album in the live feed: its outcome, status, optional parked payload."""

    outcome: AlbumOutcome
    status: ImportAlbumStatus
    parked: ParkedAlbum | None = None
    # The action the user chose for a parked album (None until decided).
    decided_action: ImportAction | None = None


@dataclass
class ImportJob:
    """One import's full in-memory state."""

    id: str
    bridge: ImportBridge
    phase: ImportPhase = ImportPhase.scanning
    albums: dict[int, _FeedAlbum] = field(default_factory=dict)
    summary: str | None = None
    error: str | None = None


class ImportJobRegistry:
    """Single-slot registry of the active (or last) import job."""

    def __init__(self, runner: ImportRunner | None = None) -> None:
        # Default to the real beets runner; tests pass a FakeImportRunner. The
        # real runner needs the Library, set via attach_library() at lifespan.
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
            if (
                self._job is not None
                and self._job.id == job_id
                and self._job.phase != ImportPhase.failed
            ):
                self._drain_locked(self._job)
                self._job.phase = ImportPhase.done
                self._job.summary = self._summarize(self._job)

    def _on_error(self, job_id: str, message: str) -> None:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                self._job.phase = ImportPhase.failed
                self._job.error = message

    @staticmethod
    def _summarize(job: ImportJob) -> str:
        imported = 0
        skipped = 0
        for album in job.albums.values():
            if album.status is ImportAlbumStatus.applied:
                imported += 1
            elif album.status is ImportAlbumStatus.skipped:
                skipped += 1
            elif album.status is ImportAlbumStatus.decided:
                if album.decided_action in _APPLY_ACTIONS:
                    imported += 1
                else:  # skip (abort never reaches 'decided' here)
                    skipped += 1
        return f"{imported} imported, {skipped} skipped"

    # ----- access -----

    def get(self, job_id: str) -> ImportJob | None:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                return self._job
            return None

    def _drain_locked(self, job: ImportJob) -> None:
        """Pull every new outcome + the (at-most-one) parked album into the feed.

        Caller holds ``self._lock``. Both bridge calls are non-blocking.
        """
        for outcome in job.bridge.drain_outcomes():
            if outcome.album_index not in job.albums:
                job.albums[outcome.album_index] = _FeedAlbum(
                    outcome=outcome, status=_OUTCOME_STATUS[outcome.status]
                )
        while True:
            parked = job.bridge.get_parked(timeout=0)
            if parked is None:
                break
            row = job.albums.get(parked.album_index)
            if row is not None:
                row.parked = parked
            # (The needs_review outcome is emitted before park, so the row
            # already exists; if ordering ever changed, we'd create it here.)

    def drain(self, job_id: str) -> list[ImportAlbumSummary]:
        """Drain the bridge and return the current feed rows (non-blocking)."""
        job = self._require(job_id)
        with self._lock:
            self._drain_locked(job)
            if job.phase == ImportPhase.scanning and any(
                a.status is ImportAlbumStatus.needs_review for a in job.albums.values()
            ):
                job.phase = ImportPhase.reviewing
            return self._summaries(job)

    def candidate(self, job_id: str, index: int) -> Candidate:
        """Return the full Candidate for the parked album at ``index``."""
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            row = job.albums.get(index)
            if row is None or row.parked is None:
                raise KeyError(index)
            return row.parked.candidate

    def record_choice(self, job_id: str, index: int, choice: ImportChoice) -> None:
        """Deliver a decision for the parked album and mark its row decided.

        Propagates the bridge's KeyError (unknown/unparked index) and
        RuntimeError (duplicate choice) to the caller; the API maps them to
        404/409.
        """
        self.drain(job_id)
        job = self._require(job_id)
        job.bridge.push_choice(index, choice)  # KeyError / RuntimeError bubble up
        with self._lock:
            row = job.albums.get(index)
            if row is not None:
                row.status = ImportAlbumStatus.decided
                row.decided_action = choice.action

    def state(self, job_id: str) -> ImportJobState:
        """Drain, then return the full job state for the GET endpoint."""
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            applied = sum(
                1
                for a in job.albums.values()
                if a.status in (ImportAlbumStatus.applied, ImportAlbumStatus.decided)
            )
            needs_review = sum(
                1 for a in job.albums.values() if a.status is ImportAlbumStatus.needs_review
            )
            return ImportJobState(
                job_id=job.id,
                phase=job.phase,
                progress=ImportProgress(applied=applied, needs_review=needs_review),
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
            row = job.albums[index]
            o = row.outcome
            rows.append(
                ImportAlbumSummary(
                    index=index,
                    folder=o.folder,
                    artist=o.artist,
                    album=o.album,
                    recommendation=o.recommendation,
                    confidence=o.confidence,
                    status=row.status,
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

The registry above is the fully-typed final form: every model symbol is imported on the single top-level `from app.models.import_models import (...)` line, `candidate` is annotated `-> Candidate`, and there are no `# type: ignore` comments. It passes `mypy --strict` because it touches no untyped beets surface (the beets contact is isolated in `app/import_jobs/runner.py`).

- [ ] **Step 5: Add the registry-reset autouse fixture**

In `backend/tests/conftest.py`, append:

```python
from collections.abc import Iterator


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
```

- [ ] **Step 6: Add `app.import_jobs.runner` to the mypy override**

In `backend/pyproject.toml`, extend the `disallow_untyped_calls = false` module list (it constructs `WebImportSession`):

```toml
module = ["app.beets.library", "app.beets.import_mapping", "app.beets.import_session", "app.import_jobs.runner", "tests.test_albums", "tests.test_artists", "tests.test_search", "tests.test_import_mapping", "tests.test_import_session"]
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv --directory backend run pytest tests/test_import_registry.py -v`
Expected: PASS (8 tests). If a flow assertion is flaky from thread timing, raise `attempts` in `_poll` (each is 10ms; 200 ≈ 2s) — do NOT add real sleeps to production code.

- [ ] **Step 8: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. Registry/fakes are pure-typed (stay out of the override); `app.import_jobs.runner` is now in it.

- [ ] **Step 9: Commit**

```bash
git add backend/app/import_jobs/__init__.py backend/app/import_jobs/registry.py backend/app/import_jobs/runner.py backend/app/import_jobs/fakes.py backend/tests/conftest.py backend/tests/test_import_registry.py backend/pyproject.toml
git commit -m "feat(import): add single-slot ImportJob registry + injectable runner seam"
```

---

## Task 4: The import API router

**Files:**
- Create: `backend/app/api/import_.py`
- Modify: `backend/app/main.py` (mount the router + attach the library)
- Test: `backend/tests/test_import_api.py` (append two wiring tests here; the full flow is Task 5)

- [ ] **Step 1: Write the failing wiring tests (append to `tests/test_import_api.py`)**

```python
from fastapi.testclient import TestClient

from app.main import app


def test_start_import_blank_path_is_422() -> None:
    # An all-whitespace path fails validation (strip + min_length=1) -> 422.
    resp = TestClient(app).post("/api/import", json={"path": "   "})
    assert resp.status_code in (400, 422)


def test_get_unknown_job_is_404() -> None:
    resp = TestClient(app).get("/api/import/does-not-exist")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv --directory backend run pytest tests/test_import_api.py::test_get_unknown_job_is_404 -v`
Expected: FAIL — the route is missing (router not mounted). Passes once Task 4 mounts it.

- [ ] **Step 3: Write the router**

Create `backend/app/api/import_.py`:

```python
"""The import API router (chunk 2, sequential review).

Thin endpoints over the in-memory ImportJobRegistry. Start an import, poll its
phase + live feed, fetch the parked album's full Candidate, and push its choice.
beets imports one album at a time, so there is no batch apply-ready route. The
registry owns all lifecycle + threading; this layer validates input and maps
registry/bridge exceptions to HTTP codes.

No beets imports: the registry + models are the whole surface here.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import StringConstraints

from app.import_jobs.registry import registry
from app.models.import_api import (
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


@router.post(
    "/import", response_model=StartImportResponse, status_code=status.HTTP_202_ACCEPTED
)
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
        # Either the job is unknown or no album is parked at this index.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None


@router.post(
    "/import/{job_id}/albums/{index}/choice", status_code=status.HTTP_204_NO_CONTENT
)
async def post_import_choice(
    job_id: str, index: Annotated[int, Path(ge=0)], choice: ImportChoice
) -> None:
    try:
        registry.record_choice(job_id, index, choice)
    except KeyError:
        # No job, or no album parked at this index (incl. a second choice after
        # the worker advanced — park popped the slot).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None
    except RuntimeError:
        # A choice was already pushed for this album, racing the same slot.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A choice was already submitted"
        ) from None
```

- [ ] **Step 4: Mount the router + attach the library in the lifespan**

In `backend/app/main.py`, add the import next to the other routers:

```python
from app.api.import_ import router as import_router
```

Add the mount after `app.include_router(search_router, prefix="/api")`:

```python
app.include_router(import_router, prefix="/api")
```

Inside `lifespan`, after `app.state.beets_library = lib`, attach the library to the registry so the production runner can build sessions:

```python
    from app.import_jobs.registry import registry as import_registry

    import_registry.attach_library(lib)
```

(The `reset_import_registry` fixture swaps the global instance with a fake-runner registry before each test, so `attach_library` only matters for the running server and the Task-6 real-runner test.)

- [ ] **Step 5: Run the wiring tests to verify they pass**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: PASS (the Task-2 model tests + the two wiring tests).

- [ ] **Step 6: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. The router imports no beets; it stays out of the override.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/import_.py backend/app/main.py backend/tests/test_import_api.py
git commit -m "feat(import): add import API router + mount + library wiring"
```

---

## Task 5: Full sequential API flow tests (start → feed → choice → done) with the fake runner

**Files:**
- Test: `backend/tests/test_import_api.py` (append the end-to-end HTTP tests)

These inject a `FakeImportRunner` into the global registry (via `reset_registry`) so the **real** `ImportBridge` is driven with canned outcomes + a parked album — no beets, no network. They exercise every endpoint over `TestClient` in the **sequential** shape.

- [ ] **Step 1: Write the failing tests (append to `tests/test_import_api.py`)**

```python
import time

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
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


def _api_applied(index: int) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=index,
        folder=f"/music/incoming/album{index}",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )


def _client_with_fake(
    parked: list[ParkedAlbum] | None = None,
    applied: list[AlbumOutcome] | None = None,
    fail_with: str | None = None,
) -> TestClient:
    reset_registry(runner=FakeImportRunner(parked=parked, applied=applied, fail_with=fail_with))
    return TestClient(app)


def _poll(client: TestClient, job_id: str, predicate, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll: predicate is an inline lambda
    for _ in range(attempts):
        state = client.get(f"/api/import/{job_id}").json()
        if predicate(state):
            return state
        time.sleep(0.01)
    return client.get(f"/api/import/{job_id}").json()


def test_start_returns_202_and_job_id() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    resp = client.post("/api/import", json={"path": "/music/incoming"})
    assert resp.status_code == 202
    assert resp.json()["job_id"]


def test_second_concurrent_import_is_409() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    first = client.post("/api/import", json={"path": "/music/incoming"})
    assert first.status_code == 202
    second = client.post("/api/import", json={"path": "/music/other"})
    assert second.status_code == 409


def test_feed_shows_applied_then_the_current_needs_review() -> None:
    # The sequential live feed: one auto-applied album streams past, then the one
    # uncertain album is parked for review.
    client = _client_with_fake(
        applied=[_api_applied(0)], parked=[_api_parked(1, Recommendation.medium)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: len(s["albums"]) == 2)
    assert state["phase"] == "reviewing"
    by_index = {a["index"]: a for a in state["albums"]}
    assert by_index[0]["status"] == "applied"
    assert by_index[1]["status"] == "needs_review"
    assert by_index[1]["album"] == "OK Computer"
    assert state["progress"] == {"applied": 1, "needs_review": 1}


def test_get_album_returns_full_candidate() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
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
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    assert client.get(f"/api/import/{job_id}/albums/99").status_code == 404


def test_choice_apply_drives_job_to_done_with_truthful_summary() -> None:
    client = _client_with_fake(
        applied=[_api_applied(0)], parked=[_api_parked(1, Recommendation.medium)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 2)

    resp = client.post(
        f"/api/import/{job_id}/albums/1/choice", json={"action": "apply", "candidate_index": None}
    )
    assert resp.status_code == 204

    state = _poll(client, job_id, lambda s: s["phase"] == "done")
    assert state["phase"] == "done"
    by_index = {a["index"]: a for a in state["albums"]}
    assert by_index[0]["status"] == "applied"
    assert by_index[1]["status"] == "decided"
    # Truthful: the auto-applied strong album is counted too -> 2 imported.
    assert "2 imported" in state["summary"]
    assert "0 skipped" in state["summary"]


def test_choice_unknown_index_is_404() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    resp = client.post(f"/api/import/{job_id}/albums/99/choice", json={"action": "skip"})
    assert resp.status_code == 404


def test_second_choice_after_advance_is_404() -> None:
    # Realistic over-HTTP outcome of a second choice: once the worker consumes
    # the first reply, ImportBridge.park POPS the reply slot, so a second
    # push_choice for that index raises KeyError -> 404. (A true 409 needs two
    # pushes RACING the same still-unconsumed slot — not reachable through
    # sequential HTTP; the 409 mapping is unit-tested in the registry, see
    # test_record_choice_duplicate_raises_runtimeerror.)
    client = _client_with_fake(
        parked=[_api_parked(0, Recommendation.medium), _api_parked(1, Recommendation.low)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    # Album 0 parks first (sequential); decide it; the worker then advances to 1.
    _poll(client, job_id, lambda s: any(a["index"] == 0 for a in s["albums"]))
    first = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "skip"})
    assert first.status_code == 204
    # Wait for the worker to advance (album 1 appears) so slot 0 is surely gone.
    _poll(client, job_id, lambda s: any(a["index"] == 1 for a in s["albums"]))
    second = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "skip"})
    assert second.status_code == 404


def test_abort_choice_ends_job_cleanly() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    # The fake worker's bridge.park returns the abort choice; the fake's park
    # loop simply moves on (it does not raise), so the job ends 'done'. (In the
    # REAL worker, abort raises ImportAbortError which run() catches -> also a
    # clean 'done' via on_finish.)
    resp = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "abort"})
    assert resp.status_code == 204
    state = _poll(client, job_id, lambda s: s["phase"] in ("done", "failed"))
    assert state["phase"] == "done"


def test_worker_crash_marks_failed_never_500() -> None:
    client = _client_with_fake(fail_with="lookup exploded")
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: s["phase"] == "failed")
    assert state["phase"] == "failed"
    assert state["error"] == "lookup exploded"
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `uv --directory backend run pytest tests/test_import_api.py -v`
Expected: PASS (model tests + wiring tests + the sequential flow tests). If a flow test is flaky from thread timing, raise `attempts` in `_poll` (each is 10ms; 200 ≈ 2s) — do NOT add real sleeps to production code.

- [ ] **Step 3: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors. `tests/test_import_api.py` builds only our Pydantic models + `FakeImportRunner` (no beets `Library`/`AlbumMatch`), so it passes strict mypy without the override. The `_poll` helper carries `# type: ignore[no-untyped-def]` for its inline-lambda arg (a test-local convenience, per repo convention — with a trailing reason on the same line). If mypy unexpectedly flags an untyped call, add `tests.test_import_api` to the override and note why.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_import_api.py
git commit -m "test(import): cover sequential start/feed/choice/abort/crash via fake runner"
```

---

## Task 6: BeetsImportRunner production-wiring test (real Library, no network)

**Files:**
- Modify: `backend/pyproject.toml` (add `tests.test_import_runner` to the override; `app.import_jobs.runner` was added in Task 3)
- Test: `backend/tests/test_import_runner.py`

`BeetsImportRunner` was implemented in Task 3 (the registry needs the protocol + a default). This task proves it builds a real `WebImportSession` from a hermetic beets `Library` + tmp folder and runs it on a daemon thread, **without any MusicBrainz/audio** — by monkeypatching `WebImportSession.run` to a no-op so `run_import_worker` exercises the real construction + thread + callback path. (The full real-audio `run()` end-to-end remains deferred — no sample file is shipped; see Out of scope.)

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
    # beets normalizes the path to bytes; the basename survives. captured["paths"]
    # is typed ``object`` (dict[str, object]), so assert against the repr.
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

    def on_error(message: str) -> None:
        errored["message"] = message
        finished.set()

    runner = BeetsImportRunner(lib)
    runner.run(
        str(tmp_path / "incoming"),
        ImportBridge(),
        on_finish=finished.set,
        on_error=on_error,
    )
    assert finished.wait(timeout=2.0)
    assert errored["message"] == "kaboom"
```

- [ ] **Step 3: Run test to verify it fails (or already passes)**

Run: `uv --directory backend run pytest tests/test_import_runner.py -v`
Expected: It may already PASS if `BeetsImportRunner` from Task 3 is correct — that is fine (this task is verification + the override addition). If it FAILS, the failure pinpoints a wiring bug (e.g. wrong ctor arg order); fix `app/import_jobs/runner.py` to match `WebImportSession(lib, None, [os.fsencode(path)], None, bridge)`.

- [ ] **Step 4: Typecheck + lint**

Run: `uv --directory backend run mypy && uv --directory backend run ruff check`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_import_runner.py backend/pyproject.toml
git commit -m "test(import): verify BeetsImportRunner builds a real session on a daemon thread"
```

---

## Task 7: Full-suite green + boundary check + self-review

**Files:** none (verification only).

- [ ] **Step 1: Run the entire backend test suite**

Run: `uv --directory backend run pytest`
Expected: PASS — all existing tests (incl. the extended `test_import_session.py`) plus the new `test_import_api.py`, `test_import_registry.py`, `test_import_runner.py`. No existing test regresses.

- [ ] **Step 2: Full typecheck**

Run: `uv --directory backend run mypy`
Expected: `Success: no issues found`. Confirm the override list includes `app.import_jobs.runner`, `tests.test_import_runner`.

- [ ] **Step 3: Lint + format check**

Run: `uv --directory backend run ruff check && uv --directory backend run ruff format --check`
Expected: no errors. Run `uv --directory backend run ruff format` and re-commit if formatting differs.

- [ ] **Step 4: Confirm the beets-boundary rule holds (CLAUDE.md rule 3)**

Run: `uv --directory backend run python -c "import subprocess,sys; out=subprocess.run(['grep','-rEn','import beets|from beets|import beetsplug','app','--include=*.py'],capture_output=True,text=True).stdout; bad=[l for l in out.splitlines() if not l.startswith('app/beets/')]; print('\n'.join(bad)); sys.exit(1 if bad else 0)"`
Expected: empty output, exit 0 — every beets import lives under `app/beets/`. `app/import_jobs/runner.py` imports from `app.beets.import_session` (our adapter) + `os` only. The router, registry, models, and fakes import no beets.

- [ ] **Step 5: Confirm the second-start rejection holds end-to-end**

Run: `uv --directory backend run pytest tests/test_import_api.py::test_second_concurrent_import_is_409 -v`
Expected: PASS — the single-slot policy holds through the router.

- [ ] **Step 6: Final commit if anything changed**

```bash
git add -A
git commit -m "chore(import): API + ImportJob lifecycle green — tests, mypy strict, ruff" || echo "nothing to commit"
```

---

## Self-review checklist (run after writing all tasks)

**Sequential-model fidelity (the corrected core):**
- At most ONE album parked at a time; the feed = drained outcomes (applied + skipped) + the one current `needs_review` row. No code path or test parks 2+ albums simultaneously. → Task 1 (outcome emission), Task 3 (`_drain_locked`), Tasks 5 (`test_feed_shows_applied_then_the_current_needs_review`).
- Strong matches auto-apply inside the worker and are visible via the outcome channel (not an endpoint). → Task 1 (`AlbumOutcomeStatus.applied`), Task 3 (feed), Task 5.
- No `apply-ready` endpoint/method and no `ApplyReadyResponse` model anywhere. → confirmed absent in `import_api.py` (Task 2), `registry.py` (Task 3), `import_.py` (Task 4).
- Truthful summary counts auto-applied + decided-apply vs skipped across ALL outcomes. → Task 3 (`_summarize`), Tasks 3/5 summary assertions.

**Spec coverage** — every chunk-2 requirement maps to a task:
- Chunk-1 addition: per-album outcomes on a non-blocking `ImportBridge` channel (`note_outcome`/`drain_outcomes` + `AlbumOutcome`), keeping chunk-1 tests green → Task 1.
- `ImportJob` lifecycle + single-slot registry, one import at a time (409 on second) → Task 3 (`ImportJobRegistry`, `_ACTIVE_PHASES`, `start` raising) + Task 4/5 (router 409).
- Worker integration: real `WebImportSession(lib, None, [os.fsencode(path)], None, bridge)` on a daemon `threading.Thread` via `run_import_worker` → Task 3 (`BeetsImportRunner`) + Task 6 (verified).
- `POST /api/import` `{path, options?}` → `{job_id}` (202), 409 if running, 400/422 blank path → Tasks 2/4/5.
- `GET /api/import/{job}` phase + progress + live feed (folder/album/artist/recommendation/confidence/status) + summary, non-blocking drain → Tasks 2 (`ImportJobState`/`ImportAlbumSummary`/`ImportProgress`), 3 (`drain`/`state`), 5.
- `GET /api/import/{job}/albums/{idx}` full `Candidate` for the parked album → Tasks 3 (`candidate`), 4 (route), 5.
- `POST .../albums/{idx}/choice` → `push_choice`, `KeyError`→404, `RuntimeError`→409 → Tasks 3 (`record_choice` propagation; `test_record_choice_duplicate_raises_runtimeerror` for the 409 race), 4 (route mapping), 5 (`test_second_choice_after_advance_is_404` for the realistic over-HTTP path).
- Plain `async def`; bridge calls non-blocking; no `anyio.to_thread` → Task 4 + Architecture.
- Worker crash → `failed` (never 500); abort → `ImportAbortError` → `run()` returns → `done` → Tasks 3 (`_on_error`/`_on_finish`), 5 (`test_worker_crash_marks_failed_never_500`, `test_abort_choice_ends_job_cleanly`).
- Test-injection seam (substitute the worker with a fake driving the real bridge, sequentially) → Tasks 3 (`ImportRunner` + `FakeImportRunner`), 5 (injected via `reset_registry`).
- mypy strict + ruff green; new beets-touching modules in the override (`app.import_jobs.runner`, `tests.test_import_runner`); chunk-1's `import_session` extension stays in its existing override entry → Tasks 1, 3, 6, 7.

**Placeholder scan:** no "TBD"/"handle edge cases"/"similar to Task N"; every code, test, and command step is complete. The only `# type: ignore` in plan code is the `_poll` test helpers' `[no-untyped-def]` (inline-lambda arg; repo convention, with a trailing reason).

**Type consistency:** model names (`ImportPhase`, `ImportAlbumStatus`, `ImportAlbumSummary`, `ImportProgress`, `ImportJobState`, `StartImportRequest`, `StartImportResponse`, `AlbumOutcome`, `AlbumOutcomeStatus`) and registry/runner symbols (`ImportJobRegistry`, `ImportJob`, `_FeedAlbum`, `ImportRunner`, `BeetsImportRunner`, `FakeImportRunner`, `registry`, `reset_registry`, methods `start`/`get`/`drain`/`candidate`/`record_choice`/`state`/`has_active_job`/`attach_library`) are used identically across Tasks 1–7. Chunk-1 symbols consumed (`ImportBridge`, `WebImportSession`, `run_import_worker`, `get_parked`/`push_choice`/`note_outcome`/`drain_outcomes`, `ParkedAlbum`, `Candidate`, `ImportChoice`, `ImportAction`, `Recommendation`) match the verified/extended signatures.

## Out of scope for chunk 2 (do NOT build here)
- **A browsable multi-album review queue / batch "apply ready" (review-any-order)** — beets imports serially and blocks per album, so this would need a **deferred-decision two-pass engine** (defer every decision, collect all candidates, then apply). Not v1. v1 surfaces beets' serial flow as a live feed (one parked album at a time).
- **All frontend** — the FE flow shell, candidate-review screen, done UI (chunks 3–5).
- **Watched-folder / incremental auto-import** (separate spec; our later layer over `import.incremental`).
- **Settings / config UI** (separate spec; v1 reads the user's beets `config.yaml` as-is).
- **SSE / websocket progress** — polling only in v1 (a later optimization).
- **enter-MBID / search-again re-lookup, duplicate-resolution UI, singletons** — chunk-1 `WebImportSession` skips singletons and no-ops duplicate resolution; not surfaced via the API here.
- **Fine-grained per-track import progress** — beets exposes no hook we rely on; the phase state machine + `{applied, needs_review}` counts are the v1 progress signal. (`applying` is reserved in the enum but the worker exposes no signal to set it transiently — `reviewing` covers the in-flight state.)
- **A full real-audio `ImportSession.run()` end-to-end test** — no sample audio is shipped; Task 6 verifies the Library→session→thread→callback wiring with a stubbed `run()`. A real-audio smoke test is deferred to chunk 5 ("live screenshot end-to-end").
- **Persisting jobs across process restarts** — the registry is in-memory by design (YAGNI; a restart cancels an in-flight import, acceptable for v1).
