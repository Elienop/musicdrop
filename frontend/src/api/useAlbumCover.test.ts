import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { useFetchAlbumCover } from "@/api/useAlbumCover";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

describe("useFetchAlbumCover", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("returns found=false on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 404 }));
    const { result } = renderHook(() => useFetchAlbumCover(7), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ found: false });
  });

  it("returns the blob + source on 200", async () => {
    const body = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, { status: 200, headers: { "X-Art-Source": "Cover Art Archive" } }),
    );
    const { result } = renderHook(() => useFetchAlbumCover(7), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({ found: true, source: "Cover Art Archive" });
  });
});
