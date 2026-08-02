// frontend/src/components/reorganize/ReorganizeControl.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import { formatTimestamp } from "@/lib/format";
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
  finished_at: null,
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
          conflicts: [],
          conflicts_total: 0,
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
          conflicts: [],
          conflicts_total: 0,
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
          orphans: [],
          orphans_total: 0,
          conflicts: [],
          conflicts_total: 0,
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

// ——— a terminal job's result ————————————————————————————————————————————
// The single slot keeps the last job until the next one starts, so this block
// is what the user is still staring at days after they fixed the cause — hence
// the finish time (is this stale?) and the explicit way out.

const FINISHED_AT = "2026-08-02T13:53:00Z";

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
  finished_at: FINISHED_AT,
};

/** Point the status endpoint at one job body; nothing else answers. */
function mockStatus(body: unknown) {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: body, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
}

test("renders per-file failures when this scope's job is terminal", async () => {
  mockStatus(doneWithFailures);
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  const list = await screen.findByRole("alert", {
    name: /files that could not be reorganized/i,
  });
  expect(list).toHaveTextContent("Arcane — Get Jinxed");
  expect(list).toHaveTextContent(/file not found on disk after move/i);
});

test("the failure block dates the run that produced it", async () => {
  mockStatus(doneWithFailures);
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  // The lead-in owns the sentence; the timestamp is its own <time> child, so
  // match the prefix and read the whole line's text.
  const line = await screen.findByText(/from the reorganize that finished/i);
  // Whitespace-normalise both sides: en-US puts a narrow no-break space before
  // "PM", which a naive substring match would miss.
  const flat = (s: string) => s.replace(/\s+/g, " ");
  expect(flat(line.textContent ?? "")).toContain(flat(formatTimestamp(FINISHED_AT)));
  // Never the raw wire value.
  expect(line.textContent).not.toContain(FINISHED_AT);
});

test("dismissing a terminal result clears it and moves focus to the trigger", async () => {
  // The slot really empties, so the poll that follows the dismiss must not
  // hand the same failures back.
  let statusBody: unknown = doneWithFailures;
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: statusBody, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  vi.spyOn(client, "POST").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/dismiss") {
      statusBody = idle;
      return { data: idle, response: { ok: true, status: 200 } } as never;
    }
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });

  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await screen.findByRole("alert", {
    name: /files that could not be reorganized/i,
  });
  await userEvent.click(screen.getByRole("button", { name: /dismiss/i }));

  expect(client.POST).toHaveBeenCalledWith("/api/reorganize/dismiss");
  await waitFor(() =>
    expect(
      screen.queryByRole("alert", { name: /files that could not be reorganized/i }),
    ).toBeNull(),
  );
  expect(screen.queryByText(/from the reorganize that finished/i)).toBeNull();
  // The block took the button that was just pressed with it — focus lands on
  // the control's own trigger instead of dropping to <body>.
  expect(screen.getByRole("button", { name: /reorganize files/i })).toHaveFocus();
});

test("dismissing while a preview is open hands focus to the plan, not <body>", async () => {
  // The inline variant swaps its trigger out for Confirm/Cancel while a plan is
  // open, so the trigger fallback does not exist — focus must land on the plan
  // container (the file's other managed focus target) when the list unmounts.
  let statusBody: unknown = doneWithFailures;
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: statusBody, response: { ok: true, status: 200 } } as never;
    if (path === "/api/reorganize/preview")
      return {
        data: {
          scope: "library",
          scope_label: "library",
          total: 1,
          will_move: 1,
          already_in_place: 0,
          truncated: false,
          moves: [
            {
              kind: "album",
              label: "Radiohead — In Rainbows",
              from_path: "/m/junk/ir",
              to_path: "/m/Radiohead/In Rainbows",
              track_count: 3,
            },
          ],
          orphans: [],
          orphans_total: 0,
          conflicts: [],
          conflicts_total: 0,
        },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  vi.spyOn(client, "POST").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/dismiss") {
      statusBody = idle;
      return { data: idle, response: { ok: true, status: 200 } } as never;
    }
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });

  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await screen.findByRole("alert", {
    name: /files that could not be reorganized/i,
  });
  await userEvent.click(screen.getByRole("button", { name: /reorganize files/i }));
  await screen.findByText(/1 will move/i);

  await userEvent.click(screen.getByRole("button", { name: /dismiss/i }));
  await waitFor(() =>
    expect(
      screen.queryByRole("alert", { name: /files that could not be reorganized/i }),
    ).toBeNull(),
  );
  expect(document.body).not.toHaveFocus();
  expect(screen.getByText(/1 will move/i).closest('[tabindex="-1"]')).toHaveFocus();
});

test("a running job offers no dismiss and claims no finish time", async () => {
  mockStatus({
    ...idle,
    phase: "running",
    scope: "library",
    job_id: "r1",
    total: 4,
    processed: 2,
    moved: 1,
    failed: 1,
    // A live job can already have failed a unit; its result is not final and
    // there is nothing to clear yet — the running UI stays as it was.
    failures: [
      { label: "Arcane — Get Jinxed", error: "file not found on disk after move" },
    ],
    finished_at: null,
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  expect(await screen.findByRole("button", { name: /^stop$/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /dismiss/i })).toBeNull();
  expect(screen.queryByText(/finished/i)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

// ——— refused units (collision pre-flight) ———————————————————————————————
// A conflicted unit is a refusal-to-be, never a move: the backend keeps it out
// of `moves` and out of `will_move`, so the UI must show it as its own thing.

/** Point the preview endpoint at one plan body; status stays idle. */
function mockPreview(plan: unknown) {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/reorganize/status")
      return { data: idle, response: { ok: true, status: 200 } } as never;
    if (path === "/api/reorganize/preview")
      return { data: plan, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
}

const intraUnitConflict = {
  kind: "album",
  label: "X — Collide",
  from_path: "/m/X/Collide",
  collisions: [
    {
      kind: "intra_unit",
      path: "X/Collide/01 Song.mp3",
      detail:
        "X/Collide/01 Song.mp3: 2 tracks resolve to this same name: " +
        "track 1 'Song' (01 Song.mp3), track 1 'Song' (01 Song other.mp3)",
    },
  ],
};

const crossUnitConflict = {
  kind: "singleton",
  label: "Y — Stray",
  from_path: "/m/Y",
  collisions: [
    {
      kind: "cross_unit",
      path: "Y/01 Stray.mp3",
      detail:
        "Y/01 Stray.mp3: already exists on disk and holds " +
        "track 1 'Stray' of X - Collide (album 1)",
    },
  ],
};

test("preview lists refused units with their label and collision detail", async () => {
  mockPreview({
    scope: "library",
    scope_label: "library",
    total: 3,
    will_move: 1,
    already_in_place: 0,
    truncated: false,
    moves: [
      {
        kind: "album",
        label: "Radiohead — In Rainbows",
        from_path: "/m/junk/ir",
        to_path: "/m/Radiohead/In Rainbows",
        track_count: 3,
      },
    ],
    orphans: [],
    orphans_total: 0,
    conflicts: [intraUnitConflict, crossUnitConflict],
    conflicts_total: 2,
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(screen.getByRole("button", { name: /reorganize files/i }));

  // Scoped to the refusal list: "X — Collide" also appears inside the OTHER
  // conflict's occupant sentence, and "cannot be moved" is in the summary too.
  const refused = await screen.findByRole("list", {
    name: /cannot be reorganized/i,
  });
  expect(within(refused).getByText("X — Collide")).toBeInTheDocument();
  expect(within(refused).getByText("Y — Stray")).toBeInTheDocument();
  expect(
    within(refused).getByText(/2 tracks resolve to this same name/i),
  ).toBeInTheDocument();
  expect(
    within(refused).getByText(/already exists on disk and holds/i),
  ).toBeInTheDocument();

  // A refusal must never read as the benign "renamed in place" move row: it
  // carries the danger treatment FailureList uses, not MoveRow's neutral one.
  expect(within(refused).queryByText(/renamed in place/i)).toBeNull();
  expect(refused).toHaveClass("text-destructive");
  // The summary partitions the scope, so the refused units are counted apart.
  expect(screen.getByText(/1 will move/i)).toBeInTheDocument();
  expect(screen.getByText(/2 cannot be moved/i)).toBeInTheDocument();
});

test("an all-refusals preview opens the plan instead of the nothing-to-do note", async () => {
  mockPreview({
    scope: "library",
    scope_label: "library",
    total: 3,
    will_move: 0,
    already_in_place: 0,
    truncated: false,
    moves: [],
    orphans: [],
    orphans_total: 0,
    // conflicts[] is capped; conflicts_total is exact.
    conflicts: [intraUnitConflict, crossUnitConflict],
    conflicts_total: 3,
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(screen.getByRole("button", { name: /reorganize files/i }));

  // Nothing WILL move, but something is wrong — the user has to see it, so this
  // must not fall into the "everything already matches your config" branch.
  const refused = await screen.findByRole("list", {
    name: /cannot be reorganized/i,
  });
  expect(within(refused).getByText("X — Collide")).toBeInTheDocument();
  expect(screen.queryByRole("status")).toBeNull();
  // The cap is visible rather than silently swallowed.
  expect(within(refused).getByText(/\+ 1 more/i)).toBeInTheDocument();

  // Nothing to confirm: no button may offer to move or clean up anything.
  expect(screen.queryByRole("button", { name: /^reorganize \d/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /clean up/i })).toBeNull();
  expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
});

test("the confirm button counts only movable units, never refused ones", async () => {
  mockPreview({
    scope: "library",
    scope_label: "library",
    total: 3,
    will_move: 1,
    already_in_place: 0,
    truncated: false,
    moves: [
      {
        kind: "album",
        label: "Radiohead — In Rainbows",
        from_path: "/m/junk/ir",
        to_path: "/m/Radiohead/In Rainbows",
        track_count: 3,
      },
    ],
    orphans: [],
    orphans_total: 0,
    conflicts: [intraUnitConflict, crossUnitConflict],
    conflicts_total: 2,
  });
  wrap(<ReorganizeControl scope={{ scope: "library" }} />);
  await userEvent.click(screen.getByRole("button", { name: /reorganize files/i }));
  await screen.findByRole("list", { name: /cannot be reorganized/i });

  // will_move already excludes the refused units — the button must not promise
  // the 3 units in scope, only the 1 that can actually move.
  expect(
    screen.getByRole("button", { name: "Reorganize 1 item" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /reorganize 3 items/i })).toBeNull();
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
