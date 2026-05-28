import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { useDuplicates, useResolveDuplicate } from "@/api/useDuplicates";
import { server } from "@/test/msw-server";

type DuplicatesReport = components["schemas"]["DuplicatesReport"];

const DUP_URL = `${window.location.origin}/api/duplicates`;
const RESOLVE_URL = `${window.location.origin}/api/duplicates/resolve`;

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, Wrapper };
}

function emptyReport(mode: "strict" | "fuzzy"): DuplicatesReport {
  return { mode, group_count: 0, album_count: 0, groups: [] };
}

describe("useDuplicates", () => {
  test("fetches the report for the given mode", async () => {
    let seenMode: string | null = null;
    server.use(
      http.get(DUP_URL, ({ request }) => {
        seenMode = new URL(request.url).searchParams.get("mode");
        return HttpResponse.json(emptyReport("fuzzy"));
      }),
    );
    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useDuplicates("fuzzy"), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seenMode).toBe("fuzzy");
    expect(result.current.data?.mode).toBe("fuzzy");
  });
});

describe("useResolveDuplicate", () => {
  test("POSTs the request and invalidates the report on success", async () => {
    let getCalls = 0;
    server.use(
      http.get(DUP_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          getCalls === 1
            ? { mode: "strict", group_count: 1, album_count: 2, groups: [] }
            : emptyReport("strict"),
        );
      }),
      http.post(RESOLVE_URL, () =>
        HttpResponse.json({ kept_album_id: 1, moved: [{ id: 2, album_artist: "A", title: "B", trash_path: "/t/B" }] }),
      ),
    );
    const { Wrapper } = makeWrapper();
    const { result } = renderHook(
      () => ({ q: useDuplicates("strict"), m: useResolveDuplicate() }),
      { wrapper: Wrapper },
    );
    await waitFor(() => expect(result.current.q.isSuccess).toBe(true));
    expect(result.current.q.data?.group_count).toBe(1);

    result.current.m.mutate({ mode: "strict", keep_album_id: 1, remove_album_ids: [2] });
    await waitFor(() => expect(result.current.m.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.q.data?.group_count).toBe(0));
  });

  test("throws a structured error carrying status on a 409", async () => {
    server.use(
      http.post(RESOLVE_URL, () => HttpResponse.json({ detail: "Import in progress" }, { status: 409 })),
    );
    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useResolveDuplicate(), { wrapper: Wrapper });
    result.current.mutate({ mode: "strict", keep_album_id: 1, remove_album_ids: [2] });
    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error as Error & { status?: number };
    expect(err.status).toBe(409);
  });
});
