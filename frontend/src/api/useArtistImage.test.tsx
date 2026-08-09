import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import {
  useArtistImageSettings,
  useArtistImageSources,
  useFetchArtistImage,
  useResetArtistImage,
  useSetArtistImageSettings,
  useUploadArtistImageOverride,
} from "@/api/useArtistImage";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

// Hand-rolled Response-likes: a real `new Response(jsdomBlob)` calls `.stream()`
// on the body when consumed, which the jsdom Blob lacks on Node 22 (undici).
function imageResponse(blob: Blob, source: string | null): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (h: string) => (h.toLowerCase() === "x-art-source" ? source : null) },
    blob: async () => blob,
  } as unknown as Response;
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.restoreAllMocks());

describe("useArtistImageSettings", () => {
  it("returns the enabled flag from the typed client", async () => {
    vi.spyOn(client, "GET").mockResolvedValue({
      data: { enabled: true },
      error: undefined,
      response: { ok: true },
    } as never);
    const { result } = renderHook(() => useArtistImageSettings(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toEqual({ enabled: true }));
  });
});

describe("useSetArtistImageSettings", () => {
  it("PUTs the new value", async () => {
    const put = vi.spyOn(client, "PUT").mockResolvedValue({
      data: { enabled: true },
      error: undefined,
      response: { ok: true },
    } as never);
    const { result } = renderHook(() => useSetArtistImageSettings(), { wrapper: wrapper() });
    await result.current.mutateAsync(true);
    expect(put).toHaveBeenCalledWith("/api/artists/image/settings", { body: { enabled: true } });
  });
});

describe("useArtistImageSources", () => {
  const sources = [
    { id: "fanarttv", label: "fanart.tv", available: false, reason: "No MusicBrainz ID" },
    { id: "deezer", label: "Deezer", available: true, reason: null },
  ];

  it("asks for the named artist and hands back the list in chain order", async () => {
    const get = vi.spyOn(client, "GET").mockResolvedValue({
      data: { sources },
      error: undefined,
      response: { ok: true },
    } as never);
    const { result } = renderHook(() => useArtistImageSources("AC/DC"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toEqual({ sources }));
    expect(get).toHaveBeenCalledWith("/api/artists/image/sources", {
      params: { query: { name: "AC/DC" } },
    });
  });

  it("stays idle while disabled, so a closed panel asks nothing", () => {
    const get = vi.spyOn(client, "GET").mockResolvedValue({
      data: { sources },
      error: undefined,
      response: { ok: true },
    } as never);
    renderHook(() => useArtistImageSources("ABBA", false), { wrapper: wrapper() });
    expect(get).not.toHaveBeenCalled();
  });
});

describe("useUploadArtistImageOverride", () => {
  it("POSTs multipart to the override endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as Response);
    const { result } = renderHook(() => useUploadArtistImageOverride("AC/DC"), {
      wrapper: wrapper(),
    });
    await result.current.mutateAsync(new Blob(["x"], { type: "image/png" }));
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/artists/image/override?name=AC%2FDC");
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toBeInstanceOf(FormData);
  });

  it("throws the server detail on failure", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      json: async () => ({ detail: "nope" }),
    } as Response);
    const { result } = renderHook(() => useUploadArtistImageOverride("ABBA"), {
      wrapper: wrapper(),
    });
    await expect(result.current.mutateAsync(new Blob(["x"]))).rejects.toThrow("nope");
  });
});

describe("useResetArtistImage", () => {
  // The route this replaced was `DELETE /api/artists/image/override`, which now
  // 405s — and a fetch mock never notices, so pin the verb and the path here.
  it("POSTs to the reset endpoint", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(200, { ok: true, cleared_override: true, cleared_auto: true }));
    const { result } = renderHook(() => useResetArtistImage("AC/DC"), { wrapper: wrapper() });
    await result.current.mutateAsync();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/artists/image/reset?name=AC%2FDC");
    expect((init as RequestInit).method).toBe("POST");
  });

  it("returns what the server said it cleared", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(200, { ok: true, cleared_override: true, cleared_auto: false }),
    );
    const { result } = renderHook(() => useResetArtistImage("ABBA"), { wrapper: wrapper() });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      ok: true,
      cleared_override: true,
      cleared_auto: false,
    });
  });

  it("raises the server's sentence when the reset is refused", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(403, { detail: "Cross-origin request refused" }),
    );
    const { result } = renderHook(() => useResetArtistImage("ABBA"), { wrapper: wrapper() });
    result.current.mutate();
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("Cross-origin request refused");
  });
});

describe("useFetchArtistImage", () => {
  // Put the real URL back by hand rather than with `vi.unstubAllGlobals()`,
  // which would also drop the `scrollTo`/`matchMedia`/`EventSource` stubs
  // `test/setup.ts` installs — and so make this file's correctness depend on
  // this describe staying last.
  const RealURL = globalThis.URL;
  beforeEach(() => {
    // jsdom implements the URL parser but not the object-URL methods.
    vi.stubGlobal("URL", { ...RealURL, createObjectURL: () => "blob:x", revokeObjectURL: () => {} });
  });
  afterEach(() => vi.stubGlobal("URL", RealURL));

  it("returns the blob and the source label on 200", async () => {
    const body = new Blob([new Uint8Array([1, 2, 3])], { type: "image/jpeg" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(imageResponse(body, "Deezer"));
    const { result } = renderHook(() => useFetchArtistImage("ABBA"), { wrapper: wrapper() });
    result.current.mutate("deezer");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      found: true,
      blob: body,
      objectUrl: "blob:x",
      source: "Deezer",
    });
  });

  it("reports a 404 as found=false carrying the server's sentence", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(404, { detail: "Deezer has no portrait for ABBA" }),
    );
    const { result } = renderHook(() => useFetchArtistImage("ABBA"), { wrapper: wrapper() });
    result.current.mutate("deezer");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      found: false,
      reason: "Deezer has no portrait for ABBA",
    });
  });

  it("raises on a 502 so a source outage never reads as 'no image'", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(502, { detail: "Deezer did not answer - try again in a moment" }),
    );
    const { result } = renderHook(() => useFetchArtistImage("ABBA"), { wrapper: wrapper() });
    result.current.mutate("deezer");
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toContain("did not answer");
  });

  it("POSTs the picked source, encoding a slash in the artist name", async () => {
    const body = new Blob([new Uint8Array([1])], { type: "image/png" });
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(imageResponse(body, "Deezer"));
    const { result } = renderHook(() => useFetchArtistImage("AC/DC"), { wrapper: wrapper() });
    result.current.mutate("deezer");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url, init] = spy.mock.calls[0];
    // Two substrings, so the assertion says nothing about query-param order.
    expect(String(url)).toContain("/api/artists/image/fetch?");
    expect(String(url)).toContain("name=AC%2FDC");
    expect(String(url)).toContain("source=deezer");
    expect((init as RequestInit).method).toBe("POST");
  });
});
