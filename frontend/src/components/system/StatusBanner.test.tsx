// frontend/src/components/system/StatusBanner.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { Info, Warning } from "@/components/icons";
import { StatusBanner } from "@/components/system/StatusBanner";

test("neutral tone: role=status with muted chrome and a muted icon", () => {
  render(
    <StatusBanner tone="neutral" icon={Info}>
      Reorganizing — library… 4 / 10
    </StatusBanner>,
  );
  const banner = screen.getByRole("status");
  expect(banner).toHaveTextContent("Reorganizing — library… 4 / 10");
  expect(banner).toHaveClass("border-border", "bg-muted/50", "rounded-xl");
  const svg = banner.querySelector("svg");
  expect(svg).not.toBeNull();
  expect(svg).toHaveClass("text-muted-foreground", "size-5");
  expect(svg).toHaveAttribute("aria-hidden", "true");
});

test("warning tone: role=alert with --warning tokens", () => {
  render(
    <StatusBanner tone="warning" icon={Warning}>
      3 files will be moved on disk.
    </StatusBanner>,
  );
  const banner = screen.getByRole("alert");
  expect(banner).toHaveClass("border-warning/50", "bg-warning/10");
  expect(banner.querySelector("svg")).toHaveClass("text-warning");
});

test("destructive tone: role=alert with destructive tokens; icon optional", () => {
  render(<StatusBanner tone="destructive">Reorganize failed: boom</StatusBanner>);
  const banner = screen.getByRole("alert");
  expect(banner).toHaveClass("border-destructive/40", "bg-destructive/5");
  expect(banner.querySelector("svg")).toBeNull();
});

test("renders the action slot inside the banner", () => {
  render(
    <StatusBanner tone="neutral" action={<button>Resume</button>}>
      Import paused.
    </StatusBanner>,
  );
  const banner = screen.getByRole("status");
  expect(banner).toContainElement(
    screen.getByRole("button", { name: "Resume" }),
  );
});
