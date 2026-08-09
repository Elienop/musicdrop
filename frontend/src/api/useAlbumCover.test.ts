import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  // Put the real URL back by hand rather than with `vi.unstubAllGlobals()`,
  // which would also drop the `scrollTo`/`matchMedia`/`EventSource` stubs
  // `test/setup.ts` installs.
  const RealURL = globalThis.URL;
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.stubGlobal("URL", RealURL));

  it("returns found=false on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyResponse(404));
    const { result } = renderHook(() => useFetchAlbumCover(7), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ found: false });
  });

  it("returns the blob + source on 200, minting no object URL", async () => {
    const body = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    const createObjectURL = vi.fn(() => "blob:x");
    vi.stubGlobal("URL", { ...RealURL, createObjectURL, revokeObjectURL: () => {} });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(imageResponse(body, "Cover Art Archive"));
    const { result } = renderHook(() => useFetchAlbumCover(7), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // Exact key set: an `objectUrl` creeping back in fails here.
    expect(result.current.data).toEqual({
      found: true,
      blob: expect.anything(),
      source: "Cover Art Archive",
    });
    const data = result.current.data;
    // Identity, not deep equality — two jsdom Blobs deep-equal each other
    // (all their state hides behind one non-enumerated `Symbol(impl)`), so
    // `toEqual({blob: body})` above would hold for ANY blob.
    expect(data?.found === true ? data.blob : null).toBe(body);
    // The URL is the CALLER's to mint. TanStack drops a per-call `onSuccess`
    // once the observer unmounts, so a URL created here would be handed to
    // nobody and leak on every fetch the user navigated away from.
    expect(createObjectURL).not.toHaveBeenCalled();
  });
});
