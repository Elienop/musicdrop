// frontend/src/pages/settings/DiskSyncPanel.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import { DiskSyncPanel } from "./DiskSyncPanel";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const idle = {
  phase: "idle",
  job_id: null,
  total: 0,
  processed: 0,
  removed: 0,
  updated: 0,
  unchanged: 0,
  read_errors: 0,
  emptied_albums: 0,
  current: null,
  error: null,
  failures: [],
};

const plan = {
  total_items: 600,
  will_remove: 2,
  will_update: 1,
  emptied_albums: ["Some Artist — Ghost Album"],
  emptied_total: 1,
  removals: [
    {
      label: "blink-182 — I Miss You",
      path: "blink-182/Enema of the State/03 I Miss You.mp3",
    },
  ],
  changes: [{ label: "Cher — Believe", fields: ["title"] }],
  read_errors: [],
  truncated: false,
};

const emptyPlan = {
  total_items: 14,
  will_remove: 0,
  will_update: 0,
  emptied_albums: [],
  emptied_total: 0,
  removals: [],
  changes: [],
  read_errors: [],
  truncated: false,
};

beforeEach(() => {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/disk-sync/status")
      return { data: idle, response: { ok: true, status: 200 } } as never;
    if (path === "/api/disk-sync/preview")
      return { data: plan, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  vi.spyOn(client, "POST").mockResolvedValue({
    data: { ...idle, phase: "running" },
    response: { ok: true, status: 200 },
  } as never);
});

afterEach(() => vi.restoreAllMocks());

test("renders the section copy", async () => {
  wrap(<DiskSyncPanel />);
  expect(
    await screen.findByText(/Sync library with disk/i),
  ).toBeInTheDocument();
  expect(screen.getByText(/never touches the files/i)).toBeInTheDocument();
});

test("preview shows the headline counts, rows, and a confirm button", async () => {
  wrap(<DiskSyncPanel />);
  await userEvent.click(screen.getByRole("button", { name: /preview sync/i }));
  // Headline count for the removals clause.
  expect(
    await screen.findByText(/2 tracks whose files are missing will be removed/i),
  ).toBeInTheDocument();
  // Removal row label + change row label with its field name.
  expect(screen.getByText("blink-182 — I Miss You")).toBeInTheDocument();
  expect(screen.getByText("Cher — Believe")).toBeInTheDocument();
  expect(screen.getByText(/title/)).toBeInTheDocument();
  // Confirm button = will_remove + will_update.
  expect(
    screen.getByRole("button", { name: /sync 3 items/i }),
  ).toBeInTheDocument();
});

test("an empty plan shows an info message and no confirm button", async () => {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/disk-sync/status")
      return { data: idle, response: { ok: true, status: 200 } } as never;
    if (path === "/api/disk-sync/preview")
      return { data: emptyPlan, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<DiskSyncPanel />);
  await userEvent.click(screen.getByRole("button", { name: /preview sync/i }));
  const note = await screen.findByRole("status");
  expect(note).toHaveTextContent(/everything already matches the disk/i);
  expect(screen.queryByRole("button", { name: /sync \d+ items/i })).toBeNull();
});

test("a failed job surfaces its error in an alert", async () => {
  const failed = {
    ...idle,
    phase: "failed",
    job_id: "f1",
    total: 10,
    processed: 4,
    error: "kaboom",
  };
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/disk-sync/status")
      return { data: failed, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<DiskSyncPanel />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/Sync failed: kaboom/i);
});

test("a stopped (partial) job labels the summary as stopped early", async () => {
  const stopped = {
    ...idle,
    phase: "stopped",
    job_id: "s1",
    total: 10,
    processed: 3,
    removed: 1,
    updated: 0,
    unchanged: 2,
    emptied_albums: 0,
  };
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/disk-sync/status")
      return { data: stopped, response: { ok: true, status: 200 } } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<DiskSyncPanel />);
  expect(
    await screen.findByText(/Stopped early — 3 of 10 processed ·/),
  ).toBeInTheDocument();
});

test("a terminal job with read errors renders the failure list", async () => {
  const doneWithFailures = {
    ...idle,
    phase: "done",
    job_id: "d1",
    total: 600,
    processed: 600,
    removed: 2,
    updated: 1,
    unchanged: 597,
    read_errors: 1,
    emptied_albums: 1,
    failures: [{ label: "X — Y", error: "boom" }],
  };
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/disk-sync/status")
      return {
        data: doneWithFailures,
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  wrap(<DiskSyncPanel />);
  const list = await screen.findByRole("alert", {
    name: /files that could not be read/i,
  });
  expect(list).toHaveTextContent("X — Y");
  expect(list).toHaveTextContent(/boom/i);
});
