import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import {
  useArtistImageSettings,
  useResetArtistImageOverride,
  useSetArtistImageSettings,
  useUploadArtistImageOverride,
} from "@/api/useArtistImage";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
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

describe("useUploadArtistImageOverride", () => {
  it("POSTs multipart to the override endpoint", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue({ ok: true } as Response);
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

describe("useResetArtistImageOverride", () => {
  it("DELETEs the override", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as Response);
    const { result } = renderHook(() => useResetArtistImageOverride("ABBA"), {
      wrapper: wrapper(),
    });
    await result.current.mutateAsync();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/artists/image/override?name=ABBA");
    expect((init as RequestInit).method).toBe("DELETE");
  });
});
