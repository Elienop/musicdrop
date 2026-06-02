import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { usePreviewAlbumEdit } from "@/api/useAlbumEdit";
import { client } from "@/api/client";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

describe("usePreviewAlbumEdit", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("POSTs to the preview endpoint and returns the diff", async () => {
    vi.spyOn(client, "POST").mockResolvedValue({
      data: {
        changed_fields: ["title"],
        album_before: { title: "A" },
        album_after: { title: "B" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
      },
    } as never);

    const { result } = renderHook(() => usePreviewAlbumEdit(7), { wrapper });
    result.current.mutate({ album: { title: "B" }, tracks: [] });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.changed_fields).toContain("title");
  });
});
