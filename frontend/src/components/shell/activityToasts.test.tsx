import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ActivityRow } from "@/api/useActivity";
import { useActivityToasts } from "@/components/shell/activityToasts";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { toast } from "sonner";

const success = vi.mocked(toast.success);
const error = vi.mocked(toast.error);

function Harness({ rows }: { rows: ActivityRow[] }) {
  useActivityToasts(rows);
  return null;
}

function row(overrides: Partial<ActivityRow> & { id: string }): ActivityRow {
  return {
    kind: "lyrics",
    label: "Lyrics backfill",
    state: "running",
    ...overrides,
  };
}

describe("useActivityToasts", () => {
  beforeEach(() => {
    success.mockClear();
    error.mockClear();
  });

  it("fires nothing on first render, even when rows are already terminal", () => {
    render(
      <Harness
        rows={[
          row({ id: "lyrics:L1", state: "done", countsText: "5 found" }),
          row({ id: "reorganize:r1", state: "failed", label: "Reorganize" }),
        ]}
      />,
    );
    expect(success).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });

  it("toasts success exactly once on running→done, including the outcome", () => {
    const { rerender } = render(
      <Harness rows={[row({ id: "lyrics:L1", state: "running" })]} />,
    );

    const done = row({ id: "lyrics:L1", state: "done", countsText: "5 found" });
    rerender(<Harness rows={[done]} />);
    // Terminal rows persist in the popover — a later re-render with the same
    // done row must NOT re-toast.
    rerender(<Harness rows={[done]} />);

    expect(success).toHaveBeenCalledTimes(1);
    expect(success).toHaveBeenCalledWith("Lyrics backfill: 5 found");
    expect(error).not.toHaveBeenCalled();
  });

  it("toasts error exactly once on running→failed, with the label", () => {
    const running = row({
      id: "reorganize:r1",
      label: "Reorganize",
      state: "running",
    });
    const { rerender } = render(<Harness rows={[running]} />);

    const failed = row({
      id: "reorganize:r1",
      label: "Reorganize",
      state: "failed",
      countsText: "library locked",
    });
    rerender(<Harness rows={[failed]} />);
    rerender(<Harness rows={[failed]} />);

    expect(error).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledWith("Reorganize failed: library locked");
    expect(success).not.toHaveBeenCalled();
  });

  it("never toasts rows that appear already terminal after the baseline", () => {
    const { rerender } = render(<Harness rows={[]} />);

    rerender(
      <Harness
        rows={[row({ id: "artist-art:a1", state: "failed" })]}
      />,
    );

    expect(error).not.toHaveBeenCalled();
    expect(success).not.toHaveBeenCalled();
  });

  it("stays silent when a running row simply disappears (the import probe has no terminal phase)", () => {
    const { rerender } = render(
      <Harness rows={[row({ id: "import:j1", label: "Import" })]} />,
    );

    rerender(<Harness rows={[]} />);

    expect(success).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });
});
