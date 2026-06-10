import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SegmentedControl } from "@/components/ui/segmented-control";

const OPTIONS = [
  { value: "strict", label: "Strict · MB-ID" },
  { value: "fuzzy", label: "Fuzzy · artist + title" },
];

describe("SegmentedControl", () => {
  it("renders one aria-pressed button per option inside a labeled group", () => {
    render(
      <SegmentedControl
        options={OPTIONS}
        value="strict"
        onChange={() => {}}
        aria-label="Match mode"
      />,
    );
    expect(
      screen.getByRole("group", { name: "Match mode" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Strict · MB-ID" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByRole("button", { name: "Fuzzy · artist + title" }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("styles the active option with the accent, not an inverted foreground", () => {
    render(
      <SegmentedControl
        options={OPTIONS}
        value="fuzzy"
        onChange={() => {}}
        aria-label="Match mode"
      />,
    );
    const active = screen.getByRole("button", {
      name: "Fuzzy · artist + title",
    });
    expect(active).toHaveClass("bg-primary/15");
    expect(active).toHaveClass("text-primary");
    expect(active).not.toHaveClass("bg-foreground");
    expect(
      screen.getByRole("button", { name: "Strict · MB-ID" }),
    ).toHaveClass("text-muted-foreground");
  });

  it("fires onChange with the clicked option's value", () => {
    const onChange = vi.fn();
    render(
      <SegmentedControl
        options={OPTIONS}
        value="strict"
        onChange={onChange}
        aria-label="Match mode"
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Fuzzy · artist + title" }),
    );
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("fuzzy");
  });

  it("uses the shared focus-ring dialect on each option", () => {
    render(
      <SegmentedControl
        options={OPTIONS}
        value="strict"
        onChange={() => {}}
        aria-label="Match mode"
      />,
    );
    expect(
      screen.getByRole("button", { name: "Strict · MB-ID" }),
    ).toHaveClass("focus-ring");
  });
});
