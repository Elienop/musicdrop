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
