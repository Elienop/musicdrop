# Library Import (beets) — Design

**Status:** Brainstorm output, 2026-05-25 (visual-companion mockups). Awaiting spec review → implementation plan.

**Goal:** A web UI for beets' **import / autotag** flow — point MusicDrop at a folder, let beets match it against MusicBrainz, review the proposed changes, and commit — so a user can do the core beets operation without the CLI. This is the centerpiece of "a GUI for beets" and the on-ramp that lets MusicDrop fill its own library.

**Guiding principle (the north star):** **Be faithful to beets.** Surface what beets actually *shows* and *decides* — render it more legibly than the terminal, never hide it or invent behavior. Every confidence score, recommendation, and proposed change comes from beets itself. Where we add UX beyond beets (a legible live feed, a watched folder), we do it by driving beets' *own* extension points without changing its decisions — and we call out that "beets vs ours" split explicitly.

**Architecture (one line):** beets is **embedded in-process** (pinned dependency); a new `app/beets/import_session.py` subclasses beets' `ImportSession` and drives an import on a **serial background worker**, mapping beets' match/candidate objects to our Pydantic models and bridging beets' blocking decision points to an async HTTP API the React flow consumes.

**Tech:** Python/FastAPI + the beets adapter (beets 2.11.0, pinned) + Pydantic; React/shadcn (the existing artist-spine app). httpx already present.

---

## Context (from the beets research, against the pinned 2.11.0 in the venv)

- **beets is embedded, not a service.** MusicDrop runs beets in-process as a pinned library; there is no separate beets server. The import driver lives in the beets-adapter boundary (`app/beets/`, CLAUDE.md rule 3). Read beets source from the **venv** (`backend/.venv/.../beets/`) — the external `beets-master` reference repo is an older snapshot (2.7.1) and must not be relied on.
- **The importer is built to be driven.** `beets.importer.ImportSession` is a base class with decision hooks — `choose_match(task)`, `choose_item(task)`, `resolve_duplicate(task, found)`, `should_resume(path)`. The terminal prompt is just one subclass (`TerminalImportSession`). **We write another subclass backed by the web UI.** At a decision point the task is fully populated (`task.candidates`, `task.rec`, `task.cur_artist`, `task.cur_album`, `task.items`); we return an `AlbumMatch`/`Action` and beets applies it. **beets imports one album at a time** (verified): single-threaded, `Pipeline.run_sequential` (`util/pipeline.py:505-514`) pulls ONE task and pushes it through every later stage — incl. `choose_match` — *before* reading the next, so a blocking decision halts the whole pipeline. This makes review inherently sequential (one parked album at a time), not a batch.
- **The candidate/quality model** (`beets/autotag/`): `Proposal(candidates, recommendation)`; `Recommendation` = none/low/medium/strong (from `match.*_rec_thresh` config); `AlbumMatch{distance, info: AlbumInfo, mapping: dict[Item,TrackInfo], extra_items, extra_tracks}`; `Distance` (weighted penalties, 0.0 = perfect, `generic_penalty_keys` = which fields differ). beets' CLI `display.py` (current→proposed album diff + per-track diff + missing/unmatched) is the field-level blueprint for the review screen.
- **Hazards for a long-lived process:** `beets.config` and the plugin registry are **process-global singletons** (concurrent imports with different settings clobber each other); the import pipeline is **threaded by default** and `run()` blocks; MB lookups are slow + rate-limited. → run imports **serially**, single-threaded (`config["threaded"]=False`) on a dedicated worker, and bridge the blocking `choose_match` to async via thread-safe queues.
- **Entry is a manual path.** beets' native entry is `beet import <path>` (it walks + groups + matches). beets does **not** watch folders; `import.incremental` records already-imported dirs and skips them on re-run. So path-import is beets-native (v1); a watched folder is our later layer (a trigger + incremental).

---

## The flow (UX — validated via mockups)

`Choose folder → Scan & match → Review uncertain albums (one at a time) → Done`

(beets applies each album inline as it's decided — strong matches auto-apply and stream past; uncertain ones surface one at a time to decide.)

1. **Choose folder.** A server **path** input + Browse (maps 1:1 to `beet import <path>`). Import options (copy vs move, autotag) come from beets config defaults, shown as a one-line reminder; a watched "incoming" folder is **deferred** (our later layer using incremental).
2. **Scan & match.** beets reads files → groups into albums → looks each up on MusicBrainz. Slow + rate-limited, so progress **streams** (read → group → per-album lookup) and albums appear as they resolve.
3. **Review — one album at a time (the hub).** beets imports **serially** (single-threaded: its pipeline blocks on each album's decision before reading the next — verified). So the hub is a **live feed**, not a browsable batch: **strong** matches **auto-apply** and stream past as "imported"; each **uncertain** (medium/low/none) album surfaces **one at a time** for review, and deciding it advances the worker to the next. This mirrors beets' own CLI prompt, rendered legibly. (No simultaneous multi-album queue or batch "apply ready" — that would need deferring every decision, a non-goal for v1.)
4. **Per-album review (the heart).** Faithful candidate-review (see below) for the album currently awaiting a decision. The chosen match is applied **inline** — beets writes tags + copies/moves files into the library per beets config — before the worker reads the next album.
5. **Done.** Summary (N imported, where each landed, what was skipped — counting the auto-applied strong albums too) + **"View in library"** → the artist spine.

### Candidate-review screen (validated design)

Faithful = **shows everything beets shows**, with the GUI making changes pop:
- **Match header:** `<confidence>% · <recommendation>` (e.g. `96% · strong`) + `Artist — Album`, the **source** (MusicBrainz release: year/media/country/label, `data_url` link), and which fields differ (from `Distance.generic_penalty_keys`).
- **Candidate switcher:** the ranked `task.candidates` (each with its own % + disambiguation) — pick a different release; plus "enter MusicBrainz id" / "search again" (re-runs `tag_album`).
- **Before → After (album, art-forward):** two calm panels — **Now (your files)** vs **After import** — led by the **album cover** (current embedded art, often none, → the matched release's cover from the **Cover Art Archive**). Identity fields (artist/album/year/label) shown both sides; changed fields marked. "+ cover art" is itself a change.
- **What-changes summary:** a one-line chip set (e.g. `+ cover art · album artist · label · 1 of 12 track titles`).
- **Full tracklist:** **every** track listed (current→proposed), changed rows highlighted + tagged, unchanged rows calm; **missing** (`extra_tracks`) / **unmatched** (`extra_items`) rows flagged when folder ≠ release. (Bound to `AlbumMatch.mapping` + `extra_*` + per-track `Distance`.)
- **Actions:** Apply · Skip · Use as-is · As tracks · Enter id · Search again (= beets' `choose_match` choices + duplicate resolution Skip/Keep/Remove/Merge when it fires).

### Sequential review — beets vs ours (explicit)

- **Beets (faithful):** the per-album recommendation (strong/medium/low/none) and **auto-apply-strong** behavior are beets', from `match.*_rec_thresh` + `import.timid`/`quiet`. beets imports **one album at a time** — `Pipeline.run_sequential` pulls a task through every stage (incl. `choose_match`) before the next, so a decision halts the pipeline. We surface beets' judgment, not ours.
- **Ours (orchestration):** we drive `ImportSession.choose_match` to **auto-apply strong** (what beets would do) and **park** an uncertain album, blocking until the user decides — exactly mirroring beets' serial prompt. Our value-add is a **legible live feed** — auto-applied albums + the current decision + a truthful summary — over that serial flow. A browsable "review-any-order / batch-apply-ready" queue is **out of scope** (it would need a deferred-decision two-pass engine; not v1).

---

## Backend architecture

**Import driver** (`app/beets/import_session.py`, the only new beets-importing module): a `WebImportSession(ImportSession)` overriding the four decision hooks. Runs single-threaded (`config["threaded"]=False`) inside a dedicated worker. On `choose_match`: if `task.rec` is strong → return the top `AlbumMatch` (mirror beets' auto-apply — applied inline, never parked); else **park** the task (push a serialized candidate to an out-queue, block on a reply event) and return the user's choice when it arrives. Because beets runs serially and `choose_match` blocks, **only one uncertain album is parked at a time** — the next is read only after the current is decided. Maps beets objects → our models at the boundary; never leaks `AlbumMatch`/`Distance` upward.

**Concurrency model:** imports run **serially** (a single import worker; a second start is queued or rejected — global-singleton safety). beets' pipeline processes one album through every stage before reading the next, so the worker parks **at most one** uncertain album at a time and blocks until it's decided. An `ImportJob` holds session state + the bridge (a non-blocking **outcome channel** that records every album the worker processes — auto-applied or needs-review — so the API can render the live feed and a truthful summary, plus the reply channel for the user's choice on the currently-parked album) + progress. The blocking `choose_match` (on the worker thread) and the async API are bridged with thread-safe queues + events.

**API (typed Pydantic; beets-free):**
- `POST /api/import` — start; body `{path, options?}` → `{job_id}` (rejects if an import is already running).
- `GET /api/import/{job}` — job state: phase (scanning/reviewing/applying/done), progress, the **live feed** (albums processed so far: auto-applied + the current parked album — each: folder, album/artist, recommendation, confidence, status), done-summary.
- `GET /api/import/{job}/albums/{idx}` — the **Candidate** payload for one album (match %, recommendation, album before/after, the full track diff, missing/extra, the ranked candidate list, source/`data_url`).
- `POST /api/import/{job}/albums/{idx}/choice` — `{action: apply|skip|asis|astracks|enter_id|search, candidate_id?|mb_id?|query?}`.
- *(Strong matches **auto-apply** inside the worker — beets applies them inline and they never park — so there is no apply endpoint; they appear in the feed already "imported".)*
- Progress transport: polling for v1 (SSE/websocket a later optimization).

**Models** (`app/models/import_*.py`): `ImportJob`, `ImportAlbumSummary` (a feed row), `Candidate` (one match), `AlbumChange` (album field diff), `TrackChange` (per-track current→proposed + status), `MissingTrack`/`UnmatchedItem`, `CandidateOption` (a ranked alternative). All mapped from beets' objects in the adapter.

**Config — MusicDrop follows the user's beets `config.yaml` (no parallel config).** beets config is a layered `confuse` YAML (the user's `config.yaml` over the packaged `config_default.yaml`); MusicDrop points at the *user's* config — the same way it points at their library DB + music dir — so **bringing an existing beets setup just works**: your `directory`, `paths`, `import` options, `match` thresholds, enabled `plugins`, and per-plugin settings all apply as-is. Import behavior here (`import.copy/move/write/autotag` + `match.*_rec_thresh`) is read straight from that config; the request only supplies the path. The future **Settings UI is a friendly editor over that same file** — it reads the merged config (defaults + user) and writes changes back to the *user* `config.yaml` layer, so a change made in MusicDrop is a change `beet` on the CLI also sees (one source of truth, no drift; edit by hand or in the GUI interchangeably). `import.incremental` powers the later watched-folder re-scan. *(Today the library/music paths are located via `MUSICDROP_*` env vars; the faithful end-state reads `directory`/`library` from the user's beets config too — folded into the Settings spec.)*

## Versioning & boundary posture (standing rule)

Pinned beets (`uv.lock` = 2.11.0; upgrades are deliberate `uv lock --upgrade`). The adapter is the **anti-corruption layer** — all beets/import code in `app/beets/`, mapped to our stable models; the API/UI never see beets internals, so a beets bump is a contained, **test-guarded** change in one module. Use beets' **intended** extension point (`ImportSession` subclass) rather than deep internals. Read beets source from the venv.

## Error handling

MB lookup failure/timeout → that album surfaces as "lookup failed · retry/skip" (others proceed). Bad/empty path → validation error. Second concurrent import → rejected/queued. beets exceptions caught at the adapter (never 500 the API). Abort → raise `ImportAbortError` from a hook. Resume/statefile: incremental on; expose nothing fancy in v1.

## Testing

- **Driver** (hermetic beets `Library` in tmp; mock the metadata-source `candidates()` so no network): a strong match auto-applies; an uncertain match parks + the returned choice applies; the mapping beets `AlbumMatch`→`Candidate` is correct (album diff, track changes, missing/extra, distance→%); abort/skip paths; the bridge (outcome recorded per album, parked candidate out, choice in).
- **API** (TestClient, stubbed driver): start/state/candidate/choice; concurrent-import rejection; bad path; done summary (truthful — counts auto-applied + decided).
- **FE** (vitest + msw, mocked API): the flow screens (entry/scan/live-feed/done), the candidate-review (before/after + full tracklist + changes), choice actions, states.
- mypy strict, ruff, generated FE types.

## Build chunks (decomposition — each its own reviewed step)

1. **Import driver core** — `WebImportSession` + the worker/queue bridge + the `Candidate`/diff models + the beets→model mapping; hermetic-tested, no API/UI. *(Hardest + most architecture-defining — validates the whole approach first.)*
2. **Import API** — the endpoints above + the `ImportJob` lifecycle, backend-tested.
3. **FE flow shell** — entry (path) + scan/progress + the live feed (auto-applied albums + the current one awaiting review).
4. **FE candidate-review** — the before/after + full-tracklist review screen + the choice actions, for the currently-parked album.
5. **Done** — the done summary (truthful: auto-applied + decided), "view in library" wiring; live screenshot end-to-end.

## Out of scope (separate specs / later)

- **Watched-folder auto-import** (our layer via a trigger + `import.incremental`).
- **Settings / config UI** — a friendly **editor over the user's beets `config.yaml`** (import options, `match` thresholds, plugins). For now those are read from the user's config as-is; the Settings spec adds the editing UI (reads merged config, writes the user layer).
- The **other library actions** (modify, move/update/write, fetchart/embedart, mbsync, duplicates, stats) — each its own later spec.
- A **browsable multi-album review queue / batch "apply ready"** (review-any-order across albums) — beets imports serially and blocks per album, so this would need a deferred-decision two-pass engine; not v1. v1 surfaces beets' serial flow as a live feed.
- Singletons (`-s`) / advanced import modes, multi-user, the AURA-aligned read API refactor.
