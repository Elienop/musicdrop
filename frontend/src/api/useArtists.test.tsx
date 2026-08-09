import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { useArtists } from "@/api/useArtists";
import { server } from "@/test/msw-server";

const URL_ = `${window.location.origin}/api/artists`;

describe("useArtists", () => {
  it("a remount within the stale window serves cache without refetching", async () => {
    let fetchCount = 0;
    server.use(
      http.get(URL_, () => {
        fetchCount += 1;
        return HttpResponse.json([
          { id: 1, name: "Artist One", album_count: 5 },
          { id: 2, name: "Artist Two", album_count: 3 },
        ]);
      }),
    );

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    function wrapper({ children }: { children: ReactNode }) {
      return (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      );
    }

    // First render should fetch
    const { result, unmount } = renderHook(() => useArtists(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchCount).toBe(1);

    // Unmount
    unmount();

    // Second render within stale window should NOT fetch (should serve from cache)
    const second = renderHook(() => useArtists(), { wrapper });
    await waitFor(() => expect(second.result.current.isSuccess).toBe(true));
    expect(fetchCount).toBe(1); // still 1, no refetch
  });
});
