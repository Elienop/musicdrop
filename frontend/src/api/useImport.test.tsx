import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import {
  CandidateNotFoundError,
  ImportConflictError,
  ImportJobNotFoundError,
  ImportStartRejectedError,
  ImportUnavailableError,
  startErrorSentence,
  useDuplicatePrompt,
  useImportCandidate,
  useImportJob,
  usePauseSweep,
  useResolveImportDuplicate,
  useStartImport,
  useSubmitChoice,
} from "@/api/useImport";
import type {
  Candidate,
  ImportAlbumSummary,
  ImportJobState,
} from "@/api/useImport";
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

  // One status, three server reasons (a running import, a held beets swap
  // lock, a running backfill / disk sync), so the sentence has to come from the
  // server — a class-only error made every caller pick one and be wrong for the
  // other two.
  test("a 409 carries the server's own reason; a bodyless one falls back", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "A library backfill is in progress; import available when it finishes" },
          { status: 409 },
        ),
      ),
    );
    const { result } = renderHook(() => useStartImport(), { wrapper: wrapper() });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({
      name: "ImportConflictError",
      message: "A library backfill is in progress; import available when it finishes",
    });

    server.use(http.post(IMPORT_URL, () => new HttpResponse(null, { status: 409 })));
    const bodyless = renderHook(() => useStartImport(), { wrapper: wrapper() });
    bodyless.result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(bodyless.result.current.isError).toBe(true));
    expect(bodyless.result.current.error).toMatchObject({
      name: "ImportConflictError",
      message: "The library is busy. Try again shortly.",
    });
  });

  // 503 is the refused store layout: a retry cannot succeed until the layout
  // changes, so it must not land in the generic "try again" arm.
  test("a 503 becomes ImportUnavailableError with the refusal's sentence", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "the music folder is not mounted; imports are refused" },
          { status: 503 },
        ),
      ),
    );
    const { result } = renderHook(() => useStartImport(), { wrapper: wrapper() });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ImportUnavailableError);
    expect(result.current.error).toMatchObject({
      message: "the music folder is not mounted; imports are refused",
    });
  });

  // Our route's 503 always carries a sentence, so a bodyless one is somebody
  // else's — a proxy answering for a restarting container — and there a retry
  // IS the right advice. It must not wear the refusal class, whose whole point
  // is that retrying cannot help.
  test("a bodyless 503 is the generic failure, not ImportUnavailableError", async () => {
    server.use(
      http.post(IMPORT_URL, () => new HttpResponse(null, { status: 503 })),
    );
    const { result } = renderHook(() => useStartImport(), { wrapper: wrapper() });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).not.toBeInstanceOf(ImportUnavailableError);
    expect(result.current.error).toMatchObject({
      message: "Failed to start import",
    });
  });

  // The same sender, with an HTML body — what a reverse proxy actually returns.
  // openapi-fetch keeps a non-JSON body as a string, which detailMessage cannot
  // read, so this is the case the old class fallback was written for.
  test("an HTML-bodied 503 (a proxy's) is the generic failure too", async () => {
    server.use(
      http.post(
        IMPORT_URL,
        () =>
          new HttpResponse("<html><body>503 Service Unavailable</body></html>", {
            status: 503,
            headers: { "Content-Type": "text/html" },
          }),
      ),
    );
    const { result } = renderHook(() => useStartImport(), { wrapper: wrapper() });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).not.toBeInstanceOf(ImportUnavailableError);
    expect(result.current.error).toMatchObject({
      message: "Failed to start import",
    });
  });
});

// The sentence every start surface shares. Only the generic half differs
// between call sites, and it is the one the caller passes in.
describe("startErrorSentence", () => {
  // The server's details carry no terminal punctuation and every client
  // sentence does, so the join is normalised here rather than in the 47
  // backend strings — a carried sentence gains a full stop when it ends in
  // none of its own.
  test("refusals carry the server's reason, ended with a full stop", () => {
    expect(
      startErrorSentence(new ImportConflictError("a backfill is running"), true, "G"),
    ).toBe("a backfill is running.");
    expect(
      startErrorSentence(new ImportStartRejectedError("that folder is in your library"), true, "G"),
    ).toBe("that folder is in your library.");
    expect(
      startErrorSentence(new ImportUnavailableError("the layout is refused"), true, "G"),
    ).toBe("the layout is refused.");
    expect(startErrorSentence(new Error("socket hang up"), true, "G")).toBe("G");
    expect(startErrorSentence(null, false, "G")).toBeNull();
  });

  test("a sentence that already ends in punctuation is left alone", () => {
    // The class fallbacks and every other client sentence are already ended;
    // a second full stop would read as a typo.
    expect(
      startErrorSentence(new ImportConflictError(null), true, "G"),
    ).toBe("The library is busy. Try again shortly.");
    expect(
      startErrorSentence(new ImportStartRejectedError("is the disk full?"), true, "G"),
    ).toBe("is the disk full?");
  });
});

describe("startImport 422 surfacing", () => {
  test("a string-detail 422 (the in-library guard) throws ImportStartRejectedError with that text", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "In-library sources must move; copy would duplicate files" },
          { status: 422 },
        ),
      ),
    );

    const { result } = renderHook(() => useStartImport(), {
      wrapper: wrapper(),
    });
    result.current.mutate({ path: "/library" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ImportStartRejectedError);
    expect(result.current.error).toMatchObject({
      name: "ImportStartRejectedError",
      message: expect.stringMatching(/must move/) as string,
    });
  });

  test("an array-detail 422 (FastAPI validation) still throws with a human message", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: [{ loc: ["body", "path"], msg: "Field required", type: "missing" }] },
          { status: 422 },
        ),
      ),
    );

    const { result } = renderHook(() => useStartImport(), {
      wrapper: wrapper(),
    });
    result.current.mutate({ path: "" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({
      name: "ImportStartRejectedError",
      message: "Field required",
    });
  });
});

const PAUSE_URL = `${window.location.origin}/api/import/j1/pause`;

describe("usePauseSweep", () => {
  test("posts the pause and resolves on 204", async () => {
    let hits = 0;
    server.use(
      http.post(PAUSE_URL, () => {
        hits += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { result } = renderHook(() => usePauseSweep("j1"), {
      wrapper: wrapper(),
    });
    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(hits).toBe(1);
  });

  test("409/404 (sweep already over) resolve quietly — the refetch shows done", async () => {
    server.use(
      http.post(PAUSE_URL, () =>
        HttpResponse.json({ detail: "only a sweep import can be paused" }, { status: 409 }),
      ),
    );

    const { result } = renderHook(() => usePauseSweep("j1"), {
      wrapper: wrapper(),
    });
    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });
});

const JOB_URL = `${window.location.origin}/api/import/job-1`;

function makeJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "job-1",
    phase: "reviewing",
    progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
    albums: [],
    error: null,
    origin: "manual",
    set_aside: 0,
    elapsed_seconds: 0,
    // Default: the worker is NOT blocked. Only the registry knows, so a test
    // that means "parked on a person" says so here rather than by adding a
    // set-aside feed row — a row status cannot answer this (an unattended
    // duplicate and a `search` re-lookup both wear one while beets works).
    awaiting_decision: false,
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
      makeJob({
        phase: "scanning",
        progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
      }),
      makeJob({ phase: "done" }),
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
    // The poll fires at IMPORT_POLL_MS (1000ms), so allow more than waitFor's
    // 1000ms default for the second fetch to land.
    await waitFor(() => expect(result.current.data?.phase).toBe("done"), {
      timeout: 3000,
    });
    const callsAtDone = calls;
    // Once done, polling stops: give it time and assert no further fetches.
    await act(() => new Promise((r) => setTimeout(r, 1500)));
    expect(calls).toBe(callsAtDone);
  });

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
    // A 404 is terminal — polling must stop (no self-heal). Give the poll
    // interval time and assert no further fetches.
    const callsAtError = calls;
    await act(() => new Promise((r) => setTimeout(r, 1500)));
    expect(calls).toBe(callsAtError);
  });
});

/** One feed row in the given status; only `status` matters to the cadence. */
function feedRow(status: ImportAlbumSummary["status"]): ImportAlbumSummary {
  return {
    index: 0,
    folder: "/music/incoming/Kid A",
    artist: "Radiohead",
    album: "Kid A",
    recommendation: "medium",
    confidence: 76,
    status,
    album_id: null,
    did_not_land: false,
  };
}

/** The cadence the live cache is actually driving: call the query's own
 * `refetchInterval` with the cached query, the way useLyricsBackfill's test
 * pins its own. Reading the option directly is the only way to tell 1s from
 * 10s without sitting through a wall-clock wait. */
async function pollIntervalFor(job: ImportJobState): Promise<number | false> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  server.use(http.get(JOB_URL, () => HttpResponse.json(job)));
  const localWrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useImportJob("job-1"), {
    wrapper: localWrapper,
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  const query = queryClient
    .getQueryCache()
    .find({ queryKey: ["import", "job", "job-1"] });
  const options = query?.options as unknown as {
    refetchInterval: (q: unknown) => number | false;
  };
  expect(typeof options.refetchInterval).toBe("function");
  return options.refetchInterval(query);
}

describe("useImportJob poll cadence", () => {
  test("stays fast whenever the worker is the one working", async () => {
    // Nothing scanned yet.
    expect(
      await pollIntervalFor(
        makeJob({
          phase: "scanning",
          progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
          albums: [],
        }),
      ),
    ).toBe(1000);
    // The regression the phase can't see: `phase` latches to "reviewing" at the
    // first parked album and never returns to "scanning", so a run that is
    // scanning album 2 after one decision still reads "reviewing". Nobody is
    // being waited on, so the feed is live and the cadence must stay fast.
    expect(
      await pollIntervalFor(
        makeJob({
          phase: "reviewing",
          progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
          albums: [feedRow("applied")],
        }),
      ),
    ).toBe(1000);
  });

  // The two states a row status gets WRONG, and the reason the cadence reads
  // the registry's own `awaiting_decision` instead of the feed. Both are real
  // and both are the owner's own path: an UNATTENDED run (the slskd inbox)
  // emits `needs_dup_resolution` and skips on without parking, and a `search`
  // re-lookup keeps its row `needs_review` while beets queries MusicBrainz —
  // the multi-minute operation this whole slice exists to surface. Reading the
  // row would drop the page to a 10s poll for a decision nobody is ever asked
  // for.
  test.each(["needs_review", "needs_dup_resolution"] as const)(
    "stays fast when a set-aside row (%s) is NOT blocking the worker",
    async (status) => {
      expect(
        await pollIntervalFor(
          makeJob({
            phase: "reviewing",
            progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
            albums: [feedRow(status)],
            awaiting_decision: false,
          }),
        ),
      ).toBe(1000);
    },
  );

  // beets runs serially (`config["threaded"] = False`) and a blocking park sits
  // in `reply.get()`, so nothing but the elapsed clock can change until the
  // operator answers. The measured defect was 60 requests a minute for the
  // three minutes one decision took.
  //
  // The feed row is `applied`, so row-based inference would call this WORKING
  // and stay at 1s — the flag is the only thing that can express this state,
  // and a revert to reading rows cannot pass.
  test("backs off to 10s while the worker is blocked on a person", async () => {
    expect(
      await pollIntervalFor(
        makeJob({
          phase: "scanning",
          progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
          albums: [feedRow("applied")],
          awaiting_decision: true,
        }),
      ),
    ).toBe(10000);
  });

  // ...but an EMPTY feed is exempt. `park()` buffers a park whose row does not
  // exist yet, so the page is blocked with nothing on screen to act on and the
  // row-creating outcome is already queued: exactly one poll separates the user
  // from the decision panel, and the backoff would make it 10s. The owner's own
  // unattended inbox path.
  test("stays fast when the feed is still empty, blocked or not", async () => {
    expect(
      await pollIntervalFor(
        makeJob({
          phase: "scanning",
          progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
          albums: [],
          awaiting_decision: true,
        }),
      ),
    ).toBe(1000);
  });

  test("stops entirely once the phase is terminal", async () => {
    expect(
      await pollIntervalFor(
        makeJob({ phase: "done" }),
      ),
    ).toBe(false);
  });

  // Guard ORDER, not just the guards: the terminal check must come FIRST. A run
  // that dies while an album is parked is terminal with a decision outstanding
  // (`_on_error` sets `failed` without touching the parked set), and if the
  // backoff branch ran first it would return 10s and poll a dead job forever.
  // Belt AND braces: today's server also zeroes the flag off an active phase,
  // but the client must not depend on the other side's guard for the stop.
  test("stops on a terminal job even with a decision outstanding", async () => {
    expect(
      await pollIntervalFor(
        makeJob({
          phase: "failed",
          error: "the session died",
          albums: [feedRow("needs_review")],
          awaiting_decision: true,
        }),
      ),
    ).toBe(false);
  });
});

const CANDIDATE_URL = `${window.location.origin}/api/import/job-1/albums/1`;
const CHOICE_URL = `${window.location.origin}/api/import/job-1/albums/1/choice`;

describe("useImportCandidate", () => {
  test("fetches the candidate when enabled", async () => {
    const candidate: Candidate = {
      confidence: 75.5,
      recommendation: "medium",
      data_source: "MusicBrainz",
      data_url: "https://mb/a1",
      cover_after_url: "https://coverartarchive.org/release/a1/front-500",
      has_current_art: false,
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
      search_revision: 0,
    };
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(candidate)));

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

  test("maps a 404 to CandidateNotFoundError and stops polling", async () => {
    // Mirror useImportJob's 404 discrimination: a no-longer-parked album is a
    // distinct, terminal state (not a generic transport failure), and even a
    // polling caller must stop hammering the 404.
    let calls = 0;
    server.use(
      http.get(CANDIDATE_URL, () => {
        calls += 1;
        return HttpResponse.json(
          { detail: "Import album not found" },
          { status: 404 },
        );
      }),
    );

    // Pass a live poll interval (as the review screen does while searching) to
    // prove the 404 stops the loop rather than keeping it alive.
    const { result } = renderHook(
      () => useImportCandidate("job-1", 1, true, 700),
      { wrapper: wrapper() },
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(CandidateNotFoundError);
    // A 404 is terminal — the poll must stop. Give the interval time and assert
    // no further fetches.
    const callsAtError = calls;
    await act(() => new Promise((r) => setTimeout(r, 1500)));
    expect(calls).toBe(callsAtError);
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

  test("a bodyless 5xx rejects (no silent success on a no-undo action)", async () => {
    // A gateway-style empty-body 500 leaves openapi-fetch's `error` undefined;
    // the hook must still reject (guarding on !response.ok), so the review
    // screen surfaces the failure rather than navigating away as if it landed.
    server.use(
      http.post(CHOICE_URL, () => new HttpResponse(null, { status: 500 })),
    );

    const { result } = renderHook(() => useSubmitChoice("job-1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ index: 1, choice: { action: "skip" } });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

const DUPLICATE_URL = `${window.location.origin}/api/import/job-1/albums/0/duplicate`;

describe("useDuplicatePrompt / useResolveImportDuplicate", () => {
  test("fetches the duplicate prompt when enabled", async () => {
    server.use(
      http.get(DUPLICATE_URL, () =>
        HttpResponse.json({
          album_index: 0,
          incoming: {
            album_artist: "Radiohead",
            album: "In Rainbows",
            year: 2007,
            track_count: 10,
            format: "FLAC",
            bitrate_kbps: 900,
            folder: "/incoming",
            has_current_art: false,
          },
          existing: [
            {
              album_id: 1,
              album_artist: "Radiohead",
              album: "In Rainbows",
              year: 2007,
              track_count: 9,
              format: "MP3",
              bitrate_kbps: 320,
              folder: "/music",
            },
          ],
        }),
      ),
    );

    const { result } = renderHook(
      () => useDuplicatePrompt("job-1", 0, true),
      { wrapper: wrapper() },
    );

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data?.existing[0].album_id).toBe(1);
  });

  test("is disabled when enabled=false (no request)", () => {
    const { result } = renderHook(
      () => useDuplicatePrompt("job-1", 0, false),
      { wrapper: wrapper() },
    );
    expect(result.current.fetchStatus).toBe("idle");
  });

  test("resolves a duplicate and swallows a 409", async () => {
    // 409 means a decision already landed — it resolves quietly (no throw); the
    // caller refetches the job to resync.
    server.use(
      http.post(DUPLICATE_URL, () =>
        HttpResponse.json({ detail: "already resolved" }, { status: 409 }),
      ),
    );

    const { result } = renderHook(() => useResolveImportDuplicate("job-1"), {
      wrapper: wrapper(),
    });
    await act(async () => {
      await result.current.mutateAsync({
        index: 0,
        decision: { action: "skip_new" },
      });
    });

    expect(result.current.isError).toBe(false);
  });

  test("POSTs the decision to the album index", async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(DUPLICATE_URL, async ({ request }) => {
        seenBody = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { result } = renderHook(() => useResolveImportDuplicate("job-1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ index: 0, decision: { action: "replace" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seenBody).toEqual({ action: "replace" });
  });

  test("a bodyless 5xx rejects (no silent success)", async () => {
    server.use(
      http.post(DUPLICATE_URL, () => new HttpResponse(null, { status: 500 })),
    );

    const { result } = renderHook(() => useResolveImportDuplicate("job-1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ index: 0, decision: { action: "skip_new" } });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
