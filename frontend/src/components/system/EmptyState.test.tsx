// frontend/src/components/system/EmptyState.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { Search } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";

test("default: borderless py-24 column with icon, title, and body", () => {
  const { container } = render(
    <EmptyState
      icon={Search}
      title="Search your library"
      body="Find artists, albums, and tracks by name."
    />,
  );
  expect(screen.getByText("Search your library")).toBeInTheDocument();
  expect(
    screen.getByText("Find artists, albums, and tracks by name."),
  ).toBeInTheDocument();
  const root = container.querySelector(
    '[data-slot="empty-state"]',
  ) as HTMLElement;
  expect(root).toHaveClass("py-24", "text-center");
  expect(root).not.toHaveClass("border-dashed");
  const svg = root.querySelector("svg");
  expect(svg).not.toBeNull();
  expect(svg).toHaveAttribute("aria-hidden", "true");
  expect(svg).toHaveClass("size-10", "text-muted-foreground");
});

test("bordered: the dashed-card recipe", () => {
  const { container } = render(
    <EmptyState icon={Search} title="No artists yet" bordered />,
  );
  const root = container.querySelector(
    '[data-slot="empty-state"]',
  ) as HTMLElement;
  expect(root).toHaveClass("border", "border-dashed", "rounded-xl", "py-16");
  expect(root).not.toHaveClass("py-24");
});

test("body is omitted from the DOM when not given", () => {
  const { container } = render(
    <EmptyState icon={Search} title="No artists yet" />,
  );
  expect(container.querySelectorAll("p")).toHaveLength(1);
});

test("renders the action slot", () => {
  render(
    <EmptyState
      icon={Search}
      title="Nothing here"
      action={<a href="/import">Add from folder</a>}
    />,
  );
  expect(
    screen.getByRole("link", { name: "Add from folder" }),
  ).toBeInTheDocument();
});

test("the destructive tone wears the app's error recipe, not the neutral box", () => {
  const { container } = render(
    <EmptyState icon={Search} title="Import failed" bordered tone="destructive" />,
  );
  const root = container.querySelector(
    '[data-slot="empty-state"]',
  ) as HTMLElement;
  expect(root).toHaveAttribute("data-tone", "destructive");
  // ErrorState's own tokens, reused — not a colour hand-written at the call
  // site. All three exist in the @theme block of src/styles.css.
  expect(root).toHaveClass(
    "border-destructive/40",
    "bg-destructive/5",
    "border",
    "rounded-xl",
  );
  // The dashed neutral box is the "nothing here" idiom; a failure is not one.
  expect(root).not.toHaveClass("border-dashed");
  expect(root).not.toHaveClass("border-border");
  const svg = root.querySelector("svg");
  expect(svg).toHaveClass("size-10", "text-destructive");
  expect(svg).not.toHaveClass("text-muted-foreground");
});

test("the tone defaults to neutral, leaving every existing caller alone", () => {
  const { container } = render(
    <EmptyState icon={Search} title="No artists yet" bordered />,
  );
  const root = container.querySelector(
    '[data-slot="empty-state"]',
  ) as HTMLElement;
  expect(root).toHaveAttribute("data-tone", "neutral");
  expect(root).toHaveClass("border-border", "border-dashed");
  expect(root).not.toHaveClass("bg-destructive/5");
  expect(root.querySelector("svg")).toHaveClass("text-muted-foreground");
});

test("an unbordered destructive state colours the icon and paints no box", () => {
  const { container } = render(
    <EmptyState icon={Search} title="Import failed" tone="destructive" />,
  );
  const root = container.querySelector(
    '[data-slot="empty-state"]',
  ) as HTMLElement;
  expect(root.querySelector("svg")).toHaveClass("text-destructive");
  expect(root).toHaveClass("py-24");
  expect(root).not.toHaveClass("border");
  expect(root).not.toHaveClass("bg-destructive/5");
});
