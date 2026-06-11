import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ActivityRow } from "@/api/useActivity";
import { ActivityButton } from "@/components/shell/ActivityPopover";
import { renderWithProviders } from "@/test/render";

let rows: ActivityRow[] = [];
const dismiss = vi.fn();

vi.mock("@/api/useActivity", () => ({
  useActivity: () => ({
    rows,
    runningCount: rows.filter((row) => row.state === "running").length,
  }),
  useActivityDismissals: () => ({ dismissed: new Set<string>(), dismiss }),
}));

const runningLyrics: ActivityRow = {
  id: "lyrics:L1",
  kind: "lyrics",
  label: "Lyrics backfill",
  scope: "library",
  state: "running",
  progress: { done: 3, total: 10 },
  href: "/settings",
};

const runningImport: ActivityRow = {
  id: "import:j1",
  kind: "import",
  label: "Import",
  state: "running",
  countsText: "2 awaiting review",
  href: "/import?job=j1",
};

const failedReorganize: ActivityRow = {
  id: "reorganize:r1",
  kind: "reorganize",
  label: "Reorganize",
  scope: "library",
  state: "failed",
  countsText: "library locked",
  href: "/settings",
};

describe("ActivityButton", () => {
  beforeEach(() => {
    rows = [];
    dismiss.mockClear();
  });

  it("shows no badge and no pulse when nothing is running", () => {
    renderWithProviders(<ActivityButton />);
    const trigger = screen.getByRole("button", { name: /^Activity/ });
    expect(trigger).toHaveTextContent("");
    expect(trigger.querySelector("svg")).not.toHaveClass(
      "motion-safe:animate-pulse",
    );
  });

  it("shows the running count badge and pulses the icon while jobs run", () => {
    rows = [runningLyrics, runningImport];
    renderWithProviders(<ActivityButton />);
    const trigger = screen.getByRole("button", { name: /^Activity/ });
    expect(within(trigger).getByText("2")).toBeInTheDocument();
    expect(trigger.querySelector("svg")).toHaveClass(
      "motion-safe:animate-pulse",
    );
  });

  it("opens a popover listing one JobProgress row per job", async () => {
    rows = [runningLyrics, runningImport];
    renderWithProviders(<ActivityButton />);

    await userEvent.click(screen.getByRole("button", { name: /^Activity/ }));

    expect(await screen.findByText("Lyrics backfill")).toBeInTheDocument();
    expect(screen.getByText("3 / 10")).toBeInTheDocument();
    expect(screen.getByText("2 awaiting review")).toBeInTheDocument();
    const viewLinks = screen.getAllByRole("link", { name: "View" });
    expect(viewLinks[1]).toHaveAttribute("href", "/import?job=j1");
    // No Stop affordance in Phase 2 — the stop mutations stay on Settings.
    expect(
      screen.queryByRole("button", { name: "Stop" }),
    ).not.toBeInTheDocument();
  });

  it("offers a Dismiss button on failed rows only, wired to the dismissals store", async () => {
    rows = [runningLyrics, failedReorganize];
    renderWithProviders(<ActivityButton />);

    await userEvent.click(screen.getByRole("button", { name: /^Activity/ }));
    await screen.findByText("Reorganize");

    // The accessible name carries the job label so multiple failed rows
    // stay distinguishable to screen-reader users.
    const dismissButtons = screen.getAllByRole("button", {
      name: "Dismiss Reorganize",
    });
    expect(dismissButtons).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: "Dismiss Lyrics backfill" }),
    ).not.toBeInTheDocument();

    await userEvent.click(dismissButtons[0]!);
    expect(dismiss).toHaveBeenCalledWith("reorganize:r1");
  });

  it("shows the empty state and the Soulseek footer hint", async () => {
    renderWithProviders(<ActivityButton />);

    await userEvent.click(screen.getByRole("button", { name: /^Activity/ }));

    expect(await screen.findByText("Nothing running.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Downloads will appear here when Soulseek search ships.",
      ),
    ).toBeInTheDocument();
  });
});
