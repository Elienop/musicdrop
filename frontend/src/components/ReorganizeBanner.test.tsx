// frontend/src/components/ReorganizeBanner.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import { ReorganizeBanner } from "./ReorganizeBanner";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const base = {
  job_id: "j",
  total: 10,
  processed: 4,
  moved: 3,
  skipped: 1,
  failed: 0,
  current: null,
  error: null,
  artist: null,
  album_id: null,
  scope_label: "library",
};

afterEach(() => vi.restoreAllMocks());

test("shows running progress with scope label", async () => {
  vi.spyOn(client, "GET").mockResolvedValue({
    data: { ...base, phase: "running" },
    response: { ok: true, status: 200 },
  } as never);
  wrap(<ReorganizeBanner />);
  expect(await screen.findByRole("status")).toHaveTextContent(
    /Reorganizing — library… 4 \/ 10/i,
  );
});

test("hides when idle", async () => {
  vi.spyOn(client, "GET").mockResolvedValue({
    data: { ...base, phase: "idle" },
    response: { ok: true, status: 200 },
  } as never);
  const { container } = wrap(<ReorganizeBanner />);
  await new Promise((r) => setTimeout(r, 0));
  expect(container).toBeEmptyDOMElement();
});
