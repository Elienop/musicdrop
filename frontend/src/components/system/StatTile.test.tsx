import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MusicFallback } from "@/components/icons";
import { StatTile } from "@/components/system/StatTile";

describe("StatTile", () => {
  it("renders the label and value", () => {
    render(<StatTile icon={MusicFallback} label="Tracks" value="4,212" />);
    expect(screen.getByText("Tracks")).toBeInTheDocument();
    expect(screen.getByText("4,212")).toBeInTheDocument();
  });

  it("renders the value with tabular figures", () => {
    render(<StatTile icon={MusicFallback} label="Tracks" value="4,212" />);
    expect(screen.getByText("4,212")).toHaveClass("tabular-nums");
  });

  it("renders the hint when given", () => {
    render(
      <StatTile
        icon={MusicFallback}
        label="Size"
        value="~128 GB"
        hint="estimated"
      />,
    );
    expect(screen.getByText("estimated")).toBeInTheDocument();
  });

  it("hides the icon from assistive tech", () => {
    const { container } = render(
      <StatTile icon={MusicFallback} label="Tracks" value="4,212" />,
    );
    expect(container.querySelector("svg")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  it("is cardless: big icon beside a value-over-label column, no Card chrome", () => {
    const { container } = render(
      <StatTile icon={MusicFallback} label="Tracks" value="4,212" />,
    );
    expect(container.querySelector("[data-slot=card]")).toBeNull();
    // Large muted icon on the left, spanning both text lines.
    expect(container.querySelector("svg")).toHaveClass(
      "size-14",
      "shrink-0",
      "text-muted-foreground",
    );
    // The value line sits ABOVE the label in the column.
    const column = screen.getByText("4,212").parentElement;
    expect(column?.firstChild).toHaveTextContent("4,212");
    expect(screen.getByText("Tracks")).toHaveClass("text-muted-foreground");
  });
});
