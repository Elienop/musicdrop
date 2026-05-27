import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import { ImportConflictError, useImportJob, useStartImport } from "@/api/useImport";
import type { ImportJobState } from "@/api/useImport";
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
});
