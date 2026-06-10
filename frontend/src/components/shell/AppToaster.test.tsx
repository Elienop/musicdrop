import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppToaster } from "@/components/shell/AppToaster";

vi.mock("sonner", () => ({
  Toaster: (props: { position?: string; theme?: string }) => (
    <div
      data-testid="sonner-toaster"
      data-position={props.position}
      data-theme={props.theme}
    />
  ),
}));

describe("AppToaster", () => {
  it("mounts the single sonner region top-right with the dark theme", () => {
    render(<AppToaster />);
    const toaster = screen.getByTestId("sonner-toaster");
    expect(toaster).toHaveAttribute("data-position", "top-right");
    expect(toaster).toHaveAttribute("data-theme", "dark");
  });
});
