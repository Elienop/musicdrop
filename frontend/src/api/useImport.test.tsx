import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import { ImportConflictError, useStartImport } from "@/api/useImport";
import { server } from "@/test/msw-server";

const IMPORT_URL = `${window.location.origin}/api/import`;

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

describe("useStartImport", () => {
  test("POSTs the path and resolves the new job id", async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(IMPORT_URL, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({ job_id: "job-1" }, { status: 202 });
      }),
    );

    const { result } = renderHook(() => useStartImport(), {
      wrapper: wrapper(),
    });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.job_id).toBe("job-1");
    expect(seenBody).toEqual({ path: "/music/incoming" });
  });

  test("maps a 409 to ImportConflictError", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json({ detail: "An import is already running" }, { status: 409 }),
      ),
    );

    const { result } = renderHook(() => useStartImport(), {
      wrapper: wrapper(),
    });
    result.current.mutate({ path: "/music/incoming" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ImportConflictError);
  });
});
