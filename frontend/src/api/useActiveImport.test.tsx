/**
 * Tests for `useActiveImport` (L3-T10).
 *
 * The hook is the SettingsPage Apply button's gate: it polls
 * `/api/imports/active`, returns `{ active: bool }`, and survives transient
 * network errors by reporting `{ active: false }` rather than rejecting (a
 * red error banner on a probe used only to disable a button would be hostile
 * UX — the worst case of a stale `false` is the click 409s, which the page
 * already handles).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import { useActiveImport } from "@/api/useActiveImport";
import { server } from "@/test/msw-server";

const URL_ = `${window.location.origin}/api/imports/active`;

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

describe("useActiveImport", () => {
  test("returns { active: false } when no import runs", async () => {
    server.use(
      http.get(URL_, () => HttpResponse.json({ active: false }, { status: 200 })),
    );

    const { result } = renderHook(() => useActiveImport(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ active: false });
  });

  test("returns { active: true } while an import is in flight", async () => {
    server.use(
      http.get(URL_, () => HttpResponse.json({ active: true }, { status: 200 })),
    );

    const { result } = renderHook(() => useActiveImport(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ active: true });
  });

  test("falls back to { active: false } on a 5xx (probe must never red-banner)", async () => {
    // A transient backend hiccup on the probe is not an error worth surfacing:
    // the worst case of a stale `false` is the next Apply click 409s, which
    // already round-trips a structured error. A throw would render a red
    // banner under a button the user only clicks every few minutes.
    server.use(
      http.get(URL_, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    const { result } = renderHook(() => useActiveImport(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toEqual({ active: false }));
    expect(result.current.isError).toBe(false);
  });
});
