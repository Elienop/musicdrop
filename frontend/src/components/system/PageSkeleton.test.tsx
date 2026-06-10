import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageSkeleton } from "@/components/system/PageSkeleton";

describe("PageSkeleton", () => {
  it("announces loading via a status region with the given text", () => {
    render(
      <PageSkeleton announce="Loading albums…">
        <div data-testid="bones" />
      </PageSkeleton>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading albums…");
  });

  it("keeps the status region OUTSIDE the aria-hidden subtree", () => {
    render(
      <PageSkeleton announce="Loading albums…">
        <div data-testid="bones" />
      </PageSkeleton>,
    );
    // If the announcement sat inside aria-hidden, screen readers would never
    // hear it (the ImportCandidatePage rule).
    expect(
      screen.getByRole("status").closest('[aria-hidden="true"]'),
    ).toBeNull();
  });

  it("hides the skeleton bones from assistive tech", () => {
    render(
      <PageSkeleton announce="Loading albums…">
        <div data-testid="bones" />
      </PageSkeleton>,
    );
    expect(
      screen.getByTestId("bones").closest('[aria-hidden="true"]'),
    ).not.toBeNull();
  });

  it("is layout-transparent (display: contents wrapper)", () => {
    render(
      <PageSkeleton announce="Loading albums…">
        <div data-testid="bones" />
      </PageSkeleton>,
    );
    expect(screen.getByTestId("bones").parentElement).toHaveClass("contents");
  });
});
