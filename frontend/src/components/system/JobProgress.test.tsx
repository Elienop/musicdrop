import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { JobProgress } from "@/components/system/JobProgress";
import { renderWithProviders } from "@/test/render";

describe("JobProgress", () => {
  it("renders a running row: label, scope, spinner chip with a text state", () => {
    const { container } = render(
      <JobProgress label="Lyrics backfill" scope="Library" state="running" />,
    );
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("Lyrics backfill")).toBeInTheDocument();
    expect(screen.getByText("Library")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(container.querySelector(".animate-spin")).not.toBeNull();
  });

  it("renders the failed state as destructive text, never color-only", () => {
    render(<JobProgress label="Artist art" state="failed" />);
    expect(screen.getByText("Failed")).toHaveClass("text-destructive");
  });

  it("renders the done state as a success chip with text", () => {
    render(<JobProgress label="Reorganize" state="done" />);
    expect(screen.getByText("Done")).toHaveClass("text-success");
  });

  it("renders a progress bar sized to done/total plus the n/total text", () => {
    const { container } = render(
      <JobProgress
        label="Import"
        state="running"
        progress={{ done: 3, total: 12 }}
      />,
    );
    const fill = container.querySelector<HTMLElement>(".bg-primary");
    expect(fill).not.toBeNull();
    expect(fill?.style.width).toBe("25%");
    expect(screen.getByText("3 / 12")).toBeInTheDocument();
  });

  it("omits the bar when no progress is given", () => {
    const { container } = render(
      <JobProgress label="Import" state="running" />,
    );
    expect(container.querySelector(".bg-primary")).toBeNull();
  });

  it("renders the counts slot", () => {
    render(
      <JobProgress
        label="Import"
        state="done"
        counts={<span>4 found · 1 skipped</span>}
      />,
    );
    expect(screen.getByText("4 found · 1 skipped")).toBeInTheDocument();
  });

  it("fires onStop from the Stop button", () => {
    const onStop = vi.fn();
    render(<JobProgress label="Import" state="running" onStop={onStop} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("puts Stop on the app's stop register, which carries the inline glyph step", () => {
    render(<JobProgress label="Import" state="running" onStop={vi.fn()} />);
    const stop = screen.getByRole("button", { name: "Stop" });
    // Every other Stop/Pause BUTTON in the app is `outline sm`; this one was
    // `xs`. (Reorganize also has a second stop control, the detail rail's
    // size-10 `IconAction`, which is a different register on purpose.)
    expect(stop).toHaveAttribute("data-variant", "outline");
    expect(stop).toHaveAttribute("data-size", "sm");
    // And the size is what decides the glyph, which is why the two are pinned
    // together: the button hands its svg a size token, and `twMerge` keeps
    // exactly one. `sm` leaves the base 16px; `xs` replaces it with 12px, where
    // Phosphor's light stroke is 0.563px — a hairline, and off the app's
    // 16/20/40 icon scale. Asserted on the class string because jsdom computes
    // no Tailwind, so the token IS the measurement available here.
    expect(stop.className).toContain("[&_svg:not([class*='size-'])]:size-4");
    expect(stop.className).not.toContain("[&_svg:not([class*='size-'])]:size-3");
  });

  it("sizes the View caret on the inline glyph step, not the button's 12px", () => {
    renderWithProviders(
      <JobProgress label="Import" state="running" href="/import" />,
    );
    const svg = screen.getByRole("link", { name: "View" }).querySelector("svg");
    // The button stays `ghost xs`; only the glyph is sized. `xs` hands an
    // UNSIZED svg `size-3` (12px), where Phosphor light is a 0.563px stroke;
    // its selector excludes a glyph that carries its own size- class, which is
    // what this asserts. Owner's call 2026-09-20.
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute("class")).toContain("size-4");
  });

  it("omits the Stop button when onStop is not given", () => {
    render(<JobProgress label="Import" state="running" />);
    expect(
      screen.queryByRole("button", { name: "Stop" }),
    ).not.toBeInTheDocument();
  });

  it("renders a deep link when href is given", () => {
    renderWithProviders(
      <JobProgress label="Import" state="running" href="/import?job=abc" />,
    );
    expect(screen.getByRole("link", { name: "View" })).toHaveAttribute(
      "href",
      "/import?job=abc",
    );
  });

  it("omits the deep link when href is not given", () => {
    render(<JobProgress label="Import" state="running" />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
