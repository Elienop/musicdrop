// frontend/src/components/reorganize/ReorganizeControl.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import { ReorganizeControl } from "./ReorganizeControl";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const idle = {
  phase: "idle",
  job_id: null,
  scope: null,
  total: 0,
  processed: 0,
  moved: 0,
  skipped: 0,
  failed: 0,
  current: null,
  error: null,
  artist: null,
  album_id: null,
  scope_label: "library",
};

beforeEach(() => {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: idle, response: { ok: true, status: 200 } } as never;
    if (path === "/api/reorganize/preview")
      return {
        data: {
          scope: "library",
          scope_label: "library",
          total: 4,
          will_move: 3,
          already_in_place: 1,
          truncated: false,
          moves: [
            {
              kind: "album",
              label: "Radiohead — In Rainbows",
              from_path: "/m/junk/ir",
              to_path: "/m/Radiohead/In Rainbows",
              track_count: 3,
            },
            {
              kind: "album",
              label: "BoC — Geogaddi",
              from_path: "/m/BoC/Geogaddi",
              to_path: "/m/BoC/Geogaddi",
              track_count: 2,
            },
          ],
        },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  vi.spyOn(client, "POST").mockResolvedValue({
    data: { ...idle, phase: "running" },
    response: { ok: true, status: 200 },
  } as never);
});

afterEach(() => vi.restoreAllMocks());

test("preview shows summary, folder move, and rename-in-place", async () => {
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(screen.getByRole("button", { name: /preview/i }));
  expect(await screen.findByText(/3 will move/i)).toBeInTheDocument();
  expect(screen.getByText(/already in place/i)).toBeInTheDocument();
  // folder move row
  expect(screen.getByText("Radiohead — In Rainbows")).toBeInTheDocument();
  // rename-in-place row (from === to)
  expect(screen.getByText(/renamed in place/i)).toBeInTheDocument();
});

test("confirm starts the job", async () => {
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(screen.getByRole("button", { name: /preview/i }));
  await screen.findByText(/3 will move/i);
  await userEvent.click(screen.getByRole("button", { name: /reorganize/i }));
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith("/api/reorganize", {
      params: { query: {} },
    }),
  );
});

test("zero-move preview shows an inline notice — no review, no Done button", async () => {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: idle, response: { ok: true, status: 200 } } as never;
    if (path === "/api/reorganize/preview")
      return {
        data: {
          scope: "library",
          scope_label: "library",
          total: 14,
          will_move: 0,
          already_in_place: 14,
          truncated: false,
          moves: [],
        },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(
    screen.getByRole("button", { name: /preview reorganize/i }),
  );
  // The "nothing to reorganize" result is action-local now (the old top-banner
  // notice channel is gone): an inline role="status" next to the buttons.
  const note = await screen.findByRole("status");
  expect(note).toHaveTextContent(/nothing to reorganize/i);
  // Still no review plan and no Done button — the control stays idle.
  expect(screen.queryByRole("button", { name: /^done$/i })).toBeNull();
  expect(
    screen.getByRole("button", { name: /preview reorganize/i }),
  ).toBeInTheDocument();
});

test("a failed preview shows an inline error next to the buttons", async () => {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: idle, response: { ok: true, status: 200 } } as never;
    if (path === "/api/reorganize/preview")
      return { data: undefined, response: { ok: false, status: 500 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(
    screen.getByRole("button", { name: /preview reorganize/i }),
  );
  // usePreviewReorganize throws Error("Failed to build preview") on !ok.
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /failed to build preview/i,
  );
  // The control stays idle — preview remains available for a retry.
  expect(
    screen.getByRole("button", { name: /preview reorganize/i }),
  ).toBeInTheDocument();
});
