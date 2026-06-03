import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

import { client } from "@/api/client";
import {
  useLyricsBackfillStatus,
  useLyricsCoverage,
  useStopLyricsBackfill,
} from "@/api/useLyricsBackfill";

vi.mock("@/api/client", () => ({ client: { GET: vi.fn(), POST: vi.fn() } }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

describe("useLyricsCoverage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fetches coverage", async () => {
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { total: 10, with_lyrics: 7, percent: 70 }, error: undefined, response: { ok: true },
    });
    const { result } = renderHook(() => useLyricsCoverage(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ total: 10, with_lyrics: 7, percent: 70 });
  });
});

const idleStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
  not_found: 0, failed: 0, skipped: 0, current: null,
  writes_enabled: false, error: null, album_id: null, scope_label: "library",
};

describe("useLyricsBackfillStatus", () => {
  beforeEach(() => vi.clearAllMocks());

  it("stops polling once the job is no longer running", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: idleStatus, error: undefined, response: { ok: true },
    });
    const statusWrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children);
    const { result } = renderHook(() => useLyricsBackfillStatus(), { wrapper: statusWrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const query = qc.getQueryCache().find({ queryKey: ["lyrics", "backfill"] });
    const options = query?.options as unknown as {
      refetchInterval: (q: unknown) => number | false;
    };
    expect(typeof options.refetchInterval).toBe("function");
    // idle (current cache state) -> no polling
    expect(options.refetchInterval(query)).toBe(false);
  });
});

describe("useStopLyricsBackfill", () => {
  beforeEach(() => vi.clearAllMocks());

  it("invalidates coverage on a successful stop", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, "invalidateQueries");
    (client.POST as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: idleStatus, error: undefined, response: { ok: true },
    });
    const stopWrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children);
    const { result } = renderHook(() => useStopLyricsBackfill(), { wrapper: stopWrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["lyrics", "coverage"] });
  });
});
