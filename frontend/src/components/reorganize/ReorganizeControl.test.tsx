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
  failures: [],
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
          orphans: [
            { name: "Old Artist feat. X", path: "Old Artist feat. X", file_count: 2 },
          ],
          orphans_total: 1,
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
  await userEvent.click(screen.getByRole("button", { name: /reorganize files/i }));
  expect(await screen.findByText(/3 will move/i)).toBeInTheDocument();
  expect(screen.getByText(/already in place/i)).toBeInTheDocument();
  // folder move row
  expect(screen.getByText("Radiohead — In Rainbows")).toBeInTheDocument();
  // rename-in-place row (from === to)
  expect(screen.getByText(/renamed in place/i)).toBeInTheDocument();
});

test("preview lists orphan folders to clean up", async () => {
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(screen.getByRole("button", { name: /reorganize files/i }));
  await screen.findByText(/3 will move/i);
  expect(screen.getByText(/Folders to clean up/i)).toBeInTheDocument();
  expect(screen.getByText("Old Artist feat. X")).toBeInTheDocument();
});

test("confirm starts the job", async () => {
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(screen.getByRole("button", { name: /reorganize files/i }));
  await screen.findByText(/3 will move/i);
  await userEvent.click(screen.getByRole("button", { name: /reorganize/i }));
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith("/api/reorganize", {
      params: { query: {} },
    }),
  );
});

test("orphan-only preview opens the plan and confirms as a folder clean-up", async () => {
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
          orphans: [
            { name: "Old Artist feat. X", path: "Old Artist feat. X", file_count: 2 },
            { name: "Ghost Album", path: "Ghost Album", file_count: 1 },
          ],
          orphans_total: 2,
        },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(
    screen.getByRole("button", { name: /reorganize files/i }),
  );
  // The plan opens even with no moves — the orphan husks are the reason.
  expect(await screen.findByText(/Folders to clean up/i)).toBeInTheDocument();
  expect(screen.getByText("Old Artist feat. X")).toBeInTheDocument();
  // Confirm reads as a clean-up, not a move.
  const confirm = screen.getByRole("button", { name: /clean up 2 folders/i });
  await userEvent.click(confirm);
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
    screen.getByRole("button", { name: /reorganize files/i }),
  );
  // The "nothing to reorganize" result is action-local now (the old top-banner
  // notice channel is gone): an inline role="status" next to the buttons.
  const note = await screen.findByRole("status");
  expect(note).toHaveTextContent(/nothing to reorganize/i);
  // Still no review plan and no Done button — the control stays idle.
  expect(screen.queryByRole("button", { name: /^done$/i })).toBeNull();
  expect(
    screen.getByRole("button", { name: /reorganize files/i }),
  ).toBeInTheDocument();
});

test("focus moves into the plan when it opens and back to the trigger on cancel", async () => {
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(
    screen.getByRole("button", { name: /reorganize files/i }),
  );
  const summary = await screen.findByText(/3 will move/i);

  // The disclosure idiom: the plan container (tabIndex -1) takes focus when
  // it appears, so keyboard users land on the confirm step.
  expect(document.activeElement).not.toBe(document.body);
  expect(document.activeElement?.contains(summary)).toBe(true);

  // Cancel unmounts the focused button — focus returns to the trigger.
  await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(
    screen.getByRole("button", { name: /reorganize files/i }),
  ).toHaveFocus();
});

test("rail: the trigger stays focusable while the plan is open and swallows re-clicks", async () => {
  wrap(<ReorganizeControl scope={{ scope: "library" }} variant="rail" />);
  const trigger = screen.getByRole("button", { name: "Reorganize files" });
  await userEvent.click(trigger);
  await screen.findByText(/3 will move/i);

  // Never `disabled` (that would strand keyboard focus on <body>) — the
  // open state is conveyed via aria-disabled and the click is swallowed.
  expect(trigger).toBeEnabled();
  expect(trigger).toHaveAttribute("aria-disabled", "true");
  // The spy's generic signature defeats vi.mocked's tuple inference — read
  // the raw call list through a minimal structural cast instead.
  const previewCalls = () =>
    (
      client.GET as unknown as { mock: { calls: [unknown][] } }
    ).mock.calls.filter(([path]) => path === "/api/reorganize/preview").length;
  const before = previewCalls();
  await userEvent.click(trigger);
  expect(previewCalls()).toBe(before);
});

test("renders per-file failures when this scope's job is terminal", async () => {
  const doneWithFailures = {
    ...idle,
    phase: "done",
    scope: "library",
    job_id: "j1",
    total: 2,
    processed: 2,
    moved: 1,
    failed: 1,
    failures: [
      { label: "Arcane — Get Jinxed", error: "file not found on disk after move" },
    ],
  };
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: doneWithFailures, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  const list = await screen.findByRole("alert", {
    name: /files that could not be reorganized/i,
  });
  expect(list).toHaveTextContent("Arcane — Get Jinxed");
  expect(list).toHaveTextContent(/file not found on disk after move/i);
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
    screen.getByRole("button", { name: /reorganize files/i }),
  );
  // usePreviewReorganize throws Error("Failed to build preview") on !ok.
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /failed to build preview/i,
  );
  // The control stays idle — preview remains available for a retry.
  expect(
    screen.getByRole("button", { name: /reorganize files/i }),
  ).toBeInTheDocument();
});
