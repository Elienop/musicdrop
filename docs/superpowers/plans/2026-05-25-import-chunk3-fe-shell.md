# Import Chunk 3 — Frontend Flow Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the frontend shell for the beets import flow — a `/import` page that is a state machine over the chunk-2 API's `ImportJobState.phase` (entry → scanning → live review feed → done/failed), plus the TanStack Query hooks (typed from the generated OpenAPI schema) that drive it and a stubbed seam to the chunk-4 candidate-review screen.

**Architecture:** Mirror the existing app exactly. Regenerate `frontend/openapi.json` from the backend's live `app.openapi()` (which now includes `/api/import*`), run `gen:api` to refresh `src/api/schema.d.ts`, then add per-resource hook modules in `src/api/` shaped like `useSearch.ts`/`useAlbums.ts`. The import page reuses the established page-state idiom (idle/skeleton/error via shadcn `Card`/`Button`/`Badge`/`Skeleton` and the same Tailwind vocabulary) and renders the live feed as a list of `ImportAlbumSummary` rows. Polling is a TanStack v5 `refetchInterval` function that stops at the terminal phases. The Review affordance navigates to a route-based seam (`/import/albums/:index`) whose destination is a thin stub that chunk 4 replaces — so chunk 3 is independently testable and screenshot-able.

**Tech Stack:** React 19, Vite, Tailwind v4, shadcn/ui (radix-ui primitives), TanStack Query v5, react-router v7, openapi-fetch + openapi-typescript (generated types). Tests: vitest + @testing-library/react + msw. All commands run from `frontend/`.

---

## Frontend/API facts this plan encodes (verified)

Read against the real tree on `feat/import` before writing any code. These are the load-bearing facts:

1. **`gen:api` is offline and dump-driven.** `frontend/package.json` script:
   `"gen:api": "openapi-typescript ./openapi.json -o ./src/api/schema.d.ts"`. It consumes a **checked-in** `frontend/openapi.json` (no live fetch). That file is currently **stale** — it predates the import router, so `src/api/schema.d.ts` has **no** `/api/import*` paths and **no** `ImportJobState`/`Candidate`/etc. schemas. The dump must be regenerated first.
2. **The backend OpenAPI already includes the import surface** (verified via `app.openapi()`): paths `/api/import` (post), `/api/import/{job_id}` (get), `/api/import/{job_id}/albums/{index}` (get), `/api/import/{job_id}/albums/{index}/choice` (post); schemas `ImportJobState, ImportAlbumSummary, ImportProgress, ImportPhase, ImportAlbumStatus, StartImportRequest, StartImportResponse, Candidate, ImportChoice, ImportAction, Recommendation, AlbumChange, TrackChange, MissingTrack, UnmatchedItem, CandidateOption, TrackChangeStatus`.
3. **No committed dump script exists.** The dump is produced by running the backend app and serializing `app.openapi()`. Verified-working command (run from `backend/`):
   `uv run python -c "import json; from app.main import app; from pathlib import Path; Path('../frontend/openapi.json').write_text(json.dumps(app.openapi()))"`.
   The dump must NOT start the uvicorn server or hit the network — `app.openapi()` builds the schema from the route table synchronously.
4. **The typed client** lives in `src/api/client.ts`: `createClient<paths>(...)`; `baseUrl` is `window.location.origin` (empty under Node); `fetch` is resolved per-call (`globalThis.fetch(...)`) so msw intercepts in tests. Same-origin: dev proxies `/api` → `:3030` (`vite.config.ts`), so paths already start with `/api`.
5. **The hook pattern** (`src/api/useSearch.ts`, `useAlbums.ts`, `useAlbum.ts`): one module per resource; a top-level `export type X = components["schemas"]["X"]`; an `async function fetchX()` calling `client.GET(...)` that throws `new Error(...)` on `error || !data`; then `export function useX()` returning `useQuery({ queryKey: [...], queryFn, ... })`. `enabled` gates disabled queries; `placeholderData: (prev) => prev` keeps prior data; a dedicated `Error` subclass (`AlbumNotFoundError` in `useAlbum.ts`) lets a page branch on a specific status with `retry: false`. **There are zero `useMutation` usages today** — the two import mutations introduce the pattern (grounded below in TanStack v5 + openapi-fetch's `client.POST`).
6. **The route table** is `createBrowserRouter([{ element: <App/>, children: [...] }])` in `src/main.tsx`. `App` (`src/App.tsx`) is the persistent shell: a sticky header with the brand `<Link to="/">`, the `HeaderSearch` box (typing navigates to `/search?q=`), and `HealthStatus`, wrapping the routed page in `<Outlet/>`. Routes today: `/` (ArtistsPage), `/artists` (→ `/`), `/artists/:artistName`, `/albums/:albumId`, `/search`, `*` (NotFoundPage).
7. **The router contract is mirrored in a test:** `src/routing.test.tsx` hand-copies the route table and asserts each path renders the right page. **Any route added to `main.tsx` MUST be added to `routing.test.tsx`'s `routes` too**, or the contract test drifts.
8. **The test harness:** `vitest.config.ts` → `globals: true`, `environment: "jsdom"`, jsdom `url: "http://localhost"`, `setupFiles: ["./src/test/setup.ts"]`. `setup.ts` starts msw (`server.listen({ onUnhandledRequest: "error" })` — a missing mock is loud), stubs `window.scrollTo`, resets handlers per test. `src/test/msw-server.ts` exports an empty `setupServer()`. `src/test/render.tsx` exports `renderWithProviders(ui, { route, path })` — a fresh retry-disabled `QueryClient` + a `MemoryRouter` mounting `ui` at `path` (default `"*"`) starting at `route` (default `"/"`), plus a `/` "Library home" target for back-links. Tests register handlers with `server.use(http.get/post(URL, () => HttpResponse.json(...)))` where `URL = \`${window.location.origin}/api/...\``.
9. **Design vocabulary** (from `SearchPage.tsx`, `ArtistsPage.tsx`, `ArtistAlbumsPage.tsx`, `AlbumDetailPage.tsx`, `components/albums/album-grid.tsx`):
   - Page root: `<section className="flex flex-col gap-6">` (or `gap-8`) with an `aria-label`; an `<h2 className="text-2xl font-semibold tracking-tight">` heading (h1 only on SearchPage); a `<p className="text-muted-foreground min-h-5 text-sm" aria-live="polite">` count line.
   - Loading: a `<p className="sr-only" role="status">Loading…</p>` + a skeleton block of shadcn `Skeleton`s; in-flight refine = `pointer-events-none opacity-60 transition-opacity` + `aria-busy`.
   - Error: `<div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">` with an `AlertCircle` icon, a `font-medium` headline, a `text-muted-foreground text-sm` body, and an `<Button variant="outline" size="sm" onClick={onRetry}>Retry</Button>`.
   - Empty/idle: same centered block, `border-dashed`, a lucide icon (`size-10`), headline + sub-line.
   - `BackLink({ to, label })` (from `album-grid.tsx`) is the hierarchical up-affordance (ghost button + `ChevronLeft`).
   - shadcn components available in `src/components/ui/`: `button`, `card` (Card/CardHeader/CardTitle/CardDescription/CardContent/CardFooter/CardAction), `badge` (variants: default/secondary/destructive/outline/ghost/link), `input`, `separator`, `skeleton`, `table`. `cn` is `src/lib/utils.ts`. Icons: `lucide-react`.
10. **Sequential semantics (chunk-2 FE notes):** the feed is a live-growing list with **no known total** (no "album N of M"); **at most one** `needs_review` row at a time; `phase: applying` may never be observed (treat as a transient working state, like scanning); a `404`/`409` on a choice POST means "no longer awaiting — refetch the job". Strong matches auto-apply in the worker and arrive already `applied`/`skipped`.
11. **`ImportAlbumStatus` values:** `needs_review | decided | applied | skipped`. **`ImportPhase` values:** `scanning | reviewing | applying | done | failed`. **`ImportAction` values:** `apply | skip | asis | astracks | abort`. `ImportChoice = { action, candidate_index?: number|null }`. `StartImportRequest = { path: string, options?: ... }`; a blank/whitespace path is a 422. A second concurrent start is a 409.

---

## File structure

| File | Status | Responsibility |
| --- | --- | --- |
| `frontend/openapi.json` | Modify (regen) | Checked-in OpenAPI dump; regenerated from `app.openapi()` so it includes `/api/import*`. Input to `gen:api`. |
| `frontend/src/api/schema.d.ts` | Modify (regen) | Generated TS types. After `gen:api`, includes the import paths + schemas. Never hand-edited. |
| `frontend/src/api/useImport.ts` | Create | All four import hooks (`useStartImport`, `useImportJob`, `useImportCandidate`, `useSubmitChoice`) + the re-exported generated types. Mirrors `useSearch.ts`. One module: the hooks share the job query key and a small surface. |
| `frontend/src/pages/import/ImportPage.tsx` | Create | The `/import` state machine: entry / scanning / reviewing-feed / done / failed. Renders the feed rows + the Review seam link. |
| `frontend/src/pages/import/ImportCandidatePage.tsx` | Create | **Stub** seam destination for `/import/albums/:index` (chunk 4 replaces it). A minimal "Review (coming in chunk 4)" panel + a BackLink to `/import`. |
| `frontend/src/main.tsx` | Modify | Register `/import` and `/import/albums/:index`; add an "Import" header nav link (in `App.tsx`). |
| `frontend/src/App.tsx` | Modify | Add an "Import" link in the header chrome (peer of the brand/search), reachable like Search. |
| `frontend/src/routing.test.tsx` | Modify | Mirror the two new routes so the route-contract test stays in lockstep with `main.tsx`. |
| `frontend/src/pages/import/ImportPage.test.tsx` | Create | vitest + msw coverage: entry→start, polling renders the feed, applied + needs_review rows, the Review link target, done/failed, 409/422 handling. |

Decomposition note: hooks live in one `useImport.ts` (they form one cohesive surface around the job query key — DRY, and matches the one-module-per-resource convention; `useSearch.ts` similarly co-locates its query + types). The page and its stub seam are separate files under `pages/import/` so chunk 4 swaps the stub without touching the feed page.

---

## The chunk-3 ↔ chunk-4 seam (decided)

**Route-based.** The Review affordance is a `<Link to={`/import/albums/${index}`}>`; chunk 3 ships `ImportCandidatePage` as a **stub** mounted at `/import/albums/:index`. Chunk 4 replaces the stub's body with the real candidate-review screen (consuming `useImportCandidate` + `useSubmitChoice`, both built here). Rationale: it matches the app's existing route-based drill-down spine (`/artists/:name`, `/albums/:id`), keeps the feed page simple (no modal/panel state), and is independently navigable + screenshot-able. The stub renders a `BackLink to="/import"` + a "Review screen — coming in chunk 4" placeholder showing the index, so the seam is exercised end-to-end without building chunk-4 UI.

---

### Task 1: Regenerate the API types so the import contract is typed

**Files:**
- Modify (regen): `frontend/openapi.json`
- Modify (regen): `frontend/src/api/schema.d.ts`

- [ ] **Step 1: Confirm the generated schema currently LACKS the import contract**

Run (from `frontend/`):
```bash
grep -c "/api/import" src/api/schema.d.ts; grep -c "ImportJobState" src/api/schema.d.ts
```
Expected: `0` and `0` — the stale dump predates the import router. (This is the "failing" precondition for this task.)

- [ ] **Step 2: Regenerate the OpenAPI dump from the live backend schema**

Run (from `backend/`):
```bash
uv run python -c "import json; from app.main import app; from pathlib import Path; Path('../frontend/openapi.json').write_text(json.dumps(app.openapi()))"
```
This serializes `app.openapi()` (synchronous; no server, no network) into the checked-in dump. Verify the import surface landed:
```bash
grep -c "/api/import" ../frontend/openapi.json; grep -c "ImportJobState" ../frontend/openapi.json
```
Expected: both `>= 1` (paths + schemas now present).

- [ ] **Step 3: Run `gen:api` to regenerate the TypeScript types**

Run (from `frontend/`):
```bash
npm run gen:api
```
Expected: writes `src/api/schema.d.ts` with no errors.

- [ ] **Step 4: Verify the generated types now include the import contract**

Run (from `frontend/`):
```bash
grep -c "/api/import" src/api/schema.d.ts
grep -E "ImportJobState|ImportAlbumSummary|ImportPhase|ImportAlbumStatus|StartImportRequest|StartImportResponse|ImportChoice|ImportAction:|Candidate:" src/api/schema.d.ts | head -20
npx tsc -b --noEmit
```
Expected: the path count is `>= 1`; the grep lists the import schemas; `tsc` passes (the regen didn't break existing typed usages).

- [ ] **Step 5: Commit**

```bash
git add frontend/openapi.json frontend/src/api/schema.d.ts
git commit -m "feat(import): regenerate API types to include the import contract"
```

---

### Task 2: `useStartImport` mutation (POST /api/import)

**Files:**
- Create: `frontend/src/api/useImport.ts`
- Test: `frontend/src/api/useImport.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/useImport.test.tsx`:
```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import { ImportConflictError, useStartImport } from "@/api/useImport";
import { server } from "@/test/msw-server";

const IMPORT_URL = `${window.location.origin}/api/import`;

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

describe("useStartImport", () => {
  test("POSTs the path and resolves the new job id", async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(IMPORT_URL, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({ job_id: "job-1" }, { status: 202 });
      }),
    );

    const { result } = renderHook(() => useStartImport(), {
      wrapper: wrapper(),
    });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.job_id).toBe("job-1");
    expect(seenBody).toEqual({ path: "/music/incoming" });
  });

  test("maps a 409 to ImportConflictError", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json({ detail: "An import is already running" }, { status: 409 }),
      ),
    );

    const { result } = renderHook(() => useStartImport(), {
      wrapper: wrapper(),
    });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ImportConflictError);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `frontend/`):
```bash
npx vitest run src/api/useImport.test.tsx
```
Expected: FAIL — `Failed to resolve import "@/api/useImport"` (module not created yet).

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/api/useImport.ts`:
```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Job state + live feed as returned by `GET /api/import/{job_id}` (generated). */
export type ImportJobState = components["schemas"]["ImportJobState"];
/** One row in the live feed (generated contract). */
export type ImportAlbumSummary = components["schemas"]["ImportAlbumSummary"];
/** Coarse lifecycle phase (generated contract). */
export type ImportPhase = components["schemas"]["ImportPhase"];
/** Per-album feed status (generated contract). */
export type ImportAlbumStatus = components["schemas"]["ImportAlbumStatus"];
/** Body of `POST /api/import` (generated contract). */
export type StartImportRequest = components["schemas"]["StartImportRequest"];
/** Response of a successful `POST /api/import` (generated contract). */
export type StartImportResponse = components["schemas"]["StartImportResponse"];
/** The full per-album review payload (generated; chunk 4 renders it). */
export type Candidate = components["schemas"]["Candidate"];
/** A user's decision for one parked album (generated contract). */
export type ImportChoice = components["schemas"]["ImportChoice"];

/** Thrown when a start is rejected because an import is already running (409).
 * Lets the entry screen surface a "an import is already running" message with a
 * link to it, instead of the generic failure. */
export class ImportConflictError extends Error {
  constructor() {
    super("An import is already running");
    this.name = "ImportConflictError";
  }
}

async function startImport(
  body: StartImportRequest,
): Promise<StartImportResponse> {
  const { data, error, response } = await client.POST("/api/import", { body });
  if (response.status === 409) {
    throw new ImportConflictError();
  }
  if (error || !data) {
    throw new Error("Failed to start import");
  }
  return data;
}

/** Start an import (`POST /api/import`). A 409 (already running) surfaces as
 * {@link ImportConflictError} so the entry screen can branch. The caller routes
 * to `/import?job=<id>` on success. */
export function useStartImport() {
  return useMutation({
    mutationFn: startImport,
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `frontend/`):
```bash
npx vitest run src/api/useImport.test.tsx
```
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/useImport.ts frontend/src/api/useImport.test.tsx
git commit -m "feat(import): add useStartImport mutation hook"
```

---

### Task 3: `useImportJob` query with terminal-aware polling

**Files:**
- Modify: `frontend/src/api/useImport.ts`
- Test: `frontend/src/api/useImport.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/api/useImport.test.tsx` (add `act` to the `@testing-library/react` import: `import { act, renderHook, waitFor } from "@testing-library/react";`, and add `useImportJob` to the `@/api/useImport` import). Add this describe block:
```tsx
import type { ImportJobState } from "@/api/useImport";

const JOB_URL = `${window.location.origin}/api/import/job-1`;

function makeJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "job-1",
    phase: "reviewing",
    progress: { applied: 1, needs_review: 1 },
    albums: [],
    summary: null,
    error: null,
    ...overrides,
  };
}

describe("useImportJob", () => {
  test("fetches the job state by id", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));

    const { result } = renderHook(() => useImportJob("job-1"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.data?.phase).toBe("reviewing"));
  });

  test("is disabled when no job id is given (no request)", async () => {
    // No handler registered: if a request fired, msw's onUnhandledRequest:error
    // would fail the test. A disabled query stays idle.
    const { result } = renderHook(() => useImportJob(undefined), {
      wrapper: wrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
  });

  test("polls while active and stops at a terminal phase", async () => {
    const seq: ImportJobState[] = [
      makeJob({ phase: "scanning", progress: { applied: 0, needs_review: 0 } }),
      makeJob({ phase: "done", summary: "1 imported, 0 skipped" }),
    ];
    let calls = 0;
    server.use(
      http.get(JOB_URL, () => {
        const body = seq[Math.min(calls, seq.length - 1)];
        calls += 1;
        return HttpResponse.json(body);
      }),
    );

    const { result } = renderHook(() => useImportJob("job-1"), {
      wrapper: wrapper(),
    });

    // First the active (scanning) state, then the poll advances to done.
    await waitFor(() => expect(result.current.data?.phase).toBe("done"));
    const callsAtDone = calls;
    // Once done, polling stops: give it time and assert no further fetches.
    await act(() => new Promise((r) => setTimeout(r, 1500)));
    expect(calls).toBe(callsAtDone);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `frontend/`):
```bash
npx vitest run src/api/useImport.test.tsx -t "useImportJob"
```
Expected: FAIL — `useImportJob` is not exported from `@/api/useImport`.

- [ ] **Step 3: Write minimal implementation**

Add to `frontend/src/api/useImport.ts` (after `useStartImport`):
```ts
/** Poll cadence (ms) while an import is active. Brisk enough that auto-applied
 * albums stream into the feed promptly; the loop stops at a terminal phase. */
const IMPORT_POLL_MS = 1000;

/** Phases where the worker is still running — the feed is live and should poll.
 * `applying` is included defensively: beets exposes no signal to set it, so it
 * may never be observed, but if it is it's a transient working state, not
 * terminal. */
const ACTIVE_PHASES: ReadonlySet<ImportPhase> = new Set([
  "scanning",
  "reviewing",
  "applying",
]);

/** Whether `phase` is terminal (the import finished or failed). */
export function isTerminalPhase(phase: ImportPhase): boolean {
  return phase === "done" || phase === "failed";
}

async function fetchJob(jobId: string): Promise<ImportJobState> {
  const { data, error } = await client.GET("/api/import/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  if (error || !data) {
    throw new Error("Failed to load import job");
  }
  return data;
}

/**
 * Poll an import job's state (`GET /api/import/{job_id}`). Disabled until a
 * `jobId` exists (no request, no error). `refetchInterval` is a function so the
 * loop runs only while the phase is active (scanning/reviewing/applying) and
 * returns `false` once terminal (done/failed) — TanStack v5 stops polling on a
 * falsy interval. No auto-retry: a transient error surfaces in the page's error
 * state behind an explicit retry rather than a silent backoff.
 */
export function useImportJob(jobId: string | undefined) {
  return useQuery({
    queryKey: ["import", "job", jobId],
    queryFn: () => fetchJob(jobId as string),
    enabled: Boolean(jobId),
    retry: false,
    refetchInterval: (query) => {
      const phase = query.state.data?.phase;
      if (phase === undefined || ACTIVE_PHASES.has(phase)) {
        return IMPORT_POLL_MS;
      }
      return false;
    },
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `frontend/`):
```bash
npx vitest run src/api/useImport.test.tsx -t "useImportJob"
```
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/useImport.ts frontend/src/api/useImport.test.tsx
git commit -m "feat(import): add useImportJob query with terminal-aware polling"
```

---

### Task 4: `useImportCandidate` query + `useSubmitChoice` mutation

**Files:**
- Modify: `frontend/src/api/useImport.ts`
- Test: `frontend/src/api/useImport.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/api/useImport.test.tsx` (add `useImportCandidate, useSubmitChoice` to the `@/api/useImport` import). Add:
```tsx
const CANDIDATE_URL = `${window.location.origin}/api/import/job-1/albums/1`;
const CHOICE_URL = `${window.location.origin}/api/import/job-1/albums/1/choice`;

describe("useImportCandidate", () => {
  test("fetches the candidate when enabled", async () => {
    server.use(
      http.get(CANDIDATE_URL, () =>
        HttpResponse.json({
          confidence: 75.5,
          recommendation: "medium",
          data_source: "MusicBrainz",
          data_url: "https://mb/a1",
          changed_fields: ["album"],
          album_before: {
            artist: "Radiohead",
            album: "OK Computr",
            year: null,
            label: null,
            country: null,
            media: null,
          },
          album_after: {
            artist: "Radiohead",
            album: "OK Computer",
            year: 1997,
            label: "Parlophone",
            country: "GB",
            media: "CD",
          },
          tracks: [],
          missing: [],
          unmatched: [],
          options: [],
        }),
      ),
    );

    const { result } = renderHook(() => useImportCandidate("job-1", 1, true), {
      wrapper: wrapper(),
    });

    await waitFor(() =>
      expect(result.current.data?.album_after.album).toBe("OK Computer"),
    );
  });

  test("is disabled when enabled=false (no request)", () => {
    const { result } = renderHook(() => useImportCandidate("job-1", 1, false), {
      wrapper: wrapper(),
    });
    expect(result.current.fetchStatus).toBe("idle");
  });
});

describe("useSubmitChoice", () => {
  test("POSTs the choice to the album index", async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(CHOICE_URL, async ({ request }) => {
        seenBody = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { result } = renderHook(() => useSubmitChoice("job-1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ index: 1, choice: { action: "skip" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seenBody).toEqual({ action: "skip" });
  });

  test("a 404 resolves (stale slot) rather than rejecting", async () => {
    // 404/409 mean 'no longer awaiting' — the page refetches the job; the
    // mutation must not throw so the UI doesn't show a hard error.
    server.use(
      http.post(CHOICE_URL, () =>
        HttpResponse.json({ detail: "Import album not found" }, { status: 404 }),
      ),
    );

    const { result } = renderHook(() => useSubmitChoice("job-1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ index: 1, choice: { action: "skip" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `frontend/`):
```bash
npx vitest run src/api/useImport.test.tsx -t "useImportCandidate"
```
Expected: FAIL — `useImportCandidate` is not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `frontend/src/api/useImport.ts`:
```ts
async function fetchCandidate(jobId: string, index: number): Promise<Candidate> {
  const { data, error } = await client.GET(
    "/api/import/{job_id}/albums/{index}",
    { params: { path: { job_id: jobId, index } } },
  );
  if (error || !data) {
    throw new Error("Failed to load candidate");
  }
  return data;
}

/**
 * Fetch the full Candidate for one parked album
 * (`GET /api/import/{job}/albums/{index}`). `enabled` gates it so it only fires
 * when the review screen opens (it 404s once the album is no longer parked).
 */
export function useImportCandidate(
  jobId: string,
  index: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["import", "candidate", jobId, index],
    queryFn: () => fetchCandidate(jobId, index),
    enabled,
    retry: false,
  });
}

/** Arguments to a choice submission: which album, and the decision. */
export interface SubmitChoiceArgs {
  index: number;
  choice: ImportChoice;
}

async function submitChoice(
  jobId: string,
  { index, choice }: SubmitChoiceArgs,
): Promise<void> {
  const { error, response } = await client.POST(
    "/api/import/{job_id}/albums/{index}/choice",
    { params: { path: { job_id: jobId, index } }, body: choice },
  );
  // 404 (slot already advanced) / 409 (a choice already landed) are not hard
  // failures: they mean 'no longer awaiting this album'. Swallow them — the
  // caller refetches the job to resync. A real transport error still throws.
  if (response.status === 404 || response.status === 409) {
    return;
  }
  if (error) {
    throw new Error("Failed to submit choice");
  }
}

/**
 * Submit a decision for a parked album
 * (`POST /api/import/{job}/albums/{index}/choice`). On settle, invalidate the
 * job query so the feed reflects the new state (the worker advances to the next
 * album). A 404/409 is treated as 'already advanced' and resolves quietly.
 */
export function useSubmitChoice(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: SubmitChoiceArgs) => submitChoice(jobId, args),
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: ["import", "job", jobId],
      });
    },
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `frontend/`):
```bash
npx vitest run src/api/useImport.test.tsx
```
Expected: PASS (all useImport tests — start, job, candidate, choice).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/useImport.ts frontend/src/api/useImport.test.tsx
git commit -m "feat(import): add useImportCandidate query and useSubmitChoice mutation"
```

---

### Task 5: The chunk-4 seam stub page

**Files:**
- Create: `frontend/src/pages/import/ImportCandidatePage.tsx`

- [ ] **Step 1: Write the implementation (stub — no test of its own; the seam link is asserted in Task 8's ImportPage tests and Task 7's routing test)**

Create `frontend/src/pages/import/ImportCandidatePage.tsx`:
```tsx
import { Wrench } from "lucide-react";
import { useParams } from "react-router";

import { BackLink } from "@/components/albums/album-grid";

/**
 * Seam destination for `/import/albums/:index` (chunk-3 ↔ chunk-4 boundary).
 *
 * Chunk 3 ships this STUB so the Review affordance in the live feed has a real,
 * navigable target — keeping the feed page testable and screenshot-able. Chunk
 * 4 replaces the body with the faithful candidate-review screen (before/after +
 * full tracklist + the choice actions), driven by `useImportCandidate` +
 * `useSubmitChoice` (both already built in chunk 3).
 */
export function ImportCandidatePage() {
  const { index } = useParams<{ index: string }>();

  return (
    <section className="flex flex-col gap-6" aria-label="Review album">
      <BackLink to="/import" label="Import" />
      <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
        <Wrench className="text-muted-foreground size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Review screen coming soon</p>
          <p className="text-muted-foreground text-sm">
            The candidate review for album {index} arrives in the next slice.
          </p>
        </div>
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Verify it typechecks**

Run (from `frontend/`):
```bash
npx tsc -b --noEmit
```
Expected: PASS (no type errors; the import of `BackLink` resolves).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/import/ImportCandidatePage.tsx
git commit -m "feat(import): add stubbed candidate-review seam page"
```

---

### Task 6: The import page state machine (`/import`)

**Files:**
- Create: `frontend/src/pages/import/ImportPage.tsx`

This task ships the whole page; Task 8 adds its behavioral tests. The page reads the active job id from the URL (`/import?job=<id>`), so a started/in-progress import is bookmarkable and a browser refresh resumes the feed — mirroring how `/search?q=` and `/artists/:name?offset=` keep state in the URL.

- [ ] **Step 1: Write the implementation**

Create `frontend/src/pages/import/ImportPage.tsx`:
```tsx
import { AlertCircle, FolderInput, Loader2 } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import type { ImportAlbumSummary, ImportJobState } from "@/api/useImport";
import {
  ImportConflictError,
  isTerminalPhase,
  useImportJob,
  useStartImport,
} from "@/api/useImport";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export function ImportPage() {
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job") ?? undefined;

  // No active job in the URL -> the entry screen (path + Start).
  if (!jobId) {
    return <ImportEntry />;
  }
  return <ImportRun jobId={jobId} />;
}

/** Entry: a server-path input + Start. Surfaces the 409 (already running) and
 * the blank-path guard locally; on success the URL gains `?job=<id>` and the
 * page flips to the live run. */
function ImportEntry() {
  const [, setSearchParams] = useSearchParams();
  const [path, setPath] = useState("");
  const start = useStartImport();

  const trimmed = path.trim();
  const conflict = start.error instanceof ImportConflictError;
  // A non-conflict error is a generic start failure.
  const genericError = start.isError && !conflict;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (trimmed.length === 0) {
      return; // Button is disabled too; guard the Enter key.
    }
    start.mutate(
      { path: trimmed },
      {
        onSuccess: (data) => {
          setSearchParams({ job: data.job_id });
        },
      },
    );
  }

  return (
    <section className="flex max-w-2xl flex-col gap-6" aria-label="Import music">
      <div className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Import music</h2>
        <p className="text-muted-foreground text-sm">
          Point beets at a folder on the server. It scans, matches each album
          against MusicBrainz, and imports what it finds.
        </p>
      </div>

      <form className="flex flex-col gap-3" onSubmit={onSubmit}>
        <label className="flex flex-col gap-2">
          <span className="text-sm font-medium">Folder path</span>
          <Input
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/music/incoming"
            aria-label="Folder path"
            aria-invalid={genericError || conflict}
          />
        </label>

        {conflict && (
          <p className="text-destructive text-sm" role="alert">
            An import is already running.{" "}
            {/* No job id in this error, so the link just returns to the page;
                a refresh with the active ?job= resumes the feed if present. */}
            <Link to="/import" className="underline underline-offset-4">
              View it
            </Link>
            .
          </p>
        )}
        {genericError && (
          <p className="text-destructive text-sm" role="alert">
            Couldn&rsquo;t start the import. Check the path and the backend, then
            try again.
          </p>
        )}

        <div>
          <Button type="submit" disabled={trimmed.length === 0 || start.isPending}>
            {start.isPending ? (
              <>
                <Loader2 className="animate-spin" aria-hidden="true" />
                Starting&hellip;
              </>
            ) : (
              <>
                <FolderInput aria-hidden="true" />
                Start import
              </>
            )}
          </Button>
        </div>
      </form>
    </section>
  );
}

/** The live run: polls the job and renders the phase-appropriate view. */
function ImportRun({ jobId }: { jobId: string }) {
  const { data, isPending, isError, refetch } = useImportJob(jobId);

  if (isPending) {
    return (
      <ImportShell>
        <p className="sr-only" role="status">
          Loading import&hellip;
        </p>
        <FeedSkeleton />
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

  if (data.phase === "failed") {
    return (
      <ImportShell>
        <JobFailed error={data.error} />
      </ImportShell>
    );
  }

  if (data.phase === "done") {
    return (
      <ImportShell>
        <JobDone summary={data.summary} albums={data.albums} />
      </ImportShell>
    );
  }

  // scanning / reviewing / applying: the live feed.
  return (
    <ImportShell>
      <LiveFeed state={data} />
    </ImportShell>
  );
}

/** Shared chrome for every run view: heading + a Start-over link. */
function ImportShell({ children }: { children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-6" aria-label="Import progress">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-2xl font-semibold tracking-tight">Import</h2>
        <Button variant="ghost" size="sm" asChild>
          <Link to="/import">Start over</Link>
        </Button>
      </div>
      {children}
    </section>
  );
}

/** scanning/reviewing/applying: a working line + the growing feed. */
function LiveFeed({ state }: { state: ImportJobState }) {
  const working = state.phase === "scanning" || state.phase === "applying";
  return (
    <div className="flex flex-col gap-4">
      <p
        className="text-muted-foreground flex min-h-5 items-center gap-2 text-sm"
        aria-live="polite"
      >
        {working && (
          <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
        )}
        <span>
          {/* No known total (the feed grows as the worker reads) — count what's
              applied + flag whether one album awaits a decision. */}
          {state.progress.applied}{" "}
          {state.progress.applied === 1 ? "album" : "albums"} imported
          {state.progress.needs_review > 0 && " · 1 album needs review"}
          {working && state.albums.length === 0 && "Scanning your folder…"}
        </span>
      </p>

      {state.albums.length === 0 ? (
        <FeedSkeleton />
      ) : (
        <ul className="border-border divide-border divide-y rounded-xl border">
          {state.albums.map((album) => (
            <li key={album.index}>
              <FeedRow album={album} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** One feed row. An `applied`/`skipped`/`decided` album is calm (a status
 * badge); the one `needs_review` row is highlighted and offers Review (→ the
 * seam). */
function FeedRow({ album }: { album: ImportAlbumSummary }) {
  const needsReview = album.status === "needs_review";
  const title = album.album ?? folderName(album.folder);
  return (
    <div
      className={cn(
        "flex min-w-0 items-center gap-3 px-4 py-3",
        needsReview && "bg-primary/5",
      )}
    >
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-medium">{title}</span>
        <span className="text-muted-foreground truncate text-sm">
          {album.artist ?? "Unknown artist"}
          <span aria-hidden="true"> · </span>
          {Math.round(album.confidence)}% · {album.recommendation}
        </span>
      </div>
      <StatusBadge status={album.status} />
      {needsReview && (
        <Button size="sm" asChild>
          <Link to={`/import/albums/${album.index}`}>Review</Link>
        </Button>
      )}
    </div>
  );
}

/** The status chip. Color + the text label both carry the state (not color
 * alone). `needs_review` reads "Needs review". */
function StatusBadge({ status }: { status: ImportAlbumSummary["status"] }) {
  const label: Record<ImportAlbumSummary["status"], string> = {
    applied: "Imported",
    decided: "Decided",
    skipped: "Skipped",
    needs_review: "Needs review",
  };
  const variant =
    status === "needs_review"
      ? "default"
      : status === "skipped"
        ? "outline"
        : "secondary";
  return (
    <Badge variant={variant} className="shrink-0">
      {label[status]}
    </Badge>
  );
}

/** Last path segment of a folder, for albums with no parsed album title. */
function folderName(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.at(-1) ?? folder;
}

/** done: a minimal summary + a link to the library (chunk 5 enriches this). */
function JobDone({
  summary,
  albums,
}: {
  summary: string | null;
  albums: ImportAlbumSummary[];
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="border-border flex flex-col items-center gap-3 rounded-xl border py-12 text-center">
        <FolderInput className="text-muted-foreground size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Import finished</p>
          <p className="text-muted-foreground text-sm">
            {summary ?? "Done."}
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to="/">View in library</Link>
        </Button>
      </div>
      {albums.length > 0 && (
        <ul className="border-border divide-border divide-y rounded-xl border">
          {albums.map((album) => (
            <li key={album.index}>
              <FeedRow album={album} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** failed: the worker's error + a way to start over. */
function JobFailed({ error }: { error: string | null }) {
  return (
    <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-destructive size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Import failed</p>
        <p className="text-muted-foreground text-sm">
          {error ?? "The import stopped unexpectedly."}
        </p>
      </div>
      <Button variant="outline" size="sm" asChild>
        <Link to="/import">Start over</Link>
      </Button>
    </div>
  );
}

/** Transient error fetching the job state (not the same as a failed import). */
function JobError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-destructive size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Couldn&rsquo;t load the import</p>
        <p className="text-muted-foreground text-sm">
          The backend didn&rsquo;t respond. Try again.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

/** A few feed-row placeholders so scanning (empty feed) doesn't look broken. */
function FeedSkeleton() {
  return (
    <div
      className="border-border divide-border divide-y rounded-xl border"
      aria-hidden="true"
    >
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-3 w-1/2" />
          </div>
          <Skeleton className="h-5 w-20 rounded-full" />
        </div>
      ))}
    </div>
  );
}
```

Note: `isTerminalPhase` is imported but the page branches on the concrete phases directly (clearer per-view rendering); the import keeps the symbol available for chunk 4/5 and documents intent. If `tsc`/lint flags the unused import in Step 2, drop it from the import list — the hook still exports it.

- [ ] **Step 2: Verify it typechecks**

Run (from `frontend/`):
```bash
npx tsc -b --noEmit
```
Expected: PASS. (If an unused-import error fires for `isTerminalPhase`, remove it from the `@/api/useImport` import line and re-run.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/import/ImportPage.tsx
git commit -m "feat(import): add the import flow page state machine"
```

---

### Task 7: Wire the routes + a header entry point

**Files:**
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/routing.test.tsx`

- [ ] **Step 1: Write the failing routing test**

In `frontend/src/routing.test.tsx`, add the import + the two routes to the mirrored table, and add the assertions.

Add to the imports:
```tsx
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { ImportPage } from "@/pages/import/ImportPage";
```

Add these two children to the `routes` array (before the `*` catch-all), matching `main.tsx`:
```tsx
      { path: "/import", element: <ImportPage /> },
      { path: "/import/albums/:index", element: <ImportCandidatePage /> },
```

Add these tests inside `describe("routing (artist spine)", ...)`:
```tsx
  test("/import renders the import entry page", async () => {
    renderAt("/import");
    expect(
      await screen.findByRole("heading", { level: 2, name: "Import music" }),
    ).toBeInTheDocument();
  });

  test("/import/albums/:index renders the review seam", async () => {
    renderAt("/import/albums/1");
    expect(
      await screen.findByText(/review screen coming soon/i),
    ).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `frontend/`):
```bash
npx vitest run src/routing.test.tsx -t "import"
```
Expected: FAIL — the new routes aren't in the test's `routes` table yet (the import statements you just added will error first if the page files exist but routes are missing → the assertions can't find the headings). Actually the failure is the two new assertions not finding their elements (the `*` catch-all renders NotFound).

- [ ] **Step 3: Add the routes to `main.tsx`**

In `frontend/src/main.tsx`, add the imports (alphabetically with the others):
```tsx
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { ImportPage } from "@/pages/import/ImportPage";
```

Add the two routes to the `children` array, immediately before `{ path: "*", element: <NotFoundPage /> }`:
```tsx
      { path: "/import", element: <ImportPage /> },
      { path: "/import/albums/:index", element: <ImportCandidatePage /> },
```

- [ ] **Step 4: Add the header entry point in `App.tsx`**

In `frontend/src/App.tsx`, add a `FolderInput` icon to the `lucide-react` import:
```tsx
import { CircleCheck, CircleSlash, FolderInput, Loader2, Search } from "lucide-react";
```
Then add an Import link in the header, between `<HeaderSearch />` and the health status `<div>` (the header's flex row). Replace this block:
```tsx
          <HeaderSearch />
          <div className="shrink-0">
            <HealthStatus />
          </div>
```
with:
```tsx
          <HeaderSearch />
          <nav className="shrink-0">
            <Link
              to="/import"
              className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex items-center gap-1.5 rounded-sm text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
            >
              <FolderInput className="size-4" aria-hidden="true" />
              {/* Label hides below sm to preserve header width, like the health
                  status; the icon + an aria-label carry it. */}
              <span className="hidden sm:inline" aria-label="Import">
                Import
              </span>
            </Link>
          </nav>
          <div className="shrink-0">
            <HealthStatus />
          </div>
```

- [ ] **Step 5: Run the routing test to verify it passes**

Run (from `frontend/`):
```bash
npx vitest run src/routing.test.tsx
```
Expected: PASS (the existing spine tests + the two new import-route tests).

- [ ] **Step 6: Verify the App test still passes (header changed)**

Run (from `frontend/`):
```bash
npx vitest run src/App.test.tsx
npx tsc -b --noEmit
```
Expected: PASS — **no `App.test.tsx` change needed.** Its assertions target the brand link ("MusicDrop" → "/"), the labelled searchbox inside the `search` landmark, and the *absence* of "Albums"/"Artists" links and a `navigation` named `/primary/i`. The added Import link sits in an **unnamed** `<nav>` with link text "Import" — it matches none of those negative queries (it's not "Albums"/"Artists", and the nav has no accessible name), so every existing assertion still holds.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/main.tsx frontend/src/App.tsx frontend/src/routing.test.tsx
git commit -m "feat(import): route /import + the review seam and add a header entry point"
```

---

### Task 8: ImportPage behavioral tests (entry, feed, states, errors)

**Files:**
- Create: `frontend/src/pages/import/ImportPage.test.tsx`

- [ ] **Step 1: Write the tests**

Create `frontend/src/pages/import/ImportPage.test.tsx`:
```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { ImportJobState } from "@/api/useImport";
import { ImportPage } from "@/pages/import/ImportPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const IMPORT_URL = `${window.location.origin}/api/import`;
const JOB_URL = `${window.location.origin}/api/import/job-1`;

function makeJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "job-1",
    phase: "reviewing",
    progress: { applied: 1, needs_review: 1 },
    albums: [
      {
        index: 0,
        folder: "/music/incoming/Radiohead - OK Computer",
        artist: "Radiohead",
        album: "OK Computer",
        recommendation: "strong",
        confidence: 99,
        status: "applied",
      },
      {
        index: 1,
        folder: "/music/incoming/Unknown Album",
        artist: "Radiohead",
        album: "Kid A",
        recommendation: "medium",
        confidence: 76,
        status: "needs_review",
      },
    ],
    summary: null,
    error: null,
    ...overrides,
  };
}

/** Render at a given URL so `useSearchParams` (the `?job=` seam) resolves. */
function renderAt(route: string) {
  return renderWithProviders(<ImportPage />, { route, path: "/import" });
}

describe("ImportPage — entry", () => {
  test("shows the path input + Start when there is no active job", () => {
    renderAt("/import");
    expect(
      screen.getByRole("heading", { name: "Import music" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Folder path")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /start import/i }),
    ).toBeInTheDocument();
  });

  test("Start is disabled until a path is typed (blank-path guard)", async () => {
    const user = userEvent.setup();
    renderAt("/import");

    const button = screen.getByRole("button", { name: /start import/i });
    expect(button).toBeDisabled();

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    expect(button).toBeEnabled();
  });

  test("starting posts the path and flips to the feed via ?job=", async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(IMPORT_URL, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({ job_id: "job-1" }, { status: 202 });
      }),
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ phase: "scanning", albums: [] }))),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    // The job query now drives the page (heading switches to "Import").
    expect(
      await screen.findByRole("heading", { name: "Import" }),
    ).toBeInTheDocument();
    expect(seenBody).toEqual({ path: "/music/incoming" });
  });

  test("a 409 surfaces 'already running' without flipping away", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json({ detail: "An import is already running" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    expect(
      await screen.findByText(/an import is already running/i),
    ).toBeInTheDocument();
    // Still on the entry screen (no ?job=, so the input is still shown).
    expect(screen.getByLabelText("Folder path")).toBeInTheDocument();
  });
});

describe("ImportPage — live feed", () => {
  test("renders applied + the current needs_review row from the job", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    // Both albums show; the strong one is calm-applied, the medium one needs
    // review.
    expect(await screen.findByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Kid A")).toBeInTheDocument();
    expect(screen.getByText("Imported")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
  });

  test("the Review affordance links to the right album index", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    const review = await screen.findByRole("link", { name: /review/i });
    // index 1 is the needs_review album.
    expect(review).toHaveAttribute("href", "/import/albums/1");
  });

  test("shows a scanning cue while the feed is still empty", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ phase: "scanning", progress: { applied: 0, needs_review: 0 }, albums: [] }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText(/scanning your folder/i)).toBeInTheDocument();
  });
});

describe("ImportPage — terminal states", () => {
  test("done shows the summary + a View-in-library link", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            summary: "2 imported, 0 skipped",
            progress: { applied: 2, needs_review: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    expect(screen.getByText("2 imported, 0 skipped")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /view in library/i });
    expect(link).toHaveAttribute("href", "/");
  });

  test("failed shows the error message + a start-over link", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ phase: "failed", error: "lookup exploded", albums: [] }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(screen.getByText("lookup exploded")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /start over/i }),
    ).toBeInTheDocument();
  });

  test("a transient job-fetch error shows a retry", async () => {
    server.use(http.get(JOB_URL, () => new HttpResponse(null, { status: 500 })));
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText(/couldn.t load the import/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the tests to verify they pass**

Run (from `frontend/`):
```bash
npx vitest run src/pages/import/ImportPage.test.tsx
```
Expected: PASS (all entry / feed / terminal-state tests).

Note on the polling tests: these use single-shot or per-call msw handlers and assert the first settled render — they don't wait through multiple poll ticks (TanStack's `refetchInterval` keeps polling reviewing/scanning, which is fine; the assertions match the first response). The terminal-aware stop is already unit-tested in Task 3.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/import/ImportPage.test.tsx
git commit -m "test(import): cover the import page entry, feed, and terminal states"
```

---

### Task 9: Full green + live screenshot verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full frontend test suite**

Run (from `frontend/`):
```bash
npm run test
```
Expected: PASS — all suites green, including the new `useImport`, `ImportPage`, and updated `routing` tests, and the pre-existing search/artists/albums/App tests.

- [ ] **Step 2: Typecheck the whole project**

Run (from `frontend/`):
```bash
npm run typecheck
```
Expected: PASS — zero type errors (all import types resolve from the generated schema).

- [ ] **Step 3: Build to confirm the production bundle compiles**

Run (from `frontend/`):
```bash
npm run build
```
Expected: PASS (`tsc -b` then `vite build` with no errors).

- [ ] **Step 4: Live screenshot — the import shell end to end**

Start the backend (from `backend/`, a background terminal): `uv run uvicorn app.main:app --port 3030`.
Start the frontend (from `frontend/`, a background terminal): `npm run dev` (Vite on `:5173`, proxying `/api` → `:3030`).

In the browser (or the chrome-devtools MCP), confirm and screenshot:
- `http://localhost:5173/import` — the entry screen (path input + Start; the header shows the Import link).
- After starting against a real incoming folder (or by manually navigating to `/import?job=<id>` with a running import) — the live feed: applied rows calm, the one needs-review row highlighted with a Review button; clicking Review lands on `/import/albums/:index` (the stub "Review screen coming soon").
- The done state (summary + View in library) once the import finishes.

Expected: each state renders in the app's design language with no console errors. (No commit — verification step. If the backend has no configured library/incoming folder, the `/import?job=` states can be exercised against the chunk-2 fake-runner path or by pointing at a small test folder.)

---

## Self-review checklist

Run this against the chunk-3 scope with fresh eyes after the plan is written:

**1. Spec coverage** — every chunk-3 requirement maps to a task:
- Regenerate API types + verify import contract → **Task 1**.
- `useStartImport` (POST) → **Task 2**; `useImportJob` polling that stops at terminal → **Task 3**; `useImportCandidate` (enabled-gated) + `useSubmitChoice` (invalidates job; 404/409-tolerant) → **Task 4**.
- Entry (path + Start, 409 + blank-path) → **Task 6** (`ImportEntry`) + **Task 8** tests. Scanning working state → **Task 6** (`LiveFeed` working cue) + **Task 8**. Reviewing/live feed (applied calm + the one needs_review highlighted with Review) → **Task 6** (`FeedRow`/`StatusBadge`) + **Task 8**. Done (summary + View in library) → **Task 6** (`JobDone`) + **Task 8**. Failed (error + start over) → **Task 6** (`JobFailed`) + **Task 8**.
- Routing `/import` + the seam route + header entry point → **Task 7** (+ `routing.test.tsx` mirror).
- The chunk-4 seam (route-based, stubbed destination) → **Task 5** + the Review link in **Task 6** + the route in **Task 7**.
- Tests (entry→start, polling renders feed, applied + needs_review, Review target, done/failed/error, 409/422) → **Task 8** (+ hook-level tests in Tasks 2–4).

**2. Placeholder scan** — no `TBD`/`TODO`/"handle errors"/"similar to Task N"/bare prose-only code steps. Every code step shows complete, runnable code. The seam destination is a deliberate, fully-written stub (named as such), not a placeholder gap.

**3. Type consistency** — symbol names line up across tasks: `useStartImport`, `useImportJob`, `useImportCandidate`, `useSubmitChoice`, `isTerminalPhase`, `ImportConflictError`, `SubmitChoiceArgs` (defined in Task 4, used by `useSubmitChoice`); `ImportJobState`/`ImportAlbumSummary`/`ImportPhase`/`ImportAlbumStatus`/`StartImportRequest`/`StartImportResponse`/`Candidate`/`ImportChoice` all re-exported from `useImport.ts` as `components["schemas"][...]` (Task 2/3/4) and consumed by `ImportPage.tsx` (Task 6) + tests (Task 8). The page component is `ImportPage` and the stub is `ImportCandidatePage`, matching the route table in `main.tsx` + `routing.test.tsx` (Task 7). Status-badge labels cover all four `ImportAlbumStatus` values. **All import types originate from the generated `schema.d.ts` — none are hand-written (CLAUDE.md rule 2).**

**4. Convention consistency** — hooks mirror `useSearch.ts`/`useAlbum.ts` (per-resource module, `client.GET/POST`, query-key arrays, `enabled`, `Error` subclass for a branchable status, `retry: false`). Page states reuse the exact idiom (`section`/`h2`/`aria-live` count line, `sr-only role="status"` loading, the destructive error block + Retry, `border-dashed` empties, `BackLink`). Tests follow the msw pattern (`server.use(http.get/post(URL, ...))`, `renderWithProviders(ui, { route, path })`, `URL = ${origin}/api/...`, `findBy*` for async). The route is added to BOTH `main.tsx` and `routing.test.tsx`.

**5. Polling correctness** — `refetchInterval` is a function reading `query.state.data?.phase`; returns `IMPORT_POLL_MS` while scanning/reviewing/applying (or before first data) and `false` once done/failed (TanStack v5 stops on a falsy interval). Unit-tested in Task 3 ("polls while active and stops at a terminal phase").

---

## Out of scope (chunk 3 does NOT build these)

- **The candidate-review SCREEN (chunk 4).** The before/after album panels, the full track diff, missing/unmatched rows, the candidate switcher, and the choice action buttons. Chunk 3 ships the *seam* (the `/import/albums/:index` route + a stub page) and the *hooks* (`useImportCandidate`, `useSubmitChoice`) it will consume — not the screen itself.
- **The rich apply/done UX + real end-to-end audio import (chunk 5).** Chunk 3's done state is intentionally minimal (the `summary` string + a "View in library" link). Per-album landing detail, an enriched summary, and the full live-import-against-real-files screenshot walkthrough are chunk 5.
- **Watched-folder auto-import and the Settings/config UI (later specs).** No folder browser/picker (the entry is a plain server-path text input, matching the chunk-2 API), no import-option toggles (copy/move/autotag come from the user's beets config), no config editing.
- **Enter-MusicBrainz-id / search-again choice actions.** The `ImportAction` enum here is `apply|skip|asis|astracks|abort` (the chunk-2 contract); `enter_id`/`search` are a later chunk per the design.
- **SSE/websocket progress transport.** v1 is polling (`refetchInterval`); a streaming transport is a later optimization.
- **Singletons (`-s`) / advanced import modes, abort UI affordance.** The `abort` action exists in the contract and `useSubmitChoice` can send it, but no dedicated Abort button is part of the chunk-3 shell (it belongs with the chunk-4 review actions).
