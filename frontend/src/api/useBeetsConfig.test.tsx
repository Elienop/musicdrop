/**
 * Mutation-hook tests for `useBeetsConfig` (L3-T10).
 *
 * The existing `useBeetsConfig` query hook is exercised indirectly by the
 * SettingsPage tests; this file is the dedicated coverage for the new
 * mutation hooks — `useSaveConfig`, `useApplyConfig`, `useValidateConfig` —
 * plus the cache-invalidation contract that wires them back to the snapshot
 * query (so a Save/Apply round-trip leaves the page reading fresh data).
 *
 * The save/apply throw a *structured* Error (`{status, body}`) on non-2xx so
 * the SettingsPage can branch on 422 (validation) vs 409 (CAS/import) without
 * a string-match on `.message`. The validate hook returns `errors[]` directly
 * — never throws on a 200-with-errors body — because it feeds CodeMirror's
 * async lint source.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { useActiveImport } from "@/api/useActiveImport";
import {
  useApplyConfig,
  useBeetsConfig,
  useSaveConfig,
  useValidateConfig,
} from "@/api/useBeetsConfig";
import { server } from "@/test/msw-server";

type BeetsConfigSnapshot = components["schemas"]["BeetsConfigSnapshot"];
type SaveRequest = components["schemas"]["SaveRequest"];

const SAVE_URL = `${window.location.origin}/api/config/save`;
const APPLY_URL = `${window.location.origin}/api/config/apply`;
const VALIDATE_URL = `${window.location.origin}/api/config/validate`;
const CONFIG_URL = `${window.location.origin}/api/config`;
const ACTIVE_IMPORT_URL = `${window.location.origin}/api/imports/active`;

/** Returns a fresh wrapper + the queryClient so tests can assert invalidation. */
function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, Wrapper };
}

function makeSnapshot(
  overrides: Partial<BeetsConfigSnapshot> = {},
): BeetsConfigSnapshot {
  return {
    yaml_text: "directory: /music\n",
    effective_yaml: "directory: /music\nimport:\n  copy: true\n",
    config_path: "/data/beets/config.yaml",
    loaded_at: "2026-05-28T00:00:00Z",
    file_modified_at: "2026-05-28T00:00:00Z",
    sha256: "deadbeef",
    apply_pending: false,
    ...overrides,
  };
}

const SAVE_BODY: SaveRequest = {
  yaml_text: "directory: /music\n",
  base_sha256: "deadbeef",
};

describe("useSaveConfig", () => {
  test("POSTs the SaveRequest and resolves to the fresh snapshot", async () => {
    let seenBody: unknown = null;
    const fresh = makeSnapshot({ apply_pending: true, sha256: "fresh-sha" });
    server.use(
      http.post(SAVE_URL, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json(fresh, { status: 200 });
      }),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useSaveConfig(), { wrapper: Wrapper });
    result.current.mutate(SAVE_BODY);

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seenBody).toEqual(SAVE_BODY);
    expect(result.current.data?.apply_pending).toBe(true);
  });

  test("invalidates ['beets-config'] on success so the snapshot query refetches", async () => {
    server.use(
      http.post(SAVE_URL, () => HttpResponse.json(makeSnapshot(), { status: 200 })),
      // First call gets one snapshot, second (post-invalidate) gets the new one.
      http.get(CONFIG_URL, (() => {
        let n = 0;
        return () => {
          n += 1;
          return HttpResponse.json(makeSnapshot({ sha256: `sha-${n}` }), { status: 200 });
        };
      })()),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(
      () => ({ save: useSaveConfig(), q: useBeetsConfig() }),
      { wrapper: Wrapper },
    );

    await waitFor(() => expect(result.current.q.isSuccess).toBe(true));
    expect(result.current.q.data?.sha256).toBe("sha-1");

    result.current.save.mutate(SAVE_BODY);
    await waitFor(() => expect(result.current.save.isSuccess).toBe(true));

    // The invalidation should drive a refetch that yields the second-call sha.
    await waitFor(() => expect(result.current.q.data?.sha256).toBe("sha-2"));
  });

  test("throws a structured error carrying status+body on a 422 (validation)", async () => {
    const body = {
      detail: { errors: [{ loc: "directory", msg: "missing", type: "schema_missing" }] },
    };
    server.use(http.post(SAVE_URL, () => HttpResponse.json(body, { status: 422 })));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useSaveConfig(), { wrapper: Wrapper });
    result.current.mutate(SAVE_BODY);

    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error as Error & { status?: number; body?: unknown };
    expect(err.status).toBe(422);
    expect(err.body).toEqual(body);
  });

  test("throws a structured error on a 409 (CAS mismatch)", async () => {
    const body = { detail: { error: "conflict", server_mtime_ns: 99, server_sha256: "x" } };
    server.use(http.post(SAVE_URL, () => HttpResponse.json(body, { status: 409 })));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useSaveConfig(), { wrapper: Wrapper });
    result.current.mutate(SAVE_BODY);

    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error as Error & { status?: number; body?: unknown };
    expect(err.status).toBe(409);
    expect(err.body).toEqual(body);
  });
});

describe("useApplyConfig", () => {
  test("POSTs (no body) and resolves to the post-reload snapshot", async () => {
    const fresh = makeSnapshot({ apply_pending: false, sha256: "sha-after-apply" });
    server.use(http.post(APPLY_URL, () => HttpResponse.json(fresh, { status: 200 })));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useApplyConfig(), { wrapper: Wrapper });
    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.apply_pending).toBe(false);
    expect(result.current.data?.sha256).toBe("sha-after-apply");
  });

  test("throws a structured error on a 409 (import in progress)", async () => {
    const body = { detail: "Import in progress — Apply available when it finishes" };
    server.use(http.post(APPLY_URL, () => HttpResponse.json(body, { status: 409 })));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useApplyConfig(), { wrapper: Wrapper });
    result.current.mutate();

    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error as Error & { status?: number; body?: unknown };
    expect(err.status).toBe(409);
    expect(err.body).toEqual(body);
  });

  test("throws a structured error on a 500 (degraded reload)", async () => {
    const body = { detail: { message: "rebuild failed", recovery: "restart the server" } };
    server.use(http.post(APPLY_URL, () => HttpResponse.json(body, { status: 500 })));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useApplyConfig(), { wrapper: Wrapper });
    result.current.mutate();

    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error as Error & { status?: number; body?: unknown };
    expect(err.status).toBe(500);
    expect(err.body).toEqual(body);
  });

  test("invalidates ['beets-config'] on success", async () => {
    server.use(
      http.post(APPLY_URL, () => HttpResponse.json(makeSnapshot(), { status: 200 })),
      http.get(CONFIG_URL, (() => {
        let n = 0;
        return () => {
          n += 1;
          return HttpResponse.json(makeSnapshot({ sha256: `sha-${n}` }), { status: 200 });
        };
      })()),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(
      () => ({ apply: useApplyConfig(), q: useBeetsConfig() }),
      { wrapper: Wrapper },
    );

    await waitFor(() => expect(result.current.q.isSuccess).toBe(true));
    result.current.apply.mutate();
    await waitFor(() => expect(result.current.apply.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.q.data?.sha256).toBe("sha-2"));
  });

  test("invalidates ['active-import'] on success (tight coupling for cross-tab apply)", async () => {
    // Apply's gate is the import-active probe. If Apply ever takes long enough
    // that an import was started + finished during the rebuild, the cached
    // probe value could lie about the gate. Cheaper to invalidate alongside
    // ["beets-config"] than to reason about the race — see T11 design notes.
    let probeHits = 0;
    server.use(
      http.post(APPLY_URL, () => HttpResponse.json(makeSnapshot(), { status: 200 })),
      http.get(ACTIVE_IMPORT_URL, () => {
        probeHits += 1;
        return HttpResponse.json({ active: false }, { status: 200 });
      }),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(
      () => ({ apply: useApplyConfig(), probe: useActiveImport() }),
      { wrapper: Wrapper },
    );

    await waitFor(() => expect(result.current.probe.isSuccess).toBe(true));
    expect(probeHits).toBe(1);

    result.current.apply.mutate();
    await waitFor(() => expect(result.current.apply.isSuccess).toBe(true));
    // The invalidation should drive a second probe fetch.
    await waitFor(() => expect(probeHits).toBe(2));
  });
});

describe("useValidateConfig", () => {
  test("returns BOTH channels — advisories ride alongside the errors", async () => {
    // The hook used to narrow to `data.errors`, which discarded the advisory
    // channel before any caller could reach it. Asserting on the whole
    // response means a re-narrowing regression fails HERE rather than
    // silently blanking the settings page's advisory banners.
    const errors = [
      { loc: "directory", msg: "must be a string", type: "schema_type", line: 3, column: 0 },
    ];
    const advisories = [
      { key: "import.autotag", message: "MusicDrop forces import.autotag on." },
    ];
    server.use(
      http.post(VALIDATE_URL, () =>
        HttpResponse.json({ errors, advisories }, { status: 200 }),
      ),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useValidateConfig(), { wrapper: Wrapper });
    result.current.mutate({ yaml_text: "directory: 5" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ errors, advisories });
  });

  test("carries advisories through even when the errors channel is clean", async () => {
    // The load-bearing case for the editor: a config that only trips an
    // advisory is VALID and saves cleanly, so `errors` is empty while
    // `advisories` is not. Any hook that gated the second channel on
    // `errors.length` would drop exactly this response.
    const advisories = [
      { key: "import.singletons", message: "MusicDrop forces import.singletons off." },
    ];
    server.use(
      http.post(VALIDATE_URL, () =>
        HttpResponse.json({ errors: [], advisories }, { status: 200 }),
      ),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useValidateConfig(), { wrapper: Wrapper });
    result.current.mutate({ yaml_text: "import:\n  singletons: yes\n" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.errors).toEqual([]);
    expect(result.current.data?.advisories).toEqual(advisories);
  });

  test("resolves to two empty channels on a clean validate", async () => {
    server.use(
      http.post(VALIDATE_URL, () =>
        HttpResponse.json({ errors: [], advisories: [] }, { status: 200 }),
      ),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useValidateConfig(), { wrapper: Wrapper });
    result.current.mutate({ yaml_text: "directory: /music\n" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ errors: [], advisories: [] });
  });
});
