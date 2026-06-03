import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

import { client } from "@/api/client";
import { useLyricsCoverage } from "@/api/useLyricsBackfill";

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
