# Import Chunk 5 — Done-Summary Polish, Skipped Counter & Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the beets import flow — surface a `skipped` progress counter end-to-end, polish the done screen into a legible outcome summary, harden the job-not-found UX (stop polling a 404), calm the screen-reader announcements, and verify the whole flow against real audio files.

**Architecture:** One small backend contract change (`ImportProgress.skipped`, derived in the registry exactly like the done-summary's skipped count) regenerates the frontend types; the rest is frontend polish on `ImportPage.tsx` + two new hook behaviors in `useImport.ts`. Everything stays on `feat/import` (one PR for the whole import feature). No new beets surface — the adapter boundary is untouched.

**Tech Stack:** Python 3.11 / FastAPI / Pydantic (backend); React 19 / TanStack Query v5 / react-router v7 / openapi-fetch / Tailwind v4 / shadcn (frontend); pytest + mypy --strict + ruff; vitest + msw + Testing Library.

**Execution order & dependencies:** T1 (backend contract) → T2 (regen types + live cue) → T3 (done polish) → T4 (job-not-found) → T5 (sr announcer/throttle) → T6 (full green + real-files walkthrough). T2 depends on T1's regenerated type. T5's announcer message references `ImportJobNotFoundError`, so T4 lands first. T4 and T5 both edit `ImportRun`; doing T4 first means T5's refactor wraps an `ImportRun` that already has the not-found branch + the `error` destructure.

**Branch:** `feat/import` (already checked out; chunks 1–4 are committed here). Keep stacking — do NOT branch or open a PR; the user merges one import PR at the end.

**Standing constraints (do not violate):**
- ALL beets access stays in `app/beets/` — this chunk does not touch it.
- `mypy --strict` + `ruff check` + `ruff format` must pass with zero errors. No bare `# type: ignore` / `# noqa` without a trailing reason; ruff's select set is `E,F,I,UP,B,ASYNC,RUF` (BLE is NOT enabled — never write `# noqa: BLE001`, use a plain `except Exception:`).
- Frontend TS types are GENERATED from OpenAPI (`schema.d.ts`) — never hand-edit them.
- Conventional commits, no `Co-Authored-By` lines.
- `uv` lives at `~/.local/bin`; prefix backend commands with `export PATH="$HOME/.local/bin:$PATH"`.
- Dev commands run from `backend/` (`uv run pytest`, `uv run mypy`, `uv run ruff check`, `uv run ruff format`) and `frontend/` (`npm run test`, `npm run typecheck`, `npm run build`, `npm run gen:api`).
- The real-files walkthrough (T6) MUST run against a throwaway sandbox library — NEVER the user's real beets library/DB.

---

## File Structure

**Backend (the only contract change):**
- `backend/app/models/import_api.py` — add `skipped: int` to `ImportProgress`.
- `backend/app/import_jobs/registry.py` — add a `_is_skipped(row)` static helper (DRY with the existing `_summarize` inline), use it in `_summarize` AND `state()` to populate `progress.skipped`.
- `backend/tests/test_import_registry.py` — assert `progress.skipped`.
- `backend/tests/test_import_api.py` — update the two `progress` serialization assertions + the direct `ImportProgress(...)` constructor.

**Frontend:**
- `frontend/src/api/schema.d.ts` — REGENERATED (T2); never hand-edited.
- `frontend/src/api/useImport.ts` — export `ImportProgress` type; add `ImportJobNotFoundError`; 404 branch in `fetchJob`; stop-polling-on-not-found in `useImportJob`.
- `frontend/src/lib/useThrottledValue.ts` — NEW reusable leading-edge throttle hook.
- `frontend/src/lib/useThrottledValue.test.ts` — NEW hook unit test.
- `frontend/src/pages/import/ImportPage.tsx` — live cue `skipped` segment; polished `JobDone`; `JobNotFound` notice; one continuous throttled sr-only announcer.
- `frontend/src/pages/import/ImportPage.test.tsx` — fixtures gain `skipped`; new tests for the cue, done polish, not-found, announcer.
- `frontend/src/api/useImport.test.tsx` — fixtures gain `skipped`; new not-found hook test.

---

## Task 1: Backend — `skipped` progress counter (contract change)

**Files:**
- Modify: `backend/app/models/import_api.py` (ImportProgress, ~line 75)
- Modify: `backend/app/import_jobs/registry.py` (`_summarize` ~145, `state` ~239)
- Test: `backend/tests/test_import_registry.py`, `backend/tests/test_import_api.py`

- [ ] **Step 1: Write the failing registry test**

Add to `backend/tests/test_import_registry.py` (a sibling of the existing `test_summary_counts_skipped_truthfully`):

```python
def test_progress_skipped_mirrors_summary_skipped() -> None:
    # progress.skipped is the LIVE mirror of the done-summary's skipped count:
    # a parked album the user resolved with skip counts toward both, and never
    # toward progress.applied.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.progress.skipped == 1
    assert state.progress.applied == 0
    assert state.summary is not None
    assert "1 skipped" in state.summary
```

Also extend the existing active-feed test so the zero case is pinned. In `test_drain_builds_feed_with_applied_then_the_current_parked`, after the existing `progress.needs_review == 1` assertion, add:

```python
    assert registry.state(job_id).progress.skipped == 0
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd backend && export PATH="$HOME/.local/bin:$PATH" && uv run pytest tests/test_import_registry.py::test_progress_skipped_mirrors_summary_skipped -v
```
Expected: FAIL — `ImportProgress` has no `skipped` (TypeError / AttributeError on `state.progress.skipped`).

- [ ] **Step 3: Add `skipped` to the contract**

In `backend/app/models/import_api.py`, extend `ImportProgress`:

```python
class ImportProgress(BaseModel):
    """Coarse progress counters derived from the drained outcomes."""

    applied: int
    needs_review: int
    # Albums that landed nothing: an auto-skip (no candidates) or a parked album
    # the user resolved with a non-apply action. The live mirror of the done
    # summary's skipped count (registry._is_skipped backs both).
    skipped: int
```

- [ ] **Step 4: Derive `skipped` in the registry (DRY)**

In `backend/app/import_jobs/registry.py`, add a static helper right after `_is_imported`:

```python
    @staticmethod
    def _is_skipped(row: _FeedAlbum) -> bool:
        """True if the album landed nothing — an auto-skip (no candidates) or a
        parked album the user resolved with a non-apply action (skip/abort).
        The terminal complement of _is_imported; backs both progress.skipped and
        the done summary so the two can never drift."""
        return row.status is ImportAlbumStatus.skipped or (
            row.status is ImportAlbumStatus.decided
            and row.decided_action not in _APPLY_ACTIONS
        )
```

Replace the inline `skipped = sum(...)` in `_summarize` with the helper:

```python
    @staticmethod
    def _summarize(job: ImportJob) -> str:
        imported = sum(1 for a in job.albums.values() if ImportJobRegistry._is_imported(a))
        skipped = sum(1 for a in job.albums.values() if ImportJobRegistry._is_skipped(a))
        return f"{imported} imported, {skipped} skipped"
```

Populate `progress.skipped` in `state()` (the `with self._lock:` block):

```python
        with self._lock:
            applied = sum(1 for a in job.albums.values() if self._is_imported(a))
            needs_review = sum(
                1 for a in job.albums.values() if a.status is ImportAlbumStatus.needs_review
            )
            skipped = sum(1 for a in job.albums.values() if self._is_skipped(a))
            return ImportJobState(
                job_id=job.id,
                phase=job.phase,
                progress=ImportProgress(
                    applied=applied, needs_review=needs_review, skipped=skipped
                ),
                albums=self._summaries(job),
                summary=job.summary,
                error=job.error,
            )
```

- [ ] **Step 5: Update the API contract tests for the new required field**

In `backend/tests/test_import_api.py`:

`test_job_state_round_trips` — the direct constructor (~line 61) gains `skipped`:
```python
        progress=ImportProgress(applied=1, needs_review=1, skipped=0),
```
and its serialization assertion (~line 87):
```python
    assert dumped["progress"] == {"applied": 1, "needs_review": 1, "skipped": 0}
```

`test_feed_shows_applied_then_the_current_needs_review` — the progress assertion (~line 197):
```python
    assert state["progress"] == {"applied": 1, "needs_review": 1, "skipped": 0}
```

- [ ] **Step 6: Run the backend gates**

```bash
cd backend && export PATH="$HOME/.local/bin:$PATH" && uv run pytest tests/test_import_registry.py tests/test_import_api.py -q && uv run mypy && uv run ruff check && uv run ruff format --check
```
Expected: all PASS, zero mypy/ruff errors.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/import_api.py backend/app/import_jobs/registry.py backend/tests/test_import_registry.py backend/tests/test_import_api.py
git commit -m "feat(import): add skipped to ImportProgress, derived like the summary"
```

---

## Task 2: Frontend — regenerate types & surface `skipped` in the live cue

**Files:**
- Regenerate: `frontend/src/api/schema.d.ts` (+ transient `frontend/openapi.json`)
- Modify: `frontend/src/api/useImport.ts` (export the `ImportProgress` type)
- Modify: `frontend/src/api/useImport.test.tsx` (fixtures), `frontend/src/pages/import/ImportPage.test.tsx` (fixtures + new cue test)
- Modify: `frontend/src/pages/import/ImportPage.tsx` (`LiveFeed` cue)

- [ ] **Step 1: Regenerate the OpenAPI types**

```bash
cd backend && export PATH="$HOME/.local/bin:$PATH" && uv run python -c "import json; from app.main import app; from pathlib import Path; Path('../frontend/openapi.json').write_text(json.dumps(app.openapi()))"
cd ../frontend && npm run gen:api
```

- [ ] **Step 2: Run tsc to see the contract change ripple**

```bash
cd frontend && npm run typecheck
```
Expected: FAIL — `progress: { applied, needs_review }` object literals in `useImport.test.tsx` (2) and `ImportPage.test.tsx` (3) are now missing the required `skipped`. This is the typed contract catching its consumers, exactly as intended.

- [ ] **Step 3: Fix the test fixtures (add `skipped`)**

`frontend/src/api/useImport.test.tsx`:
- line ~71: `progress: { applied: 1, needs_review: 1, skipped: 0 },`
- line ~102: `makeJob({ phase: "scanning", progress: { applied: 0, needs_review: 0, skipped: 0 } }),`

`frontend/src/pages/import/ImportPage.test.tsx`:
- line ~18 (`makeJob` default): `progress: { applied: 1, needs_review: 1, skipped: 0 },`
- line ~160 (scanning override): `progress: { applied: 0, needs_review: 0, skipped: 0 },`
- line ~180 (done override): `progress: { applied: 2, needs_review: 0, skipped: 0 },`

- [ ] **Step 4: Export the `ImportProgress` type (consumed by the cue + announcer)**

In `frontend/src/api/useImport.ts`, next to the other generated re-exports:

```typescript
/** Coarse progress counters from `GET /api/import/{job}` (generated contract). */
export type ImportProgress = components["schemas"]["ImportProgress"];
```

- [ ] **Step 5: Write the failing cue test**

Add to the `describe("ImportPage — live feed", ...)` block in `ImportPage.test.tsx`:

```tsx
  test("the live cue surfaces a skipped count when any album was skipped", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "reviewing",
            progress: { applied: 1, needs_review: 1, skipped: 1 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // The visible cue counts imported + skipped + the one awaiting review.
    expect(await screen.findByText(/1 skipped/)).toBeInTheDocument();
  });
```

- [ ] **Step 6: Run it to verify it fails**

```bash
cd frontend && npm run test -- ImportPage
```
Expected: FAIL — the cue has no skipped segment yet.

- [ ] **Step 7: Add the `skipped` segment to the cue**

In `ImportPage.tsx`, in `LiveFeed`'s count `<span>`, insert the skipped segment between the imported count and the needs-review segment:

```tsx
          <span>
            {state.progress.applied}{" "}
            {state.progress.applied === 1 ? "album" : "albums"} imported
            {state.progress.skipped > 0 && ` · ${state.progress.skipped} skipped`}
            {state.progress.needs_review > 0 &&
              ` · ${state.progress.needs_review} album${state.progress.needs_review === 1 ? "" : "s"} needs review`}
          </span>
```

- [ ] **Step 8: Run frontend gates**

```bash
cd frontend && npm run test -- ImportPage useImport && npm run typecheck
```
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/api/schema.d.ts frontend/src/api/useImport.ts frontend/src/api/useImport.test.tsx frontend/src/pages/import/ImportPage.tsx frontend/src/pages/import/ImportPage.test.tsx
git commit -m "feat(import): regenerate types and surface skipped in the live cue"
```
(Do NOT commit `frontend/openapi.json` — it's a transient dump and is git-ignored; verify with `git status` that it is not staged.)

---

## Task 3: Frontend — done-summary polish

The done screen currently dumps the raw backend summary string. Polish it into a legible outcome derived from `progress` (imported + skipped, counting the auto-applied albums), keeping the per-album feed list ("where each landed") and the View-in-library link — matching the spec's done step.

**Files:**
- Modify: `frontend/src/pages/import/ImportPage.tsx` (`JobDone` + its call site)
- Modify: `frontend/src/pages/import/ImportPage.test.tsx` (the done test)

- [ ] **Step 1: Update the done test to the polished shape**

Replace the existing `test("done shows the summary + a View-in-library link", ...)` with:

```tsx
  test("done shows the imported/skipped outcome + a View-in-library link", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            summary: "2 imported, 0 skipped",
            progress: { applied: 2, needs_review: 0, skipped: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    // The outcome is derived from progress (structured), counting auto-applied
    // strong albums — not the raw summary string.
    expect(screen.getByText(/2 albums imported/i)).toBeInTheDocument();
    expect(screen.getByText(/0 skipped/i)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /view in library/i });
    expect(link).toHaveAttribute("href", "/");
  });
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd frontend && npm run test -- ImportPage
```
Expected: FAIL — `JobDone` renders the raw `summary` string ("2 imported, 0 skipped"), not "2 albums imported" / "0 skipped".

- [ ] **Step 3: Polish `JobDone`**

In `ImportPage.tsx`, add `CircleCheck` to the lucide import (a success glyph; if the installed lucide-react doesn't export `CircleCheck`, use its checkmark equivalent — `npm run build` will flag a bad name):

```tsx
import { AlertCircle, CircleCheck, FolderInput, Loader2 } from "lucide-react";
```

Replace the `JobDone` component:

```tsx
/** done: a legible outcome — imported/skipped counts (counting auto-applied
 * albums), where each landed (the feed list), and a way into the library. */
function JobDone({ state, jobId }: { state: ImportJobState; jobId: string }) {
  const { applied, skipped } = state.progress;
  return (
    <div className="flex flex-col gap-4">
      <div className="border-border flex flex-col items-center gap-3 rounded-xl border py-12 text-center">
        <CircleCheck className="text-muted-foreground size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Import finished</p>
          <p className="text-muted-foreground text-sm">
            {applied} {applied === 1 ? "album" : "albums"} imported
            {` · ${skipped} skipped`}
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to="/">View in library</Link>
        </Button>
      </div>
      {state.albums.length > 0 && <FeedList albums={state.albums} jobId={jobId} />}
    </div>
  );
}
```

Update the call site in `ImportRun` (the `data.phase === "done"` branch) to pass the whole state:

```tsx
  if (data.phase === "done") {
    return (
      <ImportShell>
        <JobDone state={data} jobId={jobId} />
      </ImportShell>
    );
  }
```

- [ ] **Step 4: Run frontend gates**

```bash
cd frontend && npm run test -- ImportPage && npm run typecheck && npm run build
```
Expected: all PASS (build confirms the `CircleCheck` import resolves).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/import/ImportPage.tsx frontend/src/pages/import/ImportPage.test.tsx
git commit -m "feat(import): polish the done screen into a structured outcome"
```

---

## Task 4: Frontend — job-not-found UX (stop polling a 404)

A stale/expired `?job=` currently 404s and `useImportJob` keeps re-fetching the 404 every second (the `refetchInterval` treats an errored, `undefined`-data query as still-active). Map the 404 to a dedicated error, stop polling on it (a 404 won't self-heal; a transient 500 still retries), and show a "no longer available" notice distinct from the transient retry.

**Files:**
- Modify: `frontend/src/api/useImport.ts` (`ImportJobNotFoundError`, `fetchJob`, `useImportJob`)
- Modify: `frontend/src/api/useImport.test.tsx` (new not-found hook test)
- Modify: `frontend/src/pages/import/ImportPage.tsx` (`ImportRun` branch + `JobNotFound`)
- Modify: `frontend/src/pages/import/ImportPage.test.tsx` (new not-found page test)

- [ ] **Step 1: Write the failing hook test**

In `useImport.test.tsx`, add to the `describe("useImportJob", ...)` block, and add `ImportJobNotFoundError` to the import from `@/api/useImport` at the top:

```tsx
  test("maps a 404 to ImportJobNotFoundError and stops polling", async () => {
    let calls = 0;
    server.use(
      http.get(JOB_URL, () => {
        calls += 1;
        return HttpResponse.json(
          { detail: "Import job not found" },
          { status: 404 },
        );
      }),
    );

    const { result } = renderHook(() => useImportJob("job-1"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ImportJobNotFoundError);
    // A 404 is terminal — polling must stop (no self-heal), unlike a transient
    // error. Give the poll interval time and assert no further fetches.
    const callsAtError = calls;
    await act(() => new Promise((r) => setTimeout(r, 1500)));
    expect(calls).toBe(callsAtError);
  });
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd frontend && npm run test -- useImport
```
Expected: FAIL — `ImportJobNotFoundError` is not exported; the 404 maps to the generic error and polling continues.

- [ ] **Step 3: Add the error, the 404 branch, and stop-on-not-found**

In `frontend/src/api/useImport.ts`:

Add the error class next to `ImportConflictError`:

```typescript
/** Thrown when the polled job id is unknown or expired (backend 404). Lets the
 * run page show a dedicated "no longer available" notice and STOP polling,
 * rather than hammering the 404 every second. */
export class ImportJobNotFoundError extends Error {
  constructor() {
    super("Import job not found");
    this.name = "ImportJobNotFoundError";
  }
}
```

Branch on the 404 in `fetchJob` (pull `response` out of the call):

```typescript
async function fetchJob(jobId: string): Promise<ImportJobState> {
  const { data, error, response } = await client.GET("/api/import/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  // A 404 means the job is unknown/expired — surface it distinctly so the page
  // can show a not-found notice and the poll can stop.
  if (response.status === 404) {
    throw new ImportJobNotFoundError();
  }
  if (error || !data) {
    throw new Error("Failed to load import job");
  }
  return data;
}
```

Stop polling on not-found in `useImportJob`'s `refetchInterval` (add the guard first):

```typescript
    refetchInterval: (query) => {
      // A not-found job is gone for good — stop polling. Other transient errors
      // keep the loop (they may recover); the page shows a retry meanwhile.
      if (query.state.error instanceof ImportJobNotFoundError) {
        return false;
      }
      const phase = query.state.data?.phase;
      if (phase === undefined || ACTIVE_PHASES.has(phase)) {
        return IMPORT_POLL_MS;
      }
      return false;
    },
```

- [ ] **Step 4: Write the failing page test**

In `ImportPage.test.tsx`, add to `describe("ImportPage — terminal states", ...)`:

```tsx
  test("a 404 (expired job) shows a not-found notice, not the transient retry", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          { detail: "Import job not found" },
          { status: 404 },
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText(/no longer available/i),
    ).toBeInTheDocument();
    // Distinct from the transient error: offers a fresh start, with no Retry.
    expect(
      screen.getByRole("link", { name: /start a new import/i }),
    ).toHaveAttribute("href", "/import");
    expect(
      screen.queryByRole("button", { name: /retry/i }),
    ).not.toBeInTheDocument();
  });
```

- [ ] **Step 5: Run it to verify it fails**

```bash
cd frontend && npm run test -- ImportPage
```
Expected: FAIL — the 404 currently renders the transient "Couldn't load the import" + Retry.

- [ ] **Step 6: Add the not-found branch + the notice**

In `ImportPage.tsx`, import the error and add the `error` destructure + a branch in `ImportRun` BEFORE the generic `isError` branch:

```tsx
import {
  ImportConflictError,
  ImportJobNotFoundError,
  RECOMMENDATION_LABEL,
  useImportJob,
  useStartImport,
} from "@/api/useImport";
```

```tsx
function ImportRun({ jobId }: { jobId: string }) {
  const { data, isPending, isError, error, refetch } = useImportJob(jobId);

  if (isPending) {
    return (
      <ImportShell>
        <FeedSkeleton />
      </ImportShell>
    );
  }

  // A 404 (unknown/expired job) is terminal — a dedicated notice, no retry.
  if (error instanceof ImportJobNotFoundError) {
    return (
      <ImportShell>
        <JobNotFound />
      </ImportShell>
    );
  }

  if (isError) {
    return (
      <ImportShell>
        <JobError onRetry={() => void refetch()} />
      </ImportShell>
    );
  }
  // ... unchanged: failed / done / live-feed branches
}
```

> Note: the `isPending` branch drops its inline `<p className="sr-only" role="status">Loading import…</p>` here — T5 hoists a single continuous announcer that covers loading. (If executing T5 separately, leaving the line for now is harmless; T5 removes it.)

Add the `JobNotFound` component (near `JobError`):

```tsx
/** The polled job id is unknown or expired (404). Distinct from a transient
 * load error: there's nothing to retry, so offer a fresh start instead. */
function JobNotFound() {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">This import is no longer available</p>
        <p className="text-muted-foreground text-sm">
          It may have finished in another session, or the server restarted.
          Start a new import to continue.
        </p>
      </div>
      <Button variant="outline" size="sm" asChild>
        <Link to="/import">Start a new import</Link>
      </Button>
    </div>
  );
}
```

- [ ] **Step 7: Run frontend gates**

```bash
cd frontend && npm run test -- ImportPage useImport && npm run typecheck
```
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/useImport.ts frontend/src/api/useImport.test.tsx frontend/src/pages/import/ImportPage.tsx frontend/src/pages/import/ImportPage.test.tsx
git commit -m "feat(import): stop polling and show a notice when the job is gone (404)"
```

---

## Task 5: Frontend — one continuous, throttled screen-reader announcer

Today the live cue line is the `aria-live` region and updates every 1s poll (screen-reader spam during a fast scan), and it only mounts once data arrives. Replace it with: the visible cue stays visible-only (live-updating for sighted users), plus ONE continuously-mounted sr-only `role="status"` announcer (present from the first loading render through every phase) whose message is throttled. The announcer's phrasing is deliberately verb-first ("Imported 2, skipped 0") so it never substring-collides with the visible cue's text in tests — it is the single spoken source.

**Files:**
- Create: `frontend/src/lib/useThrottledValue.ts` + `frontend/src/lib/useThrottledValue.test.ts`
- Modify: `frontend/src/pages/import/ImportPage.tsx` (`ImportRun` announcer; `LiveFeed` drops `aria-live`)
- Modify: `frontend/src/pages/import/ImportPage.test.tsx` (announcer test)

- [ ] **Step 1: Write the failing hook unit test**

`frontend/src/lib/useThrottledValue.test.ts`:

```typescript
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useThrottledValue } from "@/lib/useThrottledValue";

describe("useThrottledValue", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  test("emits the first value immediately (leading edge)", () => {
    const { result } = renderHook(() => useThrottledValue("a", 5000));
    expect(result.current).toBe("a");
  });

  test("coalesces rapid changes, then settles on the latest", () => {
    const { result, rerender } = renderHook(
      ({ v }) => useThrottledValue(v, 5000),
      { initialProps: { v: "a" } },
    );
    expect(result.current).toBe("a");

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    rerender({ v: "b" });
    rerender({ v: "c" });
    // Still within the window: the value has not advanced past the leading "a".
    expect(result.current).toBe("a");

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    // After the interval elapses, the trailing edge emits the LATEST ("c").
    expect(result.current).toBe("c");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd frontend && npm run test -- useThrottledValue
```
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the hook**

`frontend/src/lib/useThrottledValue.ts`:

```typescript
import { useEffect, useRef, useState } from "react";

/**
 * Throttle how often `value` is surfaced. Returns the latest value but updates
 * at most once per `intervalMs`: leading-edge (the first value, and any change
 * arriving after the interval has elapsed, emits immediately) with a trailing
 * edge (rapid changes within the window coalesce, then the latest is emitted
 * when the window closes). Used to keep a polling-driven `aria-live` region
 * from announcing on every 1s poll — screen readers get calm, periodic updates
 * while sighted users read the un-throttled visible copy.
 */
export function useThrottledValue<T>(value: T, intervalMs: number): T {
  const [throttled, setThrottled] = useState(value);
  const lastEmit = useRef(Date.now());
  const latest = useRef(value);
  latest.current = value;

  useEffect(() => {
    const sinceLast = Date.now() - lastEmit.current;
    if (sinceLast >= intervalMs) {
      lastEmit.current = Date.now();
      setThrottled(value);
      return;
    }
    const timer = setTimeout(() => {
      lastEmit.current = Date.now();
      setThrottled(latest.current);
    }, intervalMs - sinceLast);
    return () => clearTimeout(timer);
  }, [value, intervalMs]);

  return throttled;
}
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd frontend && npm run test -- useThrottledValue
```
Expected: PASS. (If the trailing-edge timing assertion is flaky under fake timers, adjust the advance amounts — keep the leading-edge + eventual-latest intent.)

- [ ] **Step 5: Write the failing announcer test**

In `ImportPage.test.tsx`, add to `describe("ImportPage — live feed", ...)`:

```tsx
  test("announces progress through one polite live region (verb-first)", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    // Exactly one status region; its phrasing is distinct from the visible cue
    // so it is the single spoken source (no double announcement).
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/imported 1/i);
  });
```

- [ ] **Step 6: Run it to verify it fails**

```bash
cd frontend && npm run test -- ImportPage
```
Expected: FAIL — multiple/zero `role="status"` regions, or the visible cue's "1 album imported" phrasing rather than the verb-first "Imported 1".

- [ ] **Step 7: Hoist the throttled announcer; make the cue visible-only**

In `ImportPage.tsx`:

Add imports:
```tsx
import type { ImportAlbumSummary, ImportJobState, ImportProgress } from "@/api/useImport";
import { useThrottledValue } from "@/lib/useThrottledValue";
```

Add the announcer message builder (module scope, near the other helpers):
```tsx
/** The single spoken status for the whole run. Verb-first and intentionally
 * worded differently from the visible cue/panels so it never collides with
 * them — it is the one `aria-live` source. */
function announceMessage(args: {
  isPending: boolean;
  isError: boolean;
  notFound: boolean;
  data: ImportJobState | undefined;
}): string {
  const { isPending, isError, notFound, data } = args;
  if (notFound) return "This import is no longer available.";
  if (isError) return "Couldn’t load the import.";
  if (isPending || !data) return "Loading the import.";
  if (data.phase === "failed") return "The import failed.";
  if (data.phase === "done") {
    const { applied, skipped } = data.progress;
    return `Import complete. Imported ${applied}, skipped ${skipped}.`;
  }
  if (data.phase === "scanning" && data.albums.length === 0) {
    return "Scanning the folder for albums.";
  }
  const { applied, skipped, needs_review } = data.progress;
  let m = `Imported ${applied}.`;
  if (skipped > 0) m += ` Skipped ${skipped}.`;
  if (needs_review > 0) m += " One album awaiting review.";
  return m;
}
```

Rewrite `ImportRun` to render ONE continuous announcer above the phase body (the body keeps the per-phase branches, but none of them carry their own `aria-live`/`role="status"`):

```tsx
/** The live run: one continuous polite announcer + the phase-appropriate view. */
function ImportRun({ jobId }: { jobId: string }) {
  const { data, isPending, isError, error, refetch } = useImportJob(jobId);
  const notFound = error instanceof ImportJobNotFoundError;
  // Throttle the spoken status so a fast scan (1s poll) doesn't spam a screen
  // reader. Mounted in every branch below, so it is continuous across phases.
  const status = useThrottledValue(
    announceMessage({ isPending, isError, notFound, data }),
    4000,
  );

  const announcer = (
    <p className="sr-only" role="status" aria-live="polite">
      {status}
    </p>
  );

  if (isPending) {
    return (
      <ImportShell>
        {announcer}
        <FeedSkeleton />
      </ImportShell>
    );
  }
  if (notFound) {
    return (
      <ImportShell>
        {announcer}
        <JobNotFound />
      </ImportShell>
    );
  }
  if (isError) {
    return (
      <ImportShell>
        {announcer}
        <JobError onRetry={() => void refetch()} />
      </ImportShell>
    );
  }
  if (data.phase === "failed") {
    return (
      <ImportShell>
        {announcer}
        <JobFailed error={data.error} />
      </ImportShell>
    );
  }
  if (data.phase === "done") {
    return (
      <ImportShell>
        {announcer}
        <JobDone state={data} jobId={jobId} />
      </ImportShell>
    );
  }
  return (
    <ImportShell>
      {announcer}
      <LiveFeed state={data} jobId={jobId} />
    </ImportShell>
  );
}
```

In `LiveFeed`, the visible cue `<p>` drops `aria-live` (it is no longer the announcer — sighted-only, still live-updating every poll):

```tsx
      <p className="text-muted-foreground flex min-h-5 items-center gap-2 text-sm">
```

- [ ] **Step 8: Run frontend gates**

```bash
cd frontend && npm run test && npm run typecheck && npm run build
```
Expected: all PASS — the whole frontend suite (the existing `/scanning your folder/i`, done, failed, and the new cue/done/not-found tests all coexist with the single distinct announcer).

- [ ] **Step 9: Commit**

```bash
git add frontend/src/lib/useThrottledValue.ts frontend/src/lib/useThrottledValue.test.ts frontend/src/pages/import/ImportPage.tsx frontend/src/pages/import/ImportPage.test.tsx
git commit -m "feat(import): one continuous, throttled screen-reader announcer for the run"
```

---

## Task 6: Full-suite green + real-files end-to-end walkthrough

This is the verification close-out: the whole repo green, then the first real beets import driven through the live UI (chunks 1–4 were verified with canned/fake data; this proves the real worker → bridge → API → UI path) against a throwaway sandbox library, captured as a screenshot.

**Files:** none (verification only).

- [ ] **Step 1: Whole-repo green gate**

```bash
cd backend && export PATH="$HOME/.local/bin:$PATH" && uv run pytest -q && uv run mypy && uv run ruff check && uv run ruff format --check
cd ../frontend && npm run test && npm run typecheck && npm run build
```
Expected: backend full suite + mypy + ruff clean; frontend full suite + tsc + build clean.

- [ ] **Step 2: Cumulative UI review (FE diff for chunk 5)**

Before the walkthrough, run the design + UX reviewers over the cumulative chunk-5 frontend diff (the new done screen, cue, not-found notice, announcer) and fold any findings into a single polish commit — mirroring how chunk 4 was closed out. (The controller dispatches `ui-ux-reviewer` + `design-enforcer`; this step is a checkpoint, not code.)

- [ ] **Step 3: Stand up a throwaway sandbox (NEVER the real library)**

> CONFIRM WITH THE USER FIRST: the audio source (a few of their own throwaway files vs. generated/tagged silence) and whether MusicBrainz network lookups are allowed for this run. Then:

- Create a temp tree: `SANDBOX=$(mktemp -d)`; inside it a `beetsdir/` (with a minimal `config.yaml` whose `directory:` and `library:` point INSIDE `$SANDBOX`), an empty `library/` (beets will populate it), and an `incoming/` holding the throwaway audio.
- Start the backend pointed at the sandbox (the lifespan attaches the Library from config) on a non-conflicting port; start the frontend dev server.
- This guarantees the import mutates only `$SANDBOX`, never the user's library/DB.

- [ ] **Step 4: Drive the flow + screenshot**

Via the Playwright MCP: open `/import`, enter the sandbox `incoming/` path, Start; watch the live feed (auto-applied strong albums stream in; an uncertain album parks → Review → the candidate screen with REAL beets data → choose Apply/Skip); land on the polished done screen. Capture a full-page screenshot to `/tmp/musicdrop-chunk5-walkthrough.png` and confirm 0 console errors.

- [ ] **Step 5: Tear down the sandbox**

```bash
rm -rf "$SANDBOX"
```
(Only the temp dir — never touch the user's configured library/DB.) Confirm `git status` is clean (no stray artifacts, no `.playwright-mcp/`).

- [ ] **Step 6: Final full-branch review**

Dispatch the final code-reviewer over the whole chunk-5 diff (per subagent-driven-development's close-out). Then report completion: chunk 5 done, the import feature is feature-complete on `feat/import`, ready for the user's one PR.

---

## Self-Review (run before dispatching Task 1)

**1. Spec coverage** — Spec's chunk-5 acceptance (`docs/superpowers/specs/2026-05-25-import-design.md`, "Done — the done summary (truthful: auto-applied + decided), view-in-library wiring; live screenshot end-to-end"): the done summary polish (T3, counting auto-applied via `progress.applied`), view-in-library (already wired, retained in T3), live screenshot end-to-end (T6) are covered. The `skipped` counter (T1/T2) realizes the spec's "what was skipped — counting the auto-applied strong albums too". The not-found UX (T4) and the announcer (T5) are robustness/a11y items I flagged in chunks 3–4 reviews, scoped into this final chunk.

**2. Placeholder scan** — Every code step shows the actual code; commands have expected output. The only deferred decision is T6's audio source/network, explicitly gated on a user confirmation (a verification procedure, not a code placeholder).

**3. Type consistency** — `ImportProgress` gains `skipped: int` (T1) → regenerated `schema.d.ts` (T2) → exported `ImportProgress` TS type (T2) consumed by `cueMessage`/`announceMessage` (T5) and read field-wise in `JobDone` (T3). `ImportJobNotFoundError` is defined in T4 and referenced by `announceMessage` (T5) — T4 precedes T5. `JobDone` signature changes to `{ state, jobId }` in T3 and its call site updates in the same task (then is carried through T5's `ImportRun` rewrite). `_is_skipped`/`_is_imported` are the paired terminal predicates backing both `progress.*` and the summary.

**4. Collision check (announcer vs. visible text)** — The verb-first announcer ("Imported N", "Skipped K", "Scanning the folder for albums", "Import complete", "The import failed") shares no asserted substring with the visible cue/panels (`/scanning your folder/i`, `/1 skipped/`, `/2 albums imported/i`, `/0 skipped/i`, exact `"Import finished"`, exact `"Import failed"`). It is the only `role="status"` node. Verified per regex in T5.
