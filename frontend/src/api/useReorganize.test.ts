import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import {
  usePreviewReorganize,
  useReorganizeStatus,
  useStartReorganize,
} from "@/api/useReorganize";

vi.mock("@/api/client", () => ({ client: { GET: vi.fn(), POST: vi.fn() } }));

const idle = {
  phase: "idle", job_id: null, scope: null, total: 0, processed: 0, moved: 0,
  skipped: 0, failed: 0, orphans_trashed: 0, current: null, error: null, artist: null,
  album_id: null, scope_label: "library", failures: [], finished_at: null,
};

describe("useReorganizeStatus", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps the last running snapshot when a poll hiccups (never fabricates idle)", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const running = { ...idle, phase: "running", job_id: "r1", total: 50, processed: 10 };
    const getMock = client.GET as ReturnType<typeof vi.fn>;
    getMock.mockResolvedValue({ data: running, error: undefined, response: { ok: true } });
    const w = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children);
    const { result } = renderHook(() => useReorganizeStatus(), { wrapper: w });
    await waitFor(() => expect(result.current.data?.phase).toBe("running"));

    // A 502 mid-run must NOT overwrite the running snapshot with a fabricated
    // idle — that would stop the 1s poll for good and hide a job still running.
    getMock.mockClear();
    getMock.mockResolvedValue({
      data: undefined, error: undefined, response: { ok: false, status: 502 },
    });
    await result.current.refetch();
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(result.current.data?.phase).toBe("running");
    expect(result.current.data?.job_id).toBe("r1");
  });

  it("falls back to idle on a cold first load with nothing cached", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: undefined, error: undefined, response: { ok: false, status: 500 },
    });
    const w = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children);
    const { result } = renderHook(() => useReorganizeStatus(), { wrapper: w });
    await waitFor(() => expect(result.current.data?.phase).toBe("idle"));
  });
});

// The store-layout refusal (503) is the reorganize failure this round made
// reachable, and its sentence says what to fix — a Trash folder that is not
// below the music root, named by path (the 400/401/403 guards reach the same
// arm with their own sentences). It lands in the same inline slot as every
// other reorganize error (`ReorganizeControl.onActionError` renders
// `error.message` verbatim), so the Error's message has to BE the sentence.
// Pinned whole with `toBe`, never by fragment: a fragment match lets the
// meaning be reversed with the test green.
const REFUSAL =
  "Trash folder is not below the music root: '/tmp/elsewhere'. Point it inside your music folder.";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

/** A failed openapi-fetch result: `error` is the parsed body, `data` undefined. */
function answer(status: number, error: unknown) {
  return { data: undefined, error, response: { ok: false, status } };
}

describe("usePreviewReorganize", () => {
  beforeEach(() => vi.clearAllMocks());

  it("surfaces the server's refusal sentence on the library preview", async () => {
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue(answer(503, { detail: REFUSAL }));
    const { result } = renderHook(() => usePreviewReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "library" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(REFUSAL);
  });

  it("surfaces the server's refusal sentence on the album preview", async () => {
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue(answer(503, { detail: REFUSAL }));
    const { result } = renderHook(() => usePreviewReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "album", albumId: 7 });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(REFUSAL);
  });

  it("keeps its own message when the preview fails with no detail", async () => {
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue(answer(502, undefined));
    const { result } = renderHook(() => usePreviewReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "library" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("Failed to build preview");
  });

  // Arm order: the 404 check stands above the generic one, so a vanished album
  // still reads as one.
  it("keeps the not-found message for an album that is gone", async () => {
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue(
      answer(404, { detail: "No album has that id." }),
    );
    const { result } = renderHook(() => usePreviewReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "album", albumId: 7 });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("Album not found");
  });
});

describe("useStartReorganize", () => {
  beforeEach(() => vi.clearAllMocks());

  it("surfaces the server's refusal sentence on the library start", async () => {
    (client.POST as ReturnType<typeof vi.fn>).mockResolvedValue(answer(503, { detail: REFUSAL }));
    const { result } = renderHook(() => useStartReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "library" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(REFUSAL);
  });

  it("surfaces the server's refusal sentence on the album start", async () => {
    (client.POST as ReturnType<typeof vi.fn>).mockResolvedValue(answer(503, { detail: REFUSAL }));
    const { result } = renderHook(() => useStartReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "album", albumId: 7 });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(REFUSAL);
  });

  it("keeps its own message when the start fails with no detail", async () => {
    (client.POST as ReturnType<typeof vi.fn>).mockResolvedValue(answer(500, undefined));
    const { result } = renderHook(() => useStartReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "library" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("Failed to start reorganize");
  });

  // Arm order again: a 409 is the busy case, and its remedy is to wait.
  it("keeps the busy message when the library holds a job", async () => {
    (client.POST as ReturnType<typeof vi.fn>).mockResolvedValue(
      answer(409, { detail: "A reorganize is already running." }),
    );
    const { result } = renderHook(() => useStartReorganize(), { wrapper: wrapper() });
    result.current.mutate({ scope: "library" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("A library operation is in progress");
  });
});
