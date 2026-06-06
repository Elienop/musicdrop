import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import { useNaming, useSaveNaming } from "@/api/useNaming";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

afterEach(() => vi.restoreAllMocks());

test("useNaming returns the config", async () => {
  vi.spyOn(client, "GET").mockResolvedValue({
    data: {
      default: "$x",
      comp: null,
      singleton: null,
      custom: [],
      replace: [],
      sha256: "s",
      previews: [],
      replace_errors: [],
    },
    response: { ok: true, status: 200 },
  } as never);
  const { result } = renderHook(() => useNaming(), { wrapper: wrapper() });
  await waitFor(() => expect(result.current.data?.default).toBe("$x"));
});

test("useSaveNaming surfaces a 409 with status", async () => {
  vi.spyOn(client, "POST").mockResolvedValue({
    data: undefined,
    response: { ok: false, status: 409 },
  } as never);
  const { result } = renderHook(() => useSaveNaming(), { wrapper: wrapper() });
  await expect(
    result.current.mutateAsync({ rules: [], replace: [], base_sha256: "s" }),
  ).rejects.toMatchObject({ status: 409 });
});
