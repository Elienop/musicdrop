import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import {
  useArtistArtSettings,
  useSetArtistArtSettings,
  useStartArtistArtApply,
} from "@/api/useArtistArt";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

afterEach(() => vi.restoreAllMocks());

describe("useArtistArtSettings", () => {
  it("returns the enabled flag from the typed client", async () => {
    vi.spyOn(client, "GET").mockResolvedValue({ data: { enabled: true }, error: undefined } as never);
    const { result } = renderHook(() => useArtistArtSettings(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toEqual({ enabled: true }));
  });
});

describe("useSetArtistArtSettings", () => {
  it("PUTs the new value", async () => {
    const put = vi
      .spyOn(client, "PUT")
      .mockResolvedValue({ data: { enabled: true }, error: undefined } as never);
    const { result } = renderHook(() => useSetArtistArtSettings(), { wrapper: wrapper() });
    await result.current.mutateAsync(true);
    expect(put).toHaveBeenCalledWith("/api/artists/art/settings", { body: { enabled: true } });
  });
});

describe("useStartArtistArtApply", () => {
  it("POSTs with the artist name in the query", async () => {
    const post = vi.spyOn(client, "POST").mockResolvedValue({
      data: { phase: "running" },
      error: undefined,
      response: { status: 200 },
    } as never);
    const { result } = renderHook(() => useStartArtistArtApply("ABBA"), { wrapper: wrapper() });
    await result.current.mutateAsync();
    expect(post).toHaveBeenCalledWith("/api/artists/art/apply", {
      params: { query: { name: "ABBA" } },
    });
  });

  it("throws the enable-first message on a 403 response", async () => {
    vi.spyOn(client, "POST").mockResolvedValue({
      data: undefined,
      error: { detail: "nope" },
      response: { status: 403 },
    } as never);
    const { result } = renderHook(() => useStartArtistArtApply("ABBA"), { wrapper: wrapper() });
    await expect(result.current.mutateAsync()).rejects.toThrow(
      "Enable 'Write artist art to library' first",
    );
  });
});
