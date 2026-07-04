import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import {
  ImportConflictError,
  ImportJobNotFoundError,
  ImportStartRejectedError,
  useDuplicatePrompt,
  useImportCandidate,
  useImportJob,
  usePauseSweep,
  useResolveImportDuplicate,
  useStartImport,
  useSubmitChoice,
} from "@/api/useImport";
import type { Candidate, ImportJobState } from "@/api/useImport";
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
    progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0 },
    albums: [],
    summary: null,
    error: null,
    origin: "manual",
    set_aside: 0,
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
        progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
      }),
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
