/**
 * Tests for the bank hooks (chunk 5, Task 1).
 *
 * MSW intercepts the typed client; the test QueryClient disables retries.
 * The 409 paths matter most: the backend's InvalidTransitionError arrives as
 * {detail: string} and must surface as BankConflictError with that message.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import {
  BankConflictError,
  bankListPollMs,
  useBankDecision,
  useBankItem,
  useBankList,
  useBulkIgnoreBank,
  useDeleteBankItem,
  useIgnoreBankItem,
} from "@/api/useBank";
import { server } from "@/test/msw-server";

const O = window.location.origin;
const LIST = `${O}/api/bank`;
const ITEM = `${O}/api/bank/:itemId`;
const DECISION = `${O}/api/bank/:itemId/decision`;
const BULK = `${O}/api/bank/bulk-ignore`;

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const summary = {
  id: "b1",
  folder: "/inbox/Album X",
  source: "sweep",
  reason: "needs_review",
  artist: "Artist",
  album: "Album X",
  recommendation: "medium",
  confidence: 71.2,
  status: "needs_review",
  error: null,
  album_id: null,
  banked_at: "2026-06-12T08:00:00Z",
};

describe("useBankList", () => {
  test("passes status/offset/limit through and returns the page", async () => {
    // Captured as primitives (the useDuplicates.test.tsx idiom) — TS strict
    // doesn't track closure assignments, so a captured URLSearchParams stays
    // narrowed to null at the assertion site.
    let seenStatus: string | null = null;
    let seenOffset: string | null = null;
    let seenLimit: string | null = null;
    server.use(
      http.get(LIST, ({ request }) => {
        const params = new URL(request.url).searchParams;
        seenStatus = params.get("status");
        seenOffset = params.get("offset");
        seenLimit = params.get("limit");
        return HttpResponse.json({
          items: [summary],
          total: 1,
          offset: 48,
          limit: 48,
        });
      }),
    );
    const { result } = renderHook(
      () => useBankList({ status: "needs_review", offset: 48, limit: 48 }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBe(1);
    expect(seenStatus).toBe("needs_review");
    expect(seenOffset).toBe("48");
    expect(seenLimit).toBe("48");
  });

  test("omits the status and view params when unfiltered", async () => {
    let seenHasStatus: boolean | null = null;
    let seenHasView: boolean | null = null;
    server.use(
      http.get(LIST, ({ request }) => {
        const params = new URL(request.url).searchParams;
        seenHasStatus = params.has("status");
        seenHasView = params.has("view");
        return HttpResponse.json({ items: [], total: 0, offset: 0, limit: 48 });
      }),
    );
    const { result } = renderHook(() => useBankList({ offset: 0, limit: 48 }), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seenHasStatus).toBe(false);
    expect(seenHasView).toBe(false);
  });

  test("passes the view through (the needs-attention default)", async () => {
    let seenView: string | null = null;
    let seenHasStatus: boolean | null = null;
    server.use(
      http.get(LIST, ({ request }) => {
        const params = new URL(request.url).searchParams;
        seenView = params.get("view");
        seenHasStatus = params.has("status");
        return HttpResponse.json({ items: [], total: 0, offset: 0, limit: 48 });
      }),
    );
    const { result } = renderHook(
      () => useBankList({ view: "active", offset: 0, limit: 48 }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seenView).toBe("active");
    expect(seenHasStatus).toBe(false);
  });
});

describe("useBankItem", () => {
  test("fetches the full row; disabled without an id", async () => {
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json({
          ...summary,
          parked: null,
          duplicate: null,
          decided: null,
          fingerprint: "f",
          decided_at: null,
          resolved_at: null,
          reason: "no_match",
          recommendation: null,
          confidence: null,
        }),
      ),
    );
    const { result } = renderHook(() => useBankItem("b1"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.id).toBe("b1");

    const disabled = renderHook(() => useBankItem(undefined), {
      wrapper: wrapper(),
    });
    expect(disabled.result.current.fetchStatus).toBe("idle");
  });
});

describe("useBankDecision", () => {
  test("posts the decision and resolves with the updated row", async () => {
    let body: unknown = null;
    server.use(
      http.post(DECISION, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          ...summary,
          status: "queued",
          parked: null,
          duplicate: null,
          decided: {
            action: "asis",
            candidate_index: null,
            duplicate_action: null,
          },
          fingerprint: "f",
          decided_at: "2026-06-12T09:00:00Z",
          resolved_at: null,
          reason: "no_match",
          recommendation: null,
          confidence: null,
        });
      }),
    );
    const { result } = renderHook(() => useBankDecision("b1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ action: "asis" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(body).toEqual({ action: "asis" });
    expect(result.current.data?.status).toBe("queued");
  });

  test("a 409 surfaces as BankConflictError carrying the string detail", async () => {
    server.use(
      http.post(DECISION, () =>
        HttpResponse.json(
          {
            detail:
              "row is queued; decisions need one of ['failed', 'needs_review', 'stale']",
          },
          { status: 409 },
        ),
      ),
    );
    const { result } = renderHook(() => useBankDecision("b1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ action: "ignore" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(BankConflictError);
    expect(result.current.error?.message).toMatch(/row is queued/);
  });

  test("a 422 with the ARRAY detail still produces a human message", async () => {
    // FastAPI's auto-validation shape — the dual-shape carry-forward.
    server.use(
      http.post(DECISION, () =>
        HttpResponse.json(
          {
            detail: [
              {
                loc: ["body", "candidate_index"],
                msg: "candidate_index only applies to apply",
                type: "value_error",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    const { result } = renderHook(() => useBankDecision("b1"), {
      wrapper: wrapper(),
    });
    result.current.mutate({ action: "ignore", candidate_index: 2 });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toMatch(
      /candidate_index only applies to apply/,
    );
  });
});

describe("useIgnoreBankItem", () => {
  test("posts {action: ignore} to the row supplied at call time", async () => {
    let seenId: string | null = null;
    let body: unknown = null;
    server.use(
      http.post(DECISION, async ({ request, params }) => {
        seenId = String(params.itemId);
        body = await request.json();
        return HttpResponse.json({
          ...summary,
          status: "ignored",
          parked: null,
          duplicate: null,
          decided: {
            action: "ignore",
            candidate_index: null,
            duplicate_action: null,
          },
          fingerprint: "f",
          decided_at: "2026-06-12T09:00:00Z",
          resolved_at: "2026-06-12T09:00:00Z",
          reason: "no_match",
          recommendation: null,
          confidence: null,
        });
      }),
    );
    const { result } = renderHook(() => useIgnoreBankItem(), {
      wrapper: wrapper(),
    });
    result.current.mutate("b1");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seenId).toBe("b1");
    expect(body).toEqual({ action: "ignore" });
  });

  test("a 409 surfaces as BankConflictError (the list-row toast path)", async () => {
    server.use(
      http.post(DECISION, () =>
        HttpResponse.json(
          { detail: "row is queued; decisions need one of [...]" },
          { status: 409 },
        ),
      ),
    );
    const { result } = renderHook(() => useIgnoreBankItem(), {
      wrapper: wrapper(),
    });
    result.current.mutate("b1");
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(BankConflictError);
    expect(result.current.error?.message).toMatch(/row is queued/);
  });
});

describe("useDeleteBankItem", () => {
  test("204 resolves; 404 resolves quietly (already gone is the goal state)", async () => {
    server.use(
      http.delete(ITEM, () => new HttpResponse(null, { status: 204 })),
    );
    const ok = renderHook(() => useDeleteBankItem(), { wrapper: wrapper() });
    ok.result.current.mutate("b1");
    await waitFor(() => expect(ok.result.current.isSuccess).toBe(true));

    server.use(
      http.delete(ITEM, () =>
        HttpResponse.json({ detail: "Bank item not found" }, { status: 404 }),
      ),
    );
    const gone = renderHook(() => useDeleteBankItem(), { wrapper: wrapper() });
    gone.result.current.mutate("b1");
    await waitFor(() => expect(gone.result.current.isSuccess).toBe(true));
  });

  test("409 (row applying) surfaces as BankConflictError", async () => {
    server.use(
      http.delete(ITEM, () =>
        HttpResponse.json(
          { detail: "row is applying; wait for the apply to finish" },
          { status: 409 },
        ),
      ),
    );
    const { result } = renderHook(() => useDeleteBankItem(), {
      wrapper: wrapper(),
    });
    result.current.mutate("b1");
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(BankConflictError);
  });
});

describe("useBulkIgnoreBank", () => {
  test("posts the ids and resolves with the flipped count", async () => {
    let body: unknown = null;
    server.use(
      http.post(BULK, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ ignored: 2 });
      }),
    );
    const { result } = renderHook(() => useBulkIgnoreBank(), {
      wrapper: wrapper(),
    });
    result.current.mutate(["b1", "b2"]);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(body).toEqual({ ids: ["b1", "b2"] });
    expect(result.current.data?.ignored).toBe(2);
  });
});

describe("bankListPollMs", () => {
  test("polls fast while a row is queued or applying, lazy otherwise", () => {
    expect(bankListPollMs(undefined)).toBe(30_000);
    expect(bankListPollMs([{ status: "needs_review" }])).toBe(30_000);
    expect(bankListPollMs([{ status: "done" }, { status: "queued" }])).toBe(
      2_000,
    );
    expect(bankListPollMs([{ status: "applying" }])).toBe(2_000);
  });
});
