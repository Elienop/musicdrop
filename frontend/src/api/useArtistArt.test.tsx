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
    vi.spyOn(client, "GET").mockResolvedValue({
      data: { enabled: true },
      error: undefined,
      response: { ok: true },
    } as never);
    const { result } = renderHook(() => useArtistArtSettings(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toEqual({ enabled: true }));
  });
});

describe("useSetArtistArtSettings", () => {
  it("PUTs the new value", async () => {
    const put = vi.spyOn(client, "PUT").mockResolvedValue({
      data: { enabled: true },
      error: undefined,
      response: { ok: true },
    } as never);
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
      signal: expect.any(AbortSignal),
    });
  });

  it("fails with a short message when the start does not answer within the bound", async () => {
    vi.useFakeTimers();
    try {
      // Answers only when the signal fires, so the bound is the only thing
      // that can end this call — a stalled proxy or a dropped connection.
      vi.spyOn(client, "POST").mockImplementation((async (
        _path: string,
        init: { signal: AbortSignal },
      ) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener("abort", () => reject(new Error("aborted")));
        })) as never);
      const { result } = renderHook(() => useStartArtistArtApply("ABBA"), { wrapper: wrapper() });
      const rejects = expect(result.current.mutateAsync()).rejects.toThrow(
        "The request timed out.",
      );
      await vi.advanceTimersByTimeAsync(10_000);
      await rejects;
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a non-timeout failure's own message", async () => {
    vi.spyOn(client, "POST").mockRejectedValue(new Error("Failed to fetch"));
    const { result } = renderHook(() => useStartArtistArtApply("ABBA"), { wrapper: wrapper() });
    await expect(result.current.mutateAsync()).rejects.toThrow("Failed to fetch");
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
