import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { useFetchAlbumCover } from "@/api/useAlbumCover";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

// Hand-rolled Response-likes: a real `new Response(jsdomBlob)` calls `.stream()`
// on the body when consumed, which the jsdom Blob lacks on Node 22 (undici) ->
// "object.stream is not a function". These expose exactly what the hooks read.
function imageResponse(blob: Blob, source: string | null): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (h: string) => (h.toLowerCase() === "x-art-source" ? source : null) },
    blob: async () => blob,
  } as unknown as Response;
}
function emptyResponse(status: number): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
  } as unknown as Response;
}

describe("useFetchAlbumCover", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("returns found=false on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyResponse(404));
    const { result } = renderHook(() => useFetchAlbumCover(7), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ found: false });
  });

  it("returns the blob + source on 200", async () => {
    const body = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(imageResponse(body, "Cover Art Archive"));
    const { result } = renderHook(() => useFetchAlbumCover(7), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({ found: true, source: "Cover Art Archive" });
  });
});
