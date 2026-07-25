import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import { useDiskSyncStatus } from "@/api/useDiskSync";

vi.mock("@/api/client", () => ({ client: { GET: vi.fn(), POST: vi.fn() } }));

const idle = {
  phase: "idle", job_id: null, total: 0, processed: 0, removed: 0, updated: 0,
  unchanged: 0, read_errors: 0, emptied_albums: 0, current: null, error: null,
  failures: [],
};

describe("useDiskSyncStatus", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps the last running snapshot when a poll hiccups (never fabricates idle)", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const running = { ...idle, phase: "running", job_id: "d1", total: 100, processed: 20 };
    const getMock = client.GET as ReturnType<typeof vi.fn>;
    getMock.mockResolvedValue({ data: running, error: undefined, response: { ok: true } });
    const w = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children);
    const { result } = renderHook(() => useDiskSyncStatus(), { wrapper: w });
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
    expect(result.current.data?.job_id).toBe("d1");
  });

  it("falls back to idle on a cold first load with nothing cached", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: undefined, error: undefined, response: { ok: false, status: 500 },
    });
    const w = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children);
    const { result } = renderHook(() => useDiskSyncStatus(), { wrapper: w });
    await waitFor(() => expect(result.current.data?.phase).toBe("idle"));
  });
});
