import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

function Harness() {
  return (
    <Popover>
      <PopoverTrigger>Open activity</PopoverTrigger>
      <PopoverContent>Popover body</PopoverContent>
    </Popover>
  );
}

describe("Popover", () => {
  it("is closed until the trigger is clicked, then shows the content", async () => {
    render(<Harness />);
    expect(screen.queryByText("Popover body")).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Open activity" }),
    );

    expect(await screen.findByText("Popover body")).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    render(<Harness />);
    await userEvent.click(
      screen.getByRole("button", { name: "Open activity" }),
    );
    await screen.findByText("Popover body");

    await userEvent.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByText("Popover body")).not.toBeInTheDocument(),
    );
  });

  it("applies the dark popover tokens and data-slot to the content", async () => {
    render(<Harness />);
    await userEvent.click(
      screen.getByRole("button", { name: "Open activity" }),
    );

    // The content div is the direct parent of the text node, so getByText
    // returns the PopoverContent element itself.
    const content = await screen.findByText("Popover body");
    expect(content).toHaveAttribute("data-slot", "popover-content");
    expect(content).toHaveClass("bg-popover", "rounded-xl", "shadow-lg");
  });
});
